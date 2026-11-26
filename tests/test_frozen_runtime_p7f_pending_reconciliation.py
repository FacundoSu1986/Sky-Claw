"""P7-F Incremento 2 (PR 1) — salida productiva de PENDING por aborto autorizado y guard de consumo.

ADR 0012 §48 · Directivas D1–D8 · Requisitos RQ-1..RQ-8 (PR 1: aborto autorizado + RQ-7 + rechazo C6/C7).

Cubre:
- Anclas AST enumerantes (escritores de ``JournalState.NONE``, consumidores de abandono↔aborto).
- D1 / RQ-7: guard bajo ``participant_lock`` en ``consumir_aprobacion`` (PENDING ajeno, mismo
  ``transition_id`` sin vínculo completo/tombstone, journal corrupto, aborto incompleto,
  idempotencia legítima y carrera concurrente real con ``threading.Barrier``).
- RQ-1..RQ-4: autorización verificada del propietario, confirmaciones etiquetadas
  ``PRESENTE``/``AUSENTE``, separación estricta autorización ≠ autoría, quiescencia MO2/Skyrim/SKSE,
  journal/registro corrupto, doble invocación idempotente.
- D2: revalidación obligatoria en cada reintento desde ``SUPERFICIES_VERIFICADAS``.
- D3: fronteras inter-marcador (placeholder vacío 0 bytes, corrupto no vacío, crash tras restaurar
  una superficie, crash tras rotar el journal a ``NONE``, crash tras retirar ``state/recovery/``).
- D4 / D5: bloqueo durante aborto incompleto, autoridad durable de ``_abortar_pendiente_locked`` y
  veto permanente del ``transition_id`` abortado.
- D6 / RQ-5: IP-0 sin registro de recuperación (evidencia causal positiva en bootstrap vs HOLD).
- RQ-6: bootstrap sin historial (``CREATE``) y con historial (``START_NEXT``) + transición posterior
  completa con POST PASS y ``FINALIZE`` legítimo.
- RQ-8: protección estricta de fronteras C6 (reserva presente) y C7 (``FINALIZED`` presente).
- D8: compatibilidad de roots históricos y rechazo fail-closed de store incoherente/redirigido.
- Integración Telegram HITL ``/gm_abortar_pendiente``.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import threading
import time
import uuid

import pytest

from sky_claw.app.comms.golden_master_hitl import (
    CoordinadorTelegramGoldenMaster,
    ObjetivoTransicionGoldenMaster,
)
from sky_claw.local.frozen_runtime import approval as approval_module
from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import mo2_ini_exclusion, storage
from sky_claw.local.frozen_runtime import runtime_clone_binder as binder
from sky_claw.local.frozen_runtime import state as state_module
from sky_claw.local.frozen_runtime.approval import (
    ApprovalLifecycle,
    cargar_ledger_aprobacion,
    consumir_aprobacion,
    derivar_autoridad_aprobacion,
)
from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import (
    EstadoDurable,
    SesionTransicionGoldenMaster,
    diagnosticar_estado_durable,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    LiveProcessEvidence,
    LiveProcessKind,
    LiveProcessSnapshot,
    Mo2InstanceIdentityObservation,
    Mo2InstanceIdentityState,
    Mo2RegistryState,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.errors import (
    AbortoCorruptoError,
    AbortoIp0EvidenciaInsuficienteError,
    AbortoPendienteInvalidoError,
    AbortoPendienteReservadoError,
    AbortoSuperficieDivergenteError,
    ApprovalJournalBlockedError,
    JournalCasError,
    JournalIdentityError,
)
from sky_claw.local.frozen_runtime.journal import (
    JournalDisposition,
    _abortar_pendiente_locked,
    _reservar_seq_locked,
    cargar_journal_transicion,
    derivar_historial_finalizados,
)
from sky_claw.local.frozen_runtime.owner_consent import (
    DecisionConsentimiento,
    IdentidadPropietario,
    RegistroConsentimientos,
    verificar_identidad_telegram,
)
from sky_claw.local.frozen_runtime.pending_abort_store import (
    ConfirmacionSuperficie,
    EstadoAbortoPendiente,
    EstadoSuperficieConfirmada,
    ModoAbortoPendiente,
    RegistroAbortoPendiente,
    registro_de_aborto,
)
from sky_claw.local.frozen_runtime.pending_reconciliation import (
    ContextoVerificacionIp0,
    abortar_transicion_pendiente,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from tests.test_frozen_runtime_p5c1_binder_integrity import (
    _montar_rig,
    _probes_quiescentes,
    _Rig,
)
from tests.test_frozen_runtime_p7_a5_pr3_hitl_wiring import _ExclusionPosix

_PAQUETE_FR = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime"
_OWNER_CHAT_ID = 777111222
_AJENO_CHAT_ID = 999888777


@pytest.fixture(autouse=True)
def _mock_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version",
        lambda exe: "1.6.1170.0",
    )
    monkeypatch.setattr(approval_module, "EMISION_PRODUCTIVA_HABILITADA", True)
    if os.name != "nt":
        monkeypatch.setattr(
            binder,
            "adquirir_exclusion_ini",
            lambda ruta, **_kwargs: _ExclusionPosix(pathlib.Path(ruta)),
        )
        monkeypatch.setattr(
            mo2_ini_exclusion,
            "adquirir_exclusion_ini",
            lambda ruta, **_kwargs: _ExclusionPosix(pathlib.Path(ruta)),
        )


def _sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def _identidad_dueno(chat_id: int = _OWNER_CHAT_ID) -> IdentidadPropietario:
    return verificar_identidad_telegram(
        {"message": {"from": {"id": chat_id}, "chat": {"id": chat_id, "type": "private"}, "text": "/gm"}},
        operador_configurado=chat_id,
    )


def _limpiar_rig_hitl(tmp_path: pathlib.Path) -> tuple[_Rig, dict[str, str]]:
    """Monta un rig sintético y alinea el setup_id real + limpia la aprobación fixture de _montar_rig."""
    from sky_claw.local.frozen_runtime.provisioning import verificar_runtime_setup

    rig = _montar_rig(tmp_path)
    setup_real = verificar_runtime_setup(rig.clone_root).setup_id
    rec_path = storage.clone_record_path(rig.storage, rig.clone_id)
    rec_data = json.loads(rec_path.read_text(encoding="utf-8"))
    rec_data["intended_runtime_setup_id"] = setup_real
    rec_data["verified_runtime_setup_id"] = setup_real
    state_module.write_json_atomic(rec_path, rec_data)

    ledger = storage.approval_ledger_path(rig.storage)
    if ledger.exists():
        ledger.unlink()
    for d in (storage.issuances_state_dir(rig.storage), storage.approvals_state_dir(rig.storage)):
        if d.is_dir():
            for f in d.iterdir():
                f.unlink()
    environ = {"SKYRIM_PATH": str(rig.managed_src)}
    return rig, environ


def _probes_rig(rig: _Rig, *, procesos: LiveProcessSnapshot | None = None) -> RuntimeProbes:
    base = _probes_quiescentes()
    return dataclasses.replace(
        base,
        processes=(lambda: procesos) if procesos is not None else base.processes,
        mo2_instance=lambda _d: Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.ACTIVE_GLOBAL,
            instance_dir=rig.mo2_dir,
            registry_state=Mo2RegistryState.OBSERVED,
            registry_current_instance=rig.mo2_dir.name,
            detail="instancia global activa sintética de test",
        ),
    )


def _crear_sesion(
    rig: _Rig, environ: dict[str, str], *, probes: RuntimeProbes | None = None
) -> SesionTransicionGoldenMaster:
    return SesionTransicionGoldenMaster(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_dir,
        clone_id=rig.clone_id,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=environ,
        probes=probes if probes is not None else _probes_rig(rig),
        registro=RegistroConsentimientos(),
    )


def _emitir_y_dejar_pending(
    rig: _Rig,
    environ: dict[str, str],
    *,
    identidad: IdentidadPropietario,
    transition_id: str | None = None,
    fallar_despues_de: str = "config",
    dejar_ip0_sin_registro: bool = False,
) -> tuple[str, dict[str, bytes]]:
    """Ejecuta el flujo HITL real y detiene la transición dejando un PENDING real y superficies mutadas."""
    tid = transition_id or str(uuid.uuid4())
    bytes_antes = {
        "mo2_ini": rig.ini.read_bytes(),
        "config": rig.config.read_bytes(),
        "active_json": storage.active_state_path(rig.storage).read_bytes(),
    }
    sesion = _crear_sesion(rig, environ)
    solicitud = sesion.preparar(identidad=identidad, entregar=lambda _p: None, transition_id=tid)
    consent = sesion.decidir_y_emitir(
        solicitud=solicitud,
        decision=DecisionConsentimiento.APROBAR,
        identidad=identidad,
    )
    assert consent is not None

    if dejar_ip0_sin_registro:
        orig_escribir = recovery.escribir_registro_locked

        def _crash_ip0(*_a: object, **_kw: object) -> None:
            raise RuntimeError("crash simulado en IP-0 antes del registro de recuperación")

        recovery.escribir_registro_locked = _crash_ip0  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="IP-0"):
                sesion.ejecutar(solicitud=solicitud, consentimiento=consent)
        finally:
            recovery.escribir_registro_locked = orig_escribir  # type: ignore[assignment]
        return tid, bytes_antes

    orig_rollback = binder._rollback

    def _sin_rollback(*_a: object, **_kw: object) -> tuple[str, ...]:
        return ("mo2_ini", "config")

    orig_eval = binder.assess_effective_runtime
    orig_cas = binder._escribir_estado_activo_cas_locked

    def _explotar_cas(*_a: object, **_kw: object) -> object:
        raise RuntimeError("crash simulado tras escribir INI y config")

    def _explotar_post(*_a: object, **_kw: object) -> object:
        raise RuntimeError("crash simulado tras escribir active.json antes de FINALIZE")

    binder._rollback = _sin_rollback  # type: ignore[assignment]
    if fallar_despues_de == "config":
        binder._escribir_estado_activo_cas_locked = _explotar_cas  # type: ignore[assignment]
    else:
        binder.assess_effective_runtime = _explotar_post  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="crash simulado"):
            sesion.ejecutar(solicitud=solicitud, consentimiento=consent)
    finally:
        binder._rollback = orig_rollback  # type: ignore[assignment]
        binder._escribir_estado_activo_cas_locked = orig_cas  # type: ignore[assignment]
        binder.assess_effective_runtime = orig_eval  # type: ignore[assignment]
    return tid, bytes_antes


def _confirmaciones_desde_disco(rig: _Rig) -> dict[str, ConfirmacionSuperficie]:
    confs: dict[str, ConfirmacionSuperficie] = {}
    for kind, ruta in (
        ("mo2_ini", rig.ini),
        ("config", rig.config),
        ("active_json", storage.active_state_path(rig.storage)),
    ):
        if ruta.exists():
            confs[kind] = ConfirmacionSuperficie(
                estado=EstadoSuperficieConfirmada.PRESENTE, sha256=_sha(ruta.read_bytes())
            )
        else:
            confs[kind] = ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.AUSENTE)
    return confs


def _finalizar_transicion_completa(rig: _Rig, environ: dict[str, str], *, identidad: IdentidadPropietario) -> str:
    tid = str(uuid.uuid4())
    sesion = _crear_sesion(rig, environ)
    solicitud = sesion.preparar(identidad=identidad, entregar=lambda _p: None, transition_id=tid)
    consent = sesion.decidir_y_emitir(
        solicitud=solicitud,
        decision=DecisionConsentimiento.APROBAR,
        identidad=identidad,
    )
    assert consent is not None
    res = sesion.ejecutar(solicitud=solicitud, consentimiento=consent)
    assert res.finalized_record is not None
    assert res.finalized_record.transition_id == tid
    return tid


# ===========================================================================
# 1. Anclas AST enumerantes (no muestreo)
# ===========================================================================


def test_ancla_ast_escritores_journal_state_none_congelados() -> None:
    """Los únicos escritores de ``JournalState.NONE`` en ``journal.py`` están congelados por igualdad literal."""
    fuente = (_PAQUETE_FR / "journal.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente, filename="journal.py")
    escritores: set[str] = set()
    for nodo in arbol.body:
        if not isinstance(nodo, ast.FunctionDef):
            continue
        usa_none = any(
            isinstance(h, ast.Attribute)
            and isinstance(h.value, ast.Name)
            and h.value.id == "JournalState"
            and h.attr == "NONE"
            for h in ast.walk(nodo)
        )
        escribe = any(
            isinstance(h, ast.Call) and isinstance(h.func, ast.Attribute) and h.func.attr == "write_json_atomic"
            for h in ast.walk(nodo)
        )
        if usa_none and escribe:
            escritores.add(nodo.name)
    assert escritores == {"_rotar_journal_locked", "_abortar_pendiente_locked"}


def test_ancla_ast_consumidores_abandono_tambien_consultan_abortos() -> None:
    """Todo módulo que consulta ``transition_abandonment`` también consulta ``pending_abort_store``."""
    consumidores_abandono: set[str] = set()
    consumidores_aborto: set[str] = set()
    for mod in sorted(_PAQUETE_FR.glob("*.py")):
        if mod.name in ("transition_abandonment.py", "pending_abort_store.py", "__init__.py"):
            continue
        txt = mod.read_text(encoding="utf-8")
        if any(s in txt for s in ("registro_de_abandono", "_cargar_abandonos")):
            consumidores_abandono.add(mod.name)
        if any(s in txt for s in ("registro_de_aborto", "cargar_abortos", "abortos_incompletos")):
            consumidores_aborto.add(mod.name)
    assert consumidores_abandono == {"autorizacion_hitl_transicion.py", "runtime_clone_binder.py"}
    assert consumidores_abandono.issubset(consumidores_aborto)
    assert "journal.py" in consumidores_aborto
    assert "approval.py" in consumidores_aborto
    assert "binder_recovery.py" in consumidores_aborto


# ===========================================================================
# 2. D1 / RQ-7 — Guard en consumir_aprobacion + concurrencia real
# ===========================================================================


def test_d1_aprobacion_nueva_con_pending_previo_no_se_consume(tmp_path: pathlib.Path) -> None:
    """Una aprobación ISSUED nueva NO se quema si existe un PENDING previo de otra transición."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    # Capturar evidencia válida antes del crash (como en un escenario con aprobación nueva)
    ev = _crear_sesion(rig, env_rig).derivar_evidencia()
    tid_pending, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)

    # Emitir una segunda aprobación (bypassing diagnosticar para simular emisión directa en lab)
    prop2 = approval_module.proponer_aprobacion(
        rig.storage,
        operation=approval_module.ApprovalOperation.PROMOTION,
        source_activation=ev.source_activation,
        generation_id=ev.generation_id,
        clone_id=ev.clone_id,
        clone_evidence=ev.clone_evidence,
        runtime_setup_id=ev.runtime_setup_id,
        runtime_setup_evidence=ev.runtime_setup_evidence,
        compatibility_evidence_id=ev.compatibility_evidence_id,
        candidate_id=ev.candidate_id,
        approval_provenance=ev.approval_provenance,
    )
    reg_cons = RegistroConsentimientos()
    pres2 = reg_cons.presentar(rig.storage, prop2, identidad=dueno)
    cons2 = reg_cons.responder(pres2.solicitud_id, decision=DecisionConsentimiento.APROBAR, identidad=dueno)
    assert cons2 is not None
    from sky_claw.local.frozen_runtime.owner_consent import emitir_aprobacion_autorizada

    emitir_aprobacion_autorizada(rig.storage, prop2, cons2)
    assert cargar_ledger_aprobacion(rig.storage).ledger.state is ApprovalLifecycle.ISSUED  # type: ignore[union-attr]

    # Intentar consumir con otro transition_id → rechazado SIN quemar
    tid_nuevo = str(uuid.uuid4())
    propuesta_j = journal_module.PropuestaTransicion(
        transition_id=tid_nuevo,
        operation=journal_module.JournalOperation.PROMOTION,
        source_activation=prop2.scope.source_activation,
        source_activation_digest=prop2.scope.source_activation_digest,
        target_generation_id=prop2.scope.generation_id,
        target_clone_id=prop2.scope.clone_id,
        target_runtime_setup_id=prop2.scope.runtime_setup_id,
        candidate_id=prop2.scope.candidate_id,
        approval_scope_digest=prop2.scope.approval_scope_digest,
        evidence_digests=journal_module.EvidenceDigests(
            clone_evidence=prop2.scope.clone_evidence,
            runtime_setup_evidence=prop2.scope.runtime_setup_evidence,
            compatibility_evidence=prop2.scope.compatibility_evidence_id,
        ),
        expected_effective_path=str(rig.clone_root),
        created_at_ns=time.time_ns(),
    )
    with pytest.raises(ApprovalJournalBlockedError):
        consumir_aprobacion(
            rig.storage,
            approval_id=prop2.scope.approval_id,
            approval_seq=prop2.scope.approval_seq,
            approval_scope_digest=prop2.scope.approval_scope_digest,
            transition_id=tid_nuevo,
            transicion=propuesta_j,
        )
    # La aprobación SIGUE ISSUED y sin tombstone para seq 2
    assert cargar_ledger_aprobacion(rig.storage).ledger.state is ApprovalLifecycle.ISSUED  # type: ignore[union-attr]
    autoridad = derivar_autoridad_aprobacion(rig.storage)
    assert not any(t.approval_seq == prop2.scope.approval_seq for t in autoridad.tombstones)

    # D1: incluso pasando el MISMO transition_id del PENDING, sin vínculo con aprobación/tombstone original → RECHAZA
    propuesta_mismo_tid = journal_module.PropuestaTransicion(
        transition_id=tid_pending,
        operation=propuesta_j.operation,
        source_activation=propuesta_j.source_activation,
        source_activation_digest=propuesta_j.source_activation_digest,
        target_generation_id=propuesta_j.target_generation_id,
        target_clone_id=propuesta_j.target_clone_id,
        target_runtime_setup_id=propuesta_j.target_runtime_setup_id,
        candidate_id=propuesta_j.candidate_id,
        approval_scope_digest=propuesta_j.approval_scope_digest,
        evidence_digests=propuesta_j.evidence_digests,
        expected_effective_path=propuesta_j.expected_effective_path,
        created_at_ns=propuesta_j.created_at_ns,
    )
    with pytest.raises(ApprovalJournalBlockedError):
        consumir_aprobacion(
            rig.storage,
            approval_id=prop2.scope.approval_id,
            approval_seq=prop2.scope.approval_seq,
            approval_scope_digest=prop2.scope.approval_scope_digest,
            transition_id=tid_pending,
            transicion=propuesta_mismo_tid,
        )
    assert cargar_ledger_aprobacion(rig.storage).ledger.state is ApprovalLifecycle.ISSUED  # type: ignore[union-attr]


def test_d1_concurrencia_real_consumir_aprobacion_bajo_lock(tmp_path: pathlib.Path) -> None:
    """Dos hilos compiten simultáneamente con barrera real: serialización bajo ``participant_lock`` sin quemar doble."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    sesion = _crear_sesion(rig, env_rig)
    tid = str(uuid.uuid4())
    solicitud = sesion.preparar(identidad=dueno, entregar=lambda _p: None, transition_id=tid)
    consent = sesion.decidir_y_emitir(solicitud=solicitud, decision=DecisionConsentimiento.APROBAR, identidad=dueno)
    assert consent is not None
    prop = solicitud.propuesta
    propuesta_j = journal_module.PropuestaTransicion(
        transition_id=tid,
        operation=journal_module.JournalOperation.PROMOTION,
        source_activation=prop.scope.source_activation,
        source_activation_digest=prop.scope.source_activation_digest,
        target_generation_id=prop.scope.generation_id,
        target_clone_id=prop.scope.clone_id,
        target_runtime_setup_id=prop.scope.runtime_setup_id,
        candidate_id=prop.scope.candidate_id,
        approval_scope_digest=prop.scope.approval_scope_digest,
        evidence_digests=journal_module.EvidenceDigests(
            clone_evidence=prop.scope.clone_evidence,
            runtime_setup_evidence=prop.scope.runtime_setup_evidence,
            compatibility_evidence=prop.scope.compatibility_evidence_id,
        ),
        expected_effective_path=str(rig.clone_root),
        created_at_ns=time.time_ns(),
    )

    barrera = threading.Barrier(2)
    resultados: list[tuple[str, object]] = []
    lock_res = threading.Lock()

    def _worker(nombre: str, t_id_usado: str) -> None:
        p_local = journal_module.PropuestaTransicion(
            transition_id=t_id_usado,
            operation=propuesta_j.operation,
            source_activation=propuesta_j.source_activation,
            source_activation_digest=propuesta_j.source_activation_digest,
            target_generation_id=propuesta_j.target_generation_id,
            target_clone_id=propuesta_j.target_clone_id,
            target_runtime_setup_id=propuesta_j.target_runtime_setup_id,
            candidate_id=propuesta_j.candidate_id,
            approval_scope_digest=propuesta_j.approval_scope_digest,
            evidence_digests=propuesta_j.evidence_digests,
            expected_effective_path=propuesta_j.expected_effective_path,
            created_at_ns=propuesta_j.created_at_ns,
        )
        barrera.wait()
        try:
            res = consumir_aprobacion(
                rig.storage,
                approval_id=prop.scope.approval_id,
                approval_seq=prop.scope.approval_seq,
                approval_scope_digest=prop.scope.approval_scope_digest,
                transition_id=t_id_usado,
                transicion=p_local,
                timeout=5.0,
            )
            with lock_res:
                resultados.append((nombre, res))
        except Exception as exc:  # noqa: BLE001
            with lock_res:
                resultados.append((nombre, exc))

    t1 = threading.Thread(target=_worker, args=("A", tid))
    t2 = threading.Thread(target=_worker, args=("B", str(uuid.uuid4())))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    exitos = [r for _, r in resultados if isinstance(r, approval_module.ResultadoConsumo)]
    errores = [r for _, r in resultados if isinstance(r, Exception)]
    assert len(exitos) == 1
    assert len(errores) == 1


# ===========================================================================
# 3. Matriz principal: aborto autorizado, rechazos, presencia/ausencia, quiescencia
# ===========================================================================


def test_aborto_autorizado_reconcilia_y_habilita_create_y_finalize(tmp_path: pathlib.Path) -> None:
    """Propietario autorizado + estado coincidente ⇒ restaura, rota PENDING→NONE (sin historial) y CREATE+FINALIZE funcionan."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, bytes_antes = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno, fallar_despues_de="active")
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION

    confs = _confirmaciones_desde_disco(rig)
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="reconciliar fallo de laboratorio",
        confirmaciones=confs,
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO
    assert reg.modo is ModoAbortoPendiente.CON_REGISTRO_RECUPERACION
    assert rig.ini.read_bytes() == bytes_antes["mo2_ini"]
    assert rig.config.read_bytes() == bytes_antes["config"]
    assert storage.active_state_path(rig.storage).read_bytes() == bytes_antes["active_json"]

    j_snap = cargar_journal_transicion(rig.storage)
    assert j_snap.disposition is JournalDisposition.NONE
    assert j_snap.journal is not None
    assert j_snap.journal.last_finalized_transition_id is None
    assert derivar_historial_finalizados(rig.storage).registros == ()
    assert recovery.cargar_registro(rig.storage, tid) is None

    # Doble invocación es idempotente
    reg2 = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="otro motivo ignorado en idempotencia",
        confirmaciones=confs,
        probes=_probes_quiescentes(),
    )
    assert reg2 == reg

    # Diagnóstico vuelve a LIMPIO (tombstone CONSUMED resuelto por aborto COMPLETO)
    assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO

    # Nueva transición en proceso limpio (entorno alineado al estado restaurado) entra por CREATE y FINALIZE legítimo
    env_rig["SKYRIM_PATH"] = str(rig.managed_src)
    tid2 = _finalizar_transicion_completa(rig, env_rig, identidad=dueno)
    hist = derivar_historial_finalizados(rig.storage)
    assert len(hist.registros) == 1
    assert hist.latest is not None and hist.latest.transition_id == tid2
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE


def test_aborto_con_historial_habilita_start_next(tmp_path: pathlib.Path) -> None:
    """Tras una transición finalizada T1, abortar T2 deja testigo=T1 y permite START_NEXT con T3."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    ini_managed = rig.ini.read_bytes()
    cfg_managed = rig.config.read_bytes()

    tid1 = _finalizar_transicion_completa(rig, env_rig, identidad=dueno)
    # Simular que MO2 y config volvieron a apuntar a managed_src antes de una nueva transición T2 (START_NEXT)
    rig.ini.write_bytes(ini_managed)
    rig.config.write_bytes(cfg_managed)
    env_rig["SKYRIM_PATH"] = str(rig.managed_src)

    tid2, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno, fallar_despues_de="config")
    confs = _confirmaciones_desde_disco(rig)
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid2,
        identidad=dueno,
        motivo="abortar T2 con historial T1",
        confirmaciones=confs,
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO
    j_snap = cargar_journal_transicion(rig.storage)
    assert j_snap.disposition is JournalDisposition.NONE
    assert j_snap.journal is not None and j_snap.journal.last_finalized_transition_id == tid1

    # T3 entra por START_NEXT y finaliza (proceso nuevo con SKYRIM_PATH en managed_src)
    env_rig["SKYRIM_PATH"] = str(rig.managed_src)
    tid3 = _finalizar_transicion_completa(rig, env_rig, identidad=dueno)
    hist = derivar_historial_finalizados(rig.storage)
    assert [r.transition_id for r in hist.registros] == [tid1, tid3]
    assert hist.latest is not None and hist.latest.finalized_from_transition_id == tid1


def test_rechazos_identidad_hash_presencia_ausencia_quiescencia_corrupcion(tmp_path: pathlib.Path) -> None:
    """Rechaza sin mutar ante identidad incorrecta, hash distinto, PRESENTE/AUSENTE invertido y procesos vivos."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno(_OWNER_CHAT_ID)
    ajeno = _identidad_dueno(_AJENO_CHAT_ID)
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    ini_post = rig.ini.read_bytes()
    confs = _confirmaciones_desde_disco(rig)

    # Identidad incorrecta
    with pytest.raises(AbortoPendienteInvalidoError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=ajeno,
            motivo="ajeno",
            confirmaciones=confs,
            probes=_probes_quiescentes(),
        )
    assert rig.ini.read_bytes() == ini_post

    # Hash distinto del autorizado
    confs_mal = dict(confs)
    confs_mal["mo2_ini"] = ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.PRESENTE, sha256="0" * 64)
    with pytest.raises(AbortoSuperficieDivergenteError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="hash mal",
            confirmaciones=confs_mal,
            probes=_probes_quiescentes(),
        )
    assert rig.ini.read_bytes() == ini_post

    # Archivo PRESENTE cuando se autorizó AUSENTE
    confs_aus = dict(confs)
    confs_aus["mo2_ini"] = ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.AUSENTE)
    with pytest.raises(AbortoSuperficieDivergenteError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="ausente mal",
            confirmaciones=confs_aus,
            probes=_probes_quiescentes(),
        )

    # Archivo AUSENTE en disco cuando se autorizó PRESENTE → rechazo sin recrear archivo
    rig.ini.unlink()
    with pytest.raises(AbortoSuperficieDivergenteError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="presente pero borrado",
            confirmaciones=confs,
            probes=_probes_quiescentes(),
        )
    assert not rig.ini.exists()
    rig.ini.write_bytes(ini_post)

    # MO2 activo → rechazo
    probes_mo2 = _probes_rig(
        rig,
        procesos=LiveProcessSnapshot(
            processes=(LiveProcessEvidence(kind=LiveProcessKind.MO2, pid=1234, image_path=None),),
            indeterminate=False,
        ),
    )
    with pytest.raises(AbortoPendienteInvalidoError, match="quiescencia"):
        abortar_transicion_pendiente(
            rig.storage, transition_id=tid, identidad=dueno, motivo="mo2 vivo", confirmaciones=confs, probes=probes_mo2
        )

    # Archivo AUSENTE cuando se autorizó PRESENTE
    rig.config.unlink()
    with pytest.raises(AbortoSuperficieDivergenteError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="presente pero borrado",
            confirmaciones=confs,
            probes=_probes_quiescentes(),
        )

    # Confirmando AUSENTE correctamente → restaura desde el snapshot
    confs_borrado_ok = dict(confs)
    confs_borrado_ok["config"] = ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.AUSENTE)
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="config borrado confirmado",
        confirmaciones=confs_borrado_ok,
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO
    assert rig.config.exists()


def test_journal_corrupto_y_registro_corrupto_fallan_sin_mutar(tmp_path: pathlib.Path) -> None:
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    confs = _confirmaciones_desde_disco(rig)
    ini_post = rig.ini.read_bytes()

    # Registro de recuperación corrupto
    rec_path = storage.recovery_state_dir(rig.storage) / f"{tid}.json"
    rec_backup = rec_path.read_bytes()
    rec_path.write_text("{corrupto", encoding="utf-8")
    with pytest.raises(AbortoPendienteInvalidoError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="rec corrupto",
            confirmaciones=confs,
            probes=_probes_quiescentes(),
        )
    assert rig.ini.read_bytes() == ini_post
    rec_path.write_bytes(rec_backup)

    # Journal corrupto
    j_path = storage.transition_journal_path(rig.storage)
    j_path.write_text("{corrupto", encoding="utf-8")
    with pytest.raises(AbortoPendienteInvalidoError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="j corrupto",
            confirmaciones=confs,
            probes=_probes_quiescentes(),
        )
    assert rig.ini.read_bytes() == ini_post


# ===========================================================================
# 4. D2, D3, D4, D5 — Revalidación, fronteras inter-marcador y autoridad durable
# ===========================================================================


def test_d2_reintento_desde_superficies_verificadas_detecta_mutacion_externa(tmp_path: pathlib.Path) -> None:
    """D2: en SUPERFICIES_VERIFICADAS, una mutación externa nueva se preserva y detiene sin rotar a NONE."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    confs = _confirmaciones_desde_disco(rig)

    # Simular crash justo después de persistir SUPERFICIES_VERIFICADAS (antes de rotar journal)
    orig_abortar_j = journal_module._abortar_pendiente_locked

    def _crash_antes_de_rotar(*_a: object, **_kw: object) -> object:
        raise RuntimeError("crash simulado justo en SUPERFICIES_VERIFICADAS")

    import sky_claw.local.frozen_runtime.pending_reconciliation as pr_mod

    pr_mod._abortar_pendiente_locked = _crash_antes_de_rotar  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="SUPERFICIES_VERIFICADAS"):
            abortar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                identidad=dueno,
                motivo="d2",
                confirmaciones=confs,
                probes=_probes_quiescentes(),
            )
    finally:
        pr_mod._abortar_pendiente_locked = orig_abortar_j  # type: ignore[assignment]

    reg_mid = registro_de_aborto(rig.storage, tid)
    assert reg_mid is not None and reg_mid.estado is EstadoAbortoPendiente.SUPERFICIES_VERIFICADAS

    # Aparece una escritura externa NUEVA en config.toml
    bytes_externos = rig.config.read_bytes() + b"\n# cambio externo post-verificacion\n"
    rig.config.write_bytes(bytes_externos)

    with pytest.raises(AbortoSuperficieDivergenteError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="d2 reintento",
            confirmaciones=confs,
            probes=_probes_quiescentes(),
        )
    # Journal NO rotó y bytes externos preservados + evidencia forense guardada
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION
    assert rig.config.read_bytes() == bytes_externos
    ev_dir = rig.storage / "recovery_evidence" / tid
    assert ev_dir.is_dir() and any(p.name.startswith("config-") for p in ev_dir.iterdir())


def test_d3_fronteras_inter_marcador_completan_de_forma_segura(tmp_path: pathlib.Path) -> None:
    """D3: placeholder 0 bytes, corrupto no vacío, crash tras 1 superficie, tras rotar NONE y tras retirar recovery."""
    # (a) Placeholder vacío de 0 bytes → reintento completa; archivo corrupto no vacío → NUNCA sobrescribe
    rig_a, env_rig_a = _limpiar_rig_hitl(tmp_path / "a")
    dueno = _identidad_dueno()
    tid_a, _ = _emitir_y_dejar_pending(rig_a, env_rig_a, identidad=dueno)
    confs_a = _confirmaciones_desde_disco(rig_a)
    ruta_ab_a = storage.aborted_transition_record_path(rig_a.storage, tid_a)
    ruta_ab_a.write_bytes(b"corrupto no vacio")
    with pytest.raises(AbortoCorruptoError):
        abortar_transicion_pendiente(
            rig_a.storage,
            transition_id=tid_a,
            identidad=dueno,
            motivo="corrupto",
            confirmaciones=confs_a,
            probes=_probes_quiescentes(),
        )
    assert ruta_ab_a.read_bytes() == b"corrupto no vacio"

    # Ahora placeholder vacío de 0 bytes (crash entre open("x") y write_json_atomic)
    ruta_ab_a.write_bytes(b"")
    reg_a = abortar_transicion_pendiente(
        rig_a.storage,
        transition_id=tid_a,
        identidad=dueno,
        motivo="placeholder ok",
        confirmaciones=confs_a,
        probes=_probes_quiescentes(),
    )
    assert reg_a.estado is EstadoAbortoPendiente.COMPLETO

    # (b) Crash tras restaurar 1 superficie (mo2_ini) y antes de avanzar marcador a SUPERFICIES_VERIFICADAS
    rig_b, env_rig_b = _limpiar_rig_hitl(tmp_path / "b")
    tid_b, bytes_b = _emitir_y_dejar_pending(rig_b, env_rig_b, identidad=dueno)
    confs_b = _confirmaciones_desde_disco(rig_b)
    llamadas = {"n": 0}
    orig_rest = recovery._restaurar_persistida

    def _fallar_en_segunda(*args: object, **kwargs: object) -> recovery.VerificacionSuperficie:
        llamadas["n"] += 1
        if llamadas["n"] == 2:
            raise RuntimeError("crash tras restaurar primera superficie")
        return orig_rest(*args, **kwargs)  # type: ignore[arg-type]

    recovery._restaurar_persistida = _fallar_en_segunda  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="primera superficie"):
            abortar_transicion_pendiente(
                rig_b.storage,
                transition_id=tid_b,
                identidad=dueno,
                motivo="b",
                confirmaciones=confs_b,
                probes=_probes_quiescentes(),
            )
    finally:
        recovery._restaurar_persistida = orig_rest  # type: ignore[assignment]

    assert registro_de_aborto(rig_b.storage, tid_b).estado is EstadoAbortoPendiente.INTENCION  # type: ignore[union-attr]
    assert rig_b.ini.read_bytes() == bytes_b["mo2_ini"]
    # Reintento completa sin error
    reg_b = abortar_transicion_pendiente(
        rig_b.storage,
        transition_id=tid_b,
        identidad=dueno,
        motivo="b",
        confirmaciones=confs_b,
        probes=_probes_quiescentes(),
    )
    assert reg_b.estado is EstadoAbortoPendiente.COMPLETO
    assert rig_b.config.read_bytes() == bytes_b["config"]

    # (c) Crash DESPUÉS de rotar journal a NONE y ANTES de persistir JOURNAL_NONE
    rig_c, env_rig_c = _limpiar_rig_hitl(tmp_path / "c")
    tid_c, _ = _emitir_y_dejar_pending(rig_c, env_rig_c, identidad=dueno)
    confs_c = _confirmaciones_desde_disco(rig_c)
    import sky_claw.local.frozen_runtime.pending_reconciliation as pr_mod

    orig_avanzar = pr_mod.avanzar_estado_aborto_locked

    def _crash_en_journal_none(
        root: pathlib.Path, previo: RegistroAbortoPendiente, nuevo: RegistroAbortoPendiente
    ) -> RegistroAbortoPendiente:
        if nuevo.estado is EstadoAbortoPendiente.JOURNAL_NONE:
            raise RuntimeError("crash tras rotar journal antes de JOURNAL_NONE")
        return orig_avanzar(root, previo, nuevo)

    pr_mod.avanzar_estado_aborto_locked = _crash_en_journal_none  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="JOURNAL_NONE"):
            abortar_transicion_pendiente(
                rig_c.storage,
                transition_id=tid_c,
                identidad=dueno,
                motivo="c",
                confirmaciones=confs_c,
                probes=_probes_quiescentes(),
            )
    finally:
        pr_mod.avanzar_estado_aborto_locked = orig_avanzar  # type: ignore[assignment]

    assert cargar_journal_transicion(rig_c.storage).disposition is JournalDisposition.NONE
    assert registro_de_aborto(rig_c.storage, tid_c).estado is EstadoAbortoPendiente.SUPERFICIES_VERIFICADAS  # type: ignore[union-attr]
    # Reintento reconoce el journal NONE ya rotado y completa hasta COMPLETO
    reg_c = abortar_transicion_pendiente(
        rig_c.storage,
        transition_id=tid_c,
        identidad=dueno,
        motivo="c",
        confirmaciones=confs_c,
        probes=_probes_quiescentes(),
    )
    assert reg_c.estado is EstadoAbortoPendiente.COMPLETO

    # (d) Crash DESPUÉS de retirar recovery record y ANTES de persistir COMPLETO
    rig_d, env_rig_d = _limpiar_rig_hitl(tmp_path / "d")
    tid_d, _ = _emitir_y_dejar_pending(rig_d, env_rig_d, identidad=dueno)
    confs_d = _confirmaciones_desde_disco(rig_d)

    def _crash_en_completo(
        root: pathlib.Path, previo: RegistroAbortoPendiente, nuevo: RegistroAbortoPendiente
    ) -> RegistroAbortoPendiente:
        if nuevo.estado is EstadoAbortoPendiente.COMPLETO:
            raise RuntimeError("crash tras retirar recovery antes de COMPLETO")
        return orig_avanzar(root, previo, nuevo)

    pr_mod.avanzar_estado_aborto_locked = _crash_en_completo  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="COMPLETO"):
            abortar_transicion_pendiente(
                rig_d.storage,
                transition_id=tid_d,
                identidad=dueno,
                motivo="d",
                confirmaciones=confs_d,
                probes=_probes_quiescentes(),
            )
    finally:
        pr_mod.avanzar_estado_aborto_locked = orig_avanzar  # type: ignore[assignment]

    assert recovery.cargar_registro(rig_d.storage, tid_d) is None
    assert registro_de_aborto(rig_d.storage, tid_d).estado is EstadoAbortoPendiente.JOURNAL_NONE  # type: ignore[union-attr]
    # D4: mientras esté en JOURNAL_NONE (incompleto), diagnosticar_estado_durable exige dueño
    assert diagnosticar_estado_durable(rig_d.storage).estado is EstadoDurable.REQUIERE_DUENO
    # Reintento completa a COMPLETO
    reg_d = abortar_transicion_pendiente(
        rig_d.storage,
        transition_id=tid_d,
        identidad=dueno,
        motivo="d",
        confirmaciones=confs_d,
        probes=_probes_quiescentes(),
    )
    assert reg_d.estado is EstadoAbortoPendiente.COMPLETO


def test_d4_d5_autoridad_primitiva_y_veto_permanente_transition_id(tmp_path: pathlib.Path) -> None:
    """D5: _abortar_pendiente_locked exige evidencia durable SUPERFICIES_VERIFICADAS; el tid abortado queda vetado."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    j = cargar_journal_transicion(rig.storage).journal
    assert j is not None

    # Llamar _abortar_pendiente_locked SIN registro durable → RECHAZO
    with (
        participant_lock(rig.storage, timeout=1.0) as handle,
        pytest.raises(JournalIdentityError, match="evidencia durable"),
    ):
        _abortar_pendiente_locked(
            handle,
            rig.storage,
            expected_transition_id=tid,
            expected_journal_revision=j.journal_revision,
        )

    # Completar aborto legítimo
    confs = _confirmaciones_desde_disco(rig)
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="veto",
        confirmaciones=confs,
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO

    # D5: intentar reutilizar el MISMO transition_id abortado en la primitiva de bajo nivel del journal → RECHAZO
    from sky_claw.local.frozen_runtime.journal import (
        AprobacionConsumida,
        PropuestaTransicion,
        ProyeccionScope,
        _crear_pendiente_locked,
    )

    assert (
        j.operation is not None
        and j.source_activation is not None
        and j.source_activation_digest is not None
        and j.target_generation_id is not None
        and j.target_clone_id is not None
        and j.target_runtime_setup_id is not None
        and j.approval_scope_digest is not None
        and j.evidence_digests is not None
        and j.expected_effective_path is not None
    )
    prop_reuso = PropuestaTransicion(
        transition_id=tid,
        operation=j.operation,
        source_activation=j.source_activation,
        source_activation_digest=j.source_activation_digest,
        target_generation_id=j.target_generation_id,
        target_clone_id=j.target_clone_id,
        target_runtime_setup_id=j.target_runtime_setup_id,
        candidate_id=j.candidate_id,
        approval_scope_digest=j.approval_scope_digest,
        evidence_digests=j.evidence_digests,
        expected_effective_path=j.expected_effective_path,
        created_at_ns=j.created_at_ns + 1,
    )
    aprob_reuso = AprobacionConsumida(
        approval_id="00000000-0000-4000-8000-000000000099",
        approval_seq=99,
        scope_digest=prop_reuso.approval_scope_digest,
        consumed_by_transition_id=tid,
        proyeccion=ProyeccionScope(
            operation=j.operation.value,
            source_activation_digest=j.source_activation_digest,
            target_generation_id=j.target_generation_id,
            target_clone_id=j.target_clone_id,
            target_runtime_setup_id=j.target_runtime_setup_id,
            candidate_id=j.candidate_id,
            clone_evidence=j.evidence_digests.clone_evidence,
            runtime_setup_evidence=j.evidence_digests.runtime_setup_evidence,
            compatibility_evidence_id=j.evidence_digests.compatibility_evidence,
            approval_id="00000000-0000-4000-8000-000000000099",
            approval_seq=99,
        ),
    )
    with (
        participant_lock(rig.storage, timeout=1.0) as handle,
        pytest.raises(JournalCasError, match="abortada"),
    ):
        _crear_pendiente_locked(handle, rig.storage, prop_reuso, aprob_reuso)

    # D5: intentar reutilizar el MISMO transition_id abortado en una nueva transición HITL → RECHAZO
    env_rig["SKYRIM_PATH"] = str(rig.managed_src)
    sesion_nueva = _crear_sesion(rig, env_rig)
    sol_nueva = sesion_nueva.preparar(identidad=dueno, entregar=lambda _p: None, transition_id=tid)
    cons_nueva = sesion_nueva.decidir_y_emitir(
        solicitud=sol_nueva, decision=DecisionConsentimiento.APROBAR, identidad=dueno
    )
    assert cons_nueva is not None
    with pytest.raises((binder.BinderTransicionAbandonadaError, JournalCasError)):
        sesion_nueva.ejecutar(solicitud=sol_nueva, consentimiento=cons_nueva)


# ===========================================================================
# 5. D6 / RQ-5 — IP-0 sin registro de recuperación
# ===========================================================================


def test_d6_ip0_evidencia_positiva_y_hold_sin_evidencia(tmp_path: pathlib.Path) -> None:
    """IP-0: sin contexto ⇒ HOLD sin mutar; con evidencia causal positiva completa ⇒ aborto sin escrituras + CREATE."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, bytes_antes = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno, dejar_ip0_sin_registro=True)
    assert recovery.cargar_registro(rig.storage, tid) is None
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION

    # Sin contexto IP-0 → HOLD fail-closed sin mutaciones
    with pytest.raises(AbortoIp0EvidenciaInsuficienteError):
        abortar_transicion_pendiente(
            rig.storage, transition_id=tid, identidad=dueno, motivo="sin contexto", probes=_probes_quiescentes()
        )
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION

    # Con INI mutado un byte → HOLD fail-closed
    ctx = ContextoVerificacionIp0(
        mo2_instance_dir=rig.mo2_dir,
        clone_id=rig.clone_id,
        config_path=rig.config,
        managed_source_root=rig.managed_src,
        environ=env_rig,
    )
    rig.ini.write_bytes(bytes_antes["mo2_ini"] + b"\n; byte extra\n")
    with pytest.raises(AbortoIp0EvidenciaInsuficienteError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="ini cambiado",
            contexto_ip0=ctx,
            probes=_probes_rig(rig),
        )
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION

    # Restaurando INI original → evidencia causal positiva completa ⇒ aborto seguro sin escribir superficies
    rig.ini.write_bytes(bytes_antes["mo2_ini"])
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="ip0 valido",
        contexto_ip0=ctx,
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO
    assert reg.modo is ModoAbortoPendiente.IP0_SIN_REGISTRO
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE

    # Transición nueva entra por CREATE y finaliza
    _finalizar_transicion_completa(rig, env_rig, identidad=dueno)


# ===========================================================================
# 6. RQ-8 — Protección de fronteras C6 (reserva) y C7 (FINALIZED)
# ===========================================================================


def test_rq8_c6_reserva_y_c7_finalized_rechazan_aborto(tmp_path: pathlib.Path) -> None:
    """C6 (reserva presente) → AbortoPendienteReservadoError; C7 (FINALIZED presente) → AbortoPendienteInvalidoError."""
    # C6: reserva durable presente
    rig_c6, env_rig_c6 = _limpiar_rig_hitl(tmp_path / "c6")
    dueno = _identidad_dueno()
    tid_c6, _ = _emitir_y_dejar_pending(rig_c6, env_rig_c6, identidad=dueno)
    with participant_lock(rig_c6.storage, timeout=1.0) as handle:
        _reservar_seq_locked(handle, rig_c6.storage, seq=1, transition_id=tid_c6)
    confs_c6 = _confirmaciones_desde_disco(rig_c6)
    with pytest.raises(AbortoPendienteReservadoError):
        abortar_transicion_pendiente(
            rig_c6.storage,
            transition_id=tid_c6,
            identidad=dueno,
            motivo="c6",
            confirmaciones=confs_c6,
            probes=_probes_quiescentes(),
        )
    # La reserva sigue intacta
    assert (storage.finalization_seqs_state_dir(rig_c6.storage) / "1.json").is_file()

    # C7: transición ya FINALIZED
    rig_c7, env_rig_c7 = _limpiar_rig_hitl(tmp_path / "c7")
    tid_c7 = _finalizar_transicion_completa(rig_c7, env_rig_c7, identidad=dueno)
    with pytest.raises(AbortoPendienteInvalidoError, match="FINALIZED"):
        abortar_transicion_pendiente(
            rig_c7.storage, transition_id=tid_c7, identidad=dueno, motivo="c7", probes=_probes_quiescentes()
        )


# ===========================================================================
# 7. D8 — Store histórico / incoherente + Telegram /gm_abortar_pendiente
# ===========================================================================


def test_d8_store_ausente_con_none_sin_testigo_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """D8: journal NONE sin last_finalized_transition_id y sin store de abortos falla cerrado en diagnóstico."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    confs = _confirmaciones_desde_disco(rig)
    abortar_transicion_pendiente(
        rig.storage, transition_id=tid, identidad=dueno, motivo="d8", confirmaciones=confs, probes=_probes_quiescentes()
    )
    assert diagnosticar_estado_durable(rig.storage).estado is EstadoDurable.LIMPIO

    # Eliminar state/aborted_transitions/ deja un estado inalcanzable sin aborto → REQUIERE_DUENO (fail-closed)
    shutil.rmtree(storage.aborted_transitions_state_dir(rig.storage))
    diag = diagnosticar_estado_durable(rig.storage)
    assert diag.estado is EstadoDurable.REQUIERE_DUENO


@pytest.mark.asyncio
async def test_telegram_gm_abortar_pendiente_flujo_completo(tmp_path: pathlib.Path) -> None:
    """Comando Telegram /gm_abortar_pendiente reconcilia un PENDING con identidad verificada."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno(_OWNER_CHAT_ID)
    tid, bytes_antes = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    ini_sha = _sha(rig.ini.read_bytes())
    cfg_sha = _sha(rig.config.read_bytes())

    enviados: list[str] = []

    class _SenderFalso:
        async def send(self, chat_id: int, text: str, *, reply_markup: object = None) -> None:
            enviados.append(text)

    objetivo = ObjetivoTransicionGoldenMaster(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_dir,
        clone_id=rig.clone_id,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env_rig,
        probes=_probes_quiescentes(),
    )
    adaptador = CoordinadorTelegramGoldenMaster(
        operador_autorizado=str(_OWNER_CHAT_ID),
        sender=_SenderFalso(),  # type: ignore[arg-type]
        objetivo=objetivo,
    )
    try:
        update = {
            "update_id": 101,
            "message": {
                "message_id": 1,
                "from": {"id": _OWNER_CHAT_ID},
                "chat": {"id": _OWNER_CHAT_ID, "type": "private"},
                "text": f"/gm_abortar_pendiente {tid} mo2_ini={ini_sha},config={cfg_sha} aborto por telegram",
            },
        }
        tarea = await adaptador.iniciar_comando(update)
        assert tarea is not None
        await tarea
    finally:
        await adaptador.cerrar()

    assert any("Aborto autorizado de PENDING completado" in m for m in enviados)
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE
    assert rig.ini.read_bytes() == bytes_antes["mo2_ini"]
    assert rig.config.read_bytes() == bytes_antes["config"]


def test_recuperar_retira_registro_obsoleto_de_transicion_ya_abortada(tmp_path: pathlib.Path) -> None:
    """Si queda un registro de recuperación de una transición abortada COMPLETO, recuperar lo retira limpiamente."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    archivos_rec = list(storage.recovery_state_dir(rig.storage).glob("*.json"))
    assert len(archivos_rec) == 1
    rec_path = archivos_rec[0]
    copia_rec = rec_path.read_bytes()

    confs = _confirmaciones_desde_disco(rig)
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="aborto previo a registro obsoleto",
        confirmaciones=confs,
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO
    assert not rec_path.exists()

    # Reinyectar el registro de recuperación obsoleto de la transición ya abortada
    rec_path.write_bytes(copia_rec)
    res = recovery.recuperar_transicion_interrumpida(rig.storage)
    assert res.estado is recovery.EstadoRecuperacion.TRANSICION_YA_ABORTADA
    assert res.requiere_dueno is False
    assert not rec_path.exists()


def test_ronda_adversarial_d4_reconciliar_recuperar_y_actualizar_bloqueados_en_aborto_incompleto(
    tmp_path: pathlib.Path,
) -> None:
    """Regresión de ronda adversarial: reconciliar_journal, recuperar y _actualizar_locked respetan D4/D8."""
    from sky_claw.local.frozen_runtime.journal import (
        VeredictoReconciliacion,
        _actualizar_locked,
        reconciliar_journal,
    )

    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    j = cargar_journal_transicion(rig.storage).journal
    assert j is not None
    confs = _confirmaciones_desde_disco(rig)

    # Interrumpir aborto en INTENCION (antes de restaurar)
    orig_rest = recovery._restaurar_persistida

    def _boom(*_a: object, **_kw: object) -> object:
        raise RuntimeError("corte en INTENCION")

    recovery._restaurar_persistida = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="INTENCION"):
            abortar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                identidad=dueno,
                motivo="corte d4",
                confirmaciones=confs,
                probes=_probes_quiescentes(),
            )
    finally:
        recovery._restaurar_persistida = orig_rest  # type: ignore[assignment]

    # ROJO #2: reconciliar_journal debe reportar REQUIRE_OWNER (ABORT_INCOMPLETE)
    rec_j = reconciliar_journal(rig.storage)
    assert rec_j.veredicto is VeredictoReconciliacion.REQUIRE_OWNER
    assert rec_j.punto_crash == "ABORT_INCOMPLETE"
    assert rec_j.requiere_dueno is True

    # ROJO #2: _actualizar_locked bloqueado durante aborto incompleto
    with (
        participant_lock(rig.storage, timeout=1.0) as handle,
        pytest.raises(JournalCasError, match="incompleto"),
    ):
        _actualizar_locked(
            handle,
            rig.storage,
            expected_transition_id=tid,
            expected_journal_revision=j.journal_revision,
            cambios={"expected_effective_path": str(rig.clone_root)},
        )

    # ROJO #3: recuperar_transicion_interrumpida NO muta superficies durante aborto en curso
    res_rec = recovery.recuperar_transicion_interrumpida(rig.storage)
    assert res_rec.estado is recovery.EstadoRecuperacion.REGISTRO_NO_LIGADO
    assert res_rec.requiere_dueno is True

    # Completar aborto y verificar que reconciliar_journal vuelve a COMPLETE (o FAIL_CLOSED si se borra el store D8)
    reg_ok = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="corte d4",
        confirmaciones=confs,
        probes=_probes_quiescentes(),
    )
    assert reg_ok.estado is EstadoAbortoPendiente.COMPLETO
    shutil.rmtree(storage.aborted_transitions_state_dir(rig.storage))
    rec_d8 = reconciliar_journal(rig.storage)
    assert rec_d8.veredicto is VeredictoReconciliacion.FAIL_CLOSED
    assert rec_d8.punto_crash == "ABORT_STORE"
