"""Regresiones de los hallazgos de la revision automatica de PR #666 (Codex).

Cada test aca ancla un hallazgo que la revision adversarial encontro y que no
tenia cobertura propia: el binding de la tabla de nodos, la autoridad del path
del backup, los reintentos acotados de la seccion 19.2, la traduccion de errores
de nodo y la estrictez de los veredictos.

Se escriben como tests de COMPORTAMIENTO y no como comentarios sobre el fix: un
fix sin test que lo ate es un fix que la proxima refactorizacion se lleva puesta.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorized_plan_store import DurableWriteOutcome
from sky_claw.local.runtime_vault.finalization_orchestrator import FinalizationDisposition
from sky_claw.local.runtime_vault.golden_backup_archive import (
    GoldenBackupWriteError,
    build_golden_backup_archive,
    classify_durable_golden_backup,
)
from sky_claw.local.runtime_vault.protection_journal import ProtectionTransactionState
from tests.test_runtime_vault_s4d_finalization import (
    _OTRA_OPERACION,
    _finalize,
    _Harness,
    _resolver,
    _WriterBackup,
)

# ============================================================================
# Codex: la tabla de nodos del backup tiene que ser LA del plan
# ============================================================================


def test_un_manifiesto_con_la_tabla_de_nodos_sustituida_no_ata(tmp_path: pathlib.Path) -> None:
    """El digest del plan es un CAMPO del archivo: probarlo no prueba la tabla.

    Se toma el manifiesto legítimo, se le sustituye la tabla de nodos por otra
    internamente válida de la MISMA longitud (mismos paths y FileIds, PRE SD
    distinto) y se rebobina su ``authorized_plan_digest`` al del plan. Comparar
    sólo ``node_count`` —como antes— hacía pasar este manifiesto como durable, y
    un GP3 futuro habría restaurado los Security Descriptors que el atacante
    eligió.
    """
    import base64
    import hashlib

    from sky_claw.local.runtime_vault.golden_backup_archive import (
        GoldenBackupArchive,
        load_durable_golden_backup,
    )
    from sky_claw.local.runtime_vault.golden_protection_plan import NodeSecurityBackup

    h = _Harness(tmp_path)
    legitimo = build_golden_backup_archive(h.plan.plan)

    sd_ajeno = b"\x01\x00\x04\x80\xff\xff\xff\xff\x00" * 3
    nodos_sustituidos = tuple(
        NodeSecurityBackup(
            relative_path=nodo.relative_path,
            node_kind=nodo.node_kind,
            volume_serial_number=nodo.volume_serial_number,
            file_id=nodo.file_id,
            pre_sd_bytes_b64=base64.b64encode(sd_ajeno).decode("ascii"),
            pre_sd_length=len(sd_ajeno),
            pre_sd_sha256=hashlib.sha256(sd_ajeno).hexdigest(),
            owner_sid="S-1-1-0",
            group_sid="S-1-1-0",
            dacl_control_flags=0x0004,
            pre_dacl_protected_flag=False,
            sddl_diagnostic="D:(A;;GA;;;WD)",
        )
        for nodo in legitimo.nodes
    )
    sustituto = GoldenBackupArchive(
        operation_id=legitimo.operation_id,
        canonical_root=legitimo.canonical_root,
        volume_serial_number=legitimo.volume_serial_number,
        root_file_id=legitimo.root_file_id,
        tree_digest=legitimo.tree_digest,
        policy_version=legitimo.policy_version,
        authorized_plan_digest=h.plan.plan.plan_digest,
        nodes=nodos_sustituidos,
    )

    assert sustituto.node_count == legitimo.node_count, "el escenario exige la MISMA longitud"
    assert not sustituto.binds_to(h.plan.plan), (
        "un manifiesto con la tabla de nodos sustituida NO puede atar con el plan"
    )

    destino = h.backup_path()
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(sustituto.canonical_bytes())

    with pytest.raises(Exception) as excinfo:
        load_durable_golden_backup(h.plan.plan, programdata_resolver=_resolver(h.raiz))
    assert "no ata" in str(excinfo.value)


def test_el_binding_del_backup_es_insensible_al_orden_de_los_nodos(tmp_path: pathlib.Path) -> None:
    """Comparar la tabla no puede volverse un falso negativo por el ORDEN.

    El manifiesto guarda los nodos ordenados y el plan conserva el orden del
    planner. Si cada lado ordenara por su cuenta, un cambio de criterio en uno
    solo haría fallar un binding legítimo — o lo que es peor, pasaría inadvertido.
    """
    h = _Harness(tmp_path)
    legitimo = build_golden_backup_archive(h.plan.plan)

    assert legitimo.binds_to(h.plan.plan)
    assert legitimo.nodes == tuple(sorted(legitimo.nodes, key=lambda n: n.relative_path))


def test_el_deserializado_del_backup_nunca_acepta_un_path_arbitrario(tmp_path: pathlib.Path) -> None:
    """``load_durable_golden_backup`` acuña autoridad SÓLO desde la ruta derivada.

    Con ``path`` libre, un caller podía poner bytes canónicos en un archivo
    escribible por el usuario, cargarlos por ahí y pasarle la autoridad a
    ``commit_finalized``: ``COMMITTED`` con el backup real ausente del store
    protegido.
    """
    from sky_claw.local.runtime_vault.golden_backup_archive import load_durable_golden_backup

    h = _Harness(tmp_path)
    h._publicar_backup()

    ajeno = tmp_path / "archivo_del_atacante.json"
    ajeno.write_bytes(h.backup_path().read_bytes())

    with pytest.raises(Exception, match="ruta derivada"):
        load_durable_golden_backup(h.plan.plan, programdata_resolver=_resolver(h.raiz), path=ajeno)

    # Y la ruta derivada SÍ funciona: el guard no rompe el uso legítimo.
    durables = load_durable_golden_backup(h.plan.plan, programdata_resolver=_resolver(h.raiz), path=h.backup_path())
    assert durables.archive_digest == h._cargar_backup().archive_digest


# ============================================================================
# Codex: reintentos acotados de ARCHIVING_BACKUP (ADR §19.2)
# ============================================================================


def test_el_archivado_reintenta_antes_de_deshacer(tmp_path: pathlib.Path) -> None:
    """Un fallo transitorio se reintenta MAX_ARCHIVE_RETRIES veces, no una.

    Con un solo intento, un sharing violation pasajero de un antivirus deshacía
    una transacción que el ADR manda reintentar.
    """
    from sky_claw.local.runtime_vault.finalization_orchestrator import MAX_ARCHIVE_RETRIES

    h = _Harness(tmp_path)
    intentos: list[int] = []

    class _WriterQueFallaLasPrimeras:
        """Falla ``fallos`` veces y después publica de verdad."""

        def __init__(self, fallos: int) -> None:
            self.fallos = fallos
            self.real = _WriterBackup()

        def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
            intentos.append(len(intentos) + 1)
            if len(intentos) <= self.fallos:
                raise GoldenBackupWriteError(f"fallo transitorio #{len(intentos)}")
            self.real.write_create_once(dest, payload, object_name)

    fallos = MAX_ARCHIVE_RETRIES - 1
    reporte = _finalize(h, archive_writer=_WriterQueFallaLasPrimeras(fallos))

    assert len(intentos) == fallos + 1, f"se esperaban {fallos + 1} intentos; hubo {len(intentos)}"
    assert reporte.disposition is FinalizationDisposition.COMMITTED
    assert h.journal_estado() is ProtectionTransactionState.COMMITTED


def test_agotar_los_reintentos_del_archivado_va_a_rollback_no_a_indeterminate(tmp_path: pathlib.Path) -> None:
    """§19.2: agotados los reintentos -> ROLLBACK_REQUIRED, no estado huérfano.

    Un INDETERMINATE acá dejaría la transacción esperando un operador cuando la
    política del ADR es deshacer. Y el lock queda RETENIDO: el Golden todavía
    tiene que ser restaurado.
    """
    from sky_claw.local.runtime_vault.finalization_orchestrator import MAX_ARCHIVE_RETRIES

    h = _Harness(tmp_path)
    intentos: list[int] = []

    class _WriterSiempreFalla:
        def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
            intentos.append(len(intentos) + 1)
            raise GoldenBackupWriteError("disco lleno")

    reporte = _finalize(h, archive_writer=_WriterSiempreFalla())

    assert len(intentos) == MAX_ARCHIVE_RETRIES
    assert reporte.disposition is FinalizationDisposition.ROLLBACK_REQUIRED
    assert "reintentos" in reporte.fail_closed_reason
    assert h.journal_estado() is ProtectionTransactionState.ROLLBACK_REQUIRED
    assert reporte.lock.released is False
    assert reporte.lock.retained_as_orphan is True
    assert not h.backup_path().exists()


def test_la_evidencia_ajena_no_se_reintenta(tmp_path: pathlib.Path) -> None:
    """Un manifiesto AJENO en el destino no es transitorio: no se reintenta.

    Reintentar no cambiaría el archivo que está ahí. El desenlace es
    INDETERMINATE con el operador, y la evidencia ajena queda intacta.
    """
    h = _Harness(tmp_path)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_GP1)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_RV2)
    h.journal.enter_finalization_phase(ProtectionTransactionState.VERIFYING_NODE_SET)
    h.journal.enter_finalization_phase(ProtectionTransactionState.ARCHIVING_BACKUP)

    destino = h.backup_path()
    destino.parent.mkdir(parents=True, exist_ok=True)
    ajeno = b'{"schema_version":"gp2-golden-backup-v1","operation_id":"' + _OTRA_OPERACION.encode() + b'"}'
    destino.write_bytes(ajeno)

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert "reintentos" not in reporte.fail_closed_reason
    assert destino.read_bytes() == ajeno


# ============================================================================
# Codex: un manifiesto con un nodo inválido es corrupción, no una excepción
# ============================================================================


def test_un_nodo_invalido_en_el_manifiesto_es_indeterminate(tmp_path: pathlib.Path) -> None:
    """``SecurityBackupIntegrityError`` no hereda de ``GoldenBackupError``.

    ``node_security_backup_from_dict`` valida el modelo de nodo y lanza una
    excepción del módulo de PLAN. Sin traducirla, un manifiesto con JSON válido
    pero un SID o un digest de PRE inválido escapaba de las dos rutas de
    corrupción → INDETERMINATE y el llamador recibía una excepción en vez de un
    reporte fail-closed.
    """
    import json

    h = _Harness(tmp_path)
    h._publicar_backup()

    cuerpo = json.loads(h.backup_path().read_text(encoding="utf-8"))
    cuerpo["nodes"][0]["pre_sd_sha256"] = "f" * 64  # no coincide con los bytes del SD
    h.backup_path().write_text(json.dumps(cuerpo, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    assert classify_durable_golden_backup(h.plan.plan, programdata_resolver=_resolver(h.raiz)) is (
        DurableWriteOutcome.INDETERMINATE
    )


# ============================================================================
# Codex: un veredicto por tupla exige un bool DE VERDAD
# ============================================================================


def test_un_veredicto_por_tupla_con_string_no_pasa_como_verdadero(tmp_path: pathlib.Path) -> None:
    """``bool("false")`` es ``True``: coercionar convertiría un FAIL en PASS.

    Un adapter que devolviera el estado serializado de una observación —lo que
    hace cualquiera que cruce una frontera de proceso— haría pasar un gate que
    falló, y de ahí en adelante se autorizarían los gates siguientes y el
    COMMITTED.
    """
    h = _Harness(tmp_path)

    class _PuertoSerializado:
        """Puerto que devuelve strings, como un adapter sobre IPC."""

        def observar_gp1(self, **_kwargs: Any) -> Any:
            return ("false", "gp1 serializado")

        def observar_rv2(self, **_kwargs: Any) -> Any:
            return ("false", "rv2 serializado")

        def observar_node_set(self, **_kwargs: Any) -> Any:
            return ("false", "node_set serializado")

        def observar_quiescence(self, **_kwargs: Any) -> Any:
            return ("false", "quiescence serializado")

    h.puerto = _PuertoSerializado()

    reporte = _finalize(h)

    # El contrato se rompe DENTRO del orquestador, que lo clasifica fail-closed
    # en vez de propagarlo: el llamador recibe un reporte, no una excepción, y
    # —lo que importa— NO recibe un COMMITTED.
    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert reporte.committed is False
    assert "bool" in reporte.fail_closed_reason, (
        f"el motivo tiene que nombrar el contrato roto: {reporte.fail_closed_reason}"
    )
    assert h.journal_estado() is not ProtectionTransactionState.COMMITTED
    assert not h.backup_path().exists()


# ============================================================================
# Codex: los fallos genéricos de lock no escapan
# ============================================================================


def test_un_error_generico_de_lock_no_escapa_al_caller(tmp_path: pathlib.Path) -> None:
    """Registro de lock truncado / reparse / I/O: INDETERMINATE, no excepción.

    Ocurre ANTES de que exista handle, así que el reporte dice
    ``acquired=False``: esta transacción no tomó el lock ni lo retuvo.
    """
    from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockIoError

    h = _Harness(tmp_path)
    estado_previo = h.journal_bytes()

    h.lock_kernel.on_open = lambda: (_ for _ in ()).throw(GoldenLockIoError("registro de lock truncado"))

    reporte = _finalize(h)

    assert reporte.disposition is FinalizationDisposition.INDETERMINATE
    assert reporte.lock.acquired is False
    assert reporte.lock.retained_as_orphan is False, "un lock que nunca se adquirió no puede reportarse como retenido"
    assert h.puerto.llamadas == [], "sin lock no se ejecuta ningún gate"
    assert h.journal_bytes() == estado_previo
