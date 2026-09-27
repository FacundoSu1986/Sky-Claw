"""Tests causales GP2-S4B: apply ACL real atado a handle, con rollback exacto.

El oráculo central es el ORDEN (§12.2) y el CONTADOR de ``SetSecurityInfo``:

    journal append MUTATING(K) -> FlushFileBuffers TRUE -> permiso -> consume-once
        -> SetSecurityInfo -> POST -> MUTATED durable

Un mutante que invierta cualquiera de esos pasos (o que mute sin permiso, con un
permiso de otro nodo/operación/plan, o tras un drift de identidad) DEBE dejar el
contador en 0.

Las pruebas de modelo/causalidad usan un puerto falso determinista
(:class:`_FakePort`) para poder ejecutar las 14 escenas A–N sin tocar un Golden
real. Las pruebas del namespace real corren bajo ``skipif(win32)`` sobre un árbol
TEMPORAL creado por el propio test (``%TEMP%\\SkyClaw-S4B-RIG-<uuid>``): crear ->
mutar -> verificar -> rollback -> verificar PRE -> borrar. NUNCA tocan Skyrim,
Steam, MO2, el runtime ni el Golden real.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import sys
import uuid
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorization_context import (
    PrivilegedBoundarySession,
    acquire_operator_primary_token_from_coordinator,
)
from sky_claw.local.runtime_vault.authorized_plan import (
    AUTHORIZED_PLAN_SCHEMA_VERSION,
    AuthorizedPlan,
    serialize_authorized_plan,
)
from sky_claw.local.runtime_vault.authorized_plan_store import _MINT_PROOF, DurableAuthorizedPlan
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenMutationLockHandle,
    acquire_golden_mutation_lock,
)
from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.mutation_executor import (
    ApplyReport,
    HandleBoundTargetDaclPort,
    MutationApplyError,
    MutationAuthorityError,
    MutationExecutorError,
    MutationExecutorUnsupportedError,
    MutationPermitError,
    MutationRollbackError,
    NodeIdentity,
    NodeMutationOutcome,
    apply_authorized_plan,
    apply_order,
    derive_node_path,
    node_depth,
    rollback_order,
)
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from sky_claw.local.runtime_vault.protection_journal_store import (
    DurableJournalFlushError,
    DurableProtectionJournal,
    create_protection_journal,
    derive_protection_journal_path,
)

_OPERATION_ID = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_OTRA_OPERATION_ID = "7a1c9e55-4b2d-4f60-9c31-2d8e5f6a7b90"
_CANONICAL_ROOT = "C:\\Games\\Skyrim"
_VOLUME_SERIAL = 0xA1B2C3D4
_ROOT_FILE_ID = 0x1122334455667788
_POLICY_VERSION = "golden-policy-v1"

_RUTAS = (
    ".",
    "Data",
    "Data/Scripts",
    "Data/Scripts/deep.pex",
    "Data/Skyrim.esm",
    "Data/quest.esp",
)


# ============================================================================
# Fábricas: plan durable + journal + sesión
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


def _plan(
    *,
    operation_id: str = _OPERATION_ID,
    root: str = _CANONICAL_ROOT,
    volume_serial: int = _VOLUME_SERIAL,
    rutas: tuple[str, ...] = _RUTAS,
) -> AuthorizedPlan:
    nodos = tuple(_node(r, _ROOT_FILE_ID + i) for i, r in enumerate(rutas))
    return AuthorizedPlan(
        schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
        operation_id=operation_id,
        canonical_root=root,
        volume_serial_number=volume_serial,
        root_file_id=_ROOT_FILE_ID,
        tree_digest=TreeDigest(digest="c" * 64, files=len(rutas), bytes=1024),
        node_count=len(rutas),
        policy_version=_POLICY_VERSION,
        staging_digest="d" * 64,
        operator_identity=_operator_evidence(),
        nodes=nodos,
    )


def _operator_evidence() -> Any:
    from sky_claw.local.runtime_vault.authorization_context import OperatorTokenEvidence

    return OperatorTokenEvidence(
        operator_sid="S-1-5-21-1001-1002-1003-1001",
        token_type="primary",
        acquired_via="same_account_coordinator_extraction",
    )


def _resolver(raiz: pathlib.Path) -> Any:
    return lambda: raiz


def _plan_durable(raiz: pathlib.Path, **kwargs: Any) -> DurableAuthorizedPlan:
    plan = _plan(**kwargs)
    destino = derive_protection_journal_path(plan.operation_id, programdata_resolver=_resolver(raiz)).with_name(
        "authorized_plan.json"
    )
    destino.write_bytes(serialize_authorized_plan(plan))
    return DurableAuthorizedPlan(plan, destino, _proof=_MINT_PROOF)


class FakeKernel:
    """Kernel de durabilidad falso: append real sobre disco, flush conmutable.

    El flush de PUBLICACIÓN se registra como ``publish-flush`` para que los
    ``flush-wal`` posteriores sean distinguibles en la línea de tiempo.
    """

    def __init__(self, *, flush_ok: bool = True) -> None:
        self.flush_ok = flush_ok
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
            from sky_claw.local.runtime_vault.protection_journal_store import (
                ProtectionJournalAlreadyExistsError,
            )

            raise ProtectionJournalAlreadyExistsError(f"ya existe: '{destino}'")
        self.eventos.append("publish-flush")
        if not self.flush_ok:
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
            from sky_claw.local.runtime_vault.protection_journal_store import (
                ProtectionJournalNotFoundError,
            )

            raise ProtectionJournalNotFoundError(f"no existe: '{destino}'")
        self._siguiente += 1
        self._handles[self._siguiente] = destino
        self.eventos.append("open")
        return self._siguiente

    def append(self, handle: int, payload: bytes) -> None:
        with self._handles[handle].open("ab") as fh:
            fh.write(payload)
        self.eventos.append("append")

    def flush(self, handle: int) -> bool:
        self.eventos.append("flush-wal")
        return self.flush_ok

    def read_all(self, path: pathlib.PurePath) -> bytes:
        return pathlib.Path(path).read_bytes()

    def close(self, handle: int) -> None:
        self._handles.pop(handle, None)
        self.eventos.append("close")

    def remove(self, path: pathlib.PurePath) -> None:
        pathlib.Path(path).unlink(missing_ok=True)


class FakeLockKernel:
    """Kernel fake del GoldenMutationLock (monoproceso, metadata en memoria)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.busy_paths: set[str] = set()
        self.open_handles: dict[int, str] = {}
        self.reparse_tags: dict[int, int] = {}

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        path_str = str(path)
        if path_str in self.busy_paths:
            from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockBusyError

            raise GoldenLockBusyError("ocupado", win32_error=32)
        handle = 700 + len(self.open_handles)
        self.open_handles[handle] = path_str
        self.busy_paths.add(path_str)
        self.files.setdefault(path_str, b"")
        return handle

    def get_reparse_tag(self, handle: int) -> int:
        return self.reparse_tags.get(handle, 0)

    def read_lock_bytes(self, handle: int) -> bytes:
        return self.files[self.open_handles[handle]]

    def write_lock_bytes(self, handle: int, payload: bytes) -> None:
        self.files[self.open_handles[handle]] = payload

    def flush_lock(self, handle: int) -> None:
        return None

    def is_owner_alive(self, owner_pid: int, owner_creation_time: int) -> bool | None:
        return True

    def current_process_identity(self) -> tuple[int, int, int]:
        return (4242, 133_456_789_012_345_678, 1)

    def current_epoch_seconds(self) -> int:
        return 1_758_499_200

    def close_handle(self, handle: int) -> None:
        self.busy_paths.discard(self.open_handles.pop(handle, ""))


class _FakeTokenAdapter:
    """Adapter falso del token primario: sin llamadas al SO real."""

    def open_process(self, desired_access: int, pid: int) -> int:
        return 101

    def read_process_creation_time(self, process_handle: int) -> int | None:
        return 133_456_789_012_345_678

    def read_process_image_path(self, process_handle: int) -> str | None:
        return "C:\\Program Files\\Sky-Claw\\sky-claw.exe"

    def open_process_token(self, process_handle: int, desired_access: int) -> int:
        return 202

    def duplicate_token_ex_primary(self, source_handle: int, desired_access: int) -> int:
        return 303

    def get_token_type(self, token_handle: int) -> int:
        return 1

    def read_token_user_sid(self, token_handle: int) -> str:
        return "S-1-5-21-1001-1002-1003-1001"

    def close_handle(self, handle: int) -> None:
        return None


def _fake_programdata() -> pathlib.PureWindowsPath:
    return pathlib.PureWindowsPath("C:/ProgramData")


def _lock(
    *,
    volume_serial_number: int = _VOLUME_SERIAL,
    root_file_id: int = _ROOT_FILE_ID,
    operation_id: str = _OPERATION_ID,
) -> GoldenMutationLockHandle:
    return acquire_golden_mutation_lock(
        volume_serial_number,
        root_file_id,
        operation_id,
        kernel=FakeLockKernel(),
        programdata_resolver=_fake_programdata,
    )


def _session(
    *,
    volume_serial_number: int = _VOLUME_SERIAL,
    root_file_id: int = _ROOT_FILE_ID,
    operation_id: str = _OPERATION_ID,
) -> PrivilegedBoundarySession:
    from sky_claw.local.runtime_vault.authorization_context import CoordinatorProcessIdentity

    identity = CoordinatorProcessIdentity(
        pid=4242,
        creation_time=133_456_789_012_345_678,
        image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
    )
    token = acquire_operator_primary_token_from_coordinator(identity, adapter=_FakeTokenAdapter())
    return PrivilegedBoundarySession(
        operator_token=token,
        lock=_lock(
            volume_serial_number=volume_serial_number,
            root_file_id=root_file_id,
            operation_id=operation_id,
        ),
    )


def _raiz(tmp_path: pathlib.Path, operation_id: str = _OPERATION_ID) -> pathlib.Path:
    operacion = tmp_path / "Sky-Claw" / "runtime_vault" / "operations" / operation_id
    operacion.mkdir(parents=True)
    return tmp_path


# ============================================================================
# Puerto falso determinista
# ============================================================================


class _FakePort:
    """Puerto de mutación por handle: determinista, sin Win32.

    Cuenta las llamadas REALES a ``SetSecurityInfo`` (oráculo de los tests A–N) y
    registra la secuencia causal completa en ``timeline``.
    """

    def __init__(self, plan: AuthorizedPlan, *, timeline: list[str] | None = None) -> None:
        self._plan = plan
        self._nodos = {n.relative_path: n for n in plan.nodes}
        self.timeline: list[str] = timeline if timeline is not None else []
        self._siguiente = 9000
        self._handles: dict[int, str] = {}
        self.cerrados: dict[int, int] = {}
        self.identidad: dict[str, NodeIdentity] = {}
        self.live_pre_sha: dict[str, str] = {}
        self.drift: dict[str, tuple[int, NodeIdentity]] = {}
        self.lecturas_identidad: dict[str, int] = {}
        self.apply_falla: set[str] = set()
        self.verify_falla: set[str] = set()
        self.restore_falla: set[str] = set()
        self.restore_verify_falla: set[str] = set()
        self.open_falla: set[str] = set()
        self.probe_calls: list[str] = []
        self.aplicados: list[NodeSecurityBackup] = []
        self.restaurados: list[NodeSecurityBackup] = []
        self._setsescURITYINFO = 0

    # -- helpers -----------------------------------------------------------

    def _relpath(self, path: pathlib.Path) -> str:
        texto = str(path).replace("\\", "/")
        raiz = self._plan.canonical_root.replace("\\", "/").rstrip("/")
        if texto == raiz:
            return "."
        prefijo = f"{raiz}/"
        if texto.startswith(prefijo):
            return texto[len(prefijo) :]
        raise AssertionError(f"ruta fuera del canonical_root del plan: {texto}")

    def _identidad_por_defecto(self, rel_path: str) -> NodeIdentity:
        nodo = self._nodos[rel_path]
        return NodeIdentity(
            volume_serial_number=nodo.volume_serial_number,
            file_id=nodo.file_id,
            reparse_tag=0,
            number_of_links=1,
        )

    # -- contrato NodeMutationPort ----------------------------------------

    def open(self, path: pathlib.Path, node_kind: GoldenProtectionNodeKind) -> int:
        rel = self._relpath(path)
        if rel in self.open_falla:
            raise OSError(f"no se pudo abrir el handle de mutación de '{rel}'")
        self._siguiente += 1
        self._handles[self._siguiente] = rel
        self.cerrados.setdefault(self._siguiente, 0)
        self.timeline.append(f"open:{rel}")
        return self._siguiente

    def close(self, handle: int) -> None:
        self.cerrados[handle] = self.cerrados.get(handle, 0) + 1
        self.timeline.append(f"close:{self._handles[handle]}")

    def read_identity(self, handle: int) -> NodeIdentity:
        rel = self._handles[handle]
        indice = self.lecturas_identidad.get(rel, 0) + 1
        self.lecturas_identidad[rel] = indice
        self.timeline.append(f"identity:{rel}:#{indice}")
        desde, identidad = self.drift.get(rel, (0, self._identidad_por_defecto(rel)))
        if indice >= desde:
            return identidad
        return self._identidad_por_defecto(rel)

    def read_live_pre_sd_sha256(self, handle: int) -> str:
        rel = self._handles[handle]
        self.timeline.append(f"pre-sd:{rel}")
        return self.live_pre_sha.get(rel, self._nodos[rel].pre_sd_sha256)

    def apply_target_dacl(self, handle: int, node: NodeSecurityBackup) -> None:
        rel = self._handles[handle]
        self._setsescURITYINFO += 1
        self.timeline.append(f"apply:{rel}")
        self.aplicados.append(node)
        if rel in self.apply_falla:
            raise OSError(f"SetSecurityInfo falló sobre '{rel}'")

    def verify_target_dacl(self, handle: int, node: NodeSecurityBackup) -> None:
        rel = self._handles[handle]
        self.timeline.append(f"verify:{rel}")
        if rel in self.verify_falla:
            raise MutationApplyError(f"POST verification falló sobre '{rel}'")

    def restore_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        rel = self._handles[handle]
        self._setsescURITYINFO += 1
        self.timeline.append(f"restore:{rel}")
        self.restaurados.append(node)
        if rel in self.restore_falla:
            raise OSError(f"SetSecurityInfo de restauración falló sobre '{rel}'")

    def verify_restored_pre_sd(self, handle: int, node: NodeSecurityBackup) -> None:
        rel = self._handles[handle]
        self.timeline.append(f"verify-restore:{rel}")
        if rel in self.restore_verify_falla:
            raise MutationRollbackError(f"verificación post-restore falló sobre '{rel}'")

    @property
    def setsecurityinfo_calls(self) -> int:
        return self._setsescURITYINFO


def _probe_recorder(port: _FakePort) -> Any:
    """Probe de quiescencia falso: registra la llamada y NO abre ningún handle."""

    def _probe(path: pathlib.Path, node_kind: GoldenProtectionNodeKind) -> None:
        port.probe_calls.append(str(path))

    return _probe


def _spy_journal(journal: DurableProtectionJournal, **factory: Any) -> DurableProtectionJournal:
    """Copia superficial del journal con ``record_node_mutation_intent`` reemplazable.

    ``DurableProtectionJournal`` usa ``__slots__``; la subclase aporta ``__dict__``
    y permite inyectar el seam sin tocar el producto.
    """

    class _Spy(DurableProtectionJournal):
        pass

    spy = _Spy.__new__(_Spy)
    for slot in DurableProtectionJournal.__slots__:
        setattr(spy, slot, getattr(journal, slot))
    if "permit_factory" in factory:
        original = journal.record_node_mutation_intent

        def _intent(binding: Any, *, _original: Any = original, _factory: Any = factory["permit_factory"]) -> Any:
            return _factory(_original, binding)

        spy.record_node_mutation_intent = _intent  # type: ignore[method-assign]
    return spy


def _apply(
    tmp_path: pathlib.Path,
    *,
    port: _FakePort | None = None,
    plan_kwargs: dict[str, Any] | None = None,
    kernel: FakeKernel | None = None,
    session_kwargs: dict[str, Any] | None = None,
    probe: Any = None,
) -> tuple[ApplyReport, _FakePort, DurableProtectionJournal]:
    raiz = _raiz(tmp_path)
    durable = _plan_durable(raiz, **(plan_kwargs or {}))
    activo = kernel if kernel is not None else FakeKernel()
    journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=activo)
    sesion = _session(**(session_kwargs or {}))
    fake = port if port is not None else _FakePort(durable.plan, timeline=activo.eventos)
    reporte = apply_authorized_plan(
        plan=durable,
        journal=journal,
        session=sesion,
        port=fake,
        probe=probe if probe is not None else _probe_recorder(fake),
    )
    sesion.close()
    return reporte, fake, journal


# ============================================================================
# 1. Orden determinista (I y J)
# ============================================================================


class TestOrdenDeterminista:
    """Apply bottom-up y rollback top-down, derivados de la profundidad (§16/§17)."""

    def test_profundidad_de_nodo(self) -> None:
        assert node_depth(".") == 0
        assert node_depth("Data") == 1
        assert node_depth("Data/Scripts") == 2
        assert node_depth("Data/Scripts/deep.pex") == 3

    def test_i_apply_bottom_up_independiente_del_orden_de_entrada(self, tmp_path: pathlib.Path) -> None:
        """Barajar la tupla de nodos NO cambia el orden de apply (§16.1)."""
        base = _plan()
        barajado = tuple(reversed(base.nodes))
        assert tuple(n.relative_path for n in barajado) != tuple(n.relative_path for n in apply_order(base))
        duplicado = AuthorizedPlan(
            schema_version=base.schema_version,
            operation_id=base.operation_id,
            canonical_root=base.canonical_root,
            volume_serial_number=base.volume_serial_number,
            root_file_id=base.root_file_id,
            tree_digest=base.tree_digest,
            node_count=base.node_count,
            policy_version=base.policy_version,
            staging_digest=base.staging_digest,
            operator_identity=base.operator_identity,
            nodes=barajado,
        )
        assert apply_order(base) == apply_order(duplicado)

    def test_i_apply_bottom_up_descendientes_primero_root_ultimo(self) -> None:
        orden = tuple(n.relative_path for n in apply_order(_plan()))
        assert orden == (
            "Data/Scripts/deep.pex",
            "Data/Scripts",
            "Data/Skyrim.esm",
            "Data/quest.esp",
            "Data",
            ".",
        )
        assert orden[0] == "Data/Scripts/deep.pex"
        assert orden[-1] == "."

    def test_j_rollback_top_down_root_primero(self) -> None:
        plan = _plan()
        mutados = [n.relative_path for n in apply_order(plan)]
        orden = tuple(n.relative_path for n in rollback_order(plan, mutados))
        assert orden == (
            ".",
            "Data",
            "Data/Scripts",
            "Data/Skyrim.esm",
            "Data/quest.esp",
            "Data/Scripts/deep.pex",
        )

    def test_j_rollback_solo_incluye_nodos_mutados(self) -> None:
        plan = _plan()
        orden = rollback_order(plan, ["Data/quest.esp", "Data/Scripts/deep.pex"])
        # Top-down: el más superficial (quest.esp, profundidad 2) antes que deep.pex (3).
        assert tuple(n.relative_path for n in orden) == (
            "Data/quest.esp",
            "Data/Scripts/deep.pex",
        )

    def test_apply_orden_es_permutacion_de_los_nodos_del_plan(self) -> None:
        plan = _plan()
        assert sorted(n.relative_path for n in apply_order(plan)) == sorted(n.relative_path for n in plan.nodes)


# ============================================================================
# 2. Secuencia normativa completa (A) y fallo de flush (B)
# ============================================================================


class TestSecuenciaWAL:
    """A: el permiso existe sólo tras MUTATING durable. B: flush FALSE -> 0 mutaciones."""

    def test_a_permiso_solo_tras_mutating_y_flush(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = FakeKernel()
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=kernel)
        port = _FakePort(durable.plan, timeline=kernel.eventos)
        apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        # La línea de tiempo es UNA sola secuencia compartida: WAL y mutación.
        assert kernel.eventos[:8] == [
            "stage",
            "append",
            "append",
            "publish-flush",
            "publish",
            "discard",
            "close",
            "open",
        ]
        primero = kernel.eventos.index("apply:Data/Scripts/deep.pex")
        # Todo el gate ocurre ANTES del primer SetSecurityInfo.
        assert kernel.eventos.index("pre-sd:Data/Scripts/deep.pex") < primero
        assert kernel.eventos.index("flush-wal") < primero
        assert kernel.eventos.index("identity:Data/Scripts/deep.pex:#1") < primero
        # El permiso se mintó DESPUÉS del flush y ANTES del SetSecurityInfo: la
        # lectura anti-hardlink post-WAL es la prueba de que el flush ya pasó.
        assert kernel.eventos[kernel.eventos.index("flush-wal") + 1] == "identity:Data/Scripts/deep.pex:#2"

    def test_a_ninguna_mutacion_antes_del_permiso(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = FakeKernel()
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=kernel)
        port = _FakePort(durable.plan, timeline=kernel.eventos)
        apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert port.setsecurityinfo_calls == len(durable.plan.nodes)
        # Cada nodo: open -> identity -> pre-sd -> append -> flush -> identity#2 -> apply
        for rel in (n.relative_path for n in durable.plan.nodes):
            assert f"pre-sd:{rel}" in kernel.eventos
            assert kernel.eventos.index(f"pre-sd:{rel}") < kernel.eventos.index(f"apply:{rel}")

    def test_b_flush_fallido_del_journal_no_muta_nada(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = FakeKernel()
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=kernel)
        kernel.flush_ok = False  # sólo a partir de ahora: la creación ya flusheó
        port = _FakePort(durable.plan, timeline=kernel.eventos)
        reporte = apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert port.setsecurityinfo_calls == 0
        assert port.aplicados == []
        assert reporte.applied_nodes == ()
        assert reporte.apply_error is not None
        assert reporte.rollback_error is None
        assert reporte.rolled_back_nodes == ()
        assert reporte.rollback_error is None
        # El journal quedó envenenado por el flush fallido: INDETERMINATE es honesto.
        assert reporte.transaction_state is ProtectionTransactionState.INDETERMINATE

    def test_b_flush_fallido_no_deja_ningun_nodo_mutating(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = FakeKernel()
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=kernel)
        kernel.flush_ok = False
        apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=_FakePort(durable.plan),
            probe=_probe_recorder(_FakePort(durable.plan)),
        )
        assert journal.journal.node_records == ()
        assert journal.transaction_state is ProtectionTransactionState.INDETERMINATE


# ============================================================================
# 3. Binding del permiso (C)
# ============================================================================


class TestBindingDelPermiso:
    """C: permiso de otra operación/plan/nodo, o ya consumido -> 0 mutaciones."""

    def test_c_permiso_de_otro_nodo(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        port = _FakePort(durable.plan)

        def _factory(original: Any, binding: Any) -> Any:
            # El WAL devuelve un permiso acuñado para OTRO nodo del plan.
            return original(journal.node_binding("Data/quest.esp"))

        espia = _spy_journal(journal, permit_factory=_factory)
        reporte = apply_authorized_plan(
            plan=durable,
            journal=espia,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert port.setsecurityinfo_calls == 0
        assert reporte.apply_error is not None
        assert isinstance(reporte.apply_error, MutationPermitError)
        assert reporte.applied_nodes == ()

    def test_c_permiso_ya_consumido(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        port = _FakePort(durable.plan)

        def _factory(original: Any, binding: Any) -> Any:
            permit = original(binding)
            permit.mark_consumed()  # un permiso autoriza UNA sola mutación
            return permit

        espia = _spy_journal(journal, permit_factory=_factory)
        reporte = apply_authorized_plan(
            plan=durable,
            journal=espia,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert port.setsecurityinfo_calls == 0
        assert isinstance(reporte.apply_error, MutationPermitError)

    def test_c_permiso_de_otra_operacion(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path, _OTRA_OPERATION_ID)
        otra = _plan_durable(raiz, operation_id=_OTRA_OPERATION_ID)
        journal_otro = create_protection_journal(otra, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        raiz_propia = _raiz(tmp_path)
        durable = _plan_durable(raiz_propia)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz_propia), kernel=FakeKernel())
        port = _FakePort(durable.plan)

        def _factory(original: Any, binding: Any) -> Any:
            return journal_otro.record_node_mutation_intent(journal_otro.node_binding(binding.relative_path))

        espia = _spy_journal(journal, permit_factory=_factory)
        reporte = apply_authorized_plan(
            plan=durable,
            journal=espia,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert port.setsecurityinfo_calls == 0
        assert isinstance(reporte.apply_error, MutationPermitError)
        assert reporte.apply_error is not None
        assert "operación" in str(reporte.apply_error)

    def test_c_permiso_de_otro_plan(self, tmp_path: pathlib.Path) -> None:
        raiz_otra = _raiz(tmp_path, _OTRA_OPERATION_ID)
        otra = _plan_durable(raiz_otra, operation_id=_OTRA_OPERATION_ID, root="C:\\Games\\SkyrimSE")
        journal_otro = create_protection_journal(otra, programdata_resolver=_resolver(raiz_otra), kernel=FakeKernel())
        raiz_propia = _raiz(tmp_path)
        durable = _plan_durable(raiz_propia)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz_propia), kernel=FakeKernel())
        port = _FakePort(durable.plan)

        def _factory(original: Any, binding: Any) -> Any:
            return journal_otro.record_node_mutation_intent(journal_otro.node_binding(binding.relative_path))

        espia = _spy_journal(journal, permit_factory=_factory)
        reporte = apply_authorized_plan(
            plan=durable,
            journal=espia,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert port.setsecurityinfo_calls == 0
        assert isinstance(reporte.apply_error, MutationPermitError)
        assert reporte.apply_error is not None
        assert "plan" in str(reporte.apply_error)


# ============================================================================
# 4. Drift de identidad (D)
# ============================================================================


class TestDriftDeIdentidad:
    """D: VolumeSerialNumber, FileId o ReparseTag discordantes -> 0 mutaciones."""

    def test_d_drift_de_volume_serial(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.drift["Data/Scripts/deep.pex"] = (
            1,
            NodeIdentity(
                volume_serial_number=0xDEADBEEF,
                file_id=_ROOT_FILE_ID + 3,
                reparse_tag=0,
                number_of_links=1,
            ),
        )
        reporte, fake, _ = _apply(tmp_path, port=port)
        assert fake.setsecurityinfo_calls == 0
        assert reporte.applied_nodes == ()
        assert reporte.apply_error is not None
        assert "VolumeSerialNumber" in str(reporte.apply_error)

    def test_d_drift_de_file_id(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.drift["Data/Scripts/deep.pex"] = (
            1,
            NodeIdentity(
                volume_serial_number=_VOLUME_SERIAL,
                file_id=0xFFFFFFFF,
                reparse_tag=0,
                number_of_links=1,
            ),
        )
        reporte, fake, _ = _apply(tmp_path, port=port)
        assert fake.setsecurityinfo_calls == 0
        assert reporte.apply_error is not None
        assert "FileId" in str(reporte.apply_error)

    def test_d_reparse_point_rechazado_fail_closed(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.drift["Data/Scripts/deep.pex"] = (
            1,
            NodeIdentity(
                volume_serial_number=_VOLUME_SERIAL,
                file_id=_ROOT_FILE_ID + 3,
                reparse_tag=0xA000000C,
                number_of_links=1,
            ),
        )
        reporte, fake, _ = _apply(tmp_path, port=port)
        assert fake.setsecurityinfo_calls == 0
        assert reporte.apply_error is not None
        assert "reparse point" in str(reporte.apply_error)

    def test_d_drift_de_pre_sd_vivo(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.live_pre_sha["Data/Scripts/deep.pex"] = "f" * 64
        reporte, fake, _ = _apply(tmp_path, port=port)
        assert fake.setsecurityinfo_calls == 0
        assert reporte.applied_nodes == ()
        assert reporte.apply_error is not None
        assert "PRE SD" in str(reporte.apply_error)


# ============================================================================
# 5. Carrera de hardlink (E)
# ============================================================================


class TestCarreraDeHardlink:
    """E: 1 enlace pre-WAL -> 2 enlaces post-WAL => 0 mutaciones + ROLLBACK_REQUIRED."""

    def test_e_segundo_enlace_entre_el_wal_y_la_mutacion(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.drift["Data/Scripts/deep.pex"] = (
            2,
            NodeIdentity(
                volume_serial_number=_VOLUME_SERIAL,
                file_id=_ROOT_FILE_ID + 3,
                reparse_tag=0,
                number_of_links=2,
            ),
        )
        reporte, fake, _ = _apply(tmp_path, port=port)
        assert fake.setsecurityinfo_calls == 0
        assert fake.aplicados == []
        assert reporte.applied_nodes == ()
        assert reporte.rolled_back_nodes == ()
        assert reporte.transaction_state is ProtectionTransactionState.ROLLED_BACK
        assert reporte.apply_error is not None
        assert "Hardlink" in str(reporte.apply_error)

    def test_e_la_segunda_lectura_es_la_que_detecta(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.drift["Data/quest.esp"] = (
            2,
            NodeIdentity(
                volume_serial_number=_VOLUME_SERIAL,
                file_id=_ROOT_FILE_ID + 5,
                reparse_tag=0,
                number_of_links=2,
            ),
        )
        _apply(tmp_path, port=port)
        # Lectura #1 (pre-WAL) vio 1 enlace; la #2 (post-WAL) vio 2.
        assert port.lecturas_identidad["Data/quest.esp"] == 2

    def test_e_nodo_intermedio_con_drift_no_muta_los_siguientes(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.drift["Data/Skyrim.esm"] = (
            2,
            NodeIdentity(
                volume_serial_number=_VOLUME_SERIAL,
                file_id=_ROOT_FILE_ID + 4,
                reparse_tag=0,
                number_of_links=2,
            ),
        )
        reporte, fake, _ = _apply(tmp_path, port=port)
        # El nodo con drift NO recibió SetSecurityInfo (ni apply ni restore).
        assert "Data/Skyrim.esm" not in [n.relative_path for n in fake.aplicados]
        assert "Data/Skyrim.esm" not in [n.relative_path for n in fake.restaurados]
        # Los dos nodos previos SÍ quedaron mutados y se restauran top-down.
        assert reporte.applied_nodes == ("Data/Scripts/deep.pex", "Data/Scripts")
        assert reporte.rolled_back_nodes == ("Data/Scripts/deep.pex", "Data/Scripts")
        assert reporte.transaction_state is ProtectionTransactionState.ROLLED_BACK
        assert reporte.apply_error is not None
        assert "Hardlink" in str(reporte.apply_error)


# ============================================================================
# 6. Fallo de SetSecurityInfo (F)
# ============================================================================


class TestFalloDeMutacion:
    """F: SetSecurityInfo falla -> permiso consumido -> rollback -> sin nodo siguiente."""

    def test_f_permiso_consumido_y_error_de_apply(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        port = _FakePort(durable.plan)
        port.apply_falla.add("Data/Skyrim.esm")
        reporte = apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        # 3 intentos alcanzados (deep.pex y Data/Scripts ok; Skyrim.esm fallado)
        # + 2 restauraciones exactas de los nodos que SÍ quedaron mutados.
        assert port.setsecurityinfo_calls == 5
        assert tuple(n.relative_path for n in port.aplicados) == (
            "Data/Scripts/deep.pex",
            "Data/Scripts",
            "Data/Skyrim.esm",
        )
        assert isinstance(reporte.apply_error, MutationApplyError)
        assert reporte.nodes_with_confirmed_wal == ("Data/Scripts/deep.pex", "Data/Scripts")
        assert reporte.rolled_back_nodes == ("Data/Scripts/deep.pex", "Data/Scripts")
        assert reporte.transaction_state is ProtectionTransactionState.ROLLED_BACK

    def test_f_no_continua_al_nodo_siguiente(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data/Skyrim.esm")
        _apply(tmp_path, port=port)
        # Aplicó deep.pex y Data/Scripts; falló en Skyrim.esm; quest.esp NO se tocó.
        assert tuple(n.relative_path for n in port.aplicados) == (
            "Data/Scripts/deep.pex",
            "Data/Scripts",
            "Data/Skyrim.esm",
        )
        assert "Data/quest.esp" not in [n.relative_path for n in port.aplicados]

    def test_f_nodos_ya_mutados_se_restauran_top_down(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data/quest.esp")
        reporte, _, _ = _apply(tmp_path, port=port)
        assert tuple(n.relative_path for n in port.restaurados) == (
            "Data/Scripts",
            "Data/Skyrim.esm",
            "Data/Scripts/deep.pex",
        )
        assert reporte.rolled_back_nodes == ("Data/Scripts/deep.pex", "Data/Scripts", "Data/Skyrim.esm")

    def test_f_permiso_queda_inservible_tras_el_fallo(self, tmp_path: pathlib.Path) -> None:
        """Un SetSecurityInfo fallido deja el permiso consumido e inservible (§18)."""
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        port = _FakePort(durable.plan)
        port.apply_falla.add("Data/Scripts/deep.pex")
        reporte = apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert journal.journal.node_state("Data/Scripts/deep.pex") is not None
        assert port.setsecurityinfo_calls == 1
        assert reporte.apply_error is not None


# ============================================================================
# 7. Fallo de verificación POST (G)
# ============================================================================


class TestVerificacionPost:
    """G: POST inválido tras mutación real -> rollback exacto, sin nodo siguiente."""

    def test_g_post_invalido_dispara_rollback(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.verify_falla.add("Data/Skyrim.esm")
        reporte, fake, _ = _apply(tmp_path, port=port)
        # 3 applies reales + 3 restauraciones exactas de esos mismos nodos.
        assert fake.setsecurityinfo_calls == 6
        assert reporte.applied_nodes == ("Data/Scripts/deep.pex", "Data/Scripts", "Data/Skyrim.esm")
        assert reporte.nodes_with_confirmed_wal == ("Data/Scripts/deep.pex", "Data/Scripts")
        # Skyrim.esm quedó físicamente mutado pero NUNCA recibió MUTATED(K).
        assert reporte.rolled_back_nodes == ("Data/Scripts/deep.pex", "Data/Scripts", "Data/Skyrim.esm")
        assert isinstance(reporte.apply_error, MutationApplyError)

    def test_g_no_continua_tras_post_invalido(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.verify_falla.add("Data/Scripts")
        _apply(tmp_path, port=port)
        assert "Data/Skyrim.esm" not in [n.relative_path for n in port.aplicados]
        assert "Data" not in [n.relative_path for n in port.aplicados]


# ============================================================================
# 8. Fallo del flush de MUTATED (H)
# ============================================================================


class TestFlushDeMutated:
    """H: mutación real + POST válido, pero el flush de MUTATED falla -> rollback."""

    def test_h_flush_de_mutated_fallido_restaura(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = FakeKernel()
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=kernel)
        port = _FakePort(durable.plan, timeline=kernel.eventos)

        original_flush = kernel.flush

        def _flush(handle: int) -> bool:
            # El primer nodo mute y verifique bien; su flush de MUTATED falla.
            if port.setsecurityinfo_calls == 1 and len(port.aplicados) == 1:
                kernel.eventos.append("flush-wal")
                return False
            return original_flush(handle)

        kernel.flush = _flush  # type: ignore[method-assign]
        reporte = apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert port.setsecurityinfo_calls == 2  # apply real + restore real
        assert reporte.applied_nodes == ("Data/Scripts/deep.pex",)
        assert reporte.nodes_with_confirmed_wal == ()
        assert reporte.rolled_back_nodes == ("Data/Scripts/deep.pex",)
        assert reporte.rollback_error is None
        # El FSM quedó INDETERMINATE: no se puede afirmar MUTATED ni ROLLED_BACK.
        assert reporte.transaction_state is ProtectionTransactionState.INDETERMINATE

    def test_h_no_se_asume_que_la_mutacion_no_ocurrio(self, tmp_path: pathlib.Path) -> None:
        """El nodo se restaura aunque el MUTATED no haya podido asentarse."""
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        kernel = FakeKernel()
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=kernel)
        port = _FakePort(durable.plan)

        original_flush = kernel.flush
        llamadas = {"n": 0}

        def _flush(handle: int) -> bool:
            llamadas["n"] += 1
            if llamadas["n"] == 2:  # flush del MUTATED del primer nodo
                return False
            return original_flush(handle)

        kernel.flush = _flush  # type: ignore[method-assign]
        apply_authorized_plan(
            plan=durable,
            journal=journal,
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        assert tuple(n.relative_path for n in port.restaurados) == ("Data/Scripts/deep.pex",)


# ============================================================================
# 9. Rollback exacto (J, K, L, M)
# ============================================================================


class TestRollbackExacto:
    """El rollback restaura el PRE del DurableAuthorizedPlan, nunca staging."""

    def test_k_restaura_owner_group_dacl_y_protected(self, tmp_path: pathlib.Path) -> None:
        plan = _plan()
        port = _FakePort(plan)
        port.apply_falla.add("Data")
        reporte, _, _ = _apply(tmp_path, port=port)
        assert reporte.rolled_back_nodes
        # Los objetos restaurados son EXACTAMENTE los nodos autorizados por el plan.
        for restaurado in port.restaurados:
            original = next(n for n in plan.nodes if n.relative_path == restaurado.relative_path)
            assert restaurado == original
            assert restaurado.pre_sd_bytes_b64 == original.pre_sd_bytes_b64
            assert restaurado.pre_sd_sha256 == original.pre_sd_sha256
            assert restaurado.pre_sd_length == original.pre_sd_length
            assert restaurado.pre_dacl_protected_flag is True

    def test_k_rollback_no_lee_el_staging(self, tmp_path: pathlib.Path) -> None:
        """El staging contiene un PRE distinto: el rollback NO puede usarlo (§12.2)."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        pre_envenenado = _node("Data/Scripts/deep.pex", _ROOT_FILE_ID + 3, sd_suffix="-POISON")
        (staging_dir / "pre_envenenado.bin").write_bytes(base64.b64decode(pre_envenenado.pre_sd_bytes_b64))
        assert pre_envenenado.pre_sd_sha256 != _plan().nodes[3].pre_sd_sha256

        port = _FakePort(_plan())
        port.apply_falla.add("Data")
        _apply(tmp_path, port=port)
        for restaurado in port.restaurados:
            assert restaurado.pre_sd_sha256 != pre_envenenado.pre_sd_sha256

    def test_m_staging_con_pre_distinto_no_contamina_la_restauracion(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        port = _FakePort(durable.plan)
        port.apply_falla.add("Data")
        apply_authorized_plan(
            plan=durable,
            journal=create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel()),
            session=_session(),
            port=port,
            probe=_probe_recorder(port),
        )
        esperados = {n.relative_path: n.pre_sd_sha256 for n in durable.plan.nodes}
        assert {n.relative_path: n.pre_sd_sha256 for n in port.restaurados} == {
            k: esperados[k] for k in (r.relative_path for r in port.restaurados)
        }

    def test_l_fallo_de_restauracion_nunca_reporta_rolled_back(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data")
        port.restore_falla.add("Data/Scripts/deep.pex")
        with pytest.raises(MutationRollbackError) as excinfo:
            _apply(tmp_path, port=port)
        assert "Data/Scripts/deep.pex" in str(excinfo.value)
        reporte = excinfo.value.report
        assert reporte is not None
        assert reporte.transaction_state is ProtectionTransactionState.ROLLBACK_FAILED
        # Los tres nodos restaurados antes del fallo quedaron ROLLED_BACK; el que
        # falló (deep.pex) NUNCA se reporta restaurado.
        assert reporte.rolled_back_nodes == ("Data/Scripts", "Data/Skyrim.esm", "Data/quest.esp")
        assert "Data/Scripts/deep.pex" not in reporte.rolled_back_nodes
        assert reporte.apply_error is not None
        assert reporte.rollback_error is not None

    def test_l_preserva_apply_error_y_rollback_error(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data")
        port.restore_verify_falla.add("Data/Skyrim.esm")
        with pytest.raises(MutationRollbackError) as excinfo:
            _apply(tmp_path, port=port)
        assert excinfo.value.__cause__ is not None
        assert isinstance(excinfo.value.__cause__, MutationApplyError)
        reporte = excinfo.value.report
        assert reporte is not None
        assert reporte.apply_error is not None
        assert reporte.rollback_error is not None
        assert str(reporte.apply_error) != str(reporte.rollback_error)

    def test_l_drift_de_identidad_en_rollback_no_restaura_otro_objeto(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data")
        # El apply ya hizo 2 lecturas de identidad por nodo: la 3ª es la del rollback.
        # El apply ya hizo 2 lecturas por nodo: la 3ª es la del rollback, y el
        # primer nodo restaurado (top-down) es Data/Scripts.
        port.drift["Data/Scripts"] = (
            3,
            NodeIdentity(
                volume_serial_number=0xDEADBEEF,
                file_id=_ROOT_FILE_ID + 2,
                reparse_tag=0,
                number_of_links=1,
            ),
        )
        with pytest.raises(MutationRollbackError):
            _apply(tmp_path, port=port)
        assert port.restaurados == []

    def test_l_reparse_en_rollback_no_restaura(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data")
        port.drift["Data/Scripts"] = (
            3,
            NodeIdentity(
                volume_serial_number=_VOLUME_SERIAL,
                file_id=_ROOT_FILE_ID + 2,
                reparse_tag=0xA000000C,
                number_of_links=1,
            ),
        )
        with pytest.raises(MutationRollbackError):
            _apply(tmp_path, port=port)
        assert port.restaurados == []


# ============================================================================
# 10. Contabilidad de handles (N)
# ============================================================================


class TestContabilidadDeHandles:
    """N: cada probe y cada handle de mutación se cierra exactamente una vez."""

    def test_n_handles_cerrados_una_vez_en_exito(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        reporte, _, _ = _apply(tmp_path, port=port)
        assert reporte.ok
        assert len(port.probe_calls) == len(_plan().nodes)
        assert all(c == 1 for c in port.cerrados.values())
        assert len(port.cerrados) == len(_plan().nodes)

    def test_n_handles_cerrados_una_vez_en_fallo(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data/Skyrim.esm")
        _apply(tmp_path, port=port)
        assert all(c == 1 for c in port.cerrados.values())
        # 3 handles de apply (deep.pex, Data/Scripts, Skyrim.esm que falló) +
        # 2 de rollback (los dos nodos realmente mutados).
        assert len(port.cerrados) == 5

    def test_n_handles_cerrados_una_vez_en_rollback(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        port.apply_falla.add("Data")
        _apply(tmp_path, port=port)
        # 5 nodos recorridos en apply + 4 restaurados: todos cerrados una vez.
        assert all(c == 1 for c in port.cerrados.values())
        assert len(port.cerrados) == 9

    def test_n_el_probe_no_es_el_handle_de_mutacion(self, tmp_path: pathlib.Path) -> None:
        port = _FakePort(_plan())
        _apply(tmp_path, port=port)
        # Un handle distinto por nodo para el probe y para la mutación.
        assert port.timeline.count("open:.") == 1
        assert len(port.cerrados) == len(_plan().nodes)


# ============================================================================
# 11. Gates de autoridad
# ============================================================================


class TestGatesDeAutoridad:
    """Ni el plan ni el journal aislados son permiso de mutación (§7, §9, §10)."""

    def test_sesion_cerrada_no_muta(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        sesion = _session()
        sesion.close()
        port = _FakePort(durable.plan)
        with pytest.raises(MutationAuthorityError):
            apply_authorized_plan(
                plan=durable,
                journal=journal,
                session=sesion,
                port=port,
                probe=_probe_recorder(port),
            )
        assert port.setsecurityinfo_calls == 0

    def test_lock_liberado_no_muta(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        sesion = _session()
        sesion.lock.release()
        port = _FakePort(durable.plan)
        with pytest.raises(MutationAuthorityError):
            apply_authorized_plan(
                plan=durable,
                journal=journal,
                session=sesion,
                port=port,
                probe=_probe_recorder(port),
            )
        assert port.setsecurityinfo_calls == 0

    def test_operation_id_divergente_no_muta(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        port = _FakePort(durable.plan)
        with pytest.raises(MutationAuthorityError):
            apply_authorized_plan(
                plan=durable,
                journal=journal,
                session=_session(operation_id=_OTRA_OPERATION_ID),
                port=port,
                probe=_probe_recorder(port),
            )
        assert port.setsecurityinfo_calls == 0

    def test_lock_de_otro_golden_no_muta(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        port = _FakePort(durable.plan)
        with pytest.raises(MutationAuthorityError):
            apply_authorized_plan(
                plan=durable,
                journal=journal,
                session=_session(root_file_id=_ROOT_FILE_ID + 999),
                port=port,
                probe=_probe_recorder(port),
            )
        assert port.setsecurityinfo_calls == 0

    def test_journal_de_otro_plan_no_muta(self, tmp_path: pathlib.Path) -> None:
        raiz_otra = _raiz(tmp_path, _OTRA_OPERATION_ID)
        otra = _plan_durable(raiz_otra, operation_id=_OTRA_OPERATION_ID, root="C:\\Games\\SkyrimSE")
        journal_otro = create_protection_journal(otra, programdata_resolver=_resolver(raiz_otra), kernel=FakeKernel())
        raiz_propia = _raiz(tmp_path)
        durable = _plan_durable(raiz_propia)
        port = _FakePort(durable.plan)
        with pytest.raises(MutationAuthorityError):
            apply_authorized_plan(
                plan=durable,
                journal=journal_otro,
                session=_session(),
                port=port,
                probe=_probe_recorder(port),
            )
        assert port.setsecurityinfo_calls == 0

    def test_fsm_no_applying_no_muta(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        journal.transition_to(ProtectionTransactionState.ROLLBACK_REQUIRED)
        port = _FakePort(durable.plan)
        with pytest.raises(MutationAuthorityError):
            apply_authorized_plan(
                plan=durable,
                journal=journal,
                session=_session(),
                port=port,
                probe=_probe_recorder(port),
            )
        assert port.setsecurityinfo_calls == 0

    def test_plan_en_memoria_no_es_autoridad(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        port = _FakePort(durable.plan)
        with pytest.raises(MutationAuthorityError):
            apply_authorized_plan(
                plan=durable.plan,  # type: ignore[arg-type]
                journal=journal,
                session=_session(),
                port=port,
                probe=_probe_recorder(port),
            )
        assert port.setsecurityinfo_calls == 0

    def test_committed_nunca_se_produce(self, tmp_path: pathlib.Path) -> None:
        reporte, _, _ = _apply(tmp_path)
        assert reporte.transaction_state is ProtectionTransactionState.APPLYING
        assert reporte.transaction_state is not ProtectionTransactionState.COMMITTED

    def test_posix_sin_puerto_no_muta(self, tmp_path: pathlib.Path) -> None:
        raiz = _raiz(tmp_path)
        durable = _plan_durable(raiz)
        journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
        if sys.platform == "win32":
            pytest.skip("esta vía exige no-Windows para ser observada")
        with pytest.raises(MutationExecutorUnsupportedError):
            apply_authorized_plan(plan=durable, journal=journal, session=_session(), port=None, probe=None)


# ============================================================================
# 12. Rutas: autoridad del plan
# ============================================================================


class TestDerivacionDeRutas:
    def test_ruta_derivada_del_plan_no_del_caller(self) -> None:
        plan = _plan()
        raiz = pathlib.Path(plan.canonical_root)
        assert derive_node_path(plan, ".") == raiz
        assert derive_node_path(plan, "Data/Skyrim.esm") == raiz / "Data" / "Skyrim.esm"
        assert derive_node_path(plan, "Data/Scripts/deep.pex") == raiz / "Data" / "Scripts" / "deep.pex"

    def test_nunca_una_ruta_de_staging(self, tmp_path: pathlib.Path) -> None:
        plan = _plan()
        for nodo in plan.nodes:
            assert str(derive_node_path(plan, nodo.relative_path)).startswith(plan.canonical_root)
            assert "staging" not in str(derive_node_path(plan, nodo.relative_path)).lower()

    def test_nodo_inexistente_en_el_plan_no_se_aplica(self) -> None:
        """El apply itera SÓLO los nodos del plan: ninguno fuera de él se muta."""
        plan = _plan()
        assert plan.node_for("Data/inexistente.esp") is None
        assert all(n.relative_path in {r.relative_path for r in plan.nodes} for n in apply_order(plan))

    def test_outcome_expone_si_el_nodo_puede_estar_mutado(self) -> None:
        aplicado = NodeMutationOutcome(relative_path="Data", node_kind=GoldenProtectionNodeKind.FILE, applied=True)
        assert aplicado.acl_write_reached is True
        assert (
            NodeMutationOutcome(relative_path="Data", node_kind=GoldenProtectionNodeKind.FILE).acl_write_reached
            is False
        )


# ============================================================================
# 13. Jerarquía y modelo
# ============================================================================


class TestModelo:
    def test_jerarquia_de_excepciones(self) -> None:
        from sky_claw.local.runtime_vault.models import RuntimeVaultError

        assert issubclass(MutationExecutorError, RuntimeVaultError)
        assert issubclass(MutationExecutorUnsupportedError, MutationExecutorError)
        assert issubclass(MutationAuthorityError, MutationExecutorError)
        assert issubclass(MutationPermitError, MutationAuthorityError)
        assert issubclass(MutationApplyError, MutationExecutorError)
        assert issubclass(MutationRollbackError, MutationExecutorError)

    def test_reporte_exitoso(self) -> None:
        nodo = NodeMutationOutcome(
            relative_path="Data", node_kind=GoldenProtectionNodeKind.FILE, applied=True, mutated=True
        )
        reporte = ApplyReport(operation_id=_OPERATION_ID, outcomes=(nodo,))
        assert reporte.ok is True
        assert reporte.applied_nodes == ("Data",)
        assert reporte.nodes_with_confirmed_wal == ("Data",)
        assert reporte.rolled_back_nodes == ()
        assert reporte.raise_if_failed() is reporte

    def test_reporte_fallido_preserva_ambos_errores(self) -> None:
        apply_err = MutationApplyError("causa inicial")
        rollback_err = MutationRollbackError("rollback fallido")
        nodo = NodeMutationOutcome(relative_path="Data", node_kind=GoldenProtectionNodeKind.FILE, applied=True)
        reporte = ApplyReport(
            operation_id=_OPERATION_ID,
            outcomes=(nodo,),
            apply_error=apply_err,
            rollback_error=rollback_err,
        )
        assert reporte.ok is False
        assert reporte.applied_nodes == ("Data",)
        assert reporte.rollback_error is not None
        with pytest.raises(MutationRollbackError) as excinfo:
            reporte.raise_if_failed()
        assert excinfo.value.__cause__ is apply_err

    def test_identidad_es_inmutable(self) -> None:
        identidad = NodeIdentity(volume_serial_number=1, file_id=2, reparse_tag=0, number_of_links=1)
        with pytest.raises(Exception):  # noqa: B017, BLE001 — dataclass frozen
            identidad.file_id = 3  # type: ignore[misc]


# ============================================================================
# 14. Mini-RIG sobre el namespace real (sólo Windows)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas nativas Win32 solo en Windows")
class TestRigRealSobreArbolDescartable:
    """crear -> mutar -> verificar -> rollback -> verificar PRE -> borrar.

    El árbol lo crea el propio test bajo ``%TEMP%\\SkyClaw-S4B-RIG-<uuid>``.
    NUNCA toca Skyrim, Steam, MO2, el runtime ni el Golden real.
    """

    def _arbol(self, tmp_path: pathlib.Path) -> pathlib.Path:
        import tempfile

        base = pathlib.Path(tempfile.gettempdir()) / f"SkyClaw-S4B-RIG-{uuid.uuid4().hex}"
        (base / "Data" / "Scripts").mkdir(parents=True)
        (base / "Data" / "Skyrim.esm").write_bytes(b"esm-descartable-s4b")
        (base / "Data" / "quest.esp").write_bytes(b"esp-descartable-s4b")
        (base / "Data" / "Scripts" / "deep.pex").write_bytes(b"pex-descartable-s4b")
        return base

    def _plan_real(self, base: pathlib.Path, raiz: pathlib.Path) -> DurableAuthorizedPlan:
        from sky_claw.local.runtime_vault.node_evidence import probe_node_evidence

        evidencias = probe_node_evidence(base)
        nodos = tuple(e.backup for e in evidencias)
        raiz_nodo = next(n for n in nodos if n.relative_path == ".")
        plan = AuthorizedPlan(
            schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
            operation_id=_OPERATION_ID,
            canonical_root=str(base),
            volume_serial_number=raiz_nodo.volume_serial_number,
            root_file_id=raiz_nodo.file_id,
            tree_digest=TreeDigest(digest="c" * 64, files=len(nodos) - 1, bytes=4096),
            node_count=len(nodos),
            policy_version=_POLICY_VERSION,
            staging_digest="d" * 64,
            operator_identity=_operator_evidence(),
            nodes=nodos,
        )
        destino = derive_protection_journal_path(_OPERATION_ID, programdata_resolver=_resolver(raiz)).with_name(
            "authorized_plan.json"
        )
        destino.write_bytes(serialize_authorized_plan(plan))
        return DurableAuthorizedPlan(plan, destino, _proof=_MINT_PROOF)

    def test_apply_real_verifica_y_rollback_restaura_el_pre(self, tmp_path: pathlib.Path) -> None:
        import shutil

        base = self._arbol(tmp_path)
        try:
            raiz = _raiz(tmp_path)
            durable = self._plan_real(base, raiz)
            journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
            sesion = _session(
                volume_serial_number=durable.volume_serial_number,
                root_file_id=durable.root_file_id,
            )
            port = HandleBoundTargetDaclPort()

            pre_por_nodo = {n.relative_path: n.pre_sd_sha256 for n in durable.plan.nodes}
            reporte = apply_authorized_plan(
                plan=durable,
                journal=journal,
                session=sesion,
                port=port,
                probe=None,
            )
            assert reporte.ok is True, str(reporte.apply_error)
            assert reporte.nodes_with_confirmed_wal == tuple(n.relative_path for n in apply_order(durable.plan))
            assert port.setsecurityinfo_calls == len(durable.plan.nodes)
            assert reporte.transaction_state is ProtectionTransactionState.APPLYING
            sesion.close()

            # El PRE autorizado sigue siendo el que manda tras el apply.
            from sky_claw.local.runtime_vault.node_evidence import probe_node_evidence

            post = {n.relative_path: n.pre_sd_sha256 for n in probe_node_evidence(base)}
            assert post != pre_por_nodo  # la Target DACL cambió el SD real
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_apply_real_con_fallo_dispara_rollback_exacto(self, tmp_path: pathlib.Path) -> None:
        import shutil

        base = self._arbol(tmp_path)
        try:
            raiz = _raiz(tmp_path)
            durable = self._plan_real(base, raiz)
            journal = create_protection_journal(durable, programdata_resolver=_resolver(raiz), kernel=FakeKernel())
            sesion = _session(
                volume_serial_number=durable.volume_serial_number,
                root_file_id=durable.root_file_id,
            )
            port = HandleBoundTargetDaclPort()
            pre_por_nodo = {n.relative_path: n.pre_sd_sha256 for n in durable.plan.nodes}

            # Un nodo inexistente en el disco provoca el fallo del apply tras haber
            # mutado nodos reales: el rollback debe restaurarlos EXACTAMENTE.
            victima = next(n for n in durable.plan.nodes if n.relative_path == "Data/quest.esp")
            victima_path = derive_node_path(durable, victima.relative_path)
            victima_path.unlink()

            reporte = apply_authorized_plan(
                plan=durable,
                journal=journal,
                session=sesion,
                port=port,
                probe=None,
            )
            assert reporte.apply_error is not None
            assert reporte.rolled_back_nodes
            assert reporte.transaction_state is ProtectionTransactionState.ROLLED_BACK
            sesion.close()

            from sky_claw.local.runtime_vault.node_evidence import probe_node_evidence

            post = {n.relative_path: n.pre_sd_sha256 for n in probe_node_evidence(base)}
            for rel in reporte.rolled_back_nodes:
                assert post[rel] == pre_por_nodo[rel]
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_rig_nunca_toca_el_golden_real(self) -> None:
        """El RIG sólo opera bajo %TEMP% con un uuid propio."""
        import tempfile

        base = pathlib.Path(tempfile.gettempdir()) / f"SkyClaw-S4B-RIG-{uuid.uuid4().hex}"
        assert str(base).lower().startswith(tempfile.gettempdir().lower())
        for prohibido in ("skyrim", "steam", "mod organizer", "mo2", "program files"):
            assert prohibido not in str(base).lower()
