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
        if any(
            s in txt
            for s in ("registro_de_aborto", "cargar_abortos", "abortos_incompletos", "exigir_store_abortos_coherente")
        ):
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
    with pytest.raises((binder.BinderApprovalError, binder.BinderTransicionAbandonadaError, JournalCasError)):
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


# ===========================================================================
# 8. Regresión P1-01 y P1-02 (Codex sobre PR #1032)
# ===========================================================================


def test_p1_01_ip0_crash_tras_rotar_journal_revalida_superficies_antes_de_completo(tmp_path: pathlib.Path) -> None:
    """P1-01: tras rotar journal a NONE y crashear antes de JOURNAL_NONE, IP-0 DEBE revalidar antes de COMPLETO."""
    from sky_claw.local.frozen_runtime import pending_reconciliation as pr_mod
    from sky_claw.local.frozen_runtime.pending_abort_store import abortos_incompletos

    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, bytes_antes = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno, dejar_ip0_sin_registro=True)
    ctx = ContextoVerificacionIp0(
        mo2_instance_dir=rig.mo2_dir,
        clone_id=rig.clone_id,
        config_path=rig.config,
        managed_source_root=rig.managed_src,
        environ=env_rig,
    )

    # Crash justo después de rotar el journal a NONE y antes de persistir JOURNAL_NONE
    orig_avanzar = pr_mod.avanzar_estado_aborto_locked

    def _crash_antes_jnone(
        root_p: pathlib.Path, actual: RegistroAbortoPendiente, nuevo: RegistroAbortoPendiente
    ) -> RegistroAbortoPendiente:
        if nuevo.estado is EstadoAbortoPendiente.JOURNAL_NONE:
            raise RuntimeError("crash simulado IP-0 tras rotar journal a NONE y antes de JOURNAL_NONE")
        return orig_avanzar(root_p, actual, nuevo)

    pr_mod.avanzar_estado_aborto_locked = _crash_antes_jnone  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="JOURNAL_NONE"):
            abortar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                identidad=dueno,
                motivo="ip0 crash jnone",
                contexto_ip0=ctx,
                probes=_probes_rig(rig),
            )
    finally:
        pr_mod.avanzar_estado_aborto_locked = orig_avanzar  # type: ignore[assignment]

    # El journal ya está en NONE, pero el registro de aborto quedó en SUPERFICIES_VERIFICADAS
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE
    reg_mid = registro_de_aborto(rig.storage, tid)
    assert reg_mid is not None and reg_mid.estado is EstadoAbortoPendiente.SUPERFICIES_VERIFICADAS

    # Un tercero modifica modorganizer.ini antes del reintento → DEBE fallar cerrado y NO avanzar a COMPLETO
    rig.ini.write_bytes(bytes_antes["mo2_ini"] + b"\n; edicion ajena post-rotacion\n")
    with pytest.raises(AbortoIp0EvidenciaInsuficienteError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="ip0 crash jnone",
            contexto_ip0=ctx,
            probes=_probes_rig(rig),
        )
    # El bloqueo D4 de aborto incompleto sigue activo
    assert len(abortos_incompletos(rig.storage)) == 1
    reg_tras_rechazo = registro_de_aborto(rig.storage, tid)
    assert reg_tras_rechazo is not None and reg_tras_rechazo.estado is EstadoAbortoPendiente.SUPERFICIES_VERIFICADAS

    # Restaurando la superficie a su estado aprobado, el reintento sí completa limpiamente
    rig.ini.write_bytes(bytes_antes["mo2_ini"])
    reg_fin = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="ip0 crash jnone",
        contexto_ip0=ctx,
        probes=_probes_rig(rig),
    )
    assert reg_fin.estado is EstadoAbortoPendiente.COMPLETO
    assert len(abortos_incompletos(rig.storage)) == 0


def test_p1_01_hermano_reintento_desde_journal_none_tras_retirar_registro_revalida_superficies(
    tmp_path: pathlib.Path,
) -> None:
    """P1-01 hermano: reintentar desde JOURNAL_NONE (incluso tras retirar el registro de recuperación) revalida."""
    from sky_claw.local.frozen_runtime import pending_reconciliation as pr_mod
    from sky_claw.local.frozen_runtime.pending_abort_store import abortos_incompletos

    # Caso A: CON_REGISTRO_RECUPERACION — crash después de eliminar el registro de recuperación y antes de COMPLETO
    rig_a, env_a = _limpiar_rig_hitl(tmp_path / "caso_a")
    dueno = _identidad_dueno()
    tid_a, bytes_a = _emitir_y_dejar_pending(rig_a, env_a, identidad=dueno)
    confs_a = _confirmaciones_desde_disco(rig_a)

    orig_avanzar = pr_mod.avanzar_estado_aborto_locked

    def _crash_antes_completo(
        root_p: pathlib.Path, actual: RegistroAbortoPendiente, nuevo: RegistroAbortoPendiente
    ) -> RegistroAbortoPendiente:
        if nuevo.estado is EstadoAbortoPendiente.COMPLETO:
            raise RuntimeError("crash simulado tras retirar registro de recuperación y antes de COMPLETO")
        return orig_avanzar(root_p, actual, nuevo)

    pr_mod.avanzar_estado_aborto_locked = _crash_antes_completo  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="COMPLETO"):
            abortar_transicion_pendiente(
                rig_a.storage,
                transition_id=tid_a,
                identidad=dueno,
                motivo="hermano jnone a",
                confirmaciones=confs_a,
                probes=_probes_quiescentes(),
            )
    finally:
        pr_mod.avanzar_estado_aborto_locked = orig_avanzar  # type: ignore[assignment]

    assert recovery.cargar_registro(rig_a.storage, tid_a) is None
    reg_a_mid = registro_de_aborto(rig_a.storage, tid_a)
    assert reg_a_mid is not None and reg_a_mid.estado is EstadoAbortoPendiente.JOURNAL_NONE

    # Tercero altera modorganizer.ini antes del reintento → NO se levanta bloqueo D4
    rig_a.ini.write_bytes(bytes_a["mo2_ini"] + b"\n; cambio ajeno en JOURNAL_NONE\n")
    with pytest.raises(AbortoSuperficieDivergenteError):
        abortar_transicion_pendiente(
            rig_a.storage,
            transition_id=tid_a,
            identidad=dueno,
            motivo="hermano jnone a",
            confirmaciones=confs_a,
            probes=_probes_quiescentes(),
        )
    assert len(abortos_incompletos(rig_a.storage)) == 1
    assert registro_de_aborto(rig_a.storage, tid_a).estado is EstadoAbortoPendiente.JOURNAL_NONE  # type: ignore[union-attr]

    # Al restaurar, el reintento sí completa
    rig_a.ini.write_bytes(bytes_a["mo2_ini"])
    reg_a_fin = abortar_transicion_pendiente(
        rig_a.storage,
        transition_id=tid_a,
        identidad=dueno,
        motivo="hermano jnone a",
        confirmaciones=confs_a,
        probes=_probes_quiescentes(),
    )
    assert reg_a_fin.estado is EstadoAbortoPendiente.COMPLETO

    # Caso B: IP-0 — crash en JOURNAL_NONE antes de COMPLETO + modificación ajena de modorganizer.ini
    rig_b, env_b = _limpiar_rig_hitl(tmp_path / "caso_b")
    tid_b, bytes_b = _emitir_y_dejar_pending(rig_b, env_b, identidad=dueno, dejar_ip0_sin_registro=True)
    ctx_b = ContextoVerificacionIp0(
        mo2_instance_dir=rig_b.mo2_dir,
        clone_id=rig_b.clone_id,
        config_path=rig_b.config,
        managed_source_root=rig_b.managed_src,
        environ=env_b,
    )
    pr_mod.avanzar_estado_aborto_locked = _crash_antes_completo  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="COMPLETO"):
            abortar_transicion_pendiente(
                rig_b.storage,
                transition_id=tid_b,
                identidad=dueno,
                motivo="hermano jnone b",
                contexto_ip0=ctx_b,
                probes=_probes_rig(rig_b),
            )
    finally:
        pr_mod.avanzar_estado_aborto_locked = orig_avanzar  # type: ignore[assignment]

    assert registro_de_aborto(rig_b.storage, tid_b).estado is EstadoAbortoPendiente.JOURNAL_NONE  # type: ignore[union-attr]
    rig_b.ini.write_bytes(bytes_b["mo2_ini"] + b"\n; cambio ajeno ip0 jnone\n")
    with pytest.raises(AbortoIp0EvidenciaInsuficienteError):
        abortar_transicion_pendiente(
            rig_b.storage,
            transition_id=tid_b,
            identidad=dueno,
            motivo="hermano jnone b",
            contexto_ip0=ctx_b,
            probes=_probes_rig(rig_b),
        )
    assert len(abortos_incompletos(rig_b.storage)) == 1
    assert registro_de_aborto(rig_b.storage, tid_b).estado is EstadoAbortoPendiente.JOURNAL_NONE  # type: ignore[union-attr]


def test_p1_02_error_lectura_o_existencia_nunca_sobrescribe_ni_elimina_como_autoria(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-02: OSError al leer o comprobar existencia es UNKNOWN, nunca autoría; preserva disco en ambos caminos."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    registro_rec = recovery.cargar_registro(rig.storage, tid)
    assert registro_rec is not None

    sup_cfg = next(s for s in registro_rec.superficies if s.kind == "config")
    sup_ini = next(s for s in registro_rec.superficies if s.kind == "mo2_ini")
    bytes_ajenos_cfg = b"skyrim_path = 'C:/EscrituraAjenaIlegible'\n"
    rig.config.write_bytes(bytes_ajenos_cfg)
    bytes_ajenos_ini = rig.ini.read_bytes() + b"\n; contenido ajeno ini\n"
    rig.ini.write_bytes(bytes_ajenos_ini)

    orig_read_bytes = pathlib.Path.read_bytes

    # 1. Error de lectura (PermissionError en read_bytes) durante rollback automático → ok=False, NO sobrescribe
    def _fallar_read_cfg(self: pathlib.Path) -> bytes:
        if os.path.normcase(os.path.abspath(os.fspath(self))) == os.path.normcase(
            os.path.abspath(os.fspath(rig.config))
        ):
            raise PermissionError("acceso denegado simulado al leer config.toml")
        return orig_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _fallar_read_cfg)

    # _escrita_por_tercero no puede clasificar un error de I/O como contenido propio (False, "")
    es_ajena, motivo_ajena = recovery._escrita_por_tercero(sup_cfg, registro_rec)
    assert es_ajena is True
    assert motivo_ajena

    verif_auto = recovery._restaurar_persistida(sup_cfg, registro_rec, raiz_evidencia=rig.storage)
    assert verif_auto.ok is False
    assert verif_auto.restaurada is False
    assert verif_auto.motivo_restauracion is None
    assert orig_read_bytes(rig.config) == bytes_ajenos_cfg

    # restaurar_registro_locked también debe detenerse con ok=False sin sobrescribir ni borrar nada
    with participant_lock(rig.storage, timeout=1.0) as handle:
        verifs = recovery.restaurar_registro_locked(handle, rig.storage, registro_rec, env_rig)
    verif_cfg_lote = next(v for v in verifs if v.kind == "config")
    assert verif_cfg_lote.ok is False
    assert verif_cfg_lote.restaurada is False
    assert orig_read_bytes(rig.config) == bytes_ajenos_cfg

    # 2. Error de comprobación de existencia (PermissionError en os.stat) en descarte autorizado (mo2_ini y config)
    monkeypatch.setattr(pathlib.Path, "read_bytes", orig_read_bytes)
    orig_stat = os.stat

    def _fallar_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        norm = os.path.normcase(os.path.abspath(os.fspath(path)))  # type: ignore[arg-type]
        if norm in (
            os.path.normcase(os.path.abspath(os.fspath(rig.ini))),
            os.path.normcase(os.path.abspath(os.fspath(rig.config))),
        ):
            raise PermissionError("acceso denegado simulado en os.stat")
        return orig_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", _fallar_stat)

    confs = {
        "mo2_ini": ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.PRESENTE, sha256=_sha(bytes_ajenos_ini)),
        "config": ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.PRESENTE, sha256=_sha(bytes_ajenos_cfg)),
    }
    verif_ini_auth = recovery._restaurar_persistida(
        sup_ini, registro_rec, raiz_evidencia=rig.storage, descartes_autorizados=confs
    )
    verif_cfg_auth = recovery._restaurar_persistida(
        sup_cfg, registro_rec, raiz_evidencia=rig.storage, descartes_autorizados=confs
    )
    assert verif_ini_auth.ok is False and verif_ini_auth.restaurada is False
    assert verif_cfg_auth.ok is False and verif_cfg_auth.restaurada is False
    assert orig_read_bytes(rig.ini) == bytes_ajenos_ini
    assert orig_read_bytes(rig.config) == bytes_ajenos_cfg


# ===========================================================================
# 9. Regresión CI + 5 defectos confirmados de CodeRabbit (PR #1032)
# ===========================================================================


def test_cr_consumo_nuevo_rechaza_bootstrap_abortado_sin_store_y_reuso_tid_abortado(tmp_path: pathlib.Path) -> None:
    """CR #2 y #3: consumir_aprobacion rechaza bootstrap abortado con store incoherente y reuso de tid abortado."""
    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    confs = _confirmaciones_desde_disco(rig)
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=dueno,
        motivo="cr2 y cr3",
        confirmaciones=confs,
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO

    # Emitir una nueva aprobación legítima
    env_rig["SKYRIM_PATH"] = str(rig.managed_src)
    sesion = _crear_sesion(rig, env_rig)
    sol = sesion.preparar(identidad=dueno, entregar=lambda _p: None, transition_id=tid)
    cons = sesion.decidir_y_emitir(solicitud=sol, decision=DecisionConsentimiento.APROBAR, identidad=dueno)
    assert cons is not None

    # CR #3: intentar consumir la aprobación nueva reutilizando el MISMO transition_id abortado → RECHAZO SIN QUEMAR
    with pytest.raises(binder.BinderApprovalError, match="abortada"):
        sesion.ejecutar(solicitud=sol, consentimiento=cons)
    # La aprobación sigue ISSUED (no quemada)
    ledger_1 = cargar_ledger_aprobacion(rig.storage).ledger
    assert ledger_1 is not None and ledger_1.state is ApprovalLifecycle.ISSUED

    # CR #2: sobre otro bootstrap abortado con transition_id fresco, eliminar state/aborted_transitions/ → RECHAZO SIN QUEMAR
    rig2, env_rig2 = _limpiar_rig_hitl(tmp_path / "cr2")
    tid2, _ = _emitir_y_dejar_pending(rig2, env_rig2, identidad=dueno)
    reg2 = abortar_transicion_pendiente(
        rig2.storage,
        transition_id=tid2,
        identidad=dueno,
        motivo="cr2",
        confirmaciones=_confirmaciones_desde_disco(rig2),
        probes=_probes_quiescentes(),
    )
    assert reg2.estado is EstadoAbortoPendiente.COMPLETO
    env_rig2["SKYRIM_PATH"] = str(rig2.managed_src)
    sesion2 = _crear_sesion(rig2, env_rig2)
    sol2 = sesion2.preparar(identidad=dueno, entregar=lambda _p: None, transition_id=str(uuid.uuid4()))
    cons2 = sesion2.decidir_y_emitir(solicitud=sol2, decision=DecisionConsentimiento.APROBAR, identidad=dueno)
    assert cons2 is not None
    shutil.rmtree(storage.aborted_transitions_state_dir(rig2.storage))
    with pytest.raises(binder.BinderApprovalError, match="incoherente"):
        binder.ejecutar_transicion_binder(
            storage_root=rig2.storage,
            mo2_instance_dir=rig2.mo2_dir,
            target_clone_id=rig2.clone_id,
            propuesta=sol2.propuesta,
            consentimiento=cons2,
            managed_source_root=rig2.managed_src,
            config_path=rig2.config,
            environ=env_rig2,
            probes=_probes_rig(rig2),
            transition_id=sol2.transicion_id,
        )
    ledger_2 = cargar_ledger_aprobacion(rig2.storage).ledger
    assert ledger_2 is not None and ledger_2.state is ApprovalLifecycle.ISSUED


def test_cr_recuperar_no_elimina_registro_recuperacion_en_journal_none(tmp_path: pathlib.Path) -> None:
    """CR #7: recuperar_transicion_interrumpida NO elimina state/recovery/<tid>.json mientras el aborto esté en JOURNAL_NONE."""
    from sky_claw.local.frozen_runtime import pending_reconciliation as pr_mod

    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    confs = _confirmaciones_desde_disco(rig)

    # Crash justo después de persistir JOURNAL_NONE y ANTES de eliminar el registro de recuperación
    orig_elim = recovery.eliminar_registro_locked

    def _crash_antes_elim(*_a: object, **_kw: object) -> None:
        raise RuntimeError("crash simulado en JOURNAL_NONE antes de eliminar registro")

    recovery.eliminar_registro_locked = _crash_antes_elim  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="JOURNAL_NONE"):
            abortar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                identidad=dueno,
                motivo="cr7",
                confirmaciones=confs,
                probes=_probes_quiescentes(),
            )
    finally:
        recovery.eliminar_registro_locked = orig_elim  # type: ignore[assignment]

    assert registro_de_aborto(rig.storage, tid).estado is EstadoAbortoPendiente.JOURNAL_NONE  # type: ignore[union-attr]
    rec_path = storage.recovery_state_dir(rig.storage) / f"{tid}.json"
    assert rec_path.is_file()

    # recuperar_transicion_interrumpida NO debe borrar el registro mientras el aborto esté en JOURNAL_NONE
    res = recovery.recuperar_transicion_interrumpida(rig.storage)
    assert res.requiere_dueno is True
    assert rec_path.is_file(), "el registro de recuperación no debe borrarse mientras el aborto sigue en JOURNAL_NONE"
    _ = pr_mod  # mantiene referencia explícita


def test_cr_reintento_journal_none_exige_journal_none_revision_y_testigo(tmp_path: pathlib.Path) -> None:
    """CR #15: reintentar desde JOURNAL_NONE rechaza si el journal fue restaurado a PENDING o tiene revisión incorrecta."""
    from sky_claw.local.frozen_runtime import pending_reconciliation as pr_mod

    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    journal_pending_bytes = storage.transition_journal_path(rig.storage).read_bytes()
    confs = _confirmaciones_desde_disco(rig)

    orig_avanzar = pr_mod.avanzar_estado_aborto_locked

    def _crash_antes_completo(
        root_p: pathlib.Path, actual: RegistroAbortoPendiente, nuevo: RegistroAbortoPendiente
    ) -> RegistroAbortoPendiente:
        if nuevo.estado is EstadoAbortoPendiente.COMPLETO:
            raise RuntimeError("crash antes de COMPLETO")
        return orig_avanzar(root_p, actual, nuevo)

    pr_mod.avanzar_estado_aborto_locked = _crash_antes_completo  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="COMPLETO"):
            abortar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                identidad=dueno,
                motivo="cr15",
                confirmaciones=confs,
                probes=_probes_quiescentes(),
            )
    finally:
        pr_mod.avanzar_estado_aborto_locked = orig_avanzar  # type: ignore[assignment]

    assert registro_de_aborto(rig.storage, tid).estado is EstadoAbortoPendiente.JOURNAL_NONE  # type: ignore[union-attr]

    # Simular backup que restaura el journal PENDING viejo
    storage.transition_journal_path(rig.storage).write_bytes(journal_pending_bytes)
    with pytest.raises(AbortoPendienteInvalidoError, match="JOURNAL_NONE"):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=dueno,
            motivo="cr15",
            confirmaciones=confs,
            probes=_probes_quiescentes(),
        )
    # Sigue en JOURNAL_NONE (no avanzó a COMPLETO)
    assert registro_de_aborto(rig.storage, tid).estado is EstadoAbortoPendiente.JOURNAL_NONE  # type: ignore[union-attr]


def test_cr_verificar_superficies_sella_la_misma_lectura_que_verifico(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR #12: _verificar_superficies_contra_snapshot realiza una única lectura por superficie y sella esos bytes."""
    from sky_claw.local.frozen_runtime import pending_reconciliation as pr_mod

    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    registro_rec = recovery.cargar_registro(rig.storage, tid)
    assert registro_rec is not None
    sup_cfg = next(s for s in registro_rec.superficies if s.kind == "config")
    # Restaurar config a su snapshot para que pase la verificación
    assert sup_cfg.bytes_previos() is not None
    rig.config.write_bytes(sup_cfg.bytes_previos())  # type: ignore[arg-type]

    lecturas = 0
    orig_read = pathlib.Path.read_bytes

    def _contar_lecturas(self: pathlib.Path) -> bytes:
        nonlocal lecturas
        if os.path.normcase(os.path.abspath(os.fspath(self))) == os.path.normcase(
            os.path.abspath(os.fspath(rig.config))
        ):
            lecturas += 1
        return orig_read(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _contar_lecturas)
    obs = pr_mod._verificar_superficies_contra_snapshot((sup_cfg,), raiz_evidencia=rig.storage, registro=registro_rec)
    assert lecturas == 1, f"esperaba 1 única lectura de config.toml, observadas={lecturas}"
    assert obs["config"]["sha256"] == sup_cfg.sha256


# ===========================================================================
# 10. Reconciliación final: D8 en CREATE/binder, os.stat (Py 3.14+), state/ padre y no-regular
# ===========================================================================


def test_d8_create_y_binder_con_aprobacion_consumida_rechazan_store_abortos_ausente(tmp_path: pathlib.Path) -> None:
    """D8 no depende sólo de approval.py: CREATE y binder con aprobación ya consumida rechazan store ausente."""
    from sky_claw.local.frozen_runtime.journal import (
        AprobacionConsumida,
        PropuestaTransicion,
        ProyeccionScope,
        _crear_pendiente_locked,
    )

    rig, env_rig = _limpiar_rig_hitl(tmp_path)
    dueno = _identidad_dueno()
    tid1, _ = _emitir_y_dejar_pending(rig, env_rig, identidad=dueno)
    j1 = cargar_journal_transicion(rig.storage).journal
    assert j1 is not None
    reg = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid1,
        identidad=dueno,
        motivo="d8 create",
        confirmaciones=_confirmaciones_desde_disco(rig),
        probes=_probes_quiescentes(),
    )
    assert reg.estado is EstadoAbortoPendiente.COMPLETO

    # Borrar state/aborted_transitions/ dejando bootstrap NONE sin testigo
    shutil.rmtree(storage.aborted_transitions_state_dir(rig.storage))

    tid_nuevo = str(uuid.uuid4())
    assert (
        j1.operation is not None
        and j1.source_activation is not None
        and j1.source_activation_digest is not None
        and j1.target_generation_id is not None
        and j1.target_clone_id is not None
        and j1.target_runtime_setup_id is not None
        and j1.approval_scope_digest is not None
        and j1.evidence_digests is not None
        and j1.expected_effective_path is not None
    )
    prop = PropuestaTransicion(
        transition_id=tid_nuevo,
        operation=j1.operation,
        source_activation=j1.source_activation,
        source_activation_digest=j1.source_activation_digest,
        target_generation_id=j1.target_generation_id,
        target_clone_id=j1.target_clone_id,
        target_runtime_setup_id=j1.target_runtime_setup_id,
        candidate_id=j1.candidate_id,
        approval_scope_digest=j1.approval_scope_digest,
        evidence_digests=j1.evidence_digests,
        expected_effective_path=j1.expected_effective_path,
        created_at_ns=j1.created_at_ns + 1,
    )
    aprob = AprobacionConsumida(
        approval_id="00000000-0000-4000-8000-000000000099",
        approval_seq=99,
        scope_digest=prop.approval_scope_digest,
        consumed_by_transition_id=tid_nuevo,
        proyeccion=ProyeccionScope(
            operation=j1.operation.value,
            source_activation_digest=j1.source_activation_digest,
            target_generation_id=j1.target_generation_id,
            target_clone_id=j1.target_clone_id,
            target_runtime_setup_id=j1.target_runtime_setup_id,
            candidate_id=j1.candidate_id,
            clone_evidence=j1.evidence_digests.clone_evidence,
            runtime_setup_evidence=j1.evidence_digests.runtime_setup_evidence,
            compatibility_evidence_id=j1.evidence_digests.compatibility_evidence,
            approval_id="00000000-0000-4000-8000-000000000099",
            approval_seq=99,
        ),
    )
    with (
        participant_lock(rig.storage, timeout=1.0) as handle,
        pytest.raises(JournalIdentityError, match="D8"),
    ):
        _crear_pendiente_locked(handle, rig.storage, prop, aprob)


def test_inspeccionar_superficie_distingue_permission_error_de_ausencia_py314(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_inspeccionar_superficie_en_disco usa os.stat: PermissionError es DESCONOCIDO (nunca AUSENTE), incluso en Py 3.14+."""
    objetivo = tmp_path / "protegido.ini"
    objetivo.write_bytes(b"datos")

    orig_stat = os.stat

    def _stat_denegado(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if os.path.normcase(os.path.abspath(os.fspath(path))) == os.path.normcase(  # type: ignore[arg-type]
            os.path.abspath(os.fspath(objetivo))
        ):
            raise PermissionError("acceso denegado en os.stat")
        return orig_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", _stat_denegado)
    # Incluso si Path.exists() devolviera False (comportamiento Python 3.14), os.stat devuelve DESCONOCIDO
    monkeypatch.setattr(pathlib.Path, "exists", lambda _self: False)

    estado, datos, detalle = recovery._inspeccionar_superficie_en_disco(objetivo)
    assert estado is recovery._EstadoDiscoSuperficie.DESCONOCIDO
    assert datos is None
    assert "no se pudo comprobar existencia" in detalle


def test_store_abortos_directorio_no_regular_y_state_padre_rechazado_fallan_cerrado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directorio <tid>.json en state/aborted_transitions/ → FAIL_CLOSED; state/ padre rechazado → AbortoCorruptoError."""
    from sky_claw.local.frozen_runtime.journal import VeredictoReconciliacion, reconciliar_journal
    from sky_claw.local.frozen_runtime.pending_abort_store import cargar_abortos

    rig, _ = _limpiar_rig_hitl(tmp_path)
    dir_abortos = storage.aborted_transitions_state_dir(rig.storage)
    dir_abortos.mkdir(parents=True, exist_ok=True)
    # 1. Directorio no-regular con nombre <uuid>.json
    (dir_abortos / f"{uuid.uuid4()}.json").mkdir()
    rec = reconciliar_journal(rig.storage)
    assert rec.veredicto is VeredictoReconciliacion.FAIL_CLOSED
    assert rec.punto_crash == "ABORT_STORE"

    # 2. Admisión fallida en root/state aunque aborted_transitions/ no exista
    shutil.rmtree(dir_abortos)
    orig_admitir = storage.admitir_directorio_storage

    def _rechazar_state(ruta: pathlib.Path) -> object:
        if os.path.normcase(os.path.abspath(os.fspath(ruta))) == os.path.normcase(
            os.path.abspath(os.fspath(storage.state_dir(rig.storage)))
        ):
            return dataclasses.replace(
                orig_admitir(ruta),
                state=storage.StorageAdmissionState.REJECTED,
                message="state redirigido simulado",
            )
        return orig_admitir(ruta)

    monkeypatch.setattr(storage, "admitir_directorio_storage", _rechazar_state)
    with pytest.raises(AbortoCorruptoError, match="state/"):
        cargar_abortos(rig.storage)
