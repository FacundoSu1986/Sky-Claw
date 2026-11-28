"""P7-F Incremento 2B — recuperación hacia adelante de transiciones interrumpidas (C3/C4/C6/C7).

ADR 0012 (§35.3, §36.9, §37.5, §38.3, §48, §49) · FR-01..FR-08.

Cubre la matriz completa de verificación:
- C3 (bind aplicado, Desired atrás): bootstrap y con historial → CAS active.json → POST → FINALIZE.
- C4 (Desired y Effective alineados, antes de POST): POST idempotente → FINALIZE.
- C6 (reserva F1a presente): reutiliza la secuencia reservada sin duplicar ni renumerar → POST → FINALIZE.
- C7 (FINALIZED inmutable presente con journal PENDING): completa ÚNICAMENTE F2 (rotación a NONE)
  sin recrear FINALIZED ni fabricar POST nuevo; idempotente ante reintentos múltiples.
- Rechazos fail-closed: Effective indeterminado/divergente, Clone inválido, POST fallido,
  reserva ajena/corrupta/seq incorrecta, C7 con evidencia divergente, aprobación incorrecta,
  revisión CAS obsoleta, autoridad modificada durante observación del oráculo.
- Idempotencia ante nuevos crashes: tras POST antes de reservar, tras reservar antes de FINALIZED,
  tras FINALIZED antes de F2, tras F2 antes de retirar registro de recuperación.
- Compatibilidad con Incremento 2A: aborto incompleto (D4), transition_id abortado (veto),
  store de abortos corrupto, C6/C7 siguen rechazados por aborto.
- Continuidad operativa: nueva transición START_NEXT y FINALIZE legítimos tras recuperación válida.
- Integración Sesión HITL + Telegram ``/gm_completar_pendiente``.
- Anclas AST: escritores de ``JournalState.NONE``, ``surface_census``, ``participant_lock``.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import pathlib
import shutil
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
from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import (
    SesionTransicionGoldenMaster,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    LiveProcessEvidence,
    LiveProcessKind,
    LiveProcessSnapshot,
)
from sky_claw.local.frozen_runtime.errors import (
    AbortoCorruptoError,
    AbortoPendienteInvalidoError,
    AbortoPendienteReservadoError,
    JournalCasError,
    JournalIdentityError,
    RecuperacionAdelanteAutoridadError,
    RecuperacionAdelanteBloqueadaError,
    RecuperacionAdelanteCasError,
    RecuperacionAdelanteError,
    RecuperacionAdelantePostError,
)
from sky_claw.local.frozen_runtime.journal import (
    JournalDisposition,
    _reservar_seq_locked,
    cargar_journal_transicion,
    derivar_historial_finalizados,
)
from sky_claw.local.frozen_runtime.owner_consent import (
    DecisionConsentimiento,
    RegistroConsentimientos,
)
from sky_claw.local.frozen_runtime.pending_abort_store import (
    ConfirmacionSuperficie,
    EstadoAbortoPendiente,
    EstadoSuperficieConfirmada,
)
from sky_claw.local.frozen_runtime.pending_reconciliation import (
    FronteraRecuperacionAdelante,
    ResultadoRecuperacionAdelante,
    abortar_transicion_pendiente,
    completar_transicion_pendiente,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from tests.test_frozen_runtime_p5c1_binder_integrity import _Rig
from tests.test_frozen_runtime_p7_a5_pr3_hitl_wiring import _ExclusionPosix
from tests.test_frozen_runtime_p7f_pending_reconciliation import (
    _OWNER_CHAT_ID,
    _crear_sesion,
    _identidad_dueno,
    _limpiar_rig_hitl,
    _probes_rig,
    _sha,
)

_PAQUETE_FR = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime"


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


def _dejar_en_frontera(
    monkeypatch: pytest.MonkeyPatch,
    rig: _Rig,
    environ: dict[str, str],
    *,
    frontera: str,
    clone_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Ejecuta una transición HITL real e interrumpe exactamente en la frontera pedida (C3/C4/C6/C7).

    No ejecuta rollback sobre las superficies para reproducir un crash duro del proceso
    en esa frontera exacta.
    """
    cid = clone_id or rig.clone_id
    sesion = SesionTransicionGoldenMaster(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_dir,
        clone_id=cid,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=environ,
        probes=_probes_rig(rig),
        registro=RegistroConsentimientos(),
    )
    sol = sesion.preparar(identidad=_identidad_dueno(), entregar=lambda _p: None)
    consent = sesion.decidir_y_emitir(
        solicitud=sol,
        decision=DecisionConsentimiento.APROBAR,
        identidad=_identidad_dueno(),
    )
    assert consent is not None

    class _CrashSimulado(BaseException):
        pass

    # Neutralizamos _rollback para simular muerte abrupta del proceso (kill) en la frontera exacta.
    monkeypatch.setattr(binder, "_rollback", lambda *a, **kw: ())

    if frontera == "C3":
        # Crash justo antes de CAS UPDATE DESIRED (bind en INI + config + environ ya aplicado).
        def _crash_antes_desired(*_a: object, **_kw: object) -> object:
            raise _CrashSimulado("crash en C3 antes de actualizar active.json")

        monkeypatch.setattr(binder, "_escribir_estado_activo_cas_locked", _crash_antes_desired)
    elif frontera == "C4":
        # Crash después de actualizar active.json y antes de POST / FINALIZE.
        def _crash_antes_post(*_a: object, **_kw: object) -> object:
            raise _CrashSimulado("crash en C4 después de active.json y antes de POST")

        monkeypatch.setattr(binder, "assess_effective_runtime", _crash_antes_post)
    elif frontera == "C6":
        # Crash después de F1a (_reservar_seq_locked) y antes de F1b (_persistir_registro_locked).
        def _crash_despues_reserva(*_a: object, **_kw: object) -> object:
            raise _CrashSimulado("crash en C6 después de reservar secuencia F1a")

        monkeypatch.setattr(journal_module, "_persistir_registro_locked", _crash_despues_reserva)
    elif frontera == "C7":
        # Crash después de F1b (_persistir_registro_locked) y antes de F2 (_rotar_journal_locked).
        def _crash_antes_f2(*_a: object, **_kw: object) -> object:
            raise _CrashSimulado("crash en C7 después de FINALIZED F1b y antes de F2")

        monkeypatch.setattr(journal_module, "_rotar_journal_locked", _crash_antes_f2)
    else:
        raise ValueError(f"frontera desconocida: {frontera!r}")

    with pytest.raises((_CrashSimulado, binder.BinderError)):
        sesion.ejecutar(solicitud=sol, consentimiento=consent)

    snap = cargar_journal_transicion(rig.storage)
    assert snap.disposition is JournalDisposition.PENDING_PROMOTION
    assert snap.journal is not None and snap.journal.transition_id is not None
    return snap.journal.transition_id, environ


def _agregar_segundo_clone(rig: _Rig, clone_id: str = "clone-p7-02") -> pathlib.Path:
    """Crea un segundo Clone válido y físicamente independiente desde la misma Generation."""
    segundo_root = rig.clone_root.parent / clone_id
    shutil.copytree(rig.clone_root, segundo_root)
    base_data = json.loads(storage.clone_record_path(rig.storage, rig.clone_id).read_text(encoding="utf-8"))
    base_data["clone_id"] = clone_id
    base_data["root_path"] = str(segundo_root)
    state_module.write_json_atomic(storage.clone_record_path(rig.storage, clone_id), base_data)
    return segundo_root


# ---------------------------------------------------------------------------
# 1. C3 / C4 / C6 / C7 — Recuperación legítima (bootstrap y con historial)
# ---------------------------------------------------------------------------


def test_c3_bootstrap_recupera_hacia_adelante_y_habilita_start_next(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 (bootstrap sin historial): active.json atrás, Effective en destino → CAS active.json → POST → FINALIZE."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    ini_managed = rig.ini.read_bytes()
    cfg_managed = rig.config.read_bytes()
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C3")

    # Verificar estado previo exacto de C3: active.json aún no apunta al destino
    active_antes = state_module.load_frozen_runtime_state(storage.active_state_path(rig.storage))
    assert active_antes.state is None or active_antes.state.desired_clone_id is None

    res = completar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )

    assert isinstance(res, ResultadoRecuperacionAdelante)
    assert res.frontera is FronteraRecuperacionAdelante.C3_DESIRED_PENDIENTE
    assert res.transition_id == tid
    assert res.finalization_seq == 1
    assert res.desired_actualizado is True
    assert res.post_ejecutado is True
    assert res.reserva_reutilizada is False
    assert res.ya_estaba_completada is False

    snap = cargar_journal_transicion(rig.storage)
    assert snap.disposition is JournalDisposition.NONE
    assert snap.journal is not None and snap.journal.last_finalized_transition_id == tid
    hist = derivar_historial_finalizados(rig.storage)
    assert hist.latest is not None and hist.latest.transition_id == tid and hist.latest.finalization_seq == 1
    assert recovery.cargar_registro(rig.storage, tid) is None

    # Continuidad operativa: START_NEXT + FINALIZE legítimos hacia un segundo clone
    _agregar_segundo_clone(rig, "clone-p7-02")
    rig.ini.write_bytes(ini_managed)
    rig.config.write_bytes(cfg_managed)
    env2 = {"SKYRIM_PATH": str(rig.managed_src)}
    sesion2 = SesionTransicionGoldenMaster(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_dir,
        clone_id="clone-p7-02",
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env2,
        probes=_probes_rig(rig),
        registro=RegistroConsentimientos(),
    )
    sol2 = sesion2.preparar(identidad=_identidad_dueno(), entregar=lambda _p: None)
    consent2 = sesion2.decidir_y_emitir(
        solicitud=sol2,
        decision=DecisionConsentimiento.APROBAR,
        identidad=_identidad_dueno(),
    )
    assert consent2 is not None
    res2 = sesion2.ejecutar(solicitud=sol2, consentimiento=consent2)
    assert res2.binder is not None and res2.binder.finalized_record.finalization_seq == 2


def test_c4_bootstrap_recupera_con_post_idempotente_sin_reescribir_desired(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4: active.json ya alineado con destino → POST idempotente → FINALIZE sin reescribir active.json."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    active_bytes_antes = storage.active_state_path(rig.storage).read_bytes()

    res = completar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )

    assert res.frontera is FronteraRecuperacionAdelante.C4_POST_PENDIENTE
    assert res.desired_actualizado is False
    assert res.post_ejecutado is True
    assert res.reserva_reutilizada is False
    assert res.finalization_seq == 1
    assert storage.active_state_path(rig.storage).read_bytes() == active_bytes_antes
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE


def test_c6_reutiliza_reserva_propia_sin_duplicar_secuencia(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C6: reserva F1a propia en state/finalization-seqs/ → reutiliza exactamente esa seq tras POST PASS."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C6")

    reserva_path = storage.finalization_seqs_state_dir(rig.storage) / "1.json"
    assert reserva_path.is_file()
    reserva_antes = reserva_path.read_bytes()

    res = completar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )

    assert res.frontera is FronteraRecuperacionAdelante.C6_RESERVA_PROPIA
    assert res.reserva_reutilizada is True
    assert res.post_ejecutado is True
    assert res.finalization_seq == 1
    assert reserva_path.read_bytes() == reserva_antes
    assert sorted(p.name for p in storage.finalization_seqs_state_dir(rig.storage).glob("*.json")) == ["1.json"]
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE


def test_c7_completa_solo_f2_sin_recrear_finalized_ni_post_y_es_idempotente(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C7: FINALIZED existente + journal PENDING → sólo F2; no toca FINALIZED ni ejecuta POST; reintento idempotente."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C7")

    record_path = storage.transitions_state_dir(rig.storage) / f"{tid}.json"
    assert record_path.is_file()
    record_bytes_antes = record_path.read_bytes()

    # Probar que C7 NO invoca assess_effective_runtime (no fabrica POST nuevo).
    def _prohibido_post(*_a: object, **_kw: object) -> object:
        raise AssertionError("C7 no debe ejecutar POST nuevo ni reemplazar evidencia histórica")

    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.pending_reconciliation.assess_effective_runtime",
        _prohibido_post,
    )

    res1 = completar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )
    assert res1.frontera is FronteraRecuperacionAdelante.C7_ROTACION_F2
    assert res1.post_ejecutado is False
    assert res1.desired_actualizado is False
    assert res1.ya_estaba_completada is False
    assert res1.finalization_seq == 1
    assert record_path.read_bytes() == record_bytes_antes
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE

    # Reintentos múltiples son idempotentes y no duplican nada.
    res2 = completar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )
    assert res2.frontera is FronteraRecuperacionAdelante.C7_ROTACION_F2
    assert res2.ya_estaba_completada is True
    assert res2.finalization_seq == 1
    assert record_path.read_bytes() == record_bytes_antes


# ---------------------------------------------------------------------------
# 2. Rechazos fail-closed: Effective, Clone, POST, Reservas, C7 divergente
# ---------------------------------------------------------------------------


def test_rechaza_effective_indeterminado_y_divergente_sin_mutar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Effective indeterminado o divergente → rechazo fail-closed sin escribir active.json ni FINALIZED."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C3")

    ini_path = rig.mo2_dir / "ModOrganizer.ini"
    ini_original = ini_path.read_bytes()

    # Caso A: Effective divergente (gamePath apunta a Managed Source)
    ini_path.write_text(f"[General]\ngamePath=@ByteArray({rig.managed_src})\n", encoding="utf-8")
    with pytest.raises(RecuperacionAdelantePostError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION
    assert derivar_historial_finalizados(rig.storage).registros == ()

    # Caso B: Effective indeterminado (INI borrado / ilegible)
    ini_path.unlink()
    with pytest.raises(RecuperacionAdelantePostError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION

    # Restaurar INI para comprobar que nada se corrompió
    ini_path.write_bytes(ini_original)


def test_rechaza_clone_invalido_y_post_fallido(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clone corrompido o POST fallido (ej. proceso MO2 vivo) → no finaliza."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    # Proceso vivo impide quiescencia / POST
    procesos_vivos = LiveProcessSnapshot(
        processes=(
            LiveProcessEvidence(
                kind=LiveProcessKind.MO2,
                pid=4321,
                image_path=str(rig.mo2_dir / "ModOrganizer.exe"),
            ),
        ),
        indeterminate=False,
    )
    with pytest.raises(RecuperacionAdelanteBloqueadaError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig, procesos=procesos_vivos),
        )

    # Corromper archivo crítico del Clone → verificar_runtime_clone falla
    (rig.clone_root / "SkyrimSE.exe").write_bytes(b"CORRUPTO")
    with pytest.raises(RecuperacionAdelantePostError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION
    assert derivar_historial_finalizados(rig.storage).registros == ()


def test_rechaza_reserva_ajena_y_reserva_corrupta_sin_mutar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserva huérfana de otra transición o archivo de reserva corrupto → rechazo tipado sin mutaciones."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    # Reserva ajena en seq=1
    ajeno_tid = str(uuid.uuid4())
    with participant_lock(rig.storage, timeout=5.0) as handle:
        _reservar_seq_locked(handle, rig.storage, seq=1, transition_id=ajeno_tid)

    with pytest.raises((RecuperacionAdelanteAutoridadError, JournalIdentityError)):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION
    assert derivar_historial_finalizados(rig.storage).registros == ()

    # Reserva propia con secuencia incorrecta (seq=2 cuando next_seq=1) → prohibido duplicar o renumerar
    (storage.finalization_seqs_state_dir(rig.storage) / "1.json").unlink()
    with participant_lock(rig.storage, timeout=5.0) as handle:
        _reservar_seq_locked(handle, rig.storage, seq=2, transition_id=tid)
    with pytest.raises((RecuperacionAdelanteAutoridadError, JournalIdentityError)):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert not (storage.finalization_seqs_state_dir(rig.storage) / "1.json").exists()
    (storage.finalization_seqs_state_dir(rig.storage) / "2.json").unlink()

    # Reserva corrupta
    (storage.finalization_seqs_state_dir(rig.storage) / "1.json").write_text("{corrupto", encoding="utf-8")
    with pytest.raises(RecuperacionAdelanteError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )


def test_c7_evidencia_divergente_falla_cerrado(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C7 con registro FINALIZED cuyo target_clone_id/approval diverge del PENDING → FAIL_CLOSED sin rotar."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C7")

    rec_path = storage.transitions_state_dir(rig.storage) / f"{tid}.json"
    data = json.loads(rec_path.read_text(encoding="utf-8"))
    data["target_clone_id"] = "clone-otro-divergente"
    rec_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with pytest.raises((RecuperacionAdelanteAutoridadError, JournalIdentityError)):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION


# ---------------------------------------------------------------------------
# 3. Idempotencia ante nuevos crashes en la propia recuperación (FR-07)
# ---------------------------------------------------------------------------


def test_reintentos_tras_crash_en_cada_frontera_de_recuperacion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash tras reservar (F1a), tras FINALIZED (F1b), y tras F2 antes de borrar recovery → todos recuperan."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    # 1) Crash durante recuperación después de F1a (reserva persistida, antes de F1b)
    with monkeypatch.context() as mp:
        mp.setattr(
            journal_module,
            "_persistir_registro_locked",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash tras F1a en recovery")),
        )
        with pytest.raises(RuntimeError, match="crash tras F1a"):
            completar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                mo2_instance_dir=rig.mo2_dir,
                managed_source_root=rig.managed_src,
                config_path=rig.config,
                environ=env,
                probes=_probes_rig(rig),
            )
    assert (storage.finalization_seqs_state_dir(rig.storage) / "1.json").is_file()
    assert not (storage.transitions_state_dir(rig.storage) / f"{tid}.json").is_file()

    # 2) Reintento: ahora crashea después de F1b (FINALIZED persistido, antes de F2)
    with monkeypatch.context() as mp:
        mp.setattr(
            journal_module,
            "_rotar_journal_locked",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash tras F1b en recovery")),
        )
        with pytest.raises(RuntimeError, match="crash tras F1b"):
            completar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                mo2_instance_dir=rig.mo2_dir,
                managed_source_root=rig.managed_src,
                config_path=rig.config,
                environ=env,
                probes=_probes_rig(rig),
            )
    assert (storage.transitions_state_dir(rig.storage) / f"{tid}.json").is_file()
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION

    # 3) Reintento: ahora F2 completa pero falla la limpieza best-effort de state/recovery/<tid>.json
    with monkeypatch.context() as mp:
        mp.setattr(
            recovery,
            "eliminar_registro_locked",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("handle abierto al limpiar recovery")),
        )
        res_f2 = completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert res_f2.frontera is FronteraRecuperacionAdelante.C7_ROTACION_F2
    assert res_f2.registro_recuperacion_retirado is False
    assert recovery.cargar_registro(rig.storage, tid) is not None

    # 4) Reintento final: limpia recovery de forma idempotente sin duplicar nada
    res_limpio = completar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )
    assert res_limpio.ya_estaba_completada is True
    assert res_limpio.registro_recuperacion_retirado is True
    assert recovery.cargar_registro(rig.storage, tid) is None


# ---------------------------------------------------------------------------
# 4. Autoridad, CAS, Concurrencia (FR-01, FR-06)
# ---------------------------------------------------------------------------


def test_rechaza_aprobacion_incorrecta_y_cas_obsoleto(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión CAS obsoleta o tombstone de aprobación incoherente → rechazo sin mutar."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    # Revisión CAS obsoleta
    with pytest.raises((RecuperacionAdelanteCasError, JournalCasError)):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
            expected_journal_revision=999,
        )

    # Aprobación incorrecta (alterar consumed_approval_id en el journal)
    j_path = storage.transition_journal_path(rig.storage)
    j_data = json.loads(j_path.read_text(encoding="utf-8"))
    j_data["consumed_approval_id"] = str(uuid.uuid4())
    j_path.write_text(json.dumps(j_data, indent=2), encoding="utf-8")

    with pytest.raises(RecuperacionAdelanteAutoridadError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )


def test_rechaza_si_autoridad_cambia_durante_observacion_oraculo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-06: si el estado durable muta mientras el oráculo corre fuera del lock, falla cerrado."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    original_assess = __import__(
        "sky_claw.local.frozen_runtime.pending_reconciliation", fromlist=["assess_effective_runtime"]
    ).assess_effective_runtime
    mutado = {"hecho": False}

    def _assess_con_mutacion_concurrente(*args: object, **kwargs: object) -> object:
        res = original_assess(*args, **kwargs)
        if not mutado["hecho"]:
            mutado["hecho"] = True
            # Otro escritor reserva la secuencia mientras observábamos fuera del lock
            with participant_lock(rig.storage, timeout=5.0) as h:
                _reservar_seq_locked(h, rig.storage, seq=1, transition_id=tid)
        return res

    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.pending_reconciliation.assess_effective_runtime",
        _assess_con_mutacion_concurrente,
    )

    with pytest.raises((RecuperacionAdelanteCasError, JournalCasError)):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    # No se publicó FINALIZED sobre evidencia obsoleta
    assert derivar_historial_finalizados(rig.storage).registros == ()


# ---------------------------------------------------------------------------
# 5. Compatibilidad con Incremento 2A (FR-08)
# ---------------------------------------------------------------------------


def test_compatibilidad_aborto_incompleto_veto_y_store_corrupto(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4 aborto incompleto, transition_id abortado, store corrupto, y rechazo C6/C7 en aborto."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    # 1) Store de abortos corrupto → FAIL_CLOSED
    abort_dir = storage.aborted_transitions_state_dir(rig.storage)
    abort_dir.mkdir(parents=True, exist_ok=True)
    corrupto = abort_dir / f"{uuid.uuid4()}.json"
    corrupto.write_text("{corrupto", encoding="utf-8")
    with pytest.raises((AbortoCorruptoError, RecuperacionAdelanteError)):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    corrupto.unlink()

    # 1b) Aborto incompleto de OTRA transición (D4) bloquea incluso si t_id no tiene registro de aborto propio
    from sky_claw.local.frozen_runtime.pending_abort_store import (
        ModoAbortoPendiente,
        construir_registro_aborto,
        crear_intencion_aborto_locked,
    )
    from sky_claw.local.frozen_runtime.root_lock import canonical_root_key

    otro_tid = str(uuid.uuid4())
    reg_otro = construir_registro_aborto(
        root_id=canonical_root_key(rig.storage)[0],
        transition_id=otro_tid,
        journal_revision=1,
        approval_id=str(uuid.uuid4()),
        approval_seq=99,
        scope_digest="0" * 64,
        modo=ModoAbortoPendiente.CON_REGISTRO_RECUPERACION,
        recovery_record_digest="1" * 64,
        confirmaciones={},
        superficies_antes={},
        superficies_despues=None,
        superficies_verificadas_digest=None,
        propietario_sujeto=str(_OWNER_CHAT_ID),
        propietario_superficie="telegram",
        propietario_sesion="s1",
        propietario_mecanismo="telegram_chat_id",
        motivo="aborto ajeno incompleto D4",
        estado=EstadoAbortoPendiente.INTENCION,
        creado_en_ns=1,
        actualizado_en_ns=1,
    )
    with participant_lock(rig.storage, timeout=5.0):
        crear_intencion_aborto_locked(rig.storage, reg_otro)
    with pytest.raises(RecuperacionAdelanteBloqueadaError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    (abort_dir / f"{otro_tid}.json").unlink()

    # 2) Aborto incompleto (D4) bloquea recuperación hacia adelante
    ini_sha = _sha((rig.mo2_dir / "ModOrganizer.ini").read_bytes())
    cfg_sha = _sha(rig.config.read_bytes())
    act_sha = _sha(storage.active_state_path(rig.storage).read_bytes())
    confs = {
        "mo2_ini": ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.PRESENTE, sha256=ini_sha),
        "config": ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.PRESENTE, sha256=cfg_sha),
        "active_json": ConfirmacionSuperficie(estado=EstadoSuperficieConfirmada.PRESENTE, sha256=act_sha),
    }
    with monkeypatch.context() as mp:
        mp.setattr(
            "sky_claw.local.frozen_runtime.pending_reconciliation._abortar_pendiente_locked",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("interrupcion D4")),
        )
        with pytest.raises(RuntimeError, match="interrupcion D4"):
            abortar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                identidad=_identidad_dueno(),
                motivo="aborto interrumpido",
                confirmaciones=confs,
                probes=_probes_rig(rig),
            )
    with pytest.raises(RecuperacionAdelanteBloqueadaError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )

    # 3) Completar el aborto y verificar que transition_id abortado no puede recuperarse hacia adelante
    reg_ab = abortar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        identidad=_identidad_dueno(),
        motivo="completar aborto",
        confirmaciones=confs,
        probes=_probes_rig(rig),
    )
    assert reg_ab.estado is EstadoAbortoPendiente.COMPLETO
    with pytest.raises((RecuperacionAdelanteBloqueadaError, RecuperacionAdelanteAutoridadError)):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )


def test_c6_y_c7_siguen_rechazados_por_aborto(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C6 y C7 siguen rechazados por abortar_transicion_pendiente y recuperables hacia adelante."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C6")

    with pytest.raises(AbortoPendienteReservadoError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=_identidad_dueno(),
            motivo="intento ilegal en C6",
            probes=_probes_rig(rig),
        )

    # Recuperar hacia adelante SÍ completa C6
    res = completar_transicion_pendiente(
        rig.storage,
        transition_id=tid,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )
    assert res.frontera is FronteraRecuperacionAdelante.C6_RESERVA_PROPIA
    assert res.finalization_seq == 1

    # Con FINALIZED presente, aborto sigue rechazado
    with pytest.raises(AbortoPendienteInvalidoError):
        abortar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            identidad=_identidad_dueno(),
            motivo="intento ilegal post-FINALIZED",
            probes=_probes_rig(rig),
        )


# ---------------------------------------------------------------------------
# 6. Sesión HITL + Telegram /gm_completar_pendiente + Anclas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_gm_completar_pendiente_resuelve_c6(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comando Telegram /gm_completar_pendiente completa hacia adelante C6 con identidad verificada."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C6")

    enviados: list[str] = []

    class _SenderFake:
        async def send(self, chat_id: int, text: str, **_kw: object) -> object:
            enviados.append(text)
            return type("Msg", (), {"message_id": len(enviados)})()

    objetivo = ObjetivoTransicionGoldenMaster(
        storage_root=rig.storage,
        mo2_instance_dir=rig.mo2_dir,
        clone_id=rig.clone_id,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env,
        probes=_probes_rig(rig),
    )
    coord = CoordinadorTelegramGoldenMaster(
        sender=_SenderFake(),  # type: ignore[arg-type]
        operador_autorizado=_OWNER_CHAT_ID,
        objetivo=objetivo,
    )
    try:
        tarea = await coord.iniciar_comando(
            {
                "message": {
                    "from": {"id": _OWNER_CHAT_ID},
                    "chat": {"id": _OWNER_CHAT_ID, "type": "private"},
                    "text": f"/gm_completar_pendiente {tid}",
                }
            }
        )
        assert tarea is not None
        await tarea
    finally:
        await coord.cerrar()

    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE
    assert any("completada" in msg.lower() and tid in msg for msg in enviados)


def test_reconciliar_journal_sigue_siendo_readonly_en_c6_y_c7(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-05: reconciliar_journal no muta el journal ni las reservas en C7."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C7")

    j_antes = storage.transition_journal_path(rig.storage).read_bytes()
    ver = journal_module.reconciliar_journal(rig.storage)
    assert ver.punto_crash == "C7"
    assert storage.transition_journal_path(rig.storage).read_bytes() == j_antes
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.PENDING_PROMOTION


def test_ancla_ast_c7_rotacion_f2_no_llama_persistir_registro_ni_reservar_seq() -> None:
    """Ancla AST: _completar_rotacion_f2_locked jamás crea reservas ni reescribe registros FINALIZED."""
    fuente = (_PAQUETE_FR / "journal.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente, filename="journal.py")
    func = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "_completar_rotacion_f2_locked")
    llamadas = {
        (n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    assert "_rotar_journal_locked" in llamadas
    assert "_persistir_registro_locked" not in llamadas
    assert "_reservar_seq_locked" not in llamadas


def test_c6_con_historial_previo_preserva_cadena_seq_2(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Transición posterior (START_NEXT) interrumpida en C6 preserva seq=2 y finalized_from_transition_id=T1."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    ini_managed = rig.ini.read_bytes()
    cfg_managed = rig.config.read_bytes()

    # Primera transición completa (T1, seq=1)
    sesion1 = _crear_sesion(rig, env)
    sol1 = sesion1.preparar(identidad=_identidad_dueno(), entregar=lambda _p: None)
    consent1 = sesion1.decidir_y_emitir(
        solicitud=sol1,
        decision=DecisionConsentimiento.APROBAR,
        identidad=_identidad_dueno(),
    )
    assert consent1 is not None
    res1 = sesion1.ejecutar(solicitud=sol1, consentimiento=consent1)
    assert res1.binder is not None
    t1_id = res1.binder.finalized_record.transition_id
    assert res1.binder.finalized_record.finalization_seq == 1

    # Segunda transición (T2) hacia clone-p7-02 interrumpida en C6 (reserva seq=2)
    _agregar_segundo_clone(rig, "clone-p7-02")
    rig.ini.write_bytes(ini_managed)
    rig.config.write_bytes(cfg_managed)
    env2 = {"SKYRIM_PATH": str(rig.managed_src)}
    with monkeypatch.context() as mp:
        t2_id, env2 = _dejar_en_frontera(mp, rig, env2, frontera="C6", clone_id="clone-p7-02")

    assert (storage.finalization_seqs_state_dir(rig.storage) / "2.json").is_file()
    res2 = completar_transicion_pendiente(
        rig.storage,
        transition_id=t2_id,
        mo2_instance_dir=rig.mo2_dir,
        managed_source_root=rig.managed_src,
        config_path=rig.config,
        environ=env2,
        probes=_probes_rig(rig),
    )
    assert res2.frontera is FronteraRecuperacionAdelante.C6_RESERVA_PROPIA
    assert res2.reserva_reutilizada is True
    assert res2.finalization_seq == 2
    assert res2.finalized_record.finalized_from_transition_id == t1_id
    assert cargar_journal_transicion(rig.storage).disposition is JournalDisposition.NONE


# ---------------------------------------------------------------------------
# 7. H1 (Binding completo con witness) y H2 (Revalidación causal + Codex P1s)
# ---------------------------------------------------------------------------


def test_h1_rechaza_journal_c4_c7_y_mo2_divergentes_de_proyeccion_witness(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: journal/FINALIZED con mismo approval_id/seq/scope_digest pero campos divergentes del witness → rechazo."""
    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    # 1) C4: alterar evidence_digests.clone_evidence manteniendo approval_id/seq/scope_digest idénticos
    j_path = storage.transition_journal_path(rig.storage)
    j_orig = j_path.read_bytes()
    j_data = json.loads(j_orig.decode("utf-8"))
    j_data["evidence_digests"]["clone_evidence"] = "f" * 64
    j_path.write_text(json.dumps(j_data, indent=2), encoding="utf-8")

    with pytest.raises(RecuperacionAdelanteAutoridadError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert derivar_historial_finalizados(rig.storage).registros == ()
    j_path.write_bytes(j_orig)

    # 2) Instancia MO2 distinta de la autorizada en approval_provenance (inst=...)
    otra_mo2 = tmp_path / "MO2_otra_instancia"
    shutil.copytree(rig.mo2_dir, otra_mo2)
    with pytest.raises(RecuperacionAdelanteAutoridadError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=otra_mo2,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=_probes_rig(rig),
        )
    assert derivar_historial_finalizados(rig.storage).registros == ()

    # 3) C7: PENDING y FINALIZED coinciden entre sí en candidate_id alterado, pero divergen del witness
    rig_c7, env_c7 = _limpiar_rig_hitl(tmp_path / "c7")
    with monkeypatch.context() as mp:
        tid_c7, env_c7 = _dejar_en_frontera(mp, rig_c7, env_c7, frontera="C7")

    j_c7_path = storage.transition_journal_path(rig_c7.storage)
    r_c7_path = storage.transitions_state_dir(rig_c7.storage) / f"{tid_c7}.json"
    j_c7 = json.loads(j_c7_path.read_text(encoding="utf-8"))
    r_c7 = json.loads(r_c7_path.read_text(encoding="utf-8"))
    j_c7["candidate_id"] = "cand_alterado_divergente_del_witness"
    r_c7["candidate_id"] = "cand_alterado_divergente_del_witness"
    j_c7_path.write_text(json.dumps(j_c7, indent=2), encoding="utf-8")
    r_c7_path.write_text(json.dumps(r_c7, indent=2), encoding="utf-8")

    with pytest.raises(RecuperacionAdelanteAutoridadError):
        completar_transicion_pendiente(
            rig_c7.storage,
            transition_id=tid_c7,
            mo2_instance_dir=rig_c7.mo2_dir,
            managed_source_root=rig_c7.managed_src,
            config_path=rig_c7.config,
            environ=env_c7,
            probes=_probes_rig(rig_c7),
        )
    assert cargar_journal_transicion(rig_c7.storage).disposition is JournalDisposition.PENDING_PROMOTION


def test_h2_revalidacion_causal_detecta_deriva_desired_clone_ini_y_mo2_inactiva(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2 + Codex P1s: deriva fuera de lock en desired_clone_id, clone record, [customExecutables] o MO2 inactiva."""
    from sky_claw.local.frozen_runtime.effective_runtime.models import (
        Mo2InstanceIdentityObservation,
        Mo2InstanceIdentityState,
        Mo2RegistryState,
    )

    rig, env = _limpiar_rig_hitl(tmp_path)
    with monkeypatch.context() as mp:
        tid, env = _dejar_en_frontera(mp, rig, env, frontera="C4")

    # A) _huella_estado_durable debe distinguir desired_clone_id distinto dentro de la misma Generation
    with participant_lock(rig.storage, timeout=5.0):
        huella_1 = journal_module._observar_estado_durable(rig.storage).huella
    act_path = storage.active_state_path(rig.storage)
    act_orig = act_path.read_bytes()
    act_data = json.loads(act_orig.decode("utf-8"))
    act_data["desired_clone_id"] = "clone-p7-otro-en-misma-gen"
    act_path.write_text(json.dumps(act_data, indent=2), encoding="utf-8")
    with participant_lock(rig.storage, timeout=5.0):
        huella_2 = journal_module._observar_estado_durable(rig.storage).huella
    act_path.write_bytes(act_orig)
    assert huella_2 != huella_1

    # B) Instancia MO2 inactiva en registro global (Codex #4724466619) → rechazo sin finalizar
    probes_inactivos = dataclasses.replace(
        _probes_rig(rig),
        mo2_instance=lambda _d: Mo2InstanceIdentityObservation(
            state=Mo2InstanceIdentityState.INACTIVE,
            instance_dir=str(rig.mo2_dir),
            registry_state=Mo2RegistryState.OBSERVED,
            registry_current_instance="OtraInstanciaGlobal",
            detail="instancia global cambiada por el operador",
        ),
    )
    with pytest.raises(RecuperacionAdelantePostError):
        completar_transicion_pendiente(
            rig.storage,
            transition_id=tid,
            mo2_instance_dir=rig.mo2_dir,
            managed_source_root=rig.managed_src,
            config_path=rig.config,
            environ=env,
            probes=probes_inactivos,
        )
    assert derivar_historial_finalizados(rig.storage).registros == ()

    # C) Mutación concurrente de [customExecutables] en ModOrganizer.ini tras POST (Codex #4724466614)
    orig_assess = __import__(
        "sky_claw.local.frozen_runtime.pending_reconciliation", fromlist=["assess_effective_runtime"]
    ).assess_effective_runtime
    ini_orig = rig.ini.read_bytes()

    def _assess_muta_custom_execs(*args: object, **kwargs: object) -> object:
        res = orig_assess(*args, **kwargs)
        rig.ini.write_bytes(ini_orig + b"\n[customExecutables]\nsize=1\n1\\binary=C:/otro/skse64_loader.exe\n")
        return res

    with monkeypatch.context() as mp:
        mp.setattr(
            "sky_claw.local.frozen_runtime.pending_reconciliation.assess_effective_runtime",
            _assess_muta_custom_execs,
        )
        with pytest.raises(RecuperacionAdelantePostError):
            completar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                mo2_instance_dir=rig.mo2_dir,
                managed_source_root=rig.managed_src,
                config_path=rig.config,
                environ=env,
                probes=_probes_rig(rig),
            )
    rig.ini.write_bytes(ini_orig)
    assert derivar_historial_finalizados(rig.storage).registros == ()

    # D) Deriva del registro del Clone durante observación fuera del lock (H2)
    clone_rec_path = storage.clone_record_path(rig.storage, rig.clone_id)
    clone_rec_orig = clone_rec_path.read_bytes()

    def _assess_muta_clone_record(*args: object, **kwargs: object) -> object:
        res = orig_assess(*args, **kwargs)
        d = json.loads(clone_rec_orig.decode("utf-8"))
        orig_setup = str(d["verified_runtime_setup_id"])
        ultimo = "0" if orig_setup[-1] != "0" else "1"
        nuevo_setup = orig_setup[:-1] + ultimo
        d["intended_runtime_setup_id"] = nuevo_setup
        d["verified_runtime_setup_id"] = nuevo_setup
        clone_rec_path.write_text(json.dumps(d, indent=2), encoding="utf-8")
        return res

    with monkeypatch.context() as mp:
        mp.setattr(
            "sky_claw.local.frozen_runtime.pending_reconciliation.assess_effective_runtime",
            _assess_muta_clone_record,
        )
        with pytest.raises(RecuperacionAdelanteCasError):
            completar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                mo2_instance_dir=rig.mo2_dir,
                managed_source_root=rig.managed_src,
                config_path=rig.config,
                environ=env,
                probes=_probes_rig(rig),
            )
    clone_rec_path.write_bytes(clone_rec_orig)
    assert derivar_historial_finalizados(rig.storage).registros == ()

    # E) Deriva de la autoridad de aprobación (tombstone) durante observación fuera del lock (H2)
    tomb_files = list(storage.approvals_state_dir(rig.storage).glob("*.json"))
    assert len(tomb_files) == 1
    tomb_path = tomb_files[0]
    tomb_orig = tomb_path.read_bytes()

    def _assess_muta_tombstone(*args: object, **kwargs: object) -> object:
        res = orig_assess(*args, **kwargs)
        td = json.loads(tomb_orig.decode("utf-8"))
        td["terminal_at_ns"] = int(td["terminal_at_ns"]) + 1
        tomb_path.write_text(json.dumps(td, indent=2), encoding="utf-8")
        return res

    with monkeypatch.context() as mp:
        mp.setattr(
            "sky_claw.local.frozen_runtime.pending_reconciliation.assess_effective_runtime",
            _assess_muta_tombstone,
        )
        with pytest.raises(RecuperacionAdelanteCasError):
            completar_transicion_pendiente(
                rig.storage,
                transition_id=tid,
                mo2_instance_dir=rig.mo2_dir,
                managed_source_root=rig.managed_src,
                config_path=rig.config,
                environ=env,
                probes=_probes_rig(rig),
            )
    tomb_path.write_bytes(tomb_orig)
    assert derivar_historial_finalizados(rig.storage).registros == ()
