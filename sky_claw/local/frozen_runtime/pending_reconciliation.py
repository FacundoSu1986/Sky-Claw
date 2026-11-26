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
import os
import pathlib
import time
from collections.abc import Callable, Mapping

from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime.approval import (
    ApprovalTombstone,
    IssuanceWitness,
    TerminalState,
    derivar_autoridad_aprobacion,
)
from sky_claw.local.frozen_runtime.binder_preflight import BinderTransitionPolicy
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
    JournalCorruptError,
    JournalIdentityError,
    JournalSchemaError,
    JournalStoreNoInicializadoError,
)
from sky_claw.local.frozen_runtime.journal import (
    JournalDisposition,
    SourceActivation,
    TransitionJournal,
    _abortar_pendiente_locked,
    auditar_reservas_finalizacion,
    cargar_journal_transicion,
    derivar_historial_finalizados,
    digest_canonico,
)
from sky_claw.local.frozen_runtime.mo2_authorization import token_comprometido
from sky_claw.local.frozen_runtime.owner_consent import IdentidadPropietario
from sky_claw.local.frozen_runtime.pending_abort_store import (
    PLACEHOLDER_VACIO,
    ConfirmacionSuperficie,
    EstadoAbortoPendiente,
    ModoAbortoPendiente,
    RegistroAbortoPendiente,
    avanzar_estado_aborto_locked,
    cargar_abortos,
    construir_registro_aborto,
    crear_intencion_aborto_locked,
    exigir_transition_id_canonico,
    leer_estado_archivo_aborto,
)
from sky_claw.local.frozen_runtime.root_lock import canonical_root_key, participant_lock
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import active_state_path, recovery_state_dir


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
            if not recovery._esta_en_estado_previo(sup):
                obs = recovery.observar_superficie_en_disco(sup)
                evidencia_nota = ""
                if raiz_evidencia is not None and registro is not None and ruta.exists():
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
            observadas[sup.kind] = recovery.observar_superficie_en_disco(sup)
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
    journal: TransitionJournal,
    witness: IssuanceWitness,
    contexto_ip0: ContextoVerificacionIp0 | None,
    probes: RuntimeProbes,
) -> dict[str, dict[str, object]]:
    """Verifica evidencia causal positiva en IP-0 sin registro de recuperación (D6).

    Distingue explícitamente:
    - igualdad byte a byte de ``modorganizer.ini`` (acreditada por ``mo2_ini_sha256`` en ``mo2auth``/``bind``);
    - coherencia del valor parseado ``config.skyrim_path`` (NO igualdad byte a byte de todo ``config.toml``);
    - coherencia semántica de ``active.json`` contra ``journal.source_activation`` (bootstrap o par gen/clone);
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

    # Validar active.json contra journal.source_activation (bootstrap vs historial)
    fuente = journal.source_activation
    assert isinstance(fuente, SourceActivation)
    carga_active = load_frozen_runtime_state(active_state_path(storage_path))
    estado_active = carga_active.state
    obs_gen = None if estado_active is None else estado_active.desired_active_generation
    obs_clone = None if estado_active is None else estado_active.desired_clone_id
    if (obs_gen, obs_clone) != (fuente.generation_id, fuente.clone_id):
        raise AbortoIp0EvidenciaInsuficienteError(
            f"IP-0: active.json declara ({obs_gen!r}, {obs_clone!r}) y journal.source_activation exige "
            f"({fuente.generation_id!r}, {fuente.clone_id!r}): HOLD sin mutaciones"
        )

    # Verificar que el Effective Runtime observado NO esté en el destino ni indeterminado
    ini_ev = leer_game_path_de_mo2(evidencia_actual.mo2_ini_path)
    if ini_ev.state is not ObservationState.OBSERVED or not ini_ev.path:
        raise AbortoIp0EvidenciaInsuficienteError(
            f"IP-0: el gamePath de MO2 es indeterminado ({ini_ev.state.value}): HOLD sin mutaciones"
        )
    if _norm(ini_ev.path) == _norm(journal.expected_effective_path):
        raise AbortoIp0EvidenciaInsuficienteError(
            "IP-0: el gamePath de MO2 ya apunta al destino esperado de la transición: "
            "no corresponde abortar (HOLD sin mutaciones)"
        )
    if fuente.clone_id is not None and fuente.generation_id is not None:
        desired_origen = desired_runtime_from_storage(storage_path)
        if desired_origen.clone_root is None:
            raise AbortoIp0EvidenciaInsuficienteError(
                "IP-0: no se pudo resolver la raíz del runtime de origen desde storage: HOLD sin mutaciones"
            )
        eval_origen = assess_effective_runtime(
            mo2_instance_dir=contexto_ip0.mo2_instance_dir,
            desired=DesiredRuntime(
                clone_id=fuente.clone_id,
                clone_root=desired_origen.clone_root,
                generation_id=fuente.generation_id,
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
    }


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
                sup_antes = _verificar_evidencia_positiva_ip0(
                    storage_path,
                    journal=journal,
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
                sup_despues = _verificar_evidencia_positiva_ip0(
                    storage_path,
                    journal=journal,
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

        # 6. Estado SUPERFICIES_VERIFICADAS → SIEMPRE revalidar superficies (D2) y rotar a NONE (o reconocer D3)
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
                if snapshot_journal.journal is not None and snapshot_journal.disposition in (
                    JournalDisposition.PENDING_PROMOTION,
                    JournalDisposition.PENDING_ROLLBACK,
                ):
                    _verificar_evidencia_positiva_ip0(
                        storage_path,
                        journal=snapshot_journal.journal,
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

        # 7. Estado JOURNAL_NONE → retirar registro de recuperación (idempotente, D3) y avanzar a COMPLETO
        if reg_actual.estado is EstadoAbortoPendiente.JOURNAL_NONE:
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


__all__ = [
    "ContextoVerificacionIp0",
    "abortar_transicion_pendiente",
]
