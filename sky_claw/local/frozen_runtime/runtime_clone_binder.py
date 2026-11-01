"""Binder Transaccional de MO2 para Frozen Runtime (P5-C1).

Tracker: #672
ADR 0012 §13, §29.4, §34.4, §35.6, §36.6, §37.5
ADR 0015 §12, §13

Implementa la transición causal de seis pasos de ADR 0012:
PERSIST PENDING → BIND EFFECTIVE → VERIFY EFFECTIVE → CAS UPDATE DESIRED → POST VERIFY → FINALIZE

Garantías, y qué NO se afirma
-----------------------------
Lo que este módulo hace cumplir:

- Exclusión mutua **entre participantes de Sky-Claw** (root lock + participant
  lock) y quiescencia verificada de MO2/Skyrim/SKSE antes de mutar.
- Preflight de integración completo **dos veces**: antes de autorizar y otra vez
  dentro de la sección crítica, inmediatamente antes de la primera mutación.
- Aprobación verificada contra la autoridad durable (witness + tombstone). No
  existe camino productivo que fabrique una ``PropuestaTransicion`` con valores
  sintéticos: o viene del scope aprobado, o no hay transición.
- Snapshot **durable** de las superficies antes de mutarlas
  (``binder_recovery``), con restauración verificada superficie por superficie.
- CAS real sobre ``state/active.json``: si el Desired cambió concurrentemente,
  no se sobrescribe.
- POST VERIFY sobre los datos **realmente persistidos**, releídos del disco.
- Escritura de ``modorganizer.ini`` quirúrgica a nivel de bytes, con formatos
  admitidos restringidos (``mo2_ini_escritura``).

Lo que este módulo NO afirma:

- ``EXCLUSION_WITH_MO2 = NOT_ESTABLISHED``. El lock de Sky-Claw **no impide**
  que un operador abra ``ModOrganizer.exe``: MO2 no participa del lock. Lo que
  hay es detección (quiescencia al entrar y revalidación bajo la sección
  crítica) y un fail-closed cuando la detección ve algo. Entre la última
  revalidación y el ``os.replace`` del INI queda una ventana que ningún lock de
  Sky-Claw cierra. Por eso el switch está **limitado a rig sintético**: no hay
  wiring productivo, y no debe haberlo hasta que exista exclusión real
  (``FILE_SHARE_NONE`` sobre el INI o un protocolo con MO2).
- ``POWER_LOSS_GUARANTEE = NOT_CLAIMED``: ``os.replace`` atómico, sin WAL ni
  ``fsync`` de directorio, igual que el resto del paquete.
- ``QT_QSETTINGS_EQUIVALENCE = NOT_CLAIMED``: ver ``mo2_ini_escritura``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import time
import uuid
from collections.abc import Mapping, MutableMapping

from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime.approval import (
    ConsentimientoAprobacion,
    IssuanceWitness,
    PropuestaAprobacion,
    TerminalState,
    consumir_aprobacion,
    derivar_autoridad_aprobacion,
    proyeccion_desde_witness,
)
from sky_claw.local.frozen_runtime.binder_preflight import (
    BinderPreflightAssessment,
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.clones import (
    CANONICAL_ADMITTED_ROLE,
    RuntimeCloneLifecycle,
    cargar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    DesiredRuntime,
    LiveProcessKind,
    ObservationState,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.effective_runtime.oracle import (
    assess_effective_runtime,
    default_runtime_probes,
    desired_runtime_from_storage,
)
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeError,
    StateCasError,
)
from sky_claw.local.frozen_runtime.journal import (
    EvidenceDigests,
    FinalizedTransitionRecord,
    JournalDisposition,
    JournalOperation,
    PostVerifyEvidence,
    PostVerifyResultado,
    PropuestaTransicion,
    SourceActivation,
    _crear_pendiente_locked,
    _finalizar_locked,
    _start_next_locked,
    cargar_journal_transicion,
    derivar_historial_finalizados,
    digest_canonico,
    exigir_proyeccion_ligada,
    proyectar_propuesta,
)
from sky_claw.local.frozen_runtime.mo2_ini_escritura import (
    Mo2IniEscrituraError,
    actualizar_game_path_en_ini,
    actualizar_game_path_por_handle,
    codificar_qt_byte_array,
)
from sky_claw.local.frozen_runtime.mo2_ini_exclusion import (
    DiagnosticoExclusion,
    Mo2IniExclusionError,
    adquirir_exclusion_ini,
)
from sky_claw.local.frozen_runtime.root_lock import (
    LockDisposition,
    inspect_frozen_runtime_root_lock,
    participant_lock,
)
from sky_claw.local.frozen_runtime.state import (
    EstadoPrevioObservado,
    _escribir_estado_activo_cas_locked,
    observar_estado_previo,
)
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    recovery_state_dir,
)
from sky_claw.local.frozen_runtime.surface_census import (
    CENSO_ESCRITORES_GAME_DIR,
    CLASES_DE_SUPERFICIE,
)
from sky_claw.local.local_config import persistir_campo

logger = logging.getLogger(__name__)

__all__ = [
    "CENSO_ESCRITORES_GAME_DIR",
    "CLASES_DE_SUPERFICIE",
    "BinderApprovalError",
    "BinderCasError",
    "BinderError",
    "BinderEscrituraIniError",
    "BinderExclusionError",
    "BinderLockUnavailableError",
    "BinderPreflightFailedError",
    "BinderQuiescenceError",
    "BinderResult",
    "BinderRollbackError",
    "BinderRollbackFailedError",
    "BinderVerificationError",
    "actualizar_game_path_en_ini",
    "codificar_qt_byte_array",
    "ejecutar_transicion_binder",
]


# ---------------------------------------------------------------------------
# Excepciones de la Familia BinderError
# ---------------------------------------------------------------------------


class BinderError(FrozenRuntimeError):
    """Error base del binder transaccional de MO2."""


class BinderPreflightFailedError(BinderError):
    """El preflight de integración no superó los checks obligatorios."""


class BinderQuiescenceError(BinderError):
    """Fallo de quiescencia: se detectaron procesos activos de Skyrim, SKSE o MO2."""


class BinderApprovalError(BinderError):
    """La transición carece de aprobación válida o el consentimiento es inválido."""


class BinderVerificationError(BinderError):
    """Fallo en la verificación física de superficies (readback del INI o POST verify)."""


class BinderEscrituraIniError(BinderError):
    """Fallo al escribir ``modorganizer.ini`` (formato no admitido, I/O, TOCTOU).

    Envuelve ``Mo2IniEscrituraError`` para que el llamador del binder vea UNA
    familia de errores: una escritura que falla es un fallo de la transición, no
    un detalle de un submódulo. La causa original queda encadenada.
    """


class BinderRollbackError(BinderError):
    """Fallo o reversión durante la recuperación de un error."""


class BinderRollbackFailedError(BinderRollbackError):
    """Fallo crítico: el rollback no pudo restaurar fielmente las superficies originales."""


class BinderCasError(BinderError):
    """El CAS sobre ``state/active.json`` falló: el Desired cambió concurrentemente.

    Se envuelve la ``StateCasError`` del storage para que el llamador del binder
    vea una sola familia de errores, y para que el mensaje diga qué NO se hizo:
    el estado del otro escritor quedó intacto.
    """


class BinderLockUnavailableError(BinderError):
    """El root lock no se pudo adquirir: la transición NO debe consumir la aprobación.

    Se levanta en la SONDA previa al burn. Si aun así se pierde la carrera
    después de quemar, el mensaje declara el mecanismo de recuperación: el
    consumo es idempotente para el MISMO ``transition_id``
    (``approval.consumir_aprobacion`` reconoce ``state=CONSUMED`` y exige la
    misma transición), así que reintentar con el mismo id completa la
    transición en vez de perder la aprobación.
    """


class BinderExclusionError(BinderError):
    """No se pudo establecer la exclusión de mutación sobre ``modorganizer.ini``.

    Es la ventana TOCTOU que P5-C1 dejó abierta: sin exclusión, otro proceso
    puede escribir el INI entre la revalidación y el reemplazo. La transición se
    detiene ANTES de mutar cualquier superficie. El diagnóstico viaja en el
    mensaje con el estado medido, para que el operador distinga «MO2 tiene el
    archivo abierto» de «no se pudo determinar».
    """


# ---------------------------------------------------------------------------
# Modelos y Estructuras de Datos
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class BinderResult:
    """Resultado estructurado de la transición del binder."""

    success: bool
    message: str
    transition_id: str
    target_clone_id: str
    target_clone_root: pathlib.Path
    target_generation_id: str
    previous_effective_path: str
    new_effective_path: str
    post_verify_evidence: PostVerifyEvidence | None = None
    finalized_record: FinalizedTransitionRecord | None = None
    #: Diagnóstico de la exclusión de mutación (P5-C2). ``None`` sólo en los
    #: caminos que no mutan el INI (por ejemplo el no-op idempotente).
    exclusion: DiagnosticoExclusion | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _ObservacionPrevia:
    """Lo que el preflight INICIAL observó, para revalidar sin deriva."""

    clone_root: pathlib.Path
    generation_id: str
    game_path: str
    desired: DesiredRuntime
    ini_sha256: str


# ---------------------------------------------------------------------------
# Lectura de superficies persistidas
# ---------------------------------------------------------------------------


def _leer_skyrim_path_persistido(config_path: pathlib.Path | None) -> str | None:
    """Valor de ``skyrim_path`` REALMENTE en el disco, no el que creemos haber escrito.

    Es la mitad de FIX 4: el POST VERIFY pasaba ``config_skyrim_path=str(target)``
    —la ruta que el propio binder acababa de decidir— así que la superficie
    ``CONFIG_SKYRIM_PATH`` del oráculo era una copia de la EXPECTATIVA. Un
    ``persistir_campo`` que no persiste (permisos, merge-on-save que descarta el
    campo, un ``Config`` que no guarda) dejaba el POST en verde.
    """
    if config_path is None:
        return None
    from sky_claw.local.local_config import abrir_config_para_persistir

    try:
        cfg = abrir_config_para_persistir(config_path)
    except (OSError, ValueError) as exc:
        raise BinderVerificationError(f"no se pudo releer el config persistido ({config_path}): {exc}") from exc
    valor = getattr(cfg, "skyrim_path", None)
    if valor is None and hasattr(cfg, "_data"):
        valor = cfg._data.get("skyrim_path")
    return valor


def _norm(ruta: str | pathlib.Path | None) -> str | None:
    if ruta is None:
        return None
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(ruta))))


# ---------------------------------------------------------------------------
# Quiescencia y revalidación bajo la sección crítica (FIX 6)
# ---------------------------------------------------------------------------


def _exigir_quiescencia(observadores: RuntimeProbes, *, momento: str) -> None:
    snapshot = observadores.processes()
    if snapshot.indeterminate:
        raise BinderQuiescenceError(
            f"[{momento}] la tabla de procesos del SO es ilegible: no se puede descartar concurrencia"
        )
    vivos = snapshot.processes
    if any(p.kind is LiveProcessKind.MO2 for p in vivos):
        raise BinderQuiescenceError(
            f"[{momento}] se detectó una instancia de MO2 viva (ModOrganizer.exe): quiescencia requerida. "
            "El lock de Sky-Claw NO excluye a MO2"
        )
    if any(p.kind in (LiveProcessKind.GAME, LiveProcessKind.SKSE_LOADER) for p in vivos):
        raise BinderQuiescenceError(f"[{momento}] se detectó un proceso de juego o SKSE vivo: quiescencia requerida")


def _revalidar_bajo_lock(
    *,
    storage_path: pathlib.Path,
    mo2_dir: pathlib.Path,
    ini_path: pathlib.Path,
    target_clone_id: str,
    previo: _ObservacionPrevia,
    managed_source_root: pathlib.Path | None,
    config_skyrim_path: str | None,
    env_actual: Mapping[str, str],
    observadores: RuntimeProbes,
) -> tuple[BinderPreflightAssessment, bytes]:
    """Revalida TODO lo que el preflight inicial observó, ya bajo el lock.

    El preflight inicial corre fuera de la sección crítica (es caro y no debe
    sostener el lock). Entre ese preflight y la primera mutación hay una ventana
    en la que MO2 puede abrirse, el INI puede cambiar, el Clone puede
    reprovisionarse o el ``gamePath`` puede moverse. Esta función cierra la
    ventana hasta donde es cerrable: vuelve a mirar cada superficie justo antes
    de mutar, y devuelve los bytes del INI para que el snapshot durable se
    ancle a ESA lectura (si el archivo cambia entre la revalidación y el
    snapshot, la comparación de digests lo detecta).

    Lo que NO cierra: MO2 puede abrirse ENTRE esta función y el ``os.replace``
    del INI. Es una ventana de microsegundos que ningún lock de Sky-Claw cubre,
    porque MO2 no participa. Ver ``EXCLUSION_WITH_MO2`` en el docstring del
    módulo.
    """
    _exigir_quiescencia(observadores, momento="revalidación bajo lock")

    # Identidad del INI: se devuelve para anclar el snapshot durable, y se
    # compara contra la lectura del preflight inicial. Un INI que cambió en la
    # ventana no autoriza nada, aunque su ``gamePath`` siga siendo el mismo.
    try:
        bytes_ini = ini_path.read_bytes()
    except OSError as exc:
        raise BinderVerificationError(f"no se pudo releer modorganizer.ini bajo el lock: {exc}") from exc
    if recovery.huella(bytes_ini) != previo.ini_sha256:
        raise BinderVerificationError(
            "modorganizer.ini cambió entre el preflight inicial y la sección crítica: la identidad del INI no "
            "liga (alguien lo escribió fuera del lock de Sky-Claw)"
        )

    # Metadata del Clone: relectura fresca, no la del preflight.
    record = cargar_runtime_clone_record(storage_path, target_clone_id)
    if record is None:
        raise BinderPreflightFailedError(f"el Runtime Clone {target_clone_id} desapareció entre el preflight y el lock")
    if record.lifecycle is not RuntimeCloneLifecycle.PROVISIONED:
        raise BinderPreflightFailedError(
            f"el Runtime Clone {target_clone_id} dejó de estar PROVISIONED ({record.lifecycle.value})"
        )
    if record.source_generation_id != previo.generation_id:
        raise BinderPreflightFailedError(
            f"el Runtime Clone {target_clone_id} cambió de Generation: {record.source_generation_id} != "
            f"{previo.generation_id}"
        )
    if record.admitted_role != CANONICAL_ADMITTED_ROLE:
        raise BinderPreflightFailedError(
            f"el Runtime Clone {target_clone_id} tiene admitted_role {record.admitted_role!r}"
        )
    if _norm(record.root_path) != _norm(previo.clone_root):
        raise BinderPreflightFailedError(
            f"el Runtime Clone {target_clone_id} cambió de raíz: {record.root_path} != {previo.clone_root}"
        )

    # Preflight completo otra vez, con los datos frescos.
    preflight = evaluar_binder_preflight(
        storage_root=storage_path,
        mo2_instance_dir=mo2_dir,
        clone_id=target_clone_id,
        managed_source_root=managed_source_root,
        config_skyrim_path=config_skyrim_path,
        exigir_config_skyrim_path=True,
        environ=env_actual,
        probes=observadores,
    )
    if not preflight.is_ready:
        raise BinderPreflightFailedError(
            f"la revalidación bajo el lock no autorizó la operación: {[r.value for r in preflight.reasons]}"
        )
    if _norm(preflight.target_clone_root) != _norm(previo.clone_root):
        raise BinderVerificationError(
            f"deriva de destino entre preflight y lock: {preflight.target_clone_root} != {previo.clone_root}"
        )
    if preflight.source_generation_id != previo.generation_id:
        raise BinderVerificationError(
            f"deriva de Generation entre preflight y lock: {preflight.source_generation_id} != {previo.generation_id}"
        )
    if _norm(preflight.observed_mo2_game_path) != _norm(previo.game_path):
        raise BinderVerificationError(
            f"deriva del gamePath observado entre preflight y lock: {preflight.observed_mo2_game_path!r} != "
            f"{previo.game_path!r}"
        )
    # El Desired tampoco puede haber cambiado bajo los pies.
    desired_ahora = desired_runtime_from_storage(storage_path)
    if (desired_ahora.clone_id, desired_ahora.generation_id) != (previo.desired.clone_id, previo.desired.generation_id):
        raise BinderVerificationError(
            "el Desired Runtime cambió entre el preflight y el lock: "
            f"({desired_ahora.clone_id!r}, {desired_ahora.generation_id!r}) != "
            f"({previo.desired.clone_id!r}, {previo.desired.generation_id!r})"
        )
    return preflight, bytes_ini


# ---------------------------------------------------------------------------
# Autoridad de aprobación (FIX 5)
# ---------------------------------------------------------------------------


def _propuesta_desde_witness(
    witness: IssuanceWitness,
    *,
    transition_id: str,
    expected_effective_path: str,
    created_at_ns: int,
) -> PropuestaTransicion:
    """Reconstruye la propuesta desde el scope DURABLE, campo por campo.

    Es lo contrario del defecto que cierra FIX 5: el camino de
    ``aprobacion_consumida`` armaba una ``PropuestaTransicion`` con ``"prev"``,
    ``"cand"``, ``"setup-skse-clean"`` y digests de ceros. Esos valores no
    venían de ninguna autoridad: el journal los aceptaba porque el único
    cross-check era ``approval_scope_digest``, que el propio llamador aportaba.
    Con la propuesta reconstruida desde el witness, origen, destino, operación,
    setup, candidate y evidencia son los que el propietario aprobó.
    """
    from sky_claw.local.frozen_runtime.approval import _validar_source_activation

    payload = witness.scope_payload
    fuente_cruda = payload["source_activation"]
    if not isinstance(fuente_cruda, dict):
        raise BinderApprovalError("el witness declara un source_activation que no es un objeto: FAIL_CLOSED")
    fuente = SourceActivation(
        generation_id=None if fuente_cruda["generation_id"] is None else str(fuente_cruda["generation_id"]),
        clone_id=None if fuente_cruda["clone_id"] is None else str(fuente_cruda["clone_id"]),
        runtime_setup_id=None if fuente_cruda["runtime_setup_id"] is None else str(fuente_cruda["runtime_setup_id"]),
        effective_runtime_evidence=(
            None
            if fuente_cruda["effective_runtime_evidence"] is None
            else str(fuente_cruda["effective_runtime_evidence"])
        ),
    )
    _validar_source_activation(fuente, etiqueta="witness.scope_payload")
    if digest_canonico(fuente) != str(payload["source_activation_digest"]):
        raise BinderApprovalError(
            "el witness declara un source_activation cuyo digest no recomputa: el scope durable fue adulterado"
        )
    return PropuestaTransicion(
        transition_id=transition_id,
        operation=JournalOperation(str(payload["operation"])),
        source_activation=fuente,
        source_activation_digest=str(payload["source_activation_digest"]),
        target_generation_id=str(payload["generation_id"]),
        target_clone_id=str(payload["clone_id"]),
        target_runtime_setup_id=str(payload["runtime_setup_id"]),
        candidate_id=None if payload["candidate_id"] is None else str(payload["candidate_id"]),
        approval_scope_digest=witness.approval_scope_digest,
        evidence_digests=EvidenceDigests(
            clone_evidence=str(payload["clone_evidence"]),
            runtime_setup_evidence=str(payload["runtime_setup_evidence"]),
            compatibility_evidence=str(payload["compatibility_evidence_id"]),
        ),
        expected_effective_path=expected_effective_path,
        created_at_ns=created_at_ns,
    )


def _verificar_aprobacion_consumida_durable(
    storage_path: pathlib.Path,
    *,
    aprobacion: journal_module.AprobacionConsumida,
    propuesta: PropuestaTransicion,
) -> None:
    """Verifica una aprobación YA consumida contra la autoridad durable.

    No la consume: comprueba que la evidencia que el llamador presenta (i) existe
    como witness de emisión, (ii) fue terminalizada como ``CONSUMED`` por ESTA
    transición, y (iii) liga campo por campo el scope que la transición declara.
    """
    autoridad = derivar_autoridad_aprobacion(storage_path)
    witness = next((w for w in autoridad.witnesses if w.approval_seq == aprobacion.approval_seq), None)
    if witness is None:
        raise BinderApprovalError(
            f"no hay witness de emisión para approval_seq={aprobacion.approval_seq}: la aprobación presentada "
            "no existe en la autoridad durable"
        )
    if witness.approval_id != aprobacion.approval_id:
        raise BinderApprovalError(
            f"el witness de la seq {aprobacion.approval_seq} declara approval_id={witness.approval_id!r} y se "
            f"presentó {aprobacion.approval_id!r}"
        )
    if witness.approval_scope_digest != aprobacion.scope_digest:
        raise BinderApprovalError("el scope_digest presentado no coincide con el del witness durable")
    terminal = next(
        (
            t
            for t in autoridad.tombstones
            if t.approval_seq == aprobacion.approval_seq and t.approval_id == aprobacion.approval_id
        ),
        None,
    )
    if terminal is None:
        raise BinderApprovalError(
            f"la aprobación {aprobacion.approval_id} no tiene tombstone terminal: presentarla como consumida "
            "sería afirmar un consumo que la autoridad durable no registra"
        )
    if terminal.terminal_state is not TerminalState.CONSUMED:
        raise BinderApprovalError(
            f"la aprobación {aprobacion.approval_id} terminó en {terminal.terminal_state.value}, no CONSUMED"
        )
    if terminal.consumed_by_transition_id != propuesta.transition_id:
        raise BinderApprovalError(
            f"la aprobación fue consumida por {terminal.consumed_by_transition_id!r}, no por "
            f"{propuesta.transition_id!r} (single-use §35.2)"
        )
    if aprobacion.consumed_by_transition_id != propuesta.transition_id:
        raise BinderApprovalError("la evidencia presentada declara otra transición consumidora")
    exigir_proyeccion_ligada(
        proyeccion_desde_witness(witness),
        proyectar_propuesta(
            propuesta,
            approval_id=aprobacion.approval_id,
            approval_seq=aprobacion.approval_seq,
        ),
        etiqueta=f"reutilización de {aprobacion.approval_seq} por {propuesta.transition_id}",
    )


def _verificar_destino_de_la_aprobacion(
    propuesta: PropuestaTransicion,
    *,
    target_clone_id: str,
    target_generation_id: str,
    target_clone_root: pathlib.Path,
    runtime_setup_verificado: str | None,
) -> None:
    """La aprobación tiene que ser para el destino que el preflight va a bindear."""
    if propuesta.operation is not JournalOperation.PROMOTION:
        raise BinderApprovalError(
            f"la aprobación es de operación {propuesta.operation.value}: este binder sólo ejecuta PROMOTION"
        )
    if propuesta.target_clone_id != target_clone_id:
        raise BinderApprovalError(
            f"la aprobación es para el clone {propuesta.target_clone_id!r}, no para {target_clone_id!r}"
        )
    if propuesta.target_generation_id != target_generation_id:
        raise BinderApprovalError(
            f"la aprobación es para la Generation {propuesta.target_generation_id!r}, no para {target_generation_id!r}"
        )
    if _norm(propuesta.expected_effective_path) != _norm(target_clone_root):
        raise BinderApprovalError(
            f"la aprobación declara un destino efectivo {propuesta.expected_effective_path!r} distinto de la raíz "
            f"del clone {str(target_clone_root)!r}"
        )
    # El SETUP también es parte de lo aprobado. Sin este cruce, una aprobación
    # podía nombrar el clone y la Generation correctos y un setup distinto del
    # que el Clone tiene VERIFICADO: el preflight valida la integridad del setup
    # provisionado, no el que el propietario aprobó, y el journal sólo compara la
    # propuesta contra el scope (Codex sobre #843).
    if runtime_setup_verificado is not None and propuesta.target_runtime_setup_id != runtime_setup_verificado:
        raise BinderApprovalError(
            f"la aprobación es para el setup {propuesta.target_runtime_setup_id!r} y el Runtime Clone "
            f"{target_clone_id!r} tiene verificado {runtime_setup_verificado!r}: el propietario aprobó un "
            "runtime que no es el que se va a activar"
        )


# ---------------------------------------------------------------------------
# Coordinador Transaccional Principal
# ---------------------------------------------------------------------------


def ejecutar_transicion_binder(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    target_clone_id: str,
    propuesta: PropuestaAprobacion | None = None,
    consentimiento: ConsentimientoAprobacion | None = None,
    aprobacion_consumida: journal_module.AprobacionConsumida | None = None,
    managed_source_root: pathlib.Path | None = None,
    config_path: pathlib.Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
    transition_id: str | None = None,
) -> BinderResult:
    """Ejecuta la transición segura y transaccional de MO2 hacia un Runtime Clone.

    Sigue estrictamente el orden causal de ADR 0012:
    PERSIST PENDING → BIND EFFECTIVE → VERIFY EFFECTIVE → CAS UPDATE DESIRED → POST VERIFY → FINALIZE

    Orden de las fases, y por qué:

    1. **Preflight inicial** (fuera del lock): es caro y no debe sostener la
       exclusión. Autoriza o falla.
    2. **Autorización**: se arma la propuesta desde el scope aprobado (o se
       verifica la ya consumida contra la autoridad durable).
    3. **Sonda del lock**: si el root lock está tomado, se falla ANTES de quemar
       la aprobación. Quemar sin poder entrar deja al dueño sin aprobación y sin
       transición.
    4. **Burn**: ``consumir_aprobacion`` adquiere su propio lock, así que corre
       fuera de la sección crítica (el lock no es reentrante).
    5. **Sección crítica**: revalidación → snapshot durable → PENDING → BIND →
       VERIFY → CAS → POST → FINALIZE, con rollback desde el registro durable
       ante cualquier fallo.
    """
    storage_path = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    mo2_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))
    ini_path = mo2_dir / "modorganizer.ini"

    observadores = probes if probes is not None else default_runtime_probes()
    env_actual = environ if environ is not None else os.environ

    # -------------------------------------------------------------------------
    # FASE 1 — Preflight inicial
    # -------------------------------------------------------------------------
    _exigir_quiescencia(observadores, momento="preflight inicial")

    # Identidad del INI ANTES de cualquier lectura del preflight: es el ancla
    # contra la que se compara la revalidación bajo el lock. Sin ella, un cambio
    # entre el preflight inicial y la sección crítica es invisible —ambas
    # lecturas verían el archivo ya modificado—.
    try:
        ini_bytes_inicial = ini_path.read_bytes()
    except OSError as exc:
        raise BinderVerificationError(f"no se pudo leer modorganizer.ini al inicio: {exc}") from exc
    ini_sha_inicial = recovery.huella(ini_bytes_inicial)

    cfg_skyrim_actual = _leer_skyrim_path_persistido(config_path)
    desired_actual = desired_runtime_from_storage(storage_path)

    clone_rec_candidato = cargar_runtime_clone_record(storage_path, target_clone_id)
    if clone_rec_candidato is not None:
        candidato_root_norm = _norm(clone_rec_candidato.root_path)
        evidencia_ini_actual = leer_game_path_de_mo2(ini_path)
        if (
            desired_actual.clone_id == target_clone_id
            and desired_actual.clone_root is not None
            and _norm(desired_actual.clone_root) == candidato_root_norm
            and evidencia_ini_actual.state is ObservationState.OBSERVED
            and evidencia_ini_actual.path is not None
            and _norm(evidencia_ini_actual.path) == candidato_root_norm
        ):
            post_eval_existente = assess_effective_runtime(
                mo2_instance_dir=mo2_dir,
                desired=desired_actual,
                managed_source_root=managed_source_root,
                config_skyrim_path=cfg_skyrim_actual,
                environ=env_actual,
                probes=observadores,
            )
            if post_eval_existente.authorizes_activation:
                # El estado físico coincide, pero eso NO prueba que la
                # transición haya terminado: un crash entre BIND y FINALIZE deja
                # exactamente estas cuatro superficies y el journal en PENDING.
                # Declarar éxito ahí convertiría un `PENDING` en un `SUCCESS`
                # sin evidencia durable (Codex sobre #843).
                _exigir_finalizacion_durable(
                    storage_path,
                    target_clone_id,
                    clone_rec_candidato.source_generation_id,
                )
                return BinderResult(
                    success=True,
                    message="El runtime ya se encuentra activado y consistente en el Runtime Clone objetivo",
                    transition_id="idempotent-noop",
                    target_clone_id=target_clone_id,
                    target_clone_root=pathlib.Path(clone_rec_candidato.root_path),
                    target_generation_id=clone_rec_candidato.source_generation_id,
                    previous_effective_path=clone_rec_candidato.root_path,
                    new_effective_path=clone_rec_candidato.root_path,
                )

    preflight = evaluar_binder_preflight(
        storage_root=storage_path,
        mo2_instance_dir=mo2_dir,
        clone_id=target_clone_id,
        managed_source_root=managed_source_root,
        config_skyrim_path=cfg_skyrim_actual,
        exigir_config_skyrim_path=True,
        environ=env_actual,
        probes=observadores,
    )
    if not preflight.is_ready:
        raise BinderPreflightFailedError(
            f"El preflight de integración no autorizó la operación: {[r.value for r in preflight.reasons]}"
        )
    assert preflight.target_clone_root is not None
    assert preflight.source_generation_id is not None
    target_clone_root = preflight.target_clone_root
    target_generation_id = preflight.source_generation_id
    previous_path = preflight.observed_mo2_game_path or ""
    # Setup que el Clone tiene VERIFICADO. Es lo que el propietario tiene que
    # haber aprobado: el preflight valida la integridad del setup provisionado,
    # no su identidad contra el scope.
    setup_verificado = clone_rec_candidato.verified_runtime_setup_id if clone_rec_candidato is not None else None
    previo = _ObservacionPrevia(
        clone_root=target_clone_root,
        generation_id=target_generation_id,
        game_path=previous_path,
        desired=desired_actual,
        ini_sha256=ini_sha_inicial,
    )

    # -------------------------------------------------------------------------
    # FASE 2 — Autorización (sin valores sintéticos)
    # -------------------------------------------------------------------------
    t_id = transition_id or str(uuid.uuid4())
    aprobacion_final: journal_module.AprobacionConsumida | None = aprobacion_consumida
    ya_consumida = aprobacion_consumida is not None

    if ya_consumida:
        assert aprobacion_consumida is not None
        autoridad = derivar_autoridad_aprobacion(storage_path)
        witness = next(
            (w for w in autoridad.witnesses if w.approval_seq == aprobacion_consumida.approval_seq),
            None,
        )
        if witness is None:
            raise BinderApprovalError(
                f"no hay witness de emisión para approval_seq={aprobacion_consumida.approval_seq}"
            )
        propuesta_transicion = _propuesta_desde_witness(
            witness,
            transition_id=t_id,
            expected_effective_path=str(target_clone_root),
            created_at_ns=time.time_ns(),
        )
        _verificar_destino_de_la_aprobacion(
            propuesta_transicion,
            target_clone_id=target_clone_id,
            target_generation_id=target_generation_id,
            target_clone_root=target_clone_root,
            runtime_setup_verificado=setup_verificado,
        )
        _verificar_aprobacion_consumida_durable(
            storage_path,
            aprobacion=aprobacion_consumida,
            propuesta=propuesta_transicion,
        )
    else:
        if propuesta is None or consentimiento is None:
            raise BinderApprovalError(
                "No se suministró una propuesta ni consentimiento válido para autorizar el switch"
            )
        if consentimiento.approval_scope_digest != propuesta.scope.approval_scope_digest:
            raise BinderApprovalError("El consentimiento suministrado no coincide con el digest del scope propuesto")
        propuesta_transicion = PropuestaTransicion(
            transition_id=t_id,
            operation=JournalOperation(propuesta.scope.operation.value),
            source_activation=propuesta.scope.source_activation,
            source_activation_digest=propuesta.scope.source_activation_digest,
            target_generation_id=propuesta.scope.generation_id,
            target_clone_id=propuesta.scope.clone_id,
            target_runtime_setup_id=propuesta.scope.runtime_setup_id,
            candidate_id=propuesta.scope.candidate_id,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            evidence_digests=EvidenceDigests(
                clone_evidence=propuesta.scope.clone_evidence,
                runtime_setup_evidence=propuesta.scope.runtime_setup_evidence,
                compatibility_evidence=propuesta.scope.compatibility_evidence_id,
            ),
            expected_effective_path=str(target_clone_root),
            created_at_ns=time.time_ns(),
        )
        _verificar_destino_de_la_aprobacion(
            propuesta_transicion,
            target_clone_id=target_clone_id,
            target_generation_id=target_generation_id,
            target_clone_root=target_clone_root,
            runtime_setup_verificado=setup_verificado,
        )

    # -------------------------------------------------------------------------
    # FASE 3 — Sonda del lock ANTES de quemar la aprobación
    # -------------------------------------------------------------------------
    # El store de recuperación tiene que existir en TODAS las ramas de
    # aprobación, también en la de ya consumida: un root inicializado por una
    # versión anterior sin store descubre el fallo recién al persistir el
    # snapshot, DESPUÉS de escribir el journal PENDING — un estado que sólo
    # puede resolver el dueño (CodeRabbit sobre #843). La comprobación no tiene
    # efectos y no depende de la rama.
    try:
        recovery.exigir_store_inicializado(storage_path)
    except recovery.RecuperacionError as exc:
        raise BinderError(
            f"el store de recuperación no está inicializado en {storage_path}: NO se consume la aprobación "
            f"para una transición que no podría dejar snapshot durable. Reejecutá "
            f"initialize_frozen_runtime_storage y reintentá. Detalle: {exc}"
        ) from exc

    if not ya_consumida:
        inspeccion = inspect_frozen_runtime_root_lock(storage_path)
        if inspeccion.disposition is not LockDisposition.FREE:
            raise BinderLockUnavailableError(
                f"el root lock de {storage_path} está en {inspeccion.disposition.value}: NO se consume la "
                "aprobación para una transición que no puede entrar a la sección crítica. "
                "Reintentar cuando el lock esté libre (la aprobación sigue ISSUED)"
            )

        # ---------------------------------------------------------------------
        # FASE 4 — Burn (burn-first: la validación ya terminó)
        # ---------------------------------------------------------------------
        # Invariante de ejecución: sin ``ya_consumida`` la rama de autorización
        # exigió propuesta y consentimiento, o levantó.
        assert propuesta is not None
        try:
            res_consumo = consumir_aprobacion(
                storage_path,
                approval_id=propuesta.scope.approval_id,
                approval_seq=propuesta.scope.approval_seq,
                approval_scope_digest=propuesta.scope.approval_scope_digest,
                transition_id=t_id,
                transicion=propuesta_transicion,
            )
            aprobacion_final = res_consumo.a_evidencia_journal()
        except Exception as exc:
            raise BinderApprovalError(f"Fallo al consumir la aprobación: {exc}") from exc

    assert aprobacion_final is not None

    # -------------------------------------------------------------------------
    # FASE 5 — Sección crítica
    # -------------------------------------------------------------------------
    try:
        handle_lock = participant_lock(storage_path, timeout=0.0)
    except Exception as exc:
        raise BinderLockUnavailableError(
            f"no se pudo adquirir el root lock de {storage_path} después de consumir la aprobación: {exc}. "
            f"RECUPERACIÓN: reintentar con transition_id={t_id!r} — el consumo es idempotente para ESA "
            "transición (approval.consumir_aprobacion reconoce state=CONSUMED y exige el mismo id), así que "
            "el reintento completa la transición en vez de perder la aprobación"
        ) from exc

    with handle_lock as handle:
        handle.assert_owned()

        # 5.1 — Revalidación bajo la sección crítica (FIX 6)
        _preflight_lock, bytes_ini_revalidado = _revalidar_bajo_lock(
            storage_path=storage_path,
            mo2_dir=mo2_dir,
            ini_path=ini_path,
            target_clone_id=target_clone_id,
            previo=previo,
            managed_source_root=managed_source_root,
            config_skyrim_path=cfg_skyrim_actual,
            env_actual=env_actual,
            observadores=observadores,
        )

        # 5.2 — Snapshot durable (FIX 3). Se CAPTURA antes del PENDING para que un
        # fallo de lectura no deje un journal PENDING sin superficies ni
        # mutaciones (un PENDING así sólo lo puede resolver el dueño). Se
        # PERSISTE después, porque el binding necesita la revisión del PENDING.
        estado_previo: EstadoPrevioObservado = observar_estado_previo(active_state_path(storage_path))
        try:
            registro_base = recovery.capturar_registro(
                storage_root=storage_path,
                transition_id=t_id,
                journal_revision=0,  # se reemplaza por la real al persistir
                operation=propuesta_transicion.operation.value,
                target_clone_id=target_clone_id,
                target_generation_id=target_generation_id,
                expected_effective_path=str(target_clone_root),
                mo2_ini_path=ini_path,
                config_path=config_path,
                environ_skyrim_path=env_actual.get("SKYRIM_PATH"),
            )
        except recovery.RecuperacionError as exc:
            raise BinderError(f"Fallo al capturar el snapshot durable de superficies: {exc}") from exc

        # El snapshot tiene que describir el MISMO INI que la revalidación miró.
        # Si cambió entre ambas lecturas, alguien escribió en el medio y no hay
        # nada que autorice mutarlo.
        superficie_ini = registro_base.superficie(recovery.SUPERFICIE_MO2_INI)
        if superficie_ini is None or superficie_ini.sha256 != recovery.huella(bytes_ini_revalidado):
            raise BinderVerificationError(
                "modorganizer.ini cambió entre la revalidación bajo lock y el snapshot durable: "
                "no se muta una superficie cuya identidad no liga"
            )

        # 5.3 — PERSIST PENDING
        historial = derivar_historial_finalizados(storage_path)
        if not historial.registros or historial.latest is None:
            doc_pendiente = _crear_pendiente_locked(handle, storage_path, propuesta_transicion, aprobacion_final)
        else:
            doc_pendiente = _start_next_locked(handle, storage_path, propuesta_transicion, aprobacion_final)

        registro = dataclasses.replace(registro_base, journal_revision=doc_pendiente.journal_revision)
        recovery.escribir_registro_locked(handle, storage_path, registro)

        mutadas: set[str] = set()
        diagnostico_exclusion: DiagnosticoExclusion | None = None
        # ``mutadas`` marca la superficie ANTES de escribirla (para que el
        # rollback la cubra si el proceso muere en el medio). Eso no dice si la
        # escritura llegó a intentarse: entre la marca y el escritor están la
        # adquisición de la exclusión y la verificación de huella, que pueden
        # abortar sin tocar el archivo. Este flag es la verdad de proceso y es lo
        # que impide atribuir a la transición un cambio de un tercero.
        ini_escritura_intentada = False
        try:
            # -----------------------------------------------------------------
            # 5.4 — BIND EFFECTIVE (mutación de superficies)
            # -----------------------------------------------------------------
            recovery.marcar_fase_locked(
                handle, storage_path, transition_id=t_id, fase=recovery.FaseRecuperacion.INI_ESCRITO
            )
            mutadas.add(recovery.SUPERFICIE_MO2_INI)
            # P5-C2: la exclusión de mutación se toma ACÁ, después del snapshot
            # (que lee por ruta) y antes de la primera escritura. Con el handle
            # retenido, ningún otro proceso puede escribir, borrar ni renombrar
            # el INI; la comparación de huella y la escritura usan ESE handle, así
            # que no queda ventana entre comprobar y mutar.
            exclusion = adquirir_exclusion_ini(ini_path)
            diagnostico_exclusion = exclusion.diagnostico
            try:
                if not exclusion.autoriza:
                    raise BinderExclusionError(
                        "no se pudo establecer la exclusión de mutación sobre modorganizer.ini: "
                        f"estado={exclusion.estado.value} motivo={exclusion.diagnostico.motivo} "
                        f"intentos={exclusion.diagnostico.intentos}"
                    )
                # La huella contra la que se compara es la del SNAPSHOT durable:
                # es el estado al que el rollback volvería. Si el INI cambió
                # entre el snapshot y la exclusión, no hay nada que autorice
                # mutarlo y se falla ANTES de escribir.
                try:
                    exclusion.verificar_huella(superficie_ini.sha256)
                except Mo2IniExclusionError as exc:
                    raise BinderVerificationError(
                        "modorganizer.ini cambió entre el snapshot durable y la exclusión: "
                        f"no se muta una superficie cuya identidad no liga ({exc})"
                    ) from exc
                ini_escritura_intentada = True
                try:
                    actualizar_game_path_por_handle(exclusion, target_clone_root)
                except Mo2IniEscrituraError as exc:
                    raise BinderEscrituraIniError(f"no se pudo escribir modorganizer.ini: {exc}") from exc
            finally:
                exclusion.cerrar()

            if config_path is not None and config_path.exists():
                recovery.marcar_fase_locked(
                    handle, storage_path, transition_id=t_id, fase=recovery.FaseRecuperacion.CONFIG_ESCRITA
                )
                mutadas.add(recovery.SUPERFICIE_CONFIG)
                persistir_campo(config_path, "skyrim_path", str(target_clone_root))

            if environ is not None:
                recovery.marcar_fase_locked(
                    handle, storage_path, transition_id=t_id, fase=recovery.FaseRecuperacion.ENTORNO_ACTUALIZADO
                )
                mutadas.add(recovery.SUPERFICIE_ENVIRON)
                environ["SKYRIM_PATH"] = str(target_clone_root)

            # -----------------------------------------------------------------
            # 5.5 — VERIFY EFFECTIVE (readback de superficies persistidas)
            # -----------------------------------------------------------------
            readback_ini = leer_game_path_de_mo2(ini_path)
            if readback_ini.state is not ObservationState.OBSERVED or not readback_ini.path:
                raise BinderVerificationError(
                    f"Fallo de readback en modorganizer.ini: estado {readback_ini.state.value}"
                )
            if _norm(readback_ini.path) != _norm(target_clone_root):
                raise BinderVerificationError(
                    f"Readback divergente: {_norm(readback_ini.path)} != {_norm(target_clone_root)}"
                )
            cfg_persistido = _leer_skyrim_path_persistido(config_path)
            if config_path is not None and _norm(cfg_persistido) != _norm(target_clone_root):
                raise BinderVerificationError(
                    f"VERIFY EFFECTIVE: config.skyrim_path persistido es {cfg_persistido!r} y se escribió "
                    f"{str(target_clone_root)!r}"
                )

            # -----------------------------------------------------------------
            # 5.6 — CAS UPDATE DESIRED (FIX 2)
            # -----------------------------------------------------------------
            recovery.marcar_fase_locked(
                handle, storage_path, transition_id=t_id, fase=recovery.FaseRecuperacion.DESEADO_ACTUALIZADO
            )
            active_payload: dict[str, object] = {
                "schema_version": 2,
                "desired_generation_id": target_generation_id,
                "desired_clone_id": target_clone_id,
                "updated_at_ns": time.time_ns(),
            }
            # Se marca ANTES de escribir: si el CAS publica y el READBACK falla,
            # la superficie YA se mutó y el rollback tiene que restaurarla. Marcarla
            # después dejaba `active.json` fuera del conjunto y el rollback lo
            # excluía pese a haberlo escrito (Codex sobre #843).
            mutadas.add(recovery.SUPERFICIE_ACTIVE)
            try:
                _escribir_estado_activo_cas_locked(
                    active_state_path(storage_path),
                    active_payload,
                    esperado=estado_previo,
                )
            except StateCasError as exc:
                # El CAS falló ⇒ NO escribimos active.json. Sacarlo del conjunto
                # de superficies mutadas evita que el rollback pise la escritura
                # del tercero que ganó la carrera.
                mutadas.discard(recovery.SUPERFICIE_ACTIVE)
                raise BinderCasError(
                    f"CAS UPDATE DESIRED rechazado: el estado durable cambió entre el snapshot y la escritura. "
                    f"No se sobrescribió. Detalle: {exc}"
                ) from exc

            # -----------------------------------------------------------------
            # 5.7 — POST VERIFY (oráculo sobre datos REALES, FIX 4)
            # -----------------------------------------------------------------
            desired_persistido = desired_runtime_from_storage(storage_path)
            if desired_persistido.clone_id != target_clone_id:
                raise BinderVerificationError(
                    f"POST VERIFY: el Desired persistido declara clone {desired_persistido.clone_id!r} y se "
                    f"escribió {target_clone_id!r}"
                )
            if _norm(desired_persistido.clone_root) != _norm(target_clone_root):
                raise BinderVerificationError(
                    f"POST VERIFY: el Desired persistido resuelve la raíz {desired_persistido.clone_root} y se "
                    f"escribió {target_clone_root}"
                )
            if desired_persistido.generation_id != target_generation_id:
                raise BinderVerificationError(
                    f"POST VERIFY: el Desired persistido declara Generation {desired_persistido.generation_id!r} "
                    f"y se escribió {target_generation_id!r}"
                )

            cfg_post = _leer_skyrim_path_persistido(config_path)
            post_eval = assess_effective_runtime(
                mo2_instance_dir=mo2_dir,
                desired=DesiredRuntime(
                    clone_id=target_clone_id,
                    clone_root=target_clone_root,
                    generation_id=target_generation_id,
                ),
                managed_source_root=managed_source_root,
                # El valor PERSISTIDO, no la ruta que acabamos de decidir.
                config_skyrim_path=cfg_post,
                environ=env_actual,
                probes=observadores,
            )
            if not post_eval.authorizes_activation:
                raise BinderVerificationError(
                    f"POST VERIFY no autoriza la activación del runtime: verdict={post_eval.verdict.value}, "
                    f"reasons={[r.value for r in post_eval.reasons]}"
                )

            # -----------------------------------------------------------------
            # 5.8 — FINALIZE
            # -----------------------------------------------------------------
            journal_actual = cargar_journal_transicion(storage_path).journal
            rev_journal = journal_actual.journal_revision if journal_actual else doc_pendiente.journal_revision

            eff_obs_payload = {
                "verdict": post_eval.verdict.value,
                "effective_path": str(target_clone_root),
                "mo2_instance_dir": str(mo2_dir),
                "observed_at_ns": post_eval.observed_at_ns,
            }
            effective_obs_digest = digest_canonico(eff_obs_payload)

            verified_now = time.time_ns()
            post_evidence_payload = {
                "transition_id": t_id,
                "resultado": PostVerifyResultado.PASS.value,
                "expected_journal_revision": rev_journal,
                "source_activation_digest": propuesta_transicion.source_activation_digest,
                "target_generation_id": target_generation_id,
                "target_clone_id": target_clone_id,
                "target_runtime_setup_id": propuesta_transicion.target_runtime_setup_id,
                "desired_active_generation": target_generation_id,
                "effective_observation_digest": effective_obs_digest,
                "verified_at_ns": verified_now,
            }
            evidence_digest = digest_canonico(post_evidence_payload)

            post_evidence = PostVerifyEvidence(
                transition_id=t_id,
                resultado=PostVerifyResultado.PASS,
                source_activation_digest=propuesta_transicion.source_activation_digest,
                target_generation_id=target_generation_id,
                target_clone_id=target_clone_id,
                target_runtime_setup_id=propuesta_transicion.target_runtime_setup_id,
                desired_active_generation=target_generation_id,
                effective_observation_digest=effective_obs_digest,
                payload_digest=evidence_digest,
                verified_at_ns=verified_now,
                expected_journal_revision=rev_journal,
            )

            record_final = _finalizar_locked(handle, storage_path, post_evidence)

            resultado = BinderResult(
                success=True,
                message="Transición a Runtime Clone ejecutada y finalizada con éxito",
                transition_id=t_id,
                target_clone_id=target_clone_id,
                target_clone_root=target_clone_root,
                target_generation_id=target_generation_id,
                previous_effective_path=previous_path,
                new_effective_path=str(target_clone_root),
                post_verify_evidence=post_evidence,
                finalized_record=record_final,
                exclusion=diagnostico_exclusion,
            )

        except BaseException as exc:
            try:
                publicada = _finalizacion_publicada(storage_path, t_id)
            except Exception as lectura_exc:  # noqa: BLE001 — la lectura rota NO revierte: ver comentario
                # La autoridad de finalización NO se pudo leer: la publicación no
                # se puede descartar, y revertir una transición durablente
                # finalizada es peor que dejar las superficies mutadas con su
                # registro de recuperación intacto. Se re-levanta el error
                # ORIGINAL, sin enmascararlo con el de la lectura (CodeRabbit
                # sobre d2a17829).
                logger.error(
                    "No se pudo leer el historial de finalizaciones de la transición %s: no se revierte porque "
                    "la publicación no se puede descartar; el registro de recuperación se conserva. Error: %s",
                    t_id,
                    lectura_exc,
                )
                # Sin ``from``: el error original puede tener su propio
                # ``__cause__`` (BinderEscrituraIniError desde Mo2IniEscrituraError,
                # BinderCasError desde StateCasError) y un re-raise encadenado lo
                # pisaría. La lectura rota queda como ``__context__`` implícito y
                # el logger.error la registra (CodeRabbit sobre 7d9859eb).
                raise exc  # noqa: B904 — deliberado: re-raise SIN from preserva el __cause__ original
            if publicada:
                # El registro inmutable ya está publicado (crash/fallo entre la
                # F1b y la F2 de FINALIZE): la transición terminó de forma
                # durable y NO es reversible. Revertir las superficies dejaría
                # un historial que afirma que el runtime se activó con las
                # superficies apuntando al anterior (Codex sobre #843).
                logger.warning(
                    "Fallo %s DESPUÉS de publicar la finalización de la transición %s: no se revierte. "
                    "La reconciliación del journal corresponde a su dueño",
                    type(exc).__name__,
                    t_id,
                )
                raise BinderError(
                    f"la transición {t_id} ya tiene su registro FINALIZED publicado: el fallo es POSTERIOR a "
                    "la finalización durable y NO se revierte (la evidencia no se deshace). Las superficies "
                    "quedan en el estado verificado por POST y la reconciliación del journal corresponde a "
                    "su dueño"
                ) from exc
            logger.warning(
                "Fallo durante la transición del binder (transition_id=%s): iniciando rollback desde el "
                "registro durable. Error: %s",
                t_id,
                exc,
            )
            _rollback(
                handle,
                storage_path,
                registro,
                env_actual if environ is not None else None,
                mutadas,
                t_id,
                ini_escritura_intentada=ini_escritura_intentada,
            )
            raise

        # FUERA de la transacción, a propósito. El journal ya finalizó y el
        # registro inmutable está publicado: revertir las superficies acá
        # dejaría un historial que afirma que el nuevo runtime se activó con las
        # superficies apuntando al anterior (Codex sobre #843). Si el borrado
        # falla, el registro queda obsoleto y `recuperar_transicion_interrumpida`
        # lo retira sin restaurar nada.
        _retirar_registro_finalizado(handle, storage_path, t_id)
        return resultado


def _finalizacion_publicada(storage_path: pathlib.Path, transition_id: str) -> bool:
    """¿Ya existe el registro inmutable de ESTA transición en el historial?

    Es la autoridad durable que distingue «fallo reversible» de «fallo posterior
    a la finalización»: a partir de la F1b de ``_finalizar_locked`` el registro
    está publicado y la transición ya no se revierte.
    """
    return any(
        registro.transition_id == transition_id for registro in derivar_historial_finalizados(storage_path).registros
    )


def _exigir_finalizacion_durable(
    storage_path: pathlib.Path,
    target_clone_id: str,
    target_generation_id: str,
) -> None:
    """El no-op idempotente sólo es legítimo si la transición TERMINÓ de verdad.

    ``Desired == Effective`` con las cuatro superficies apuntando al Clone es
    también el estado que deja un crash entre ``BIND`` y ``FINALIZE``. La única
    evidencia que distingue «terminó» de «quedó a medias» es durable: journal sin
    ``PENDING`` y un registro FINALIZED que nombre ese Clone y esa Generation.
    """
    snapshot = cargar_journal_transicion(storage_path)
    if snapshot.disposition is not JournalDisposition.NONE:
        raise BinderVerificationError(
            "el runtime ya coincide con el Clone, pero el journal está en "
            f"{snapshot.disposition.value}: la transición NO terminó (crash entre BIND y FINALIZE). "
            "No se declara éxito sobre un estado que sólo la recuperación puede clasificar"
        )
    historial = derivar_historial_finalizados(storage_path)
    ultimo = historial.latest
    if (
        ultimo is None
        or ultimo.target_clone_id != target_clone_id
        or ultimo.target_generation_id != target_generation_id
    ):
        raise BinderVerificationError(
            "el runtime ya coincide con el Clone, pero no hay un registro FINALIZED que lo respalde "
            f"(último: {None if ultimo is None else (ultimo.target_clone_id, ultimo.target_generation_id)}). "
            "Sin evidencia durable no se declara éxito"
        )


def _retirar_registro_finalizado(handle: object, storage_path: pathlib.Path, transition_id: str) -> None:
    """Retira el registro DESPUÉS de que el journal finalizó. Best-effort declarado.

    No forma parte de la transacción: si falla (permisos, un handle abierto en
    Windows), la transición YA está publicada y revertirla sería peor que dejar
    el registro. Queda como obsoleto y la recuperación lo detecta por el
    historial FINALIZED y lo retira sin restaurar.
    """
    try:
        recovery.eliminar_registro_locked(handle, storage_path, transition_id)
    except OSError as exc:
        logger.warning(
            "no se pudo retirar el registro de recuperación %s tras finalizar la transición: %s. "
            "Queda como obsoleto: recuperar_transicion_interrumpida lo retira sin restaurar.",
            transition_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Rollback (FIX 1)
# ---------------------------------------------------------------------------


def _rollback(
    handle: object,
    storage_path: pathlib.Path,
    registro: recovery.RegistroRecuperacion,
    environ: MutableMapping[str, str] | None,
    mutadas: set[str],
    transition_id: str,
    *,
    ini_escritura_intentada: bool = False,
) -> None:
    """Restaura las superficies REALMENTE mutadas y verifica cada una.

    ``mutadas`` es la verdad de proceso: el binder sabe qué llamadas
    completaron. La fase del registro es la verdad de reinicio: se marca ANTES
    de cada mutación, así que sobre-restaura (idempotente) en vez de dejar una
    mutación suelta. Las dos alimentan la MISMA rutina de restauración.
    """
    try:
        verificaciones = recovery.restaurar_registro_locked(
            handle,
            storage_path,
            registro,
            environ,
            solo_superficies=frozenset(mutadas),
            # Sólo el INI tiene estado intermedio atribuible: el binder sabe si
            # llegó a llamar al escritor. Si no lo llamó, un contenido que
            # "parece" un prefijo nuestro es de un tercero y no se toca.
            escritura_intentada=ini_escritura_intentada,
        )
        recovery.exigir_restauracion_completa(verificaciones)
    except Exception as rollback_exc:
        raise BinderRollbackFailedError(
            f"CRÍTICO: el rollback de la transición {transition_id} no pudo restaurar las superficies "
            f"({[v.kind for v in locals().get('verificaciones', ()) if not v.ok]}): {rollback_exc}. "
            f"El registro durable queda en {recovery_state_dir(storage_path) / (transition_id + '.json')} "
            "para recuperación manual con binder_recovery.recuperar_transicion_interrumpida"
        ) from rollback_exc
