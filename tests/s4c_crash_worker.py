"""Worker de crash / recovery de GP2-S4C — SÓLO PARA TESTS (proceso real).

Este módulo NO es código productivo y NO es invocable desde ninguna CLI del
producto: existe exclusivamente para que ``test_runtime_vault_s4c_windows_rig.py``
pueda matar/reiniciar PROCESOS REALES y probar la recuperación desde evidencia
durable (§24/§25/§37 del encargo). No acepta ``--root``/``--file``/``--pre-sd``:
la raíz del rig, la operación y los paths se derivan de la evidencia durable.

Barrera dura (§27): sólo opera bajo ``%TEMP%\\SkyClaw-S4C-RIG-<uuid>``; si la
raíz parece una instalación real (Skyrim/Steam/MO2/Program Files) ABORTA.

Fases (el controller orquesta; ``os._exit`` mata SÓLO a este proceso hijo):

    w01_crash_before_sdsi    journal real: MUTATING(K) durable -> muere antes de SetSecurityInfo
    w02_crash_after_sdsi     MUTATING(K) durable -> SetSecurityInfo REAL -> muere sin MUTATED
    w03_crash_all_mutated    apply completo (todos MUTATED) -> muere antes de post-verificación
    w06_hold_lock            lock real + MUTATING durable + duerme (el controller lo mata)
    recover                  recovery completo; escribe report.json y sale 0
    recover_crash_restored1  recovery que muere tras la PRIMERA restauración real (C8)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
import time
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # ejecución directa (no -m)
    sys.path.insert(0, str(_REPO_ROOT))

_CODIGO_CRASH_BEFORE_SDSI = 0x51
_CODIGO_CRASH_AFTER_SDSI = 0x52
_CODIGO_CRASH_ALL_MUTATED = 0x53
_CODIGO_CRASH_RESTORED_1 = 0x54


def _abortar_si_raiz_peligrosa(rig_root: pathlib.Path) -> None:
    texto = str(rig_root).lower()
    marcadores = ("skyrim", "steam", "mod organizer", "mo2", "program files", "programdata")
    if any(marcador in texto for marcador in marcadores):
        raise SystemExit(f"raíz de rig peligrosa: {rig_root}")
    temp = pathlib.Path(os.environ.get("TEMP", "")).resolve()
    try:
        rig_root.resolve().relative_to(temp)
    except ValueError as exc:
        raise SystemExit(f"la raíz del rig debe vivir bajo %TEMP%: {rig_root}") from exc
    if "skyclaw-s4c-rig-" not in texto:
        raise SystemExit(f"la raíz del rig debe llevar el prefijo SkyClaw-S4C-RIG-: {rig_root}")


class _Breadcrumbs:
    """Evidencia causal de hasta dónde llegó este proceso antes de morir."""

    def __init__(self, rig_root: pathlib.Path) -> None:
        self._path = rig_root / "breadcrumbs.log"

    def marcar(self, evento: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {evento}\n")


# ============================================================================
# Fixtures del rig (árbol desechable + plan + journal + lock reales)
# ============================================================================


def _programdata(rig_root: pathlib.Path) -> pathlib.Path:
    return rig_root / "programdata"


def _resolver(rig_root: pathlib.Path) -> Any:
    return lambda: _programdata(rig_root)


def _crear_arbol(rig_root: pathlib.Path) -> pathlib.Path:
    base = rig_root / "golden"
    (base / "Data").mkdir(parents=True)
    (base / "Data" / "Skyrim.esm").write_bytes(b"esm-descartable-s4c")
    return base


def _plan_durable(rig_root: pathlib.Path, operation_id: str) -> Any:
    from sky_claw.local.runtime_vault.authorized_plan import (
        AUTHORIZED_PLAN_SCHEMA_VERSION,
        AuthorizedPlan,
        serialize_authorized_plan,
    )
    from sky_claw.local.runtime_vault.authorized_plan_store import (
        derive_authorized_plan_path,
        load_durable_authorized_plan,
    )
    from sky_claw.local.runtime_vault.models import TreeDigest
    from sky_claw.local.runtime_vault.node_evidence import probe_node_evidence
    from sky_claw.local.runtime_vault.operator_token import OperatorTokenEvidence

    base = rig_root / "golden"
    evidencias = probe_node_evidence(base)
    nodos = tuple(e.backup for e in evidencias)
    raiz_nodo = next(n for n in nodos if n.relative_path == ".")
    plan = AuthorizedPlan(
        schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
        operation_id=operation_id,
        canonical_root=str(base),
        volume_serial_number=raiz_nodo.volume_serial_number,
        root_file_id=raiz_nodo.file_id,
        tree_digest=TreeDigest(digest="c" * 64, files=len(nodos) - 1, bytes=4096),
        node_count=len(nodos),
        policy_version="golden-policy-v1",
        staging_digest="d" * 64,
        operator_identity=OperatorTokenEvidence(
            operator_sid="S-1-5-21-1001-1002-1003-1001",
            token_type="primary",
            acquired_via="same_account_coordinator_extraction",
        ),
        nodes=nodos,
    )
    destino = pathlib.Path(str(derive_authorized_plan_path(operation_id, programdata_resolver=_resolver(rig_root))))
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(serialize_authorized_plan(plan))
    return load_durable_authorized_plan(operation_id, programdata_resolver=_resolver(rig_root))


def _crear_journal_fixture(rig_root: pathlib.Path, durable: Any) -> None:
    """Crea el journal (header + estado inicial) con BYTES canónicos reales.

    El kernel de durabilidad que el resto del flujo usa es el REAL
    (``_Win32JournalKernel``: write-through + ``FlushFileBuffers`` verificado).
    La creación se hace acá con ACL de usuario para que el rig descartable
    funcione sin elevación; la DACL canónica del namespace productivo se cubre
    con el probe de elevación de los escenarios de restauración.
    """
    from sky_claw.local.runtime_vault.protection_journal import (
        INITIAL_TRANSACTION_STATE,
        JournalTransactionRecord,
        ProtectionTransactionState,
        build_journal_header_record,
        serialize_journal_record,
    )
    from sky_claw.local.runtime_vault.protection_journal_store import derive_protection_journal_path

    plan = durable.plan
    path = derive_protection_journal_path(plan.operation_id, programdata_resolver=_resolver(rig_root))
    path.parent.mkdir(parents=True, exist_ok=True)
    header = build_journal_header_record(
        operation_id=plan.operation_id,
        authorized_plan_digest=plan.plan_digest,
        canonical_root=plan.canonical_root,
        volume_serial_number=plan.volume_serial_number,
        root_file_id=plan.root_file_id,
        created_at="2026-09-29T00:00:00Z",
    )
    inicial = JournalTransactionRecord(sequence=2, state=ProtectionTransactionState(INITIAL_TRANSACTION_STATE))
    path.write_bytes(serialize_journal_record(header) + serialize_journal_record(inicial))


def _abrir_journal(rig_root: pathlib.Path, durable: Any) -> Any:
    from sky_claw.local.runtime_vault.protection_journal_store import open_protection_journal

    return open_protection_journal(durable.operation_id, durable, programdata_resolver=_resolver(rig_root))


def _adquirir_lock(rig_root: pathlib.Path, durable: Any) -> Any:
    from sky_claw.local.runtime_vault.golden_mutation_lock import (
        acquire_golden_mutation_lock,
        derive_golden_lock_path,
    )

    lock_path = derive_golden_lock_path(
        durable.volume_serial_number,
        durable.root_file_id,
        programdata_resolver=_resolver(rig_root),
    )
    pathlib.Path(str(lock_path)).parent.mkdir(parents=True, exist_ok=True)
    return acquire_golden_mutation_lock(
        durable.volume_serial_number,
        durable.root_file_id,
        durable.operation_id,
        programdata_resolver=_resolver(rig_root),
    )


class _TokenAdapterFalso:
    """Adapter de token primario (SÓLO test): determinista, sin tocar el SO."""

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


def _sesion(lock: Any) -> Any:
    from sky_claw.local.runtime_vault.authorization_context import (
        CoordinatorProcessIdentity,
        PrivilegedBoundarySession,
        acquire_operator_primary_token_from_coordinator,
    )

    identidad = CoordinatorProcessIdentity(
        pid=4242,
        creation_time=133_456_789_012_345_678,
        image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
    )
    token = acquire_operator_primary_token_from_coordinator(identidad, adapter=_TokenAdapterFalso())
    return PrivilegedBoundarySession(operator_token=token, lock=lock)


class _PuertoConCrash:
    """Envuelve al puerto productivo y mata el proceso en el punto pedido."""

    def __init__(self, interno: Any, modo: str, breadcrumbs: _Breadcrumbs) -> None:
        self._interno = interno
        self._modo = modo
        self._breadcrumbs = breadcrumbs

    def open(self, path: pathlib.Path, node_kind: Any) -> int:
        return self._interno.open(path, node_kind)

    def close(self, handle: int) -> None:
        self._interno.close(handle)

    def read_identity(self, handle: int) -> Any:
        return self._interno.read_identity(handle)

    def read_live_pre_sd_sha256(self, handle: int) -> str:
        return self._interno.read_live_pre_sd_sha256(handle)

    def apply_target_dacl(self, handle: int, node: Any) -> None:
        # El engine ya registró MUTATING durable al entrar acá (el permit precede
        # a SetSecurityInfo): por eso el punto "pre-sdsi" es el borde exacto.
        if self._modo == "before_sdsi":
            self._breadcrumbs.marcar(f"pre-sdsi:{node.relative_path}")
            os._exit(_CODIGO_CRASH_BEFORE_SDSI)
        self._breadcrumbs.marcar(f"sdsi:{node.relative_path}")
        self._interno.apply_target_dacl(handle, node)
        if self._modo == "after_sdsi":
            self._breadcrumbs.marcar(f"post-sdsi:{node.relative_path}")
            os._exit(_CODIGO_CRASH_AFTER_SDSI)

    def verify_target_dacl(self, handle: int, node: Any) -> Any:
        return self._interno.verify_target_dacl(handle, node)

    def restore_pre_sd(self, handle: int, node: Any) -> None:
        self._interno.restore_pre_sd(handle, node)
        self._breadcrumbs.marcar(f"restore:{node.relative_path}")
        if self._modo == "restored_1":
            os._exit(_CODIGO_CRASH_RESTORED_1)

    def verify_restored_pre_sd(self, handle: int, node: Any) -> None:
        self._interno.verify_restored_pre_sd(handle, node)

    @property
    def setsecurityinfo_calls(self) -> int:
        return self._interno.setsecurityinfo_calls


def _marcar_mutating(journal: Any, relative_path: str, breadcrumbs: _Breadcrumbs) -> None:
    journal.record_node_mutation_intent(journal.node_binding(relative_path))
    breadcrumbs.marcar(f"mutating:{relative_path}")


# ============================================================================
# Fases
# ============================================================================


def _fase_apply_crash(rig_root: pathlib.Path, operation_id: str, modo: str) -> None:
    from sky_claw.local.runtime_vault.mutation_executor import (
        HandleBoundTargetDaclPort,
        apply_authorized_plan,
    )

    breadcrumbs = _Breadcrumbs(rig_root)
    _crear_arbol(rig_root)
    durable = _plan_durable(rig_root, operation_id)
    _crear_journal_fixture(rig_root, durable)
    lock = _adquirir_lock(rig_root, durable)
    breadcrumbs.marcar("lock-acquired")
    journal = _abrir_journal(rig_root, durable)
    breadcrumbs.marcar("journal-open")
    sesion = _sesion(lock)
    puerto = _PuertoConCrash(HandleBoundTargetDaclPort(), modo, breadcrumbs)

    if modo == "all_mutated":
        # Apply completo; el "crash" llega recién tras todos los MUTATED.
        reporte = apply_authorized_plan(plan=durable, journal=journal, session=sesion, port=puerto)
        breadcrumbs.marcar(f"apply-complete:{reporte.transaction_state.value}")
        os._exit(_CODIGO_CRASH_ALL_MUTATED)

    # before_sdsi / after_sdsi: el puerto mata el proceso en el nodo 1 (el más
    # profundo, primer SetSecurityInfo del apply bottom-up).
    from sky_claw.local.runtime_vault.mutation_executor import apply_order

    primer_nodo = apply_order(durable)[0].relative_path
    breadcrumbs.marcar(f"primer-nodo:{primer_nodo}")
    reporte = apply_authorized_plan(plan=durable, journal=journal, session=sesion, port=puerto)
    raise SystemExit(
        "el puerto con crash no mató el proceso: fase inválida; "
        f"apply_error={reporte.apply_error!r} state={reporte.transaction_state.value}"
    )


def _fase_hold_lock(rig_root: pathlib.Path, operation_id: str) -> None:
    breadcrumbs = _Breadcrumbs(rig_root)
    _crear_arbol(rig_root)
    durable = _plan_durable(rig_root, operation_id)
    _crear_journal_fixture(rig_root, durable)
    lock = _adquirir_lock(rig_root, durable)
    breadcrumbs.marcar("lock-acquired")
    journal = _abrir_journal(rig_root, durable)
    from sky_claw.local.runtime_vault.mutation_executor import apply_order

    primer_nodo = apply_order(durable)[0].relative_path
    _marcar_mutating(journal, primer_nodo, breadcrumbs)
    assert lock.identity.operation_id == durable.operation_id, "el dueño vivo debe mantener el lock"
    breadcrumbs.marcar("hold")
    # El dueño VIVE con el lock tomado: el controller intenta recovery (BUSY),
    # luego lo mata con TerminateProcess.
    time.sleep(120)


def _reporte_a_dict(reporte: Any) -> dict[str, Any]:
    def _convertir(valor: Any) -> Any:
        if dataclasses.is_dataclass(valor) and not isinstance(valor, type):
            return {campo.name: _convertir(getattr(valor, campo.name)) for campo in dataclasses.fields(valor)}
        if isinstance(valor, (list, tuple)):
            return [_convertir(v) for v in valor]
        if hasattr(valor, "value") and hasattr(valor, "name"):  # StrEnum
            return valor.value
        return valor

    return _convertir(reporte)


def _fase_recover(rig_root: pathlib.Path, operation_id: str, *, crash_restored_1: bool) -> None:
    from sky_claw.local.runtime_vault.mutation_executor import HandleBoundTargetDaclPort
    from sky_claw.local.runtime_vault.recovery_orchestrator import recover_interrupted_protection

    breadcrumbs = _Breadcrumbs(rig_root)
    breadcrumbs.marcar("recover-start")
    puerto: Any = HandleBoundTargetDaclPort()
    if crash_restored_1:
        puerto = _PuertoConCrash(puerto, "restored_1", breadcrumbs)
    reporte = recover_interrupted_protection(
        operation_id=operation_id,
        programdata_resolver=_resolver(rig_root),
        port=puerto,
    )
    breadcrumbs.marcar(f"recover-done:{reporte.disposition.value}")
    (rig_root / "report.json").write_text(
        json.dumps(_reporte_a_dict(reporte), indent=2, sort_keys=True), encoding="utf-8"
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Worker de crash S4-C (SÓLO tests)")
    parser.add_argument("--rig-root", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument(
        "--phase",
        required=True,
        choices=(
            "w01_crash_before_sdsi",
            "w02_crash_after_sdsi",
            "w03_crash_all_mutated",
            "w06_hold_lock",
            "recover",
            "recover_crash_restored1",
        ),
    )
    args = parser.parse_args()
    rig_root = pathlib.Path(args.rig_root).resolve()
    _abortar_si_raiz_peligrosa(rig_root)

    if args.phase == "w01_crash_before_sdsi":
        _fase_apply_crash(rig_root, args.operation_id, "before_sdsi")
    elif args.phase == "w02_crash_after_sdsi":
        _fase_apply_crash(rig_root, args.operation_id, "after_sdsi")
    elif args.phase == "w03_crash_all_mutated":
        _fase_apply_crash(rig_root, args.operation_id, "all_mutated")
    elif args.phase == "w06_hold_lock":
        _fase_hold_lock(rig_root, args.operation_id)
    elif args.phase == "recover":
        _fase_recover(rig_root, args.operation_id, crash_restored_1=False)
    elif args.phase == "recover_crash_restored1":
        _fase_recover(rig_root, args.operation_id, crash_restored_1=True)


if __name__ == "__main__":
    _main()
