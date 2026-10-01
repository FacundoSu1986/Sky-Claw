"""Tests causales GP2-S4D: finalización de la transacción de protección del Golden.

``COMMITTED`` no es un mensaje de éxito: es una afirmación durable. Estos tests
tratan cada ``COMMITTED`` como una afirmación que tiene que poder sustentarse
contra evidencia de disco después de un crash, y no contra la memoria de un
proceso que quizá ya no existe.

La autoridad de S4-D es EXCLUSIVAMENTE durable y observable:

    authorized_plan.json (protegido) + protection_journal.json (protegido)
    + metadata del GoldenMutationLock + backup durable + filesystem fresco

Prohibido como autoridad: staging, el candidate manifest, el resultado GP1/RV-2
PRE-apply, un ``path`` del caller, o cualquier objeto Python quecruce un crash.

La matriz F01..F15 (orden de gates y fail-closed) y M-D1..M-D10 (mutantes) vive
acá con kernels y puertos falsos deterministas. Los escenarios de proceso MUERTO
viven en ``test_runtime_vault_s4d_windows_rig.py`` (procesos REALES, sólo
Windows, árbol descartable en %TEMP%): un crash simulado con una excepción Python
no demuestra nada sobre el contrato de recuperación.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorized_plan import (
    AUTHORIZED_PLAN_SCHEMA_VERSION,
    AuthorizedPlan,
    serialize_authorized_plan,
)
from sky_claw.local.runtime_vault.authorized_plan_store import _MINT_PROOF, DurableAuthorizedPlan
from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationAuthorityError,
    FinalizationDisposition,
    FinalizationForensicReport,
    FinalizationPhase,
    FinalizationPreconditionError,
    GateVerdict,
    finalize_protection_transaction,
)
from sky_claw.local.runtime_vault.golden_backup_archive import (
    GoldenBackupAlreadyExistsError,
    GoldenBackupWriteError,
    archive_golden_backup,
    derive_golden_backup_path,
    deserialize_golden_backup_archive,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockBusyError,
    GoldenLockMetadata,
    GoldenLockPhase,
    acquire_golden_mutation_lock_for_recovery,
    derive_golden_lock_key,
    derive_golden_lock_path,
    serialize_golden_lock_metadata,
)
from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.protection_journal import (
    NodeWalState,
    ProtectionTransactionState,
    parse_journal_bytes,
)
from sky_claw.local.runtime_vault.protection_journal_store import (
    ProtectionJournalAlreadyExistsError,
    ProtectionJournalNotFoundError,
    create_protection_journal,
    derive_protection_journal_path,
)

_OPERACION = "5a1c9e44-2b3d-4e60-9f31-2d8e5f6a7b91"
_OTRA_OPERACION = "9f0b2c77-51aa-4b3e-8c22-77aa11bb22cc"
_RAIZ_CANONICA = "C:\\Games\\Skyrim"
_VOLUME_SERIAL = 0xC0FFEE01
_ROOT_FILE_ID = 0x0F1E2D3C4B5A6978
_POLICY_VERSION = "gp2-v1"
_TREE_DIGEST = "e" * 64

_RUTAS = (".", "Data", "Data/Scripts", "Data/Skyrim.esm")

_PID_ACTUAL = 5150
_CREATION_ACTUAL = 140_000_000_000_000_001
_PID_DUENO_MUERTO = 616
_PID_DUENO_VIVO = 8123


# ============================================================================
# Fábricas de fixtures
# ============================================================================


def _node(rel_path: str, file_id: int, *, sd_suffix: str = "") -> NodeSecurityBackup:
    sd = b"\x01\x00\x04\x80" + rel_path.encode("utf-8") + sd_suffix.encode("utf-8") + b"\x00" * 8
    return NodeSecurityBackup(
        relative_path=rel_path,
        node_kind=GoldenProtectionNodeKind.DIR if rel_path == "." else GoldenProtectionNodeKind.FILE,
        volume_serial_number=_VOLUME_SERIAL,
        file_id=file_id,
        pre_sd_bytes_b64=base64.b64encode(sd).decode("ascii"),
        pre_sd_length=len(sd),
        pre_sd_sha256=hashlib.sha256(sd).hexdigest(),
        owner_sid="S-1-5-18",
        group_sid="S-1-5-32-544",
        dacl_control_flags=0x1000,
        pre_dacl_protected_flag=True,
        sddl_diagnostic="D:(A;;FA;;;BA)",
    )


def _operator_evidence() -> Any:
    from sky_claw.local.runtime_vault.authorization_context import OperatorTokenEvidence

    return OperatorTokenEvidence(
        operator_sid="S-1-5-21-1001-1002-1003-1001",
        token_type="primary",
        acquired_via="same_account_coordinator_extraction",
    )


def _plan(
    *,
    operation_id: str = _OPERACION,
    root: str = _RAIZ_CANONICA,
    rutas: tuple[str, ...] = _RUTAS,
    tree_digest: str = _TREE_DIGEST,
) -> AuthorizedPlan:
    nodos = tuple(_node(r, _ROOT_FILE_ID + i) for i, r in enumerate(rutas))
    return AuthorizedPlan(
        schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
        operation_id=operation_id,
        canonical_root=root,
        volume_serial_number=_VOLUME_SERIAL,
        root_file_id=_ROOT_FILE_ID,
        tree_digest=TreeDigest(digest=tree_digest, files=len(rutas) - 1, bytes=4096),
        node_count=len(rutas),
        policy_version=_POLICY_VERSION,
        staging_digest="d" * 64,
        operator_identity=_operator_evidence(),
        nodes=nodos,
    )


def _resolver(raiz: pathlib.Path) -> Any:
    return lambda: raiz


def _raiz(tmp_path: pathlib.Path, operation_id: str = _OPERACION) -> pathlib.Path:
    """Raíz del namespace de prueba.

    Crea la cadena COMPLETA que el bootstrap del namespace de confianza
    aprovisionaría, incluido el scope del backup. No es una comodidad: el
    store de S4-D falla cerrado si el padre no existe, y un fixture que lo
    creara por dentro estaría probando un contrato distinto del de producción.
    """
    (tmp_path / "Sky-Claw" / "runtime_vault" / "operations" / operation_id).mkdir(parents=True)
    plan = _plan(operation_id=operation_id)
    backup_dir = derive_golden_backup_path(plan, programdata_resolver=lambda: tmp_path).parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _plan_path(raiz: pathlib.Path, operation_id: str = _OPERACION) -> pathlib.Path:
    return derive_protection_journal_path(operation_id, programdata_resolver=_resolver(raiz)).with_name(
        "authorized_plan.json"
    )


def _plan_durable(raiz: pathlib.Path, **kwargs: Any) -> DurableAuthorizedPlan:
    plan = _plan(**kwargs)
    _plan_path(raiz, plan.operation_id).write_bytes(serialize_authorized_plan(plan))
    return DurableAuthorizedPlan(plan, _plan_path(raiz, plan.operation_id), _proof=_MINT_PROOF)


def _journal_path(raiz: pathlib.Path, operation_id: str = _OPERACION) -> pathlib.Path:
    return derive_protection_journal_path(operation_id, programdata_resolver=_resolver(raiz))


def _lock_path(raiz: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(derive_golden_lock_path(_VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=_resolver(raiz)))


# ============================================================================
# Kernels y puertos falsos
# ============================================================================


class _KernelDiario:
    """Kernel de durabilidad falso: append real sobre disco, flush conmutable.

    Reproduce el contrato observable del kernel real sin el ABI Win32:
    ``append`` mueve bytes, ``flush`` es la GATE que decide si lo escrito pasa a
    ser autoridad, y el contenido NO se descarta al fallar: un tercer proceso
    debe poder releer exactamente lo que quedó.
    """

    def __init__(
        self,
        *,
        flush_ok: bool = True,
        fallar_append_de: frozenset[str] = frozenset(),
        fallar_flush_de: frozenset[str] = frozenset(),
    ) -> None:
        self.flush_ok = flush_ok
        self.fallar_append_de = fallar_append_de
        self.fallar_flush_de = fallar_flush_de
        self._ultimo_append = b""
        self.eventos: list[str] = []
        self._handles: dict[int, pathlib.Path] = {}
        self._provisionales: dict[int, pathlib.Path] = {}
        self._siguiente = 500

    def exists(self, path: pathlib.PurePath) -> bool:
        return pathlib.Path(path).exists()

    def stage(self, dest: pathlib.PurePath) -> int:
        destino = pathlib.Path(dest)
        destino.parent.mkdir(parents=True, exist_ok=True)
        provisional = destino.parent / f".tmp_journal_{uuid.uuid4().hex}.{destino.name}"
        provisional.touch()
        self._siguiente += 1
        self._handles[self._siguiente] = provisional
        self._provisionales[self._siguiente] = provisional
        self.eventos.append("stage")
        return self._siguiente

    def publish(self, handle: int, dest: pathlib.PurePath) -> None:
        destino = pathlib.Path(dest)
        if destino.exists():
            raise ProtectionJournalAlreadyExistsError(f"ya existe: '{destino}'")
        self.eventos.append("publish-flush")
        if not self.flush_ok:
            from sky_claw.local.runtime_vault.protection_journal_store import DurableJournalFlushError

            raise DurableJournalFlushError("flush no confirmado: no se publica")
        provisional = self._provisionales[handle]
        destino.write_bytes(pathlib.Path(provisional).read_bytes())
        self.eventos.append("publish")

    def discard(self, handle: int) -> None:
        provisional = self._provisionales.pop(handle, None)
        if provisional is not None:
            pathlib.Path(provisional).unlink(missing_ok=True)
        self.eventos.append("discard")

    def open_append(self, path: pathlib.PurePath) -> int:
        destino = pathlib.Path(path)
        if not destino.exists():
            raise ProtectionJournalNotFoundError(f"no existe: '{destino}'")
        self._siguiente += 1
        self._handles[self._siguiente] = destino
        self.eventos.append("open")
        return self._siguiente

    def append(self, handle: int, payload: bytes) -> None:
        for estado in self.fallar_append_de:
            if f'"state":"{estado}"'.encode() in payload:
                self.eventos.append(f"append-fallido:{estado}")
                raise OSError(f"append simulado falló para '{estado}'")
        with self._handles[handle].open("ab") as fh:
            fh.write(payload)
        self._ultimo_append = payload
        self.eventos.append("append")

    def flush(self, handle: int) -> bool:
        self.eventos.append("flush-wal")
        if not self.flush_ok:
            return False
        for estado in self.fallar_flush_de:
            if f'"state":"{estado}"'.encode() in self._ultimo_append:
                self.eventos.append(f"flush-fallido:{estado}")
                return False
        return True

    def read_all(self, path: pathlib.PurePath) -> bytes:
        return pathlib.Path(path).read_bytes()

    def close(self, handle: int) -> None:
        self._handles.pop(handle, None)
        self.eventos.append("close")

    def remove(self, path: pathlib.PurePath) -> None:
        pathlib.Path(path).unlink(missing_ok=True)


class _KernelLock:
    """Kernel fake del GoldenMutationLock con evidencia en memoria por path."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.busy_paths: set[str] = set()
        self.open_handles: dict[int, str] = {}
        self.owner_alive: bool | None = False
        self.close_calls: list[int] = []
        self.flush_calls: list[str] = []
        self.escrituras: list[tuple[str, bytes]] = []
        self.on_open: Any = None
        self.fallar_escritura: bool = False
        self._siguiente = 700

    def seed(self, path: pathlib.PurePath, metadata: GoldenLockMetadata) -> None:
        self.files[str(path)] = serialize_golden_lock_metadata(metadata)

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        path_str = str(path)
        if path_str in self.busy_paths:
            from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockBusyError

            raise GoldenLockBusyError("ocupado", win32_error=32)
        if self.on_open is not None:
            self.on_open()
        self._siguiente += 1
        self.open_handles[self._siguiente] = path_str
        self.busy_paths.add(path_str)
        self.files.setdefault(path_str, b"")
        return self._siguiente

    def get_reparse_tag(self, handle: int) -> int:
        return 0

    def read_lock_bytes(self, handle: int) -> bytes:
        return self.files[self.open_handles[handle]]

    def write_lock_bytes(self, handle: int, payload: bytes) -> None:
        # `fallar_escritura` simula la fila Q de §20: la escritura de
        # phase=RELEASED falla DESPUÉS de un COMMITTED durable. Se limita a ese
        # payload para no romper la adquisición, que también escribe metadata.
        if self.fallar_escritura and b'"RELEASED"' in payload:
            raise OSError("escritura de metadata de lock simulada como fallida")
        path_str = self.open_handles[handle]
        self.files[path_str] = payload
        self.escrituras.append((path_str, payload))

    def flush_lock(self, handle: int) -> None:
        self.flush_calls.append(self.open_handles[handle])

    def is_owner_alive(self, owner_pid: int, owner_process_creation_time: int) -> bool | None:
        return self.owner_alive

    def current_process_identity(self) -> tuple[int, int, int]:
        return (_PID_ACTUAL, _CREATION_ACTUAL, 1)

    def current_epoch_seconds(self) -> int:
        return 1_758_499_200

    def close_handle(self, handle: int) -> None:
        self.close_calls.append(handle)
        self.busy_paths.discard(self.open_handles.pop(handle, ""))

    def metadata_de(self, path: pathlib.PurePath) -> GoldenLockMetadata | None:
        raw = self.files.get(str(path), b"")
        if not raw:
            return None
        from sky_claw.local.runtime_vault.golden_mutation_lock import deserialize_golden_lock_metadata

        return deserialize_golden_lock_metadata(raw)


class _WriterBackup:
    """Writer de backup con create-once REAL y fallo inyectable.

     Escribe de verdad en disco con ``O_EXCL`` (publicación single-winner sin
     ventana check-then-write), y por eso un crash del writer puede dejar el
     archivo ausente o completo, nunca "casi completo con el nombre final".

     ``fallar_flush`` simula el caso que importa: el byte LLEGA al archivo pero
     la durabilidad NO se confirma. Es el escenario en el que un store que se
    *fía* del retorno del writer declararía un backup durable que no lo está.
    """

    def __init__(self, *, fallar_flush: bool = False, fallar_write: bool = False) -> None:
        self.fallar_flush = fallar_flush
        self.fallar_write = fallar_write
        self.escrituras: list[tuple[str, int]] = []

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        from sky_claw.local.runtime_vault.golden_backup_archive import GoldenBackupDurabilityError

        if self.fallar_write:
            raise GoldenBackupWriteError("fallo de escritura inyectado")
        # NO crea el directorio padre: ése es el contrato real
        # (`write_secured_file_create_once_at` exige que exista y sea confiable).
        # Un fake que hiciera `mkdir` probaría un contrato más permisivo que el
        # de producción, y el test "el store falla cerrado sin padre" no
        # significaría nada.
        try:
            handle = dest.open("xb")
        except FileExistsError as exc:
            raise GoldenBackupAlreadyExistsError(str(dest)) from exc
        with handle:
            handle.write(payload)
            handle.flush()
        self.escrituras.append((str(dest), len(payload)))
        if self.fallar_flush:
            # El byte está en disco pero el GATE de durabilidad no se confirma:
            # el store NO debe acuñar autoridad. Para que el test sea causal
            # sobre el store (y no sobre el writer), el archivo se retira: un
            # objeto no durable no puede quedar como "evidencia existente".
            dest.unlink(missing_ok=True)
            raise GoldenBackupDurabilityError("FlushFileBuffers no confirmado (inyectado)")


def _obs(passed: bool, detail: str, gate: str = "observacion") -> GateVerdict:
    """Observación fresca de un gate, con digest para trazabilidad forense."""
    return GateVerdict(
        gate=gate,
        passed=passed,
        detail=detail,
        evidence_digest=hashlib.sha256(f"{gate}:{detail}".encode()).hexdigest(),
    )


class _PuertoFinalizacion:
    """Puerto de verificación final falso: un veredicto por gate, sin Win32.

    Los gates son INDEPENDIENTES a propósito: cada test puede reprobar uno solo
    y observar que los demás ni siquiera se ejecutan. Un puerto que devuelva
    "todo bien" ocultaría exactamente el defecto que F02..F04 buscan.
    """

    def __init__(self) -> None:
        self.gp1 = _obs(True, "HARDENED", "gp1")
        self.rv2 = _obs(True, "VERIFIED", "rv2")
        self.node_set = _obs(True, "conjunto_identico", "node_set")
        self.quiescence = _obs(True, "sin_bloqueos", "quiescence")
        self.llamadas: list[str] = []

    def observar_gp1(self, **_kwargs: Any) -> GateVerdict:
        self.llamadas.append("gp1")
        return self.gp1

    def observar_rv2(self, **_kwargs: Any) -> GateVerdict:
        self.llamadas.append("rv2")
        return self.rv2

    def observar_node_set(self, **_kwargs: Any) -> GateVerdict:
        self.llamadas.append("node_set")
        return self.node_set

    def observar_quiescence(self, **_kwargs: Any) -> GateVerdict:
        self.llamadas.append("quiescence")
        return self.quiescence


# ============================================================================
# Harness
# ============================================================================


class _Harness:
    """Estado durable completo de una operación en APPLIED, listo para S4-D."""

    def __init__(self, tmp_path: pathlib.Path, *, nodos_incompletos: int = 0, **plan_kwargs: Any) -> None:
        self.nodos_incompletos = nodos_incompletos
        self.raiz = _raiz(tmp_path, plan_kwargs.get("operation_id", _OPERACION))
        self.plan = _plan_durable(self.raiz, **plan_kwargs)
        self.journal_kernel = _KernelDiario()
        self.lock_kernel = _KernelLock()
        self.writer = _WriterBackup()
        self.puerto = _PuertoFinalizacion()

        self.journal = create_protection_journal(
            self.plan,
            programdata_resolver=_resolver(self.raiz),
            kernel=self.journal_kernel,
        )
        # Los cuatro nodos del plan quedan MUTATED durable: es el precondición de
        # S4-D (F15 falla si esto no está).
        for indice, node in enumerate(self.plan.plan.nodes):
            nodo_binding = self.journal.node_binding(node.relative_path)
            permit = self.journal.record_node_mutation_intent(nodo_binding)
            permit.mark_consumed()
            if indice < self.nodos_incompletos:
                # Apply a medio camino: MUTATING durable, MUTATED nunca escrito.
                continue
            self.journal.record_node_mutation_completed(nodo_binding)
        self._adquirir_lock_huerfano()

    def _adquirir_lock_huerfano(self) -> None:
        """Simula el lock retenido por S4-C tras dejar POST_VERIFICATION_REQUIRED.

        El dueño está MUERTO: es el caso real de S4-D tras un crash, y el que
        obliga a probar la continuidad de ``operation_id`` (nunca un lock nuevo
        como operación distinta).
        """
        key = derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID)
        self.lock_kernel.seed(
            _lock_path(self.raiz),
            GoldenLockMetadata(
                lock_key=key,
                operation_id=self.plan.operation_id,
                owner_pid=_PID_DUENO_MUERTO,
                owner_process_creation_time=_CREATION_ACTUAL - 5000,
                session_id=1,
                created_at=1_758_499_100,
                phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            ),
        )

    def params(self) -> dict[str, Any]:
        """Argumentos de ``finalize_protection_transaction`` para esta operación."""
        return {
            "operation_id": self.plan.operation_id,
            "plan": self.plan,
            "journal": self.journal,
            "port": self.puerto,
            "programdata_resolver": _resolver(self.raiz),
            "archive_writer": self.writer,
            "lock_kernel": self.lock_kernel,
        }

    # -- utilidades de observacion durable ---------------------------------------

    def journal_bytes(self) -> bytes:
        return _journal_path(self.raiz, self.plan.operation_id).read_bytes()

    def journal_estado(self) -> ProtectionTransactionState:
        """Estado durable leído de disco, no del objeto en memoria.

        Si el journal no es interpretable devuelve INDETERMINATE: un test que
        leyera el estado de un archivo corrupto como si fuera válido probaría
        menos de lo que cree.
        """
        resultado = parse_journal_bytes(self.journal_bytes())
        if resultado.journal is None:
            return ProtectionTransactionState.INDETERMINATE
        return resultado.journal.transaction_state

    def journal_terminal(self) -> ProtectionTransactionState | None:
        return parse_journal_bytes(self.journal_bytes()).terminal_state

    def backup_path(self) -> pathlib.Path:
        return derive_golden_backup_path(self.plan.plan, programdata_resolver=_resolver(self.raiz))

    def _publicar_backup(self) -> None:
        """Escribe el backup durable de esta operación, como lo haría S4-D."""
        archive_golden_backup(self.plan, programdata_resolver=_resolver(self.raiz), writer=self.writer)

    def _cargar_backup(self) -> Any:
        """Relee el backup desde disco y lo acuña como autoridad durable."""
        from sky_claw.local.runtime_vault.golden_backup_archive import load_durable_golden_backup

        return load_durable_golden_backup(self.plan.plan, programdata_resolver=_resolver(self.raiz))

    def _sembrar_lock_de_otra_operacion(self) -> None:
        """Pone en disco un lock huérfano que pertenece a OTRA operación."""
        self.lock_kernel.seed(
            _lock_path(self.raiz),
            GoldenLockMetadata(
                lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
                operation_id=_OTRA_OPERACION,
                owner_pid=_PID_DUENO_MUERTO,
                owner_process_creation_time=_CREATION_ACTUAL - 5000,
                session_id=1,
                created_at=1_758_499_100,
                phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            ),
        )

    def reload_journal(self) -> Any:
        """Reabre el journal desde disco: simula el proceso B."""
        from sky_claw.local.runtime_vault.protection_journal_store import open_protection_journal

        return open_protection_journal(
            self.plan.operation_id,
            self.plan,
            programdata_resolver=_resolver(self.raiz),
            kernel=_KernelDiario(),
        )

    def _writer(self) -> _WriterBackup:
        return self.writer


@pytest.fixture
def harness(tmp_path: pathlib.Path) -> Iterator[_Harness]:
    yield _Harness(tmp_path)


def _finalize(h: _Harness, **overrides: Any) -> FinalizationForensicReport:
    params = h.params()
    params.update(overrides)
    return finalize_protection_transaction(**params)


# ============================================================================
# F01..F05 — orden de gates: cada fallo detiene la cadena en su lugar
# ============================================================================


def test_f01_todos_los_gates_pasan_termina_en_committed(harness: _Harness) -> None:
    """F01: camino feliz completo -> COMMITTED durable y lock liberado."""
    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.COMMITTED
    assert reporte.journal_state is ProtectionTransactionState.COMMITTED
    # El estado COMMITTED se LEE de disco, no del objeto en memoria: un
    # `success=True` sin bytes durables no contaría.
    assert harness.journal_estado() is ProtectionTransactionState.COMMITTED
    assert reporte.archive_digest is not None
    assert harness.backup_path().exists()
    assert reporte.lock.released is True
    assert reporte.lock.retained_as_orphan is False


def test_f01b_el_orden_de_los_gates_es_exactamente_el_normativo(harness: _Harness) -> None:
    """El orden NO es negociable, y hay TRES observaciones de RV-2.

    Ninguna de las tres es redundante:

    1. la de ``VERIFYING_RV2`` (contenido al empezar a verificar);
    2. la post-quiescence, que §19.2 mete DENTRO de la arista
       ``VERIFYING_NODE_SET -> ARCHIVING_BACKUP``;
    3. la inmediatamente previa al archivado, que es la que hace segura una
       REANUDACIÓN desde ``ARCHIVING_BACKUP``: el FSM no admite retroceso, así
       que esa puerta se cubre re-observando sin escribir transición.

    Sin la segunda, un escritor podría cambiar contenido entre la verificación
    y el archivado. Sin la tercera, un proceso que murió durante el archivado
    podría comitear contenido alterado DESDE el crash.
    """
    _finalize(harness)

    assert harness.puerto.llamadas == [
        "gp1",
        "rv2",
        "node_set",
        "quiescence",
        "rv2",
        "rv2",
    ]


def test_f02_gp1_falla_no_hay_rv2_ni_archive_ni_committed(harness: _Harness) -> None:
    """F02: GP1 final falla -> ROLLBACK_REQUIRED durable, cero gates posteriores."""
    harness.puerto.gp1 = _obs(False, "gp1:WRITING_LEFT", "gp1")

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert harness.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED
    assert harness.puerto.llamadas == ["gp1"], "no se puede ejecutar RV-2 con GP1 fallando"
    assert not harness.backup_path().exists()
    assert reporte.lock.released is False
    assert reporte.lock.retained_as_orphan is True


def test_f03_rv2_falla_no_hay_nodeset_ni_archive(harness: _Harness) -> None:
    """F03: RV-2 final falla -> rollback; NodeSet y archive nunca se tocan."""
    harness.puerto.rv2 = _obs(False, "rv2:tree_digest_difiere", "rv2")

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert harness.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED
    assert "node_set" not in harness.puerto.llamadas
    assert "quiescence" not in harness.puerto.llamadas
    assert not harness.backup_path().exists()


def test_f04_nodeset_falla_no_hay_quiescence_ni_archive(harness: _Harness) -> None:
    """F04: NodeSet falla -> rollback; el probe de quiescence ni se intenta."""
    harness.puerto.node_set = _obs(False, "nodeset:falta_Data_Skyrim.esm", "node_set")

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert "quiescence" not in harness.puerto.llamadas
    assert not harness.backup_path().exists()


def test_f04b_quiescence_falla_no_hay_archive_ni_committed(harness: _Harness) -> None:
    """El rerun de quiescence es un gate real: si falla, no se archiva.

    Un escritor que mantiene un handle abierto sobre el Golden es el caso
    normativo (§4.1.1 matriz H1-H6) y NO puede producir un COMMITTED.
    """
    harness.puerto.quiescence = _obs(False, "quiescence:1_nodo_bloqueado", "quiescence")

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert harness.puerto.llamadas == ["gp1", "rv2", "node_set", "quiescence"]
    assert not harness.backup_path().exists()
    assert harness.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED


def test_f04c_rv2_post_quiescence_falla_tampoco_hay_archive(harness: _Harness) -> None:
    """El SEGUNDO RV-2 detecta drift introducido entre la verificación y el archivo.

    Es el caso F de la revisión adversarial: si el primer RV-2 pasa pero el
    contenido cambia después, sólo la re-observación final lo detecta.
    """

    class _PuertoConDrift(_PuertoFinalizacion):
        def observar_rv2(self, **kwargs: Any) -> GateVerdict:
            self.llamadas.append("rv2")
            if self.llamadas.count("rv2") == 1:
                return self.rv2
            return _obs(False, "contenido_cambio_despues_del_probe", "rv2")

    harness.puerto = _PuertoConDrift()
    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert not harness.backup_path().exists()


# ============================================================================
# F05..F07 — el backup y el commit: durabilidad y flush
# ============================================================================


def test_f05_falla_la_escritura_del_backup_no_hay_committed(harness: _Harness) -> None:
    """F05: el writer falla -> INDETERMINATE, cero backup, cero COMMITTED.

    El store NO confía en el retorno del writer: decide por evidencia de disco.
    """
    harness.writer = _WriterBackup(fallar_write=True)

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert not harness.backup_path().exists()
    assert harness.journal_estado() is not ProtectionTransactionState.COMMITTED
    assert reporte.lock.retained_as_orphan is True


def test_f06_falla_el_flush_del_backup_no_hay_committed(harness: _Harness) -> None:
    """F06: el byte llega pero el flush NO se confirma -> no hay backup durable.

    Es la diferencia entre «WriteFile retornó éxito» y «el backup es durable».
    """
    harness.writer = _WriterBackup(fallar_flush=True)

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert not harness.backup_path().exists()
    assert harness.journal_estado() is not ProtectionTransactionState.COMMITTED


def test_f07_falla_el_flush_de_committed_no_se_reporta_commit_durable(harness: _Harness) -> None:
    """F07: el registro COMMITTED no queda durable -> NO se afirma commit.

    El journal queda INDETERMINATE en memoria tras un flush fallido, y el
    lock se RETIENE: liberar un Golden cuyo estado final se desconoce es
    exactamente el modo de falla que el ADR prohíbe.
    """
    harness.journal_kernel.fallar_flush_de = frozenset({"committed"})

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert not reporte.committed
    assert reporte.lock.released is False
    assert reporte.lock.retained_as_orphan is True
    assert reporte.phase_reached is FinalizationPhase.COMMITTED
    assert "COMMITTED" in reporte.fail_closed_reason
    # El backup SÍ existe y es durable: se publicó y flusheó antes del fallo.
    # Lo que no se confirma es la durabilidad del REGISTRO que lo referencia.
    assert harness.backup_path().exists()
    # Y por eso el estado durable es exactamente el que la evidencia puede
    # probar, no el que el proceso quiso: el journal quedó marcado como no
    # interpretable por el store, así que CUALQUIER estado durable derivado de
    # esos bytes es INDETERMINATE por definición. Afirmar COMMITTED o afirmar
    # APPLYING sería inventar.
    assert harness.journal_estado() is not ProtectionTransactionState.VERIFYING_GP1


def test_f07b_el_backup_es_autocertificado_por_el_plan_no_por_el_proceso(
    harness: _Harness,
) -> None:
    """El COMMITTED queda respaldado por evidencia legible sin el proceso que lo escribió.

    Se releen disco, journal y backup como lo haría un tercero: si el binding
    no se sostiene sin S4-D en memoria, la afirmación era falsa.
    """
    _finalize(harness)

    assert harness.journal_estado() is ProtectionTransactionState.COMMITTED

    backup = harness.backup_path().read_bytes()
    archivo = deserialize_golden_backup_archive(backup)
    assert archivo.binds_to(harness.plan.plan)
    assert archivo.operation_id == harness.plan.operation_id
    assert archivo.authorized_plan_digest == harness.plan.digest
    # Y el journal liga con el MISMO plan.
    parseado = parse_journal_bytes(harness.journal_bytes())
    assert parseado.journal is not None
    assert parseado.journal.authorized_plan_digest == harness.plan.digest


# ============================================================================
# F08..F10 — autoridad: operación, lock y plan
# ============================================================================


def test_f08_lock_de_otra_operacion_cero_gates_destructivos(harness: _Harness) -> None:
    """F08: el lock huérfano pertenece a OTRA operación -> no se toca nada.

    Cero gates ejecutados, cero transiciones, cero escrituras. Un recovery que
    "toma el lock que encuentra" es exactamente el defecto que S4-C ya clausuró
    con ``GoldenLockOrphanedOperationMismatchError``; S4-D lo conserva.
    """
    from sky_claw.local.runtime_vault.golden_mutation_lock import (
        GoldenLockMetadata,
        GoldenLockPhase,
        derive_golden_lock_key,
    )

    harness.lock_kernel.seed(
        _lock_path(harness.raiz),
        GoldenLockMetadata(
            lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            operation_id=_OTRA_OPERACION,
            owner_pid=_PID_DUENO_MUERTO,
            owner_process_creation_time=_CREATION_ACTUAL - 5000,
            session_id=1,
            created_at=1_758_499_100,
            phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
        ),
    )
    estado_previo = harness.journal_bytes()

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert harness.puerto.llamadas == []
    assert harness.journal_bytes() == estado_previo, "un journal intacto es la prueba del cero escritura"
    assert not harness.backup_path().exists()


def test_f08b_lock_con_dueno_vivo_es_lock_busy_sin_tocar_nada(harness: _Harness) -> None:
    """Otra mutadora VIVA sobre el Golden -> LOCK_BUSY, cero escrituras.

    Ningún resultado en memoria es autoridad tras un restart, pero acá el
    fundamento es más fuerte: la exclusión es de sistema operativo, no de
    bookkeeping.
    """
    harness.lock_kernel.owner_alive = True
    estado_previo = harness.journal_bytes()

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.LOCK_BUSY
    assert harness.puerto.llamadas == []
    assert harness.journal_bytes() == estado_previo
    assert not harness.backup_path().exists()


def test_f09_operation_id_mismatch_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """F09: S4-D no puede operar sobre el plan de otra operación."""
    h = _Harness(tmp_path)

    with pytest.raises(FinalizationAuthorityError):
        _finalize(h, operation_id=_OTRA_OPERACION)

    assert h.puerto.llamadas == []
    assert h.journal_estado() is ProtectionTransactionState.APPLYING


def test_f10_digest_del_plan_diferente_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """F10: el journal creado contra un plan no puede finalizarse con otro.

    Se reescribe el ``authorized_plan.json`` del disco con un plan distinto
    (mismo ``operation_id``, otro contenido) y se intenta finalizar con él: el
    binding del journal es la autoridad, y no coincide.
    """
    h = _Harness(tmp_path)
    plan_trocaado = _plan(tree_digest="9" * 64)
    _plan_path(h.raiz).write_bytes(serialize_authorized_plan(plan_trocaado))
    plan = DurableAuthorizedPlan(plan_trocaado, _plan_path(h.raiz), _proof=_MINT_PROOF)

    reporte = _finalize(h, plan=plan)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert "fail-closed" in reporte.fail_closed_reason
    assert h.puerto.llamadas == [], "el binding se comprueba ANTES de cualquier gate"
    assert h.journal_estado() is ProtectionTransactionState.APPLYING
    assert not h.backup_path().exists()


# ============================================================================
# F11..F14 — replay, idempotencia y normalización
# ============================================================================


def test_f11_replay_con_committed_durable_es_idempotente(harness: _Harness) -> None:
    """F11: volver a llamar S4-D tras un COMMITTED durable no repite nada.

    No re-verifica, no re-archiva, no re-muta. Su único trabajo es normalizar el
    lock huérfano. Y si el lock ya quedó RELEASED, tampoco hay nada que hacer.
    """
    _finalize(harness)
    bytes_tras_commit = harness.journal_bytes()
    backup_tras_commit = harness.backup_path().read_bytes()
    harness.puerto.llamadas.clear()

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.ALREADY_COMMITTED
    assert harness.puerto.llamadas == [], "un COMMITTED durable no se re-verifica"
    assert harness.journal_bytes() == bytes_tras_commit
    assert harness.backup_path().read_bytes() == backup_tras_commit


def test_f12_crash_despues_del_archive_durable_el_replay_no_sobrescribe(
    harness: _Harness,
) -> None:
    """F12 (§20 C8): archive durable + crash antes de COMMITTED -> continuar.

    Se deja el journal durable en ``ARCHIVING_BACKUP`` con el backup ya
    escrito, que es exactamente el estado en el que queda el sistema tras un
    crash en D08. El replay debe revalidar el backup y comitear SIN escribirlo
    de nuevo.
    """

    harness.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    harness.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)
    harness.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_NODE_SET)
    harness.journal.enter_finalization_phase(ProtectionTransactionState.ARCHIVING_BACKUP)
    escritura = archive_golden_backup(harness.plan, programdata_resolver=_resolver(harness.raiz), writer=harness.writer)
    assert escritura.republished is True

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.COMMITTED
    # El backup NO se volvió a publicar: create-once.
    assert len(harness.writer.escrituras) == 1
    assert reporte.lock.released is True


def test_f13_backup_existente_ajeno_es_indeterminate(harness: _Harness) -> None:
    """F13 (§20 M): un objeto que no es nuestro backup nunca se sobrescribe."""
    harness.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    harness.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)
    harness.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_NODE_SET)
    harness.journal.enter_finalization_phase(ProtectionTransactionState.ARCHIVING_BACKUP)

    destino = harness.backup_path()
    destino.parent.mkdir(parents=True, exist_ok=True)
    contenido_ajeno = b'{"schema_version":"gp2-golden-backup-v1","operation_id":"' + _OTRA_OPERACION.encode() + b'"}'
    destino.write_bytes(contenido_ajeno)

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert destino.read_bytes() == contenido_ajeno, "evidencia ajena intacta"
    assert harness.journal_estado() is ProtectionTransactionState.ARCHIVING_BACKUP


def test_f14_release_fallido_tras_committed_durable_el_commit_sigue_durable(
    harness: _Harness,
) -> None:
    """F14 (§20 Q): si ``phase=RELEASED`` no se escribe, el commit NO se deshace.

    El commit ya ocurrió y es un hecho durable. Lo que queda pendiente es la
    NORMALIZACIÓN del lock. Reportarlo como INDETERMINATE mentiría sobre el
    commit; reportarlo como COMMITTED limpio mentiría sobre el lock. El reporte
    distingue las dos cosas.
    """
    harness.lock_kernel.fallar_escritura = True

    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.COMMITTED
    assert reporte.committed is True
    assert harness.journal_estado() is ProtectionTransactionState.COMMITTED
    assert reporte.lock.released is False
    assert reporte.lock.retained_as_orphan is True
    assert "RELEASED" in reporte.lock.detail


def test_f15_s4d_antes_de_todos_mutated_es_rechazado(tmp_path: pathlib.Path) -> None:
    """F15: sin todos los nodos MUTATED durable, S4-D no arranca.

    Un nodo en ``MUTATING`` significa un apply a medio camino. Verificar «lo que
    haya» produciría un COMMITTED sobre un Golden incompleto.
    """
    # El harness deja el último nodo en MUTATING durable: MUTATED nunca escrito.
    h = _Harness(tmp_path, nodos_incompletos=1)
    # El plan ordena sus nodos, así que el incompleto se localiza por estado y
    # no por posición: la prueba tiene que ser sobre la CONDICIÓN, no sobre el
    # orden de recorrido.
    en_mutating = [
        node.relative_path
        for node in h.plan.plan.nodes
        if h.journal.journal.node_state(node.relative_path) is NodeWalState.MUTATING
    ]
    assert len(en_mutating) == 1, f"se esperaba exactamente un nodo a medio camino; hubo {en_mutating}"

    with pytest.raises(FinalizationPreconditionError):
        _finalize(h)

    assert h.puerto.llamadas == []
    assert h.journal_estado() is ProtectionTransactionState.APPLYING
    assert not h.backup_path().exists()


# ============================================================================
# Continuidad del lock (requisito central de S4-D)
# ============================================================================


def test_el_lock_se_adquiere_para_la_misma_operation_id(harness: _Harness) -> None:
    """La exclusividad se conserva bajo la MISMA operación, nunca como otra."""
    reporte = _finalize(harness)

    metadata = harness.lock_kernel.files[str(_lock_path(harness.raiz))]
    assert metadata  # el lock se escribió
    assert reporte.committed is True
    # Y la metadata final dice RELEASED con la MISMA operation_id.
    from sky_claw.local.runtime_vault.golden_mutation_lock import deserialize_golden_lock_metadata

    final = deserialize_golden_lock_metadata(metadata)
    assert final.operation_id == harness.plan.operation_id
    assert final.phase == GoldenLockPhase.RELEASED.value


def test_otro_proceso_no_puede_mutar_entre_el_ultimo_mutated_y_committed(
    harness: _Harness,
) -> None:
    """§4: desde el último MUTATED durable hasta COMMITTED, nadie se intercala.

    Se comprueba con el kernel REAL de exclusión (fake determinista, misma
    semántica que ``dwShareMode=0``): mientras S4-D tiene el lock tomado, una
    segunda adquisición falla. El check se hace en CADA gate, no sólo al final:
    una ventana entre gates también sería una ventana.
    """
    intrusos: list[str] = []

    class _PuertoQueSondea(_PuertoFinalizacion):
        def _verificar_exclusion(self, gate: str) -> None:
            with pytest.raises(GoldenLockBusyError):
                acquire_golden_mutation_lock_for_recovery(
                    volume_serial_number=_VOLUME_SERIAL,
                    root_file_id=_ROOT_FILE_ID,
                    operation_id=_OTRA_OPERACION,
                    programdata_resolver=_resolver(harness.raiz),
                    kernel=harness.lock_kernel,
                )
            intrusos.append(gate)

        def observar_gp1(self, **kwargs: Any) -> GateVerdict:
            self.llamadas.append("gp1")
            self._verificar_exclusion("gp1")
            return self.gp1

        def observar_rv2(self, **kwargs: Any) -> GateVerdict:
            self.llamadas.append("rv2")
            self._verificar_exclusion("rv2")
            return self.rv2

        def observar_node_set(self, **kwargs: Any) -> GateVerdict:
            self.llamadas.append("node_set")
            self._verificar_exclusion("node_set")
            return self.node_set

        def observar_quiescence(self, **kwargs: Any) -> GateVerdict:
            self.llamadas.append("quiescence")
            self._verificar_exclusion("quiescence")
            return self.quiescence

    harness.puerto = _PuertoQueSondea()
    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.COMMITTED
    assert intrusos == ["gp1", "rv2", "node_set", "quiescence", "rv2", "rv2"]


def test_rollback_required_retiene_el_lock(harness: _Harness) -> None:
    """Un gate fallido NO libera el lock: el Golden todavía debe ser restaurado.

    Liberar acá abriría una ventana en la que otra operación muta un Golden a
    medio deshacer. Se retiene como huérfano de ESTA operación, que es la única
    que puede reclamarlo.
    """
    from sky_claw.local.runtime_vault.golden_mutation_lock import deserialize_golden_lock_metadata

    harness.puerto.gp1 = _obs(False, "gp1:WRITING_LEFT", "gp1")
    reporte = _finalize(harness)

    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    metadata = deserialize_golden_lock_metadata(harness.lock_kernel.files[str(_lock_path(harness.raiz))])
    assert metadata.phase != GoldenLockPhase.RELEASED.value
    assert metadata.operation_id == harness.plan.operation_id
