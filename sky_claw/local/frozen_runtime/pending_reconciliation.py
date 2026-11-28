"""Reconciliación productiva de ``PENDING`` por aborto autorizado del propietario (P7-F Inc. 2, PR 1).

Implementa ``abortar_transicion_pendiente`` conforme a ADR 0012 §48 y las directivas D2–D8:
- Autorización explícita del propietario ligada a identidad verificada, storage root,
  ``transition_id``, ``journal_revision``, registro de recuperación (cuando existe),
  huellas/presencia de superficies y motivo.
- Separación conceptual estricta entre autorización de descarte y prueba de autoría.
- Intención durable single-winner en ``state/aborted_transitions/<transition_id>.json``
  antes de mutar cualquier superficie (``INTENCION → SUPERFICIES_VERIFICADAS → JOURNAL_NONE → COMPLETO``).
- Revalidación obligatoria de superficies y precondiciones en cada reintento, incluido
  desde ``SUPERFICIES_VERIFICADAS`` (D2).
- Manejo explícito de fronteras inter-marcador (D3): placeholder vacío de 0 bytes, crash
  tras restaurar una superficie, crash tras rotar el journal a ``NONE`` y crash tras
  retirar el registro de recuperación.
- IP-0 sin registro de recuperación (D6): exige evidencia causal positiva simultánea
  (``mo2auth`` + ``bind`` de ``approval_provenance``, ``active.json`` coherente con
  ``source_activation`` y oráculo Effective Runtime en origen); ante evidencia
  insuficiente, HOLD fail-closed sin mutaciones.
- Rechazo fail-closed de fronteras C6 (reserva presente) y C7 (``FINALIZED`` presente).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from enum import StrEnum

from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime.approval import (
    ApprovalTombstone,
    IssuanceWitness,
    TerminalState,
    derivar_autoridad_aprobacion,
    proyeccion_desde_witness,
)
from sky_claw.local.frozen_runtime.binder_preflight import BinderTransitionPolicy
from sky_claw.local.frozen_runtime.clones import leer_runtime_clone_record, verificar_runtime_clone
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import leer_game_path_de_mo2
from sky_claw.local.frozen_runtime.effective_runtime.models import DesiredRuntime, ObservationState, RuntimeProbes
from sky_claw.local.frozen_runtime.effective_runtime.oracle import (
    assess_effective_runtime,
    default_runtime_probes,
    desired_runtime_from_storage,
)
from sky_claw.local.frozen_runtime.errors import (
    AbortoCorruptoError,
    AbortoIp0EvidenciaInsuficienteError,
    AbortoPendienteInvalidoError,
    AbortoPendienteReservadoError,
    AbortoSuperficieDivergenteError,
    FrozenRuntimeError,
    InvalidGenerationIdError,
    JournalCasError,
    JournalCorruptError,
    JournalIdentityError,
    JournalSchemaError,
    JournalStoreNoInicializadoError,
    RecuperacionAdelanteAutoridadError,
    RecuperacionAdelanteBloqueadaError,
    RecuperacionAdelanteCasError,
    RecuperacionAdelantePostError,
    ScopeBindingError,
    StateCasError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.journal import (
    FinalizedTransitionRecord,
    JournalDisposition,
    JournalLoadResult,
    PostVerifyEvidence,
    PostVerifyResultado,
    ProyeccionScope,
    SourceActivation,
    TransitionJournal,
    _abortar_pendiente_locked,
    _completar_rotacion_f2_locked,
    _finalizar_locked,
    _observar_estado_durable,
    auditar_reservas_finalizacion,
    cargar_journal_transicion,
    derivar_historial_finalizados,
    digest_canonico,
    exigir_proyeccion_ligada,
)
from sky_claw.local.frozen_runtime.mo2_authorization import clave_de_ruta, token_comprometido
from sky_claw.local.frozen_runtime.owner_consent import IdentidadPropietario
from sky_claw.local.frozen_runtime.pending_abort_store import (
    PLACEHOLDER_VACIO,
    ConfirmacionSuperficie,
    EstadoAbortoPendiente,
    EstadoSuperficieConfirmada,
    ModoAbortoPendiente,
    RegistroAbortoPendiente,
    avanzar_estado_aborto_locked,
    cargar_abortos,
    construir_registro_aborto,
    crear_intencion_aborto_locked,
    exigir_transition_id_canonico,
    leer_estado_archivo_aborto,
    registro_de_aborto,
)
from sky_claw.local.frozen_runtime.provisioning import verificar_runtime_setup
from sky_claw.local.frozen_runtime.root_lock import canonical_root_key, participant_lock
from sky_claw.local.frozen_runtime.state import (
    _escribir_estado_activo_cas_locked,
    load_frozen_runtime_state,
    observar_estado_previo,
)
from sky_claw.local.frozen_runtime.storage import active_state_path, recovery_state_dir
from sky_claw.local.frozen_runtime.storage_models import GenerationVerificationState

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class ContextoVerificacionIp0:
    """Contexto necesario para evaluar evidencia causal positiva en IP-0 (sin registro de recuperación, D6).

    ``approval_provenance`` compromete:
    - ``mo2_ini_sha256`` (igualdad byte a byte de ``modorganizer.ini``), instancia e identidad MO2,
      política y runtime saliente (segmento ``mo2auth``);
    - ``config_path`` y el valor parseado ``config.skyrim_path`` (NO igualdad byte a byte de todo
      ``config.toml``), ``source_activation`` y digest del Clone (segmento ``bind``).
    """

    mo2_instance_dir: pathlib.Path
    clone_id: str
    config_path: pathlib.Path
    transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STRICT_PROVIDER_IDLE
    managed_source_root: pathlib.Path | None = None
    outgoing_manual_runtime_root: pathlib.Path | None = None
    stock_source_root: pathlib.Path | None = None
    approved_stock_reference_digest: str | None = None
    approved_stock_reference_digest_file: pathlib.Path | None = None
    environ: Mapping[str, str] | None = None


def _norm(ruta: str | pathlib.Path | None) -> str | None:
    if ruta is None:
        return None
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(ruta))))


def _evidencia_aprobacion_y_dueno(
    storage_path: pathlib.Path,
    transition_id: str,
    identidad: IdentidadPropietario,
) -> tuple[ApprovalTombstone, IssuanceWitness]:
    """Valida identidad verificada y pertenencia al dueño del ``solicitud_binding`` del witness."""
    from sky_claw.local.frozen_runtime.owner_consent import (
        _exigir_identidad_verificada,
    )

    try:
        _exigir_identidad_verificada(identidad)
    except FrozenRuntimeError as exc:
        raise AbortoPendienteInvalidoError(f"identidad del propietario no verificada: {exc}") from exc

    try:
        autoridad = derivar_autoridad_aprobacion(storage_path)
    except (FrozenRuntimeError, OSError) as exc:
        raise AbortoPendienteInvalidoError(f"no se pudo consultar la autoridad de aprobación: {exc}") from exc

    terminales = [
        t
        for t in autoridad.tombstones
        if t.terminal_state is TerminalState.CONSUMED and t.consumed_by_transition_id == transition_id
    ]
    if len(terminales) != 1:
        raise AbortoPendienteInvalidoError(
            f"se esperaba exactamente un tombstone CONSUMED para {transition_id!r} y hay {len(terminales)}"
        )
    tombstone = terminales[0]
    witness = next((w for w in autoridad.witnesses if w.approval_seq == tombstone.approval_seq), None)
    if witness is None:
        raise AbortoPendienteInvalidoError(f"el tombstone de seq {tombstone.approval_seq} no tiene witness de emisión")

    binding = witness.solicitud_binding
    if binding is None:
        raise AbortoPendienteInvalidoError(
            f"la aprobación seq {witness.approval_seq} no tiene dueño durable (solicitud_binding nulo): fail-closed"
        )
    esperado = (binding.get("surface"), binding.get("owner_subject"), binding.get("mecanismo"))
    observado = (identidad.superficie.value, identidad.sujeto, identidad.mecanismo)
    if esperado != observado:
        raise AbortoPendienteInvalidoError(
            "la identidad verificada no corresponde al dueño que autorizó esta aprobación: rechazo sin mutaciones"
        )
    return tombstone, witness


def _exigir_quiescencia_para_aborto(probes: RuntimeProbes, *, momento: str) -> None:
    from sky_claw.local.frozen_runtime.runtime_clone_binder import _exigir_quiescencia

    try:
        _exigir_quiescencia(probes, momento=momento)
    except FrozenRuntimeError as exc:
        raise AbortoPendienteInvalidoError(str(exc)) from exc


def _superficies_persistidas_del_registro(
    registro: recovery.RegistroRecuperacion,
) -> tuple[recovery.SuperficieCapturada, ...]:
    """Todas las superficies persistidas capturadas en el registro de recuperación."""
    return tuple(s for s in registro.superficies if s.kind in recovery._KINDS_PERSISTIDAS and s.path is not None)


def _observar_mapa_superficies(
    superficies: tuple[recovery.SuperficieCapturada, ...],
) -> dict[str, dict[str, object]]:
    return {s.kind: recovery.observar_superficie_en_disco(s) for s in superficies}


def _verificar_superficies_contra_snapshot(
    superficies: tuple[recovery.SuperficieCapturada, ...],
    *,
    raiz_evidencia: pathlib.Path | None = None,
    registro: recovery.RegistroRecuperacion | None = None,
) -> dict[str, dict[str, object]]:
    """Relee TODAS las superficies persistidas bajo su coordinación y exige igualdad exacta con el snapshot (D2).

    Si apareció contenido externo nuevo, preserva copia forense en ``recovery_evidence/<tid>/``
    y detiene la reconciliación sin rotar el journal.
    """
    observadas: dict[str, dict[str, object]] = {}
    for sup in superficies:
        assert sup.path is not None
        ruta = pathlib.Path(sup.path)
        with recovery._coordinacion_de_escritores(sup.kind, ruta):
            # CR #12: una única lectura bajo la coordinación de escritores; se verifica y se sella esa misma observación.
            estado_disco, actuales, err_disco = recovery._inspeccionar_superficie_en_disco(ruta)
            if estado_disco is recovery._EstadoDiscoSuperficie.DESCONOCIDO:
                raise AbortoPendienteInvalidoError(
                    f"no se pudo verificar la superficie '{sup.kind}' ({ruta}) contra el snapshot ({err_disco}): "
                    "se detiene sin rotar el journal (D2)"
                )
            coincide = (estado_disco is recovery._EstadoDiscoSuperficie.AUSENTE and not sup.existed) or (
                estado_disco is recovery._EstadoDiscoSuperficie.PRESENTE
                and sup.existed
                and actuales is not None
                and recovery.huella(actuales) == sup.sha256
            )
            obs: dict[str, object] = {
                "path": str(ruta),
                "estado": (
                    EstadoSuperficieConfirmada.AUSENTE.value
                    if estado_disco is recovery._EstadoDiscoSuperficie.AUSENTE
                    else EstadoSuperficieConfirmada.PRESENTE.value
                ),
                "sha256": None if actuales is None else recovery.huella(actuales),
            }
            if not coincide:
                evidencia_nota = ""
                if (
                    raiz_evidencia is not None
                    and registro is not None
                    and estado_disco is recovery._EstadoDiscoSuperficie.PRESENTE
                ):
                    ev_path, ev_diag = recovery._preservar_evidencia(raiz_evidencia, registro, sup.kind, ruta)
                    evidencia_nota = (
                        f" Evidencia conservada en {ev_path}."
                        if ev_path is not None
                        else f" Evidencia no preservada ({ev_diag})."
                    )
                raise AbortoSuperficieDivergenteError(
                    f"la superficie '{sup.kind}' ({ruta}) ya no coincide con el snapshot previo "
                    f"(observado={obs}, esperado_existed={sup.existed}, esperado_sha256={sup.sha256}): "
                    f"se preserva y se detiene sin rotar el journal (D2).{evidencia_nota}"
                )
            observadas[sup.kind] = obs
    return observadas


def _prevalidar_confirmaciones_contra_disco(
    superficies: tuple[recovery.SuperficieCapturada, ...],
    confirmaciones: Mapping[str, ConfirmacionSuperficie],
) -> None:
    """Antes de persistir INTENCION, verifica que cada superficie coincida con snapshot o con la confirmación."""
    for sup in superficies:
        assert sup.path is not None
        ruta = pathlib.Path(sup.path)
        with recovery._coordinacion_de_escritores(sup.kind, ruta):
            if recovery._esta_en_estado_previo(sup):
                continue
            conf = confirmaciones.get(sup.kind)
            if conf is None:
                raise AbortoSuperficieDivergenteError(
                    f"la superficie '{sup.kind}' ({ruta}) difiere del snapshot y falta confirmación explícita del dueño"
                )
            try:
                actuales: bytes | None = ruta.read_bytes() if ruta.exists() else None
            except OSError as exc:
                raise AbortoPendienteInvalidoError(f"no se pudo leer '{sup.kind}' ({ruta}): {exc}") from exc
            coincide, motivo = recovery._confirmacion_coincide_en_disco(ruta, conf, actuales=actuales)
            if not coincide:
                raise AbortoSuperficieDivergenteError(motivo)


def _verificar_evidencia_positiva_ip0(
    storage_path: pathlib.Path,
    *,
    source_generation_id: str | None,
    source_clone_id: str | None,
    expected_effective_path: str,
    witness: IssuanceWitness,
    contexto_ip0: ContextoVerificacionIp0 | None,
    probes: RuntimeProbes,
) -> dict[str, dict[str, object]]:
    """Verifica evidencia causal positiva en IP-0 sin registro de recuperación (D6).

    Distingue explícitamente:
    - igualdad byte a byte de ``modorganizer.ini`` (acreditada por ``mo2_ini_sha256`` en ``mo2auth``/``bind``);
    - coherencia del valor parseado ``config.skyrim_path`` (NO igualdad byte a byte de todo ``config.toml``);
    - coherencia semántica de ``active.json`` contra ``source_activation`` (bootstrap o par gen/clone);
    - coherencia del Effective Runtime observado en origen (no en destino ni indeterminado).

    Ante cualquier falta de evidencia o divergencia: ``AbortoIp0EvidenciaInsuficienteError`` (HOLD sin mutaciones).
    """
    if contexto_ip0 is None:
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0 (PENDING sin registro de recuperación): se requiere ContextoVerificacionIp0 para "
            "verificar evidencia causal positiva; la ausencia del registro no prueba ausencia de mutaciones (HOLD)"
        )

    provenance_aprobada = witness.scope_payload.get("approval_provenance")
    if not isinstance(provenance_aprobada, str) or token_comprometido(provenance_aprobada) is None:
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: la aprobación consumida no contiene vínculo criptográfico mo2auth/bind (aprobación legacy): "
            "evidencia causal insuficiente para abortar sin snapshot (HOLD sin mutaciones)"
        )

    from sky_claw.local.frozen_runtime.autorizacion_hitl_transicion import derivar_evidencia_transicion

    try:
        evidencia_actual = derivar_evidencia_transicion(
            storage_root=storage_path,
            mo2_instance_dir=contexto_ip0.mo2_instance_dir,
            clone_id=contexto_ip0.clone_id,
            transition_policy=contexto_ip0.transition_policy,
            managed_source_root=contexto_ip0.managed_source_root,
            outgoing_manual_runtime_root=contexto_ip0.outgoing_manual_runtime_root,
            stock_source_root=contexto_ip0.stock_source_root,
            approved_stock_reference_digest=contexto_ip0.approved_stock_reference_digest,
            approved_stock_reference_digest_file=contexto_ip0.approved_stock_reference_digest_file,
            config_path=contexto_ip0.config_path,
            environ=contexto_ip0.environ,
            probes=probes,
        )
    except FrozenRuntimeError as exc:
        raise AbortoIp0EvidenciaInsuficienteError(
            f"IP-0: no se pudo re-derivar evidencia coherente del estado aprobado ({exc}): HOLD sin mutaciones"
        ) from exc

    observados = evidencia_actual.campos_derivados()
    distintos = sorted(k for k, v in observados.items() if witness.scope_payload.get(k) != v)
    if distintos:
        raise AbortoIp0EvidenciaInsuficienteError(
            f"IP-0: el estado observado diverge de lo aprobado por el propietario en {distintos}: "
            "no se puede acreditar que las superficies sigan en el origen aprobado (HOLD sin mutaciones)"
        )

    # Validar active.json contra source_activation (bootstrap vs historial)
    carga_active = load_frozen_runtime_state(active_state_path(storage_path))
    estado_active = carga_active.state
    obs_gen = None if estado_active is None else estado_active.desired_active_generation
    obs_clone = None if estado_active is None else estado_active.desired_clone_id
    if (obs_gen, obs_clone) != (source_generation_id, source_clone_id):
        raise AbortoIp0EvidenciaInsuficienteError(
            f"IP-0: active.json declara ({obs_gen!r}, {obs_clone!r}) y source_activation exige "
            f"({source_generation_id!r}, {source_clone_id!r}): HOLD sin mutaciones"
        )

    # Verificar que el Effective Runtime observado NO esté en el destino ni indeterminado
    ini_ev = leer_game_path_de_mo2(evidencia_actual.mo2_ini_path)
    if ini_ev.state is not ObservationState.OBSERVED or not ini_ev.path:
        raise AbortoIp0EvidenciaInsuficienteError(
            f"IP-0: el gamePath de MO2 es indeterminado ({ini_ev.state.value}): HOLD sin mutaciones"
        )
    if _norm(ini_ev.path) == _norm(expected_effective_path):
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: el gamePath de MO2 ya apunta al destino esperado de la transición: "
            "no corresponde abortar (HOLD sin mutaciones)"
        )
    if source_clone_id is not None and source_generation_id is not None:
        desired_origen = desired_runtime_from_storage(storage_path)
        if desired_origen.clone_root is None:
            raise AbortoIp0EvidenciaInsuficienteError(
                "IP-0: no se pudo resolver la raíz del runtime de origen desde storage: HOLD sin mutaciones"
            )
        eval_origen = assess_effective_runtime(
            mo2_instance_dir=contexto_ip0.mo2_instance_dir,
            desired=DesiredRuntime(
                clone_id=source_clone_id,
                clone_root=desired_origen.clone_root,
                generation_id=source_generation_id,
            ),
            managed_source_root=contexto_ip0.managed_source_root,
            config_skyrim_path=evidencia_actual.config_skyrim_path,
            environ=contexto_ip0.environ,
            probes=probes,
        )
        if not eval_origen.authorizes_activation:
            raise AbortoIp0EvidenciaInsuficienteError(
                f"IP-0: el oráculo Effective Runtime no acredita el origen ({eval_origen.verdict.value}): "
                "HOLD sin mutaciones"
            )

    return {
        "mo2_ini": {
            "evidencia": "byte_exact_mo2auth",
            "path": str(evidencia_actual.mo2_ini_path),
            "sha256": evidencia_actual.mo2_ini_sha256,
        },
        "config": {
            "evidencia": "parsed_skyrim_path_bind",
            "path": str(evidencia_actual.config_path),
            "skyrim_path": evidencia_actual.config_skyrim_path,
        },
        "active_json": {
            "evidencia": "source_activation_semantic",
            "desired_generation_id": obs_gen,
            "desired_clone_id": obs_clone,
        },
        "ip0_origen": {
            "expected_effective_path": expected_effective_path,
            "source_generation_id": source_generation_id,
            "source_clone_id": source_clone_id,
        },
    }


def _resolver_origen_ip0_durable(
    reg_actual: RegistroAbortoPendiente,
    *,
    journal_snapshot: JournalLoadResult,
    witness: IssuanceWitness,
) -> tuple[str | None, str | None, str]:
    """Obtiene ``(source_generation_id, source_clone_id, expected_effective_path)`` desde evidencia durable.

    Jamás reconstruye autorización por inferencia. Cuando el journal todavía está en ``PENDING_*``,
    usa los campos del journal. Cuando el journal ya rotó a ``NONE`` (crash entre rotación y
    ``JOURNAL_NONE`` o ``COMPLETO``), exige ``ip0_origen`` sellado en ``reg_actual.superficies_despues``
    y lo cruza contra ``witness.scope_payload["source_activation"]``.
    """
    if journal_snapshot.journal is not None and journal_snapshot.disposition in (
        JournalDisposition.PENDING_PROMOTION,
        JournalDisposition.PENDING_ROLLBACK,
    ):
        j = journal_snapshot.journal
        if not isinstance(j.source_activation, SourceActivation) or not j.expected_effective_path:
            raise AbortoIp0EvidenciaInsuficienteError(
                "IP-0: el journal PENDING carece de source_activation o expected_effective_path (HOLD)"
            )
        return j.source_activation.generation_id, j.source_activation.clone_id, j.expected_effective_path

    if reg_actual.superficies_despues is None or reg_actual.superficies_verificadas_digest is None:
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: el journal ya rotó a NONE y el registro de aborto no conserva superficies_despues "
            "para revalidar el origen aprobado (HOLD sin avanzar a COMPLETO)"
        )
    if digest_canonico(reg_actual.superficies_despues) != reg_actual.superficies_verificadas_digest:
        raise AbortoCorruptoError("el digest de superficies_despues en el registro de aborto IP-0 no coincide")
    ip0_origen = reg_actual.superficies_despues.get("ip0_origen")
    if not isinstance(ip0_origen, Mapping):
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: falta evidencia durable 'ip0_origen' en superficies_despues tras rotar el journal a NONE (HOLD)"
        )
    expected_path = ip0_origen.get("expected_effective_path")
    src_gen = ip0_origen.get("source_generation_id")
    src_clone = ip0_origen.get("source_clone_id")
    if not isinstance(expected_path, str) or not expected_path.strip():
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: 'expected_effective_path' ausente o inválido en evidencia durable 'ip0_origen' (HOLD)"
        )
    if (src_gen is not None and not isinstance(src_gen, str)) or (
        src_clone is not None and not isinstance(src_clone, str)
    ):
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: 'source_generation_id'/'source_clone_id' inválidos en evidencia durable 'ip0_origen' (HOLD)"
        )
    src_witness = witness.scope_payload.get("source_activation")
    if not isinstance(src_witness, Mapping):
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: el witness no contiene source_activation verificable para cruzar contra 'ip0_origen' (HOLD)"
        )
    if (src_witness.get("generation_id"), src_witness.get("clone_id")) != (src_gen, src_clone):
        raise AbortoCorruptoError(
            "IP-0: 'ip0_origen' en el registro de aborto contradice source_activation del witness inmutable"
        )
    return src_gen, src_clone, expected_path


def _revalidar_superficies_desde_registro_aborto(reg_actual: RegistroAbortoPendiente) -> None:
    """Revalida superficies contra ``reg_actual.superficies_despues`` cuando el registro de recuperación ya fue retirado.

    Se invoca en el camino hermano que reanuda desde ``JOURNAL_NONE`` después de haber retirado
    ``state/recovery/<tid>.json`` y antes de persistir ``COMPLETO`` (P1-01). No levanta el bloqueo D4
    si alguna superficie relevante divergió o es ilegible.
    """
    if reg_actual.superficies_despues is None or reg_actual.superficies_verificadas_digest is None:
        raise AbortoCorruptoError(
            "el registro de aborto en JOURNAL_NONE carece de superficies_despues selladas para revalidar"
        )
    if digest_canonico(reg_actual.superficies_despues) != reg_actual.superficies_verificadas_digest:
        raise AbortoCorruptoError("el digest de superficies_despues no coincide al revalidar desde JOURNAL_NONE")
    for kind, esperado in sorted(reg_actual.superficies_despues.items()):
        ruta_raw = esperado.get("path")
        estado_esp = esperado.get("estado")
        sha_esp = esperado.get("sha256")
        if not isinstance(ruta_raw, str) or not ruta_raw.strip():
            raise AbortoCorruptoError(f"superficies_despues[{kind!r}] sin ruta válida en JOURNAL_NONE")
        ruta = pathlib.Path(ruta_raw)
        with recovery._coordinacion_de_escritores(kind, ruta):
            estado_disco, actuales, err_disco = recovery._inspeccionar_superficie_en_disco(ruta)
            if estado_disco is recovery._EstadoDiscoSuperficie.DESCONOCIDO:
                raise AbortoPendienteInvalidoError(
                    f"no se pudo revalidar '{kind}' en JOURNAL_NONE ({err_disco}): "
                    "se mantiene el bloqueo de aborto incompleto (D4)"
                )
            if estado_esp == EstadoSuperficieConfirmada.AUSENTE.value:
                if estado_disco is not recovery._EstadoDiscoSuperficie.AUSENTE:
                    raise AbortoSuperficieDivergenteError(
                        f"la superficie '{kind}' ({ruta}) reapareció en disco tras JOURNAL_NONE: "
                        "se mantiene el bloqueo de aborto incompleto (D4)"
                    )
            elif estado_esp == EstadoSuperficieConfirmada.PRESENTE.value:
                if estado_disco is not recovery._EstadoDiscoSuperficie.PRESENTE or actuales is None:
                    raise AbortoSuperficieDivergenteError(
                        f"la superficie '{kind}' ({ruta}) desapareció en disco tras JOURNAL_NONE: "
                        "se mantiene el bloqueo de aborto incompleto (D4)"
                    )
                if recovery.huella(actuales) != sha_esp:
                    raise AbortoSuperficieDivergenteError(
                        f"la superficie '{kind}' ({ruta}) divergió en disco tras JOURNAL_NONE "
                        f"(observado={recovery.huella(actuales)}, esperado={sha_esp}): "
                        "se mantiene el bloqueo de aborto incompleto (D4)"
                    )
            else:
                raise AbortoCorruptoError(f"estado desconocido {estado_esp!r} en superficies_despues[{kind!r}]")


def _digest_archivo_recuperacion(storage_path: pathlib.Path, transition_id: str) -> str:
    ruta = recovery_state_dir(storage_path) / f"{transition_id}.json"
    try:
        return recovery.huella(ruta.read_bytes())
    except OSError as exc:
        raise AbortoPendienteInvalidoError(f"no se pudo calcular digest del registro de recuperación: {exc}") from exc


def _normalizar_confirmaciones(
    confirmaciones: Mapping[str, ConfirmacionSuperficie] | None,
) -> dict[str, ConfirmacionSuperficie]:
    if confirmaciones is None:
        return {}
    salida: dict[str, ConfirmacionSuperficie] = {}
    for k, v in confirmaciones.items():
        if k not in recovery._KINDS_PERSISTIDAS:
            raise AbortoPendienteInvalidoError(
                f"superficie desconocida en confirmaciones: {k!r} (permitidas: {recovery._KINDS_PERSISTIDAS})"
            )
        if not isinstance(v, ConfirmacionSuperficie):
            raise AbortoPendienteInvalidoError(
                f"la confirmación de {k!r} debe ser ConfirmacionSuperficie, no {type(v).__name__}"
            )
        salida[k] = v
    return salida


def abortar_transicion_pendiente(
    storage_root: pathlib.Path,
    *,
    transition_id: str,
    identidad: IdentidadPropietario,
    motivo: str,
    confirmaciones: Mapping[str, ConfirmacionSuperficie] | None = None,
    contexto_ip0: ContextoVerificacionIp0 | None = None,
    probes: RuntimeProbes | None = None,
    timeout: float = 30.0,
    clock_ns: Callable[[], int] = time.time_ns,
) -> RegistroAbortoPendiente:
    """Aborta de forma durable, auditable e idempotente una transición en ``PENDING`` (ADR 0012 §48).

    Orden bajo ``participant_lock``:
    1. Validar identidad del dueño, ausencia de C7 (``FINALIZED``) y ausencia de C6 (reserva).
    2. Si ya existe registro de aborto en ``COMPLETO`` → devolverlo (idempotente).
    3. Si no existe (o hay placeholder vacío de 0 bytes, D3) → validar precondiciones,
       confirmaciones o evidencia IP-0 y crear ``INTENCION`` single-winner antes de mutar.
    4. Desde ``INTENCION`` → restaurar superficies pendientes usando ``descartes_autorizados``
       (nunca ``huellas_posteriores``), verificar todas las superficies contra snapshot (o IP-0)
       y avanzar a ``SUPERFICIES_VERIFICADAS``.
    5. Desde ``SUPERFICIES_VERIFICADAS`` → revalidar SIEMPRE superficies y precondiciones (D2),
       rotar ``PENDING → NONE`` vía ``_abortar_pendiente_locked`` (D5) (o reconocer crash tras
       rotación ya aplicada, D3) y avanzar a ``JOURNAL_NONE``.
    6. Desde ``JOURNAL_NONE`` → retirar ``state/recovery/<tid>.json`` si persiste (idempotente, D3)
       y avanzar a ``COMPLETO``.
    """
    t_id = exigir_transition_id_canonico(transition_id)
    if not isinstance(motivo, str) or not motivo.strip():
        raise AbortoPendienteInvalidoError("se exige un motivo no vacío para autorizar el aborto")
    motivo_limpio = motivo.strip()
    confs_pedidas = _normalizar_confirmaciones(confirmaciones)
    observadores = probes if probes is not None else default_runtime_probes()

    storage_path = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    if not storage_path.is_dir():
        raise AbortoPendienteInvalidoError(f"storage_root no existe o no es directorio: {storage_path}")

    with participant_lock(storage_path, timeout=timeout, reclaim_orphaned=True) as handle:
        handle.assert_owned()
        root_id = canonical_root_key(storage_path)[0]

        # 1. Identidad del propietario y vínculo con aprobación consumida
        tombstone, witness = _evidencia_aprobacion_y_dueno(storage_path, t_id, identidad)

        # 2. Prohibido abortar C7 (FINALIZED) o C6 (reserva de finalización)
        try:
            historial = derivar_historial_finalizados(storage_path)
            auditoria = auditar_reservas_finalizacion(storage_path, historial)
        except (FrozenRuntimeError, OSError) as exc:
            raise AbortoPendienteInvalidoError(
                f"no se pudo auditar el historial/reservas ({exc}): fail-closed"
            ) from exc

        if any(r.transition_id == t_id for r in historial.registros):
            raise AbortoPendienteInvalidoError(
                f"la transición {t_id!r} ya está FINALIZED (C7): prohibido convertirla en aborto"
            )
        if any(tid == t_id for _, tid in auditoria.huerfanas):
            raise AbortoPendienteReservadoError(
                f"la transición {t_id!r} tiene una reserva durable en state/finalization-seqs/ (C6): "
                "prohibido abortar; la única salida legal es completar hacia adelante"
            )

        # 3. Inspeccionar estado durable del registro de aborto propio y de otros (D3/D4)
        estado_archivo = leer_estado_archivo_aborto(storage_path, t_id)
        es_placeholder = estado_archivo == PLACEHOLDER_VACIO
        reg_actual: RegistroAbortoPendiente | None = (
            estado_archivo if isinstance(estado_archivo, RegistroAbortoPendiente) else None
        )

        # Verificar que no haya OTRO aborto incompleto o archivo corrupto en el mismo root (D4),
        # incluso cuando la propia transición se reanuda desde un placeholder vacío de 0 bytes (D3).
        todos_abortos = cargar_abortos(storage_path, ignorar_placeholder_de=t_id if es_placeholder else None)
        otros_incompletos = sorted(
            tid for tid, r in todos_abortos.items() if tid != t_id and r.estado is not EstadoAbortoPendiente.COMPLETO
        )
        if otros_incompletos:
            raise AbortoPendienteInvalidoError(
                f"existen otros abortos incompletos en este root ({otros_incompletos}): "
                "resolvelos antes de iniciar otro aborto (D4)"
            )

        # Idempotencia si ya está COMPLETO
        if reg_actual is not None and reg_actual.estado is EstadoAbortoPendiente.COMPLETO:
            if (reg_actual.approval_id, reg_actual.approval_seq, reg_actual.scope_digest) != (
                tombstone.approval_id,
                int(tombstone.approval_seq),
                tombstone.approval_scope_digest,
            ):
                raise AbortoCorruptoError(f"el registro de aborto COMPLETO de {t_id!r} no liga el tombstone CONSUMED")
            return reg_actual

        # Leer journal vigente
        try:
            snapshot_journal = cargar_journal_transicion(storage_path)
        except (JournalCorruptError, JournalSchemaError, JournalIdentityError, JournalStoreNoInicializadoError) as exc:
            raise AbortoPendienteInvalidoError(f"el journal no es legible ({exc}): fail-closed") from exc

        # 4. Si no existe registro de aborto (o era placeholder vacío de 0 bytes), crear INTENCION
        if reg_actual is None:
            if snapshot_journal.disposition not in (
                JournalDisposition.PENDING_PROMOTION,
                JournalDisposition.PENDING_ROLLBACK,
            ):
                raise AbortoPendienteInvalidoError(
                    f"no hay un PENDING activo que abortar (journal={snapshot_journal.disposition.value})"
                )
            journal = snapshot_journal.journal
            assert journal is not None
            if journal.transition_id != t_id:
                raise AbortoPendienteInvalidoError(
                    f"el PENDING vigente pertenece a {journal.transition_id!r}, no a {t_id!r}"
                )
            if (
                journal.consumed_approval_id != tombstone.approval_id
                or journal.consumed_approval_seq != int(tombstone.approval_seq)
                or journal.consumed_scope_digest != tombstone.approval_scope_digest
            ):
                raise AbortoPendienteInvalidoError(
                    "el PENDING vigente no liga la misma aprobación consumida que el tombstone"
                )

            try:
                registro_rec = recovery.cargar_registro(storage_path, t_id)
            except recovery.RecuperacionError as exc:
                raise AbortoPendienteInvalidoError(f"registro de recuperación corrupto o ilegible: {exc}") from exc

            if registro_rec is not None:
                if registro_rec.journal_revision != journal.journal_revision:
                    raise AbortoPendienteInvalidoError(
                        f"el registro de recuperación declara journal_revision={registro_rec.journal_revision} "
                        f"y el journal está en {journal.journal_revision}: no ligan"
                    )
                _exigir_quiescencia_para_aborto(observadores, momento="precondición de aborto autorizado")
                superficies_rec = _superficies_persistidas_del_registro(registro_rec)
                _prevalidar_confirmaciones_contra_disco(superficies_rec, confs_pedidas)
                sup_antes = _observar_mapa_superficies(superficies_rec)
                modo = ModoAbortoPendiente.CON_REGISTRO_RECUPERACION
                rec_digest: str | None = _digest_archivo_recuperacion(storage_path, t_id)
            else:
                # IP-0 sin registro de recuperación (D6)
                _exigir_quiescencia_para_aborto(observadores, momento="verificación IP-0")
                if not isinstance(journal.source_activation, SourceActivation) or not journal.expected_effective_path:
                    raise AbortoIp0EvidenciaInsuficienteError(
                        "IP-0: el journal PENDING carece de source_activation o expected_effective_path (HOLD)"
                    )
                sup_antes = _verificar_evidencia_positiva_ip0(
                    storage_path,
                    source_generation_id=journal.source_activation.generation_id,
                    source_clone_id=journal.source_activation.clone_id,
                    expected_effective_path=journal.expected_effective_path,
                    witness=witness,
                    contexto_ip0=contexto_ip0,
                    probes=observadores,
                )
                modo = ModoAbortoPendiente.IP0_SIN_REGISTRO
                rec_digest = None

            ahora = int(clock_ns())
            reg_intencion = construir_registro_aborto(
                root_id=root_id,
                transition_id=t_id,
                journal_revision=journal.journal_revision,
                approval_id=tombstone.approval_id,
                approval_seq=int(tombstone.approval_seq),
                scope_digest=tombstone.approval_scope_digest,
                modo=modo,
                recovery_record_digest=rec_digest,
                confirmaciones=confs_pedidas,
                superficies_antes=sup_antes,
                superficies_despues=None,
                superficies_verificadas_digest=None,
                propietario_sujeto=identidad.sujeto,
                propietario_superficie=identidad.superficie.value,
                propietario_sesion=identidad.sesion,
                propietario_mecanismo=identidad.mecanismo,
                motivo=motivo_limpio,
                estado=EstadoAbortoPendiente.INTENCION,
                creado_en_ns=ahora,
                actualizado_en_ns=ahora,
            )
            reg_actual = crear_intencion_aborto_locked(
                storage_path,
                reg_intencion,
                completar_placeholder_vacio=es_placeholder,
            )
        else:
            # Reintento sobre intención existente: nunca aceptar confirmaciones distintas (RQ-2)
            if confs_pedidas and confs_pedidas != reg_actual.confirmaciones:
                raise AbortoPendienteInvalidoError(
                    "el reintento declaró confirmaciones distintas de las persistidas en la intención durable: "
                    "no se alteran las confirmaciones de un aborto en curso"
                )
            if (reg_actual.approval_id, reg_actual.approval_seq, reg_actual.scope_digest) != (
                tombstone.approval_id,
                int(tombstone.approval_seq),
                tombstone.approval_scope_digest,
            ):
                raise AbortoCorruptoError(f"el registro de aborto en curso de {t_id!r} no liga el tombstone CONSUMED")

        estado_inicial_reintento = reg_actual.estado

        # 5. Estado INTENCION → restaurar (si corresponde) y avanzar a SUPERFICIES_VERIFICADAS
        if reg_actual.estado is EstadoAbortoPendiente.INTENCION:
            if snapshot_journal.disposition not in (
                JournalDisposition.PENDING_PROMOTION,
                JournalDisposition.PENDING_ROLLBACK,
            ):
                raise AbortoPendienteInvalidoError(
                    f"aborto en INTENCION pero el journal ya está en {snapshot_journal.disposition.value}"
                )
            journal = snapshot_journal.journal
            assert journal is not None
            if journal.transition_id != t_id or journal.journal_revision != reg_actual.journal_revision:
                raise AbortoPendienteInvalidoError(
                    "el journal cambió de transición o revisión respecto de la intención durable"
                )
            _exigir_quiescencia_para_aborto(observadores, momento="restauración autorizada")

            if reg_actual.modo is ModoAbortoPendiente.CON_REGISTRO_RECUPERACION:
                try:
                    registro_rec = recovery.cargar_registro(storage_path, t_id)
                except recovery.RecuperacionError as exc:
                    raise AbortoPendienteInvalidoError(f"registro de recuperación ilegible: {exc}") from exc
                if registro_rec is None:
                    raise AbortoPendienteInvalidoError(
                        "el registro de aborto declara CON_REGISTRO_RECUPERACION pero el registro desapareció"
                    )
                if _digest_archivo_recuperacion(storage_path, t_id) != reg_actual.recovery_record_digest:
                    raise AbortoCorruptoError(
                        "el registro de recuperación cambió de digest desde que se persistió la intención"
                    )
                superficies_rec = _superficies_persistidas_del_registro(registro_rec)
                for sup in superficies_rec:
                    verif = recovery._restaurar_persistida(
                        sup,
                        registro_rec,
                        raiz_evidencia=storage_path,
                        descartes_autorizados=reg_actual.confirmaciones,
                    )
                    if verif.escritura_ajena:
                        raise AbortoSuperficieDivergenteError(verif.detail)
                    if not verif.ok:
                        raise AbortoPendienteInvalidoError(verif.detail)
                sup_despues = _verificar_superficies_contra_snapshot(
                    superficies_rec, raiz_evidencia=storage_path, registro=registro_rec
                )
            else:
                src_gen_ip0, src_clone_ip0, expected_path_ip0 = _resolver_origen_ip0_durable(
                    reg_actual,
                    journal_snapshot=snapshot_journal,
                    witness=witness,
                )
                sup_despues = _verificar_evidencia_positiva_ip0(
                    storage_path,
                    source_generation_id=src_gen_ip0,
                    source_clone_id=src_clone_ip0,
                    expected_effective_path=expected_path_ip0,
                    witness=witness,
                    contexto_ip0=contexto_ip0,
                    probes=observadores,
                )

            verif_digest = digest_canonico(sup_despues)
            ahora = max(int(clock_ns()), reg_actual.creado_en_ns)
            reg_verif = construir_registro_aborto(
                root_id=reg_actual.root_id,
                transition_id=reg_actual.transition_id,
                journal_revision=reg_actual.journal_revision,
                approval_id=reg_actual.approval_id,
                approval_seq=reg_actual.approval_seq,
                scope_digest=reg_actual.scope_digest,
                modo=reg_actual.modo,
                recovery_record_digest=reg_actual.recovery_record_digest,
                confirmaciones=reg_actual.confirmaciones,
                superficies_antes=reg_actual.superficies_antes,
                superficies_despues=sup_despues,
                superficies_verificadas_digest=verif_digest,
                propietario_sujeto=reg_actual.propietario_sujeto,
                propietario_superficie=reg_actual.propietario_superficie,
                propietario_sesion=reg_actual.propietario_sesion,
                propietario_mecanismo=reg_actual.propietario_mecanismo,
                motivo=reg_actual.motivo,
                estado=EstadoAbortoPendiente.SUPERFICIES_VERIFICADAS,
                creado_en_ns=reg_actual.creado_en_ns,
                actualizado_en_ns=ahora,
            )
            reg_actual = avanzar_estado_aborto_locked(storage_path, reg_actual, reg_verif)

        # 6. Estado SUPERFICIES_VERIFICADAS → SIEMPRE revalidar superficies (D2/P1-01) y rotar a NONE (o reconocer D3)
        if reg_actual.estado is EstadoAbortoPendiente.SUPERFICIES_VERIFICADAS:
            _exigir_quiescencia_para_aborto(observadores, momento="revalidación previa a rotación del journal (D2)")
            snapshot_journal = cargar_journal_transicion(storage_path)

            if reg_actual.modo is ModoAbortoPendiente.CON_REGISTRO_RECUPERACION:
                try:
                    registro_rec = recovery.cargar_registro(storage_path, t_id)
                except recovery.RecuperacionError as exc:
                    raise AbortoPendienteInvalidoError(f"registro de recuperación ilegible: {exc}") from exc
                if registro_rec is None:
                    raise AbortoPendienteInvalidoError(
                        "falta el registro de recuperación al revalidar SUPERFICIES_VERIFICADAS (D2)"
                    )
                if _digest_archivo_recuperacion(storage_path, t_id) != reg_actual.recovery_record_digest:
                    raise AbortoCorruptoError("el registro de recuperación cambió de digest antes de rotar el journal")
                _verificar_superficies_contra_snapshot(
                    _superficies_persistidas_del_registro(registro_rec),
                    raiz_evidencia=storage_path,
                    registro=registro_rec,
                )
            else:
                # P1-01: revalidar IP-0 SIEMPRE, incluso si el journal ya rotó a NONE antes del crash.
                src_gen_ip0, src_clone_ip0, expected_path_ip0 = _resolver_origen_ip0_durable(
                    reg_actual,
                    journal_snapshot=snapshot_journal,
                    witness=witness,
                )
                _verificar_evidencia_positiva_ip0(
                    storage_path,
                    source_generation_id=src_gen_ip0,
                    source_clone_id=src_clone_ip0,
                    expected_effective_path=expected_path_ip0,
                    witness=witness,
                    contexto_ip0=contexto_ip0,
                    probes=observadores,
                )

            testigo_esperado = historial.latest.transition_id if historial.latest is not None else None
            if (
                snapshot_journal.disposition is JournalDisposition.NONE
                and snapshot_journal.journal is not None
                and snapshot_journal.journal.journal_revision == reg_actual.journal_revision + 1
                and snapshot_journal.journal.last_finalized_transition_id == testigo_esperado
            ):
                # D3: crash entre _abortar_pendiente_locked (journal ya en NONE) y persistir JOURNAL_NONE.
                pass
            else:
                _abortar_pendiente_locked(
                    handle,
                    storage_path,
                    expected_transition_id=t_id,
                    expected_journal_revision=reg_actual.journal_revision,
                )

            ahora = max(int(clock_ns()), reg_actual.actualizado_en_ns)
            reg_jnone = construir_registro_aborto(
                root_id=reg_actual.root_id,
                transition_id=reg_actual.transition_id,
                journal_revision=reg_actual.journal_revision,
                approval_id=reg_actual.approval_id,
                approval_seq=reg_actual.approval_seq,
                scope_digest=reg_actual.scope_digest,
                modo=reg_actual.modo,
                recovery_record_digest=reg_actual.recovery_record_digest,
                confirmaciones=reg_actual.confirmaciones,
                superficies_antes=reg_actual.superficies_antes,
                superficies_despues=reg_actual.superficies_despues,
                superficies_verificadas_digest=reg_actual.superficies_verificadas_digest,
                propietario_sujeto=reg_actual.propietario_sujeto,
                propietario_superficie=reg_actual.propietario_superficie,
                propietario_sesion=reg_actual.propietario_sesion,
                propietario_mecanismo=reg_actual.propietario_mecanismo,
                motivo=reg_actual.motivo,
                estado=EstadoAbortoPendiente.JOURNAL_NONE,
                creado_en_ns=reg_actual.creado_en_ns,
                actualizado_en_ns=ahora,
            )
            reg_actual = avanzar_estado_aborto_locked(storage_path, reg_actual, reg_jnone)

        # 7. Estado JOURNAL_NONE → revalidar si es reintento en frío (P1-01), retirar registro (D3) y avanzar a COMPLETO
        if reg_actual.estado is EstadoAbortoPendiente.JOURNAL_NONE:
            if estado_inicial_reintento is EstadoAbortoPendiente.JOURNAL_NONE:
                _exigir_quiescencia_para_aborto(
                    observadores, momento="revalidación previa a completar aborto desde JOURNAL_NONE (P1-01)"
                )
                snapshot_journal = cargar_journal_transicion(storage_path)
                # CR #15: al reanudar desde JOURNAL_NONE, exigir explícitamente que el journal siga en NONE
                # con la revisión post-rotación (journal_revision + 1) y el testigo esperado.
                testigo_esperado_jnone = historial.latest.transition_id if historial.latest is not None else None
                if (
                    snapshot_journal.disposition is not JournalDisposition.NONE
                    or snapshot_journal.journal is None
                    or snapshot_journal.journal.journal_revision != reg_actual.journal_revision + 1
                    or snapshot_journal.journal.last_finalized_transition_id != testigo_esperado_jnone
                ):
                    raise AbortoPendienteInvalidoError(
                        "el aborto está en JOURNAL_NONE pero el journal en disco no coincide con NONE / "
                        f"revision={reg_actual.journal_revision + 1} / testigo={testigo_esperado_jnone!r} "
                        f"(observado: {snapshot_journal.disposition.value}): no se retira el registro ni se avanza a COMPLETO"
                    )
                if reg_actual.modo is ModoAbortoPendiente.CON_REGISTRO_RECUPERACION:
                    try:
                        registro_rec = recovery.cargar_registro(storage_path, t_id)
                    except recovery.RecuperacionError as exc:
                        raise AbortoPendienteInvalidoError(f"registro de recuperación ilegible: {exc}") from exc
                    if registro_rec is not None:
                        if _digest_archivo_recuperacion(storage_path, t_id) != reg_actual.recovery_record_digest:
                            raise AbortoCorruptoError("el registro de recuperación cambió de digest en JOURNAL_NONE")
                        _verificar_superficies_contra_snapshot(
                            _superficies_persistidas_del_registro(registro_rec),
                            raiz_evidencia=storage_path,
                            registro=registro_rec,
                        )
                    else:
                        # Crash tras eliminar_registro_locked y antes de persistir COMPLETO:
                        # revalidar contra superficies_despues selladas en el registro durable.
                        _revalidar_superficies_desde_registro_aborto(reg_actual)
                else:
                    src_gen_ip0, src_clone_ip0, expected_path_ip0 = _resolver_origen_ip0_durable(
                        reg_actual,
                        journal_snapshot=snapshot_journal,
                        witness=witness,
                    )
                    _verificar_evidencia_positiva_ip0(
                        storage_path,
                        source_generation_id=src_gen_ip0,
                        source_clone_id=src_clone_ip0,
                        expected_effective_path=expected_path_ip0,
                        witness=witness,
                        contexto_ip0=contexto_ip0,
                        probes=observadores,
                    )
            if reg_actual.modo is ModoAbortoPendiente.CON_REGISTRO_RECUPERACION:
                recovery.eliminar_registro_locked(handle, storage_path, t_id)
            ahora = max(int(clock_ns()), reg_actual.actualizado_en_ns)
            reg_completo = construir_registro_aborto(
                root_id=reg_actual.root_id,
                transition_id=reg_actual.transition_id,
                journal_revision=reg_actual.journal_revision,
                approval_id=reg_actual.approval_id,
                approval_seq=reg_actual.approval_seq,
                scope_digest=reg_actual.scope_digest,
                modo=reg_actual.modo,
                recovery_record_digest=reg_actual.recovery_record_digest,
                confirmaciones=reg_actual.confirmaciones,
                superficies_antes=reg_actual.superficies_antes,
                superficies_despues=reg_actual.superficies_despues,
                superficies_verificadas_digest=reg_actual.superficies_verificadas_digest,
                propietario_sujeto=reg_actual.propietario_sujeto,
                propietario_superficie=reg_actual.propietario_superficie,
                propietario_sesion=reg_actual.propietario_sesion,
                propietario_mecanismo=reg_actual.propietario_mecanismo,
                motivo=reg_actual.motivo,
                estado=EstadoAbortoPendiente.COMPLETO,
                creado_en_ns=reg_actual.creado_en_ns,
                actualizado_en_ns=ahora,
            )
            reg_actual = avanzar_estado_aborto_locked(storage_path, reg_actual, reg_completo)

        return reg_actual


# ============================================================================
# Recuperación hacia adelante de PENDING (C3 / C4 / C6 / C7 — ADR 0012 §49)
# ============================================================================


class FronteraRecuperacionAdelante(StrEnum):
    """Frontera durable de interrupción recuperable hacia adelante (ADR 0012 §35.3/§36.9/§37.5)."""

    C3_DESIRED_PENDIENTE = "C3_DESIRED_PENDIENTE"
    C4_POST_PENDIENTE = "C4_POST_PENDIENTE"
    C6_RESERVA_PROPIA = "C6_RESERVA_PROPIA"
    C7_ROTACION_F2 = "C7_ROTACION_F2"


@dataclasses.dataclass(frozen=True, slots=True)
class ResultadoRecuperacionAdelante:
    """Resultado estructurado y auditable de ``completar_transicion_pendiente``."""

    frontera: FronteraRecuperacionAdelante
    transition_id: str
    finalization_seq: int
    finalized_record: FinalizedTransitionRecord
    desired_actualizado: bool
    post_ejecutado: bool
    reserva_reutilizada: bool
    ya_estaba_completada: bool
    registro_recuperacion_retirado: bool


def _evidencia_aprobacion_consumida_para_adelante(
    storage_path: pathlib.Path,
    transition_id: str,
) -> tuple[ApprovalTombstone, IssuanceWitness]:
    """Verifica el vínculo durable con la aprobación CONSUMED original sin emitir ni consumir otra."""
    try:
        autoridad = derivar_autoridad_aprobacion(storage_path)
    except (FrozenRuntimeError, OSError) as exc:
        raise RecuperacionAdelanteAutoridadError(
            f"no se pudo derivar la autoridad de aprobación ({exc}): fail-closed"
        ) from exc
    tombstones = [
        t
        for t in autoridad.tombstones
        if t.terminal_state is TerminalState.CONSUMED and t.consumed_by_transition_id == transition_id
    ]
    if len(tombstones) != 1:
        raise RecuperacionAdelanteAutoridadError(
            f"se esperaba exactamente un tombstone CONSUMED para {transition_id!r}, encontrados {len(tombstones)}"
        )
    tombstone = tombstones[0]
    witness = next((w for w in autoridad.witnesses if w.approval_seq == tombstone.approval_seq), None)
    if witness is None or witness.approval_id != tombstone.approval_id:
        raise RecuperacionAdelanteAutoridadError(
            f"falta witness inmutable coherente para approval_seq={tombstone.approval_seq}"
        )
    if witness.approval_scope_digest != tombstone.approval_scope_digest:
        raise RecuperacionAdelanteAutoridadError(
            "el approval_scope_digest del witness no coincide con el tombstone CONSUMED"
        )
    return tombstone, witness


def _exigir_quiescencia_para_adelante(probes: RuntimeProbes, *, momento: str) -> None:
    from sky_claw.local.frozen_runtime.runtime_clone_binder import _exigir_quiescencia

    try:
        _exigir_quiescencia(probes, momento=momento)
    except FrozenRuntimeError as exc:
        raise RecuperacionAdelanteBloqueadaError(str(exc)) from exc


_SEGMENTO_INST_RE = re.compile(r"(?:^|:)inst=([^:]+)(?=:|$)")


def _huella_autoridad_aprobacion(
    tombstone: ApprovalTombstone,
    witness: IssuanceWitness,
) -> tuple[object, ...]:
    """Huella causal inmutable de la aprobación CONSUMED + witness para revalidación (H2)."""
    return (
        tombstone.approval_id,
        int(tombstone.approval_seq),
        tombstone.approval_scope_digest,
        tombstone.consumed_by_transition_id,
        tombstone.terminal_at_ns,
        digest_canonico(witness.scope_payload),
    )


def _exigir_source_activation_ligado_a_witness(
    source_activation: SourceActivation | None,
    witness: IssuanceWitness,
    *,
    etiqueta: str,
) -> None:
    src_w = witness.scope_payload.get("source_activation")
    if not isinstance(src_w, Mapping):
        return
    if source_activation is None:
        raise RecuperacionAdelanteAutoridadError(f"{etiqueta}: falta source_activation para cruzar contra witness")
    esperado = (
        src_w.get("generation_id"),
        src_w.get("clone_id"),
        src_w.get("runtime_setup_id"),
        src_w.get("effective_runtime_evidence"),
    )
    actual = (
        source_activation.generation_id,
        source_activation.clone_id,
        source_activation.runtime_setup_id,
        source_activation.effective_runtime_evidence,
    )
    if actual != esperado:
        raise RecuperacionAdelanteAutoridadError(
            f"{etiqueta}: source_activation ({actual!r}) diverge del witness ({esperado!r})"
        )


def _exigir_journal_ligado_a_witness(journal: TransitionJournal, witness: IssuanceWitness) -> None:
    """H1: liga TODOS los campos de transición y evidencias del journal contra ``proyeccion_desde_witness``."""
    assert journal.operation is not None
    assert journal.source_activation_digest is not None
    assert journal.target_generation_id is not None
    assert journal.target_clone_id is not None
    assert journal.target_runtime_setup_id is not None
    assert journal.evidence_digests is not None
    assert journal.consumed_approval_id is not None
    assert journal.consumed_approval_seq is not None

    proyeccion_aprobada = proyeccion_desde_witness(witness)
    proyeccion_journal = ProyeccionScope(
        operation=journal.operation.value,
        source_activation_digest=journal.source_activation_digest,
        target_generation_id=journal.target_generation_id,
        target_clone_id=journal.target_clone_id,
        target_runtime_setup_id=journal.target_runtime_setup_id,
        candidate_id=journal.candidate_id,
        clone_evidence=journal.evidence_digests.clone_evidence,
        runtime_setup_evidence=journal.evidence_digests.runtime_setup_evidence,
        compatibility_evidence_id=journal.evidence_digests.compatibility_evidence,
        approval_id=journal.consumed_approval_id,
        approval_seq=journal.consumed_approval_seq,
    )
    try:
        exigir_proyeccion_ligada(
            proyeccion_aprobada,
            proyeccion_journal,
            etiqueta="recuperación hacia adelante (journal vs witness)",
        )
    except ScopeBindingError as exc:
        raise RecuperacionAdelanteAutoridadError(str(exc)) from exc
    _exigir_source_activation_ligado_a_witness(
        journal.source_activation, witness, etiqueta="recuperación hacia adelante (journal)"
    )


def _exigir_registro_finalizado_ligado_a_witness(
    registro: FinalizedTransitionRecord,
    witness: IssuanceWitness,
) -> None:
    """H1 (C7): liga el registro FINALIZED existente contra ``proyeccion_desde_witness``."""
    aprobado = proyeccion_desde_witness(witness)
    discrepancias: list[str] = []
    for nombre, val_aprob, val_reg in (
        ("operation", aprobado.operation, registro.operation.value),
        ("source_activation_digest", aprobado.source_activation_digest, registro.source_activation_digest),
        ("target_generation_id", aprobado.target_generation_id, registro.target_generation_id),
        ("target_clone_id", aprobado.target_clone_id, registro.target_clone_id),
        ("target_runtime_setup_id", aprobado.target_runtime_setup_id, registro.target_runtime_setup_id),
        ("candidate_id", aprobado.candidate_id, registro.candidate_id),
        ("approval_id", aprobado.approval_id, registro.consumed_approval_id),
        ("approval_seq", aprobado.approval_seq, registro.consumed_approval_seq),
    ):
        if val_aprob != val_reg:
            discrepancias.append(f"{nombre} (aprobado {val_aprob!r} != FINALIZED {val_reg!r})")
    if discrepancias:
        raise RecuperacionAdelanteAutoridadError(
            "C7: el registro FINALIZED diverge de la aprobación emitida (witness): "
            f"{'; '.join(discrepancias)}. No se rota el journal"
        )
    _exigir_source_activation_ligado_a_witness(
        registro.source_activation, witness, etiqueta="C7 (FINALIZED vs witness)"
    )


def _exigir_instancia_mo2_autorizada_por_witness(
    mo2_dir: pathlib.Path,
    witness: IssuanceWitness,
) -> None:
    """H1: verifica que ``mo2_dir`` coincida con la instancia MO2 autorizada en ``approval_provenance``.

    No compara el digest previo de ``modorganizer.ini`` (que ya mutó tras el bind): liga únicamente
    la instancia de MO2 autorizada (segmento ``inst=...``).
    """
    provenance = witness.scope_payload.get("approval_provenance")
    if not isinstance(provenance, str):
        return
    requiere_inst = provenance.startswith("golden-master-hitl:") or token_comprometido(provenance) is not None
    coincidencias = _SEGMENTO_INST_RE.findall(provenance)
    if len(coincidencias) > 1:
        raise RecuperacionAdelanteAutoridadError(
            "approval_provenance contiene múltiples segmentos 'inst=': forma ambigua (fail-closed)"
        )
    if not coincidencias:
        if requiere_inst:
            raise RecuperacionAdelanteAutoridadError(
                "approval_provenance de autorización HITL carece del segmento 'inst=' para validar la instancia MO2"
            )
        return
    inst_autorizada = urllib.parse.unquote(coincidencias[0])
    if clave_de_ruta(mo2_dir) != clave_de_ruta(inst_autorizada):
        raise RecuperacionAdelanteAutoridadError(
            f"la instancia MO2 solicitada ({mo2_dir}) no coincide con la instancia autorizada por el propietario "
            f"en el witness ({inst_autorizada}): rechazo sin mutaciones"
        )


def _exigir_instancia_mo2_activa(probes: RuntimeProbes, mo2_dir: pathlib.Path, *, momento: str) -> None:
    """Verifica que ``mo2_dir`` siga siendo la instancia global activa de MO2 (Codex #4724466619)."""
    probe_mo2 = probes.mo2_instance if probes.mo2_instance is not None else default_runtime_probes().mo2_instance
    if probe_mo2 is None:
        raise RecuperacionAdelantePostError(
            f"{momento}: no hay sonda de identidad de instancia MO2 disponible (fail-closed)"
        )
    obs = probe_mo2(mo2_dir)
    if not obs.is_active:
        raise RecuperacionAdelantePostError(
            f"{momento}: la instancia MO2 ({mo2_dir}) no está activa como instancia global "
            f"(state={obs.state.value}, detail={obs.detail!r}): prohibido finalizar"
        )


def _retirar_recovery_best_effort(handle: object, storage_path: pathlib.Path, transition_id: str) -> bool:
    """Retira ``state/recovery/<tid>.json`` tras finalizar/rotar; best-effort declarado (no revierte FINALIZED)."""
    try:
        recovery.eliminar_registro_locked(handle, storage_path, transition_id)
        return True
    except OSError as exc:
        logger.warning(
            "recuperación hacia adelante (%s): no se pudo retirar state/recovery/%s.json (%s); "
            "reintentar completará la limpieza de forma idempotente",
            transition_id,
            transition_id,
            exc,
        )
        return False


def completar_transicion_pendiente(
    storage_root: pathlib.Path,
    *,
    transition_id: str,
    mo2_instance_dir: pathlib.Path,
    managed_source_root: pathlib.Path | None = None,
    config_path: pathlib.Path | None = None,
    environ: Mapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
    expected_journal_revision: int | None = None,
    timeout: float = 30.0,
    clock_ns: Callable[[], int] = time.time_ns,
) -> ResultadoRecuperacionAdelante:
    """Completa hacia adelante una transición interrumpida en C3/C4/C6/C7 (ADR 0012 §35.3/§36.9/§37.5/§49).

    Preserva todas las autoridades durables existentes:
    - Reutiliza el mismo ``transition_id``, la misma aprobación ``CONSUMED`` y la reserva F1a (C6).
    - En C7 completa ÚNICAMENTE F2 (rotación a ``NONE``) sin recrear ``FINALIZED`` ni fabricar POST.
    - En C3/C4/C6 ejecuta la verificación física del Clone, setup y el oráculo Effective Runtime
      FUERA de ``participant_lock`` y revalida la huella causal durable al recuperar el lock (FR-06).
    - Ante abortos incompletos (D4), IDs abortados, stores corruptos, reservas ajenas/corruptas,
      Effective desconocido/divergente, Clone inválido o CAS obsoleto: falla cerrado sin mutar.
    """
    from sky_claw.local.frozen_runtime.runtime_clone_binder import _leer_skyrim_path_persistido

    try:
        t_id = exigir_transition_id_canonico(transition_id)
    except AbortoPendienteInvalidoError as exc:
        raise RecuperacionAdelanteAutoridadError(str(exc)) from exc

    if expected_journal_revision is not None and (
        isinstance(expected_journal_revision, bool)
        or not isinstance(expected_journal_revision, int)
        or expected_journal_revision < 0
    ):
        raise RecuperacionAdelanteCasError("expected_journal_revision debe ser un entero >= 0")

    storage_path = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    if not storage_path.is_dir():
        raise RecuperacionAdelanteAutoridadError(f"storage_root no existe o no es directorio: {storage_path}")
    mo2_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))
    observadores = probes if probes is not None else default_runtime_probes()

    # -----------------------------------------------------------------------
    # Fase 1 (bajo participant_lock): lectura causalmente coherente + C7 directa
    # -----------------------------------------------------------------------
    with participant_lock(storage_path, timeout=timeout, reclaim_orphaned=True) as handle:
        handle.assert_owned()
        try:
            estado = _observar_estado_durable(storage_path)
        except AbortoCorruptoError:
            raise
        except (
            JournalCorruptError,
            JournalSchemaError,
            JournalIdentityError,
            JournalStoreNoInicializadoError,
            StateCorruptError,
            StateSchemaError,
            InvalidGenerationIdError,
        ) as exc:
            raise RecuperacionAdelanteAutoridadError(f"autoridad durable ilegible o incoherente ({exc})") from exc

        # FR-08: bloqueo D4 ante aborto incompleto y veto permanente de transition_id abortado.
        if estado.abortos_incompletos:
            raise RecuperacionAdelanteBloqueadaError(
                f"aborto incompleto en curso (D4): {estado.abortos_incompletos}. "
                "Prohibido recuperar hacia adelante hasta cerrar el aborto"
            )
        if registro_de_aborto(storage_path, t_id) is not None:
            raise RecuperacionAdelanteBloqueadaError(
                f"la transición {t_id!r} fue abortada (veto permanente): prohibido recuperarla hacia adelante"
            )

        tombstone, witness = _evidencia_aprobacion_consumida_para_adelante(storage_path, t_id)
        huella_aprobacion_inicial = _huella_autoridad_aprobacion(tombstone, witness)
        _exigir_instancia_mo2_autorizada_por_witness(mo2_dir, witness)

        registro_existente = next((r for r in estado.historial.registros if r.transition_id == t_id), None)
        if registro_existente is not None:
            # Frontera C7 (FINALIZED ya existe) o reintento idempotente post-F2.
            if (
                registro_existente.consumed_approval_id != tombstone.approval_id
                or registro_existente.consumed_approval_seq != int(tombstone.approval_seq)
                or registro_existente.approval_scope_digest != tombstone.approval_scope_digest
            ):
                raise RecuperacionAdelanteAutoridadError(
                    f"el registro FINALIZED de {t_id!r} no coincide con la aprobación CONSUMED"
                )
            _exigir_registro_finalizado_ligado_a_witness(registro_existente, witness)
            ya_completada = estado.snapshot.disposition is JournalDisposition.NONE
            if not ya_completada:
                j_c7 = estado.snapshot.journal
                if j_c7 is None or (
                    j_c7.consumed_approval_id != tombstone.approval_id
                    or j_c7.consumed_approval_seq != int(tombstone.approval_seq)
                    or j_c7.approval_scope_digest != tombstone.approval_scope_digest
                ):
                    raise RecuperacionAdelanteAutoridadError(
                        "el journal PENDING en C7 no coincide con la aprobación CONSUMED"
                    )
                _exigir_journal_ligado_a_witness(j_c7, witness)
            try:
                rec_f2 = _completar_rotacion_f2_locked(
                    handle,
                    storage_path,
                    transition_id=t_id,
                    expected_journal_revision=expected_journal_revision if not ya_completada else None,
                )
            except JournalCasError as exc:
                raise RecuperacionAdelanteCasError(str(exc)) from exc
            except JournalIdentityError as exc:
                raise RecuperacionAdelanteAutoridadError(str(exc)) from exc

            retirado = _retirar_recovery_best_effort(handle, storage_path, t_id)
            return ResultadoRecuperacionAdelante(
                frontera=FronteraRecuperacionAdelante.C7_ROTACION_F2,
                transition_id=t_id,
                finalization_seq=rec_f2.finalization_seq,
                finalized_record=rec_f2,
                desired_actualizado=False,
                post_ejecutado=False,
                reserva_reutilizada=False,
                ya_estaba_completada=ya_completada,
                registro_recuperacion_retirado=retirado,
            )

        # Sin FINALIZED: debe haber un PENDING activo de ESTA transición (C3/C4/C6).
        if (
            estado.snapshot.disposition
            not in (
                JournalDisposition.PENDING_PROMOTION,
                JournalDisposition.PENDING_ROLLBACK,
            )
            or estado.snapshot.journal is None
        ):
            raise RecuperacionAdelanteBloqueadaError(
                f"no hay PENDING recuperable para {t_id!r} (journal={estado.snapshot.disposition.value})"
            )
        journal = estado.snapshot.journal
        if journal.transition_id != t_id:
            raise RecuperacionAdelanteAutoridadError(
                f"el PENDING vigente pertenece a {journal.transition_id!r}, no a {t_id!r}"
            )
        if expected_journal_revision is not None and journal.journal_revision != expected_journal_revision:
            raise RecuperacionAdelanteCasError(
                f"CAS desactualizado: esperado journal_revision={expected_journal_revision}, "
                f"vigente={journal.journal_revision}"
            )
        if (
            journal.consumed_approval_id != tombstone.approval_id
            or journal.consumed_approval_seq != int(tombstone.approval_seq)
            or journal.approval_scope_digest != tombstone.approval_scope_digest
        ):
            raise RecuperacionAdelanteAutoridadError(
                "el journal PENDING no coincide con la aprobación CONSUMED (approval_id/seq/scope_digest)"
            )
        _exigir_journal_ligado_a_witness(journal, witness)

        # Auditar reservas huérfanas (C6 vs reserva ajena/corrupta/seq incorrecta)
        reserva_reutilizada = False
        if estado.auditoria.huerfanas:
            if len(estado.auditoria.huerfanas) > 1:
                raise RecuperacionAdelanteAutoridadError(
                    f"múltiples reservas huérfanas {estado.auditoria.huerfanas}: autoridad ambigua"
                )
            seq_h, tid_h = estado.auditoria.huerfanas[0]
            if tid_h != t_id:
                raise RecuperacionAdelanteAutoridadError(
                    f"reserva huérfana ajena (seq={seq_h}, transition_id={tid_h!r}): prohibido reutilizar"
                )
            if seq_h != estado.historial.next_seq:
                raise RecuperacionAdelanteAutoridadError(
                    f"reserva huérfana de {t_id!r} tiene seq={seq_h} != next_seq={estado.historial.next_seq}"
                )
            reserva_reutilizada = True

        assert journal.target_generation_id is not None
        assert journal.target_clone_id is not None
        assert journal.target_runtime_setup_id is not None
        assert journal.source_activation is not None
        assert journal.source_activation_digest is not None
        assert journal.expected_effective_path is not None

        target_generation_id = journal.target_generation_id
        target_clone_id = journal.target_clone_id
        target_runtime_setup_id = journal.target_runtime_setup_id
        source_activation_digest = journal.source_activation_digest
        expected_effective_path = journal.expected_effective_path
        rev_inicial = journal.journal_revision
        huella_inicial = estado.huella

        # Clasificar C3 vs C4 vs C6 contra active.json y source_activation
        active_state = estado.deseado.state
        obs_gen = None if active_state is None else active_state.desired_active_generation
        obs_clone = None if active_state is None else active_state.desired_clone_id
        target_pair = (target_generation_id, target_clone_id)
        source_pair = (journal.source_activation.generation_id, journal.source_activation.clone_id)

        if reserva_reutilizada:
            if (obs_gen, obs_clone) != target_pair:
                raise RecuperacionAdelanteAutoridadError(
                    f"C6 incoherente: existe reserva propia para {t_id!r}, pero active.json declara "
                    f"({obs_gen!r}, {obs_clone!r}) en lugar del destino {target_pair!r}"
                )
            frontera = FronteraRecuperacionAdelante.C6_RESERVA_PROPIA
        elif (obs_gen, obs_clone) == target_pair:
            frontera = FronteraRecuperacionAdelante.C4_POST_PENDIENTE
        elif (obs_gen, obs_clone) == source_pair:
            frontera = FronteraRecuperacionAdelante.C3_DESIRED_PENDIENTE
        else:
            raise RecuperacionAdelanteAutoridadError(
                f"active.json declara ({obs_gen!r}, {obs_clone!r}), incompatible tanto con "
                f"source_activation {source_pair!r} (C3) como con target {target_pair!r} (C4)"
            )

        estado_previo_active = observar_estado_previo(active_state_path(storage_path))

        try:
            clone_record = leer_runtime_clone_record(storage_path, target_clone_id)
        except FrozenRuntimeError as exc:
            raise RecuperacionAdelantePostError(f"no se pudo leer RuntimeCloneRecord destino: {exc}") from exc

        clone_record_inicial = dataclasses.asdict(clone_record)
        if clone_record.source_generation_id != target_generation_id:
            raise RecuperacionAdelanteAutoridadError(
                f"el Clone {target_clone_id!r} declara source_generation_id={clone_record.source_generation_id!r}, "
                f"incompatible con target_generation_id={target_generation_id!r}"
            )
        target_clone_root = pathlib.Path(clone_record.root_path)
        if _norm(target_clone_root) != _norm(expected_effective_path):
            raise RecuperacionAdelanteAutoridadError(
                f"la ruta del Clone ({target_clone_root}) no coincide con expected_effective_path "
                f"({expected_effective_path})"
            )

    # -----------------------------------------------------------------------
    # Fase 2 (FUERA de participant_lock — FR-06): quiescencia, MO2 activa, Clone, setup, POST
    # -----------------------------------------------------------------------
    _exigir_quiescencia_para_adelante(observadores, momento="recuperación hacia adelante (observación previa)")
    _exigir_instancia_mo2_activa(observadores, mo2_dir, momento="recuperación hacia adelante (observación previa)")

    try:
        clone_check = verificar_runtime_clone(
            storage_path,
            target_clone_id,
            managed_source_root=managed_source_root,
        )
    except FrozenRuntimeError as exc:
        raise RecuperacionAdelantePostError(f"verificación del Clone destino falló: {exc}") from exc
    if clone_check.state is not GenerationVerificationState.VALID:
        raise RecuperacionAdelantePostError(
            f"el Clone destino {target_clone_id!r} no es VALID (state={clone_check.state.value}): prohibido finalizar"
        )

    try:
        setup_obs = verificar_runtime_setup(target_clone_root)
    except FrozenRuntimeError as exc:
        raise RecuperacionAdelantePostError(f"verificación de runtime setup falló: {exc}") from exc
    if setup_obs.setup_id != target_runtime_setup_id:
        raise RecuperacionAdelantePostError(
            f"runtime_setup_id observado ({setup_obs.setup_id!r}) != esperado ({target_runtime_setup_id!r}): "
            "prohibido finalizar"
        )

    ini_path = mo2_dir / "modorganizer.ini"
    try:
        ini_sha_post = recovery.huella(ini_path.read_bytes())
    except OSError as exc:
        raise RecuperacionAdelantePostError(f"POST VERIFY: no se pudo leer modorganizer.ini ({exc})") from exc

    cfg_post = _leer_skyrim_path_persistido(config_path)
    post_eval = assess_effective_runtime(
        mo2_instance_dir=mo2_dir,
        desired=DesiredRuntime(
            clone_id=target_clone_id,
            clone_root=target_clone_root,
            generation_id=target_generation_id,
        ),
        managed_source_root=managed_source_root,
        config_skyrim_path=cfg_post,
        environ=environ,
        probes=observadores,
    )
    if not post_eval.authorizes_activation:
        raise RecuperacionAdelantePostError(
            f"POST VERIFY no autoriza la activación del runtime en {frontera.value}: "
            f"verdict={post_eval.verdict.value}, reasons={[r.value for r in post_eval.reasons]}"
        )
    try:
        ini_sha_tras_post = recovery.huella(ini_path.read_bytes())
    except OSError as exc:
        raise RecuperacionAdelantePostError(f"POST VERIFY: no se pudo releer modorganizer.ini ({exc})") from exc
    if ini_sha_tras_post != ini_sha_post:
        raise RecuperacionAdelantePostError(
            "POST VERIFY: modorganizer.ini cambió durante la observación del oráculo (fail-closed)"
        )

    # -----------------------------------------------------------------------
    # Fase 3 (bajo participant_lock): revalidar huella causal, aprobación, clone, INI, MO2 activa, FINALIZE
    # -----------------------------------------------------------------------
    desired_actualizado = False
    with participant_lock(storage_path, timeout=timeout, reclaim_orphaned=True) as handle:
        handle.assert_owned()
        try:
            estado_reval = _observar_estado_durable(storage_path)
        except AbortoCorruptoError:
            raise
        except (
            JournalCorruptError,
            JournalSchemaError,
            JournalIdentityError,
            JournalStoreNoInicializadoError,
            StateCorruptError,
            StateSchemaError,
            InvalidGenerationIdError,
        ) as exc:
            raise RecuperacionAdelanteAutoridadError(f"revalidación durable falló ({exc})") from exc

        if estado_reval.huella != huella_inicial:
            raise RecuperacionAdelanteCasError(
                "la autoridad durable cambió mientras se ejecutaba la observación externa del oráculo: "
                "no se continúa sobre evidencia obsoleta (FR-06)"
            )

        tombstone_reval, witness_reval = _evidencia_aprobacion_consumida_para_adelante(storage_path, t_id)
        if _huella_autoridad_aprobacion(tombstone_reval, witness_reval) != huella_aprobacion_inicial:
            raise RecuperacionAdelanteCasError(
                "la autoridad de aprobación cambió durante la observación externa del oráculo (H2)"
            )

        try:
            clone_record_reval = leer_runtime_clone_record(storage_path, target_clone_id)
        except FrozenRuntimeError as exc:
            raise RecuperacionAdelanteCasError(
                f"no se pudo releer el RuntimeCloneRecord destino bajo lock ({exc})"
            ) from exc
        if dataclasses.asdict(clone_record_reval) != clone_record_inicial:
            raise RecuperacionAdelanteCasError(
                "el RuntimeCloneRecord destino derivó durante la observación externa del oráculo (H2)"
            )

        _exigir_quiescencia_para_adelante(observadores, momento="recuperación hacia adelante (revalidación bajo lock)")
        _exigir_instancia_mo2_activa(
            observadores, mo2_dir, momento="recuperación hacia adelante (revalidación bajo lock)"
        )

        # Revalidación bajo lock de los bytes completos de modorganizer.ini usados en POST (cierra
        # cambios en [customExecutables] o gamePath, Codex #4724466614) y config.skyrim_path.
        try:
            ini_sha_bajo_lock = recovery.huella(ini_path.read_bytes())
        except OSError as exc:
            raise RecuperacionAdelantePostError(
                f"POST VERIFY (revalidación bajo lock): no se pudo releer modorganizer.ini ({exc})"
            ) from exc
        if ini_sha_bajo_lock != ini_sha_post:
            raise RecuperacionAdelantePostError(
                "POST VERIFY (revalidación bajo lock): modorganizer.ini cambió tras la observación del oráculo "
                "(incluyendo posibles cambios en [customExecutables] o gamePath)"
            )
        if config_path is not None:
            cfg_reval = _leer_skyrim_path_persistido(config_path)
            if _norm(cfg_reval) != _norm(cfg_post) or _norm(cfg_reval) != _norm(target_clone_root):
                raise RecuperacionAdelantePostError(
                    "POST VERIFY (revalidación bajo lock): config.skyrim_path divergió del Clone destino"
                )

        if frontera is FronteraRecuperacionAdelante.C3_DESIRED_PENDIENTE:
            active_payload: dict[str, object] = {
                "schema_version": 2,
                "desired_generation_id": target_generation_id,
                "desired_clone_id": target_clone_id,
                "updated_at_ns": int(clock_ns()),
            }
            try:
                _escribir_estado_activo_cas_locked(
                    active_state_path(storage_path),
                    active_payload,
                    esperado=estado_previo_active,
                )
            except StateCasError as exc:
                raise RecuperacionAdelanteCasError(f"CAS UPDATE DESIRED rechazado en recuperación C3: {exc}") from exc
            desired_actualizado = True

        # Lectura ligera bajo lock (sin re-hashear el árbol físico del Clone, Codex #4724466610):
        # verifica active.json persistido contra target + clone_record ya revalidado.
        active_persistido = load_frozen_runtime_state(active_state_path(storage_path)).state
        if (
            active_persistido is None
            or active_persistido.desired_clone_id != target_clone_id
            or active_persistido.desired_active_generation != target_generation_id
            or _norm(clone_record_reval.root_path) != _norm(target_clone_root)
        ):
            raise RecuperacionAdelantePostError(
                "POST VERIFY: active.json persistido no coincide con el destino autorizado de la transición"
            )

        eff_obs_payload = {
            "verdict": post_eval.verdict.value,
            "effective_path": str(target_clone_root),
            "mo2_instance_dir": str(mo2_dir),
            "observed_at_ns": post_eval.observed_at_ns,
        }
        effective_obs_digest = digest_canonico(eff_obs_payload)
        verified_now = int(clock_ns())
        post_evidence_payload = {
            "transition_id": t_id,
            "resultado": PostVerifyResultado.PASS.value,
            "expected_journal_revision": rev_inicial,
            "source_activation_digest": source_activation_digest,
            "target_generation_id": target_generation_id,
            "target_clone_id": target_clone_id,
            "target_runtime_setup_id": target_runtime_setup_id,
            "desired_active_generation": target_generation_id,
            "effective_observation_digest": effective_obs_digest,
            "verified_at_ns": verified_now,
        }
        post_evidence = PostVerifyEvidence(
            transition_id=t_id,
            resultado=PostVerifyResultado.PASS,
            source_activation_digest=source_activation_digest,
            target_generation_id=target_generation_id,
            target_clone_id=target_clone_id,
            target_runtime_setup_id=target_runtime_setup_id,
            desired_active_generation=target_generation_id,
            effective_observation_digest=effective_obs_digest,
            payload_digest=digest_canonico(post_evidence_payload),
            verified_at_ns=verified_now,
            expected_journal_revision=rev_inicial,
        )

        try:
            record_final = _finalizar_locked(handle, storage_path, post_evidence)
        except JournalCasError as exc:
            raise RecuperacionAdelanteCasError(str(exc)) from exc
        except JournalIdentityError as exc:
            raise RecuperacionAdelanteAutoridadError(str(exc)) from exc

        retirado = _retirar_recovery_best_effort(handle, storage_path, t_id)

    return ResultadoRecuperacionAdelante(
        frontera=frontera,
        transition_id=t_id,
        finalization_seq=record_final.finalization_seq,
        finalized_record=record_final,
        desired_actualizado=desired_actualizado,
        post_ejecutado=True,
        reserva_reutilizada=reserva_reutilizada,
        ya_estaba_completada=False,
        registro_recuperacion_retirado=retirado,
    )


__all__ = [
    "ContextoVerificacionIp0",
    "FronteraRecuperacionAdelante",
    "ResultadoRecuperacionAdelante",
    "abortar_transicion_pendiente",
    "completar_transicion_pendiente",
]
