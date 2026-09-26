"""Servicio backend ``REGISTER_OR_REFRESH_TRUSTED_GOLDEN`` (ADR 0010 §11.4).

Contrato único de alta/refresco del Trusted Golden Registry. Es **independiente
del FSM de GP2 apply**: no agrega estados a ese FSM y su registro de operación
no es un WAL ni un mecanismo de recovery/replay del TGR.

Garantías que este módulo concentra (ninguna superficie reimplementa una):

- la expectativa nunca viene de staging: la fuente se valida, la expectativa se
  admite y el receipt lo emite este servicio (helper-issued, one-use, atado a
  ``operation_id``);
- ``INDEPENDENT_PROVENANCE`` exige un provider de provenance explícito: los
  ``expected_tree``/``expected_runtime``/``source_reference`` de la solicitud son
  candidatos del caller y **nunca** autoridad. Sin provider cableado la operación
  termina ``SOURCE_UNAVAILABLE`` (``REJECTED``, cero TGR writes) antes de
  cualquier efecto secundario; la expectativa ``ADMITTED`` se construye sólo con
  lo que devuelve el provider;
- el claim del ``operation_id`` es one-use y pertenece a UNA sola solicitud: el
  perdedor de la carrera nunca escribe (ni lee) el registro del ganador;
- la observación fresca y el RV-2 fresco pasan por el mismo bridge bajo el
  token original del operador; el runtime se re-adquiere en cada pass y jamás se
  copia de ``expected_runtime``;
- la confirmación privilegiada usa el DTO propio de Golden Admission (no la
  PPSC de GP2 apply) y sólo ``CONFIRMED`` produce recibo;
- el registro de auditoría se persiste con ``before``/``intended after`` ANTES
  del replace del TGR y se revalida contra disco; si esa revalidación falla, no
  se escribe el TGR;
- todo desenlace que no es ``REGISTERED`` deja el TGR sin modificar; si el
  resultado del replace llegara a ser ambiguo se revalida el TGR contra
  ``before``/``after`` exactos y se reporta ``COMMIT_OUTCOME_UNKNOWN`` (nunca
  ``REJECTED``).

La distinción que sostiene el punto anterior es mecánica y no de criterio:
``mutate_trusted_golden_registry`` sólo ejecuta el writer DESPUÉS de que el
mutador devuelve, así que una excepción con el mutador incompleto significa
"no hubo replace" (``REJECTED``, TGR intacto) y una excepción posterior a ese
punto exige revalidar antes de declarar nada.

Síncrono y bloqueante por diseño: en asyncio usar ``asyncio.to_thread``.
"""

from __future__ import annotations

import datetime as _datetime
import logging
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sky_claw.local.runtime_vault.critical_expectations import (
    CriticalExpectationsDigestError,
    critical_expectations_digest,
)
from sky_claw.local.runtime_vault.golden_admission import (
    ACTIVE_HELPER_POLICY_VERSION,
    GoldenAdmissionClaimAlreadyExistsError,
    GoldenAdmissionConfirmation,
    GoldenAdmissionConfirmationProvider,
    GoldenAdmissionConfirmationReceipt,
    GoldenAdmissionConfirmationStage,
    GoldenAdmissionExpectation,
    GoldenAdmissionModelError,
    GoldenAdmissionObservation,
    GoldenAdmissionOutcome,
    GoldenAdmissionReceipt,
    GoldenAdmissionRejectionReason,
    GoldenAdmissionRequestError,
    GoldenAdmissionSource,
    GoldenAdmissionSourceError,
    GoldenAdmissionSourceUnavailableError,
    GoldenAdmissionState,
    GoldenAdmissionStoreError,
    issue_golden_admission_receipt,
    require_golden_admission_outcome,
    validate_operation_id,
)
from sky_claw.local.runtime_vault.golden_admission_store import (
    GoldenAdmissionObservationRecord,
    GoldenAdmissionResultRecord,
    GoldenAdmissionSourceReference,
    append_observation,
    append_result,
    bind_tgr_replacement,
    create_admission_record,
    derive_admission_record_dir,
    revalidate_for_tgr_replace,
)
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    RuntimeIdentity,
    RuntimeVaultError,
    TreeDigest,
)
from sky_claw.local.runtime_vault.operator_token import OperatorPrimaryToken
from sky_claw.local.runtime_vault.operator_verifier_bridge import (
    OperatorVerifierBridge,
    OperatorVerifierBridgeError,
    OperatorVerifierObservationResult,
    OperatorVerifierVerificationResult,
)
from sky_claw.local.runtime_vault.physical_root import (
    PhysicalRootError,
    PhysicalRootIdentity,
    derive_physical_root,
)
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenEntry,
    TrustedGoldenRegistry,
    TrustedRegistryError,
    load_trusted_golden_registry,
)
from sky_claw.local.runtime_vault.trusted_registry_lock import (
    TrustedRegistryLockBusyError,
    TrustedRegistryLockError,
    derive_trusted_registry_path,
    mutate_trusted_golden_registry,
)

logger = logging.getLogger(__name__)

#: Intentos máximos para obtener un ``registered_at`` único por root físico
#: antes de fallar cerrado ANTES del replace (ADR 0010 §11.4).
MAX_INTENTOS_REGISTERED_AT = 1000

#: El bridge produce ``GameKey``/``game_version`` canónicos de Skyrim SE.
_GAME_KEY_DEFECTO = "skyrimse"


class GoldenAdmissionServiceError(RuntimeVaultError):
    """Error de dominio del servicio de Golden Admission."""


class GoldenAdmissionCommitOutcomeUnknownError(GoldenAdmissionServiceError):
    """El resultado del replace del TGR es ambiguo: nunca es ``REJECTED``."""


# ============================================================================
# Contrato de entrada / salida
# ============================================================================


class IndependentProvenanceProvider(Protocol):
    """Puerto del verificador de ``INDEPENDENT_PROVENANCE`` (§11.4).

    No existe provider productivo ni default. El servicio NO construye la
    expectativa desde ``request.expected_*``: los campos de la solicitud son
    candidatos del caller y sólo el provider, que valida el bundle
    independiente, puede devolver la expectativa ``ADMITTED``. Sin provider
    cableado la operación termina ``SOURCE_UNAVAILABLE`` fail-closed, sin
    efectos secundarios y con cero TGR writes.

    Contrato del retorno: exactamente un ``GoldenAdmissionExpectation`` en
    estado ``ADMITTED``, con ``source=INDEPENDENT_PROVENANCE``, un
    ``source_provenance_digest`` no nulo y las mismas ``critical_expectations``
    declaradas en la solicitud (son la cobertura que el operador confirma). Un
    fallo se comunica con ``GoldenAdmissionSourceUnavailableError`` (fuente no
    disponible) o ``GoldenAdmissionSourceError``/``GoldenAdmissionModelError``
    (fuente inválida).
    """

    def admit(self, request: RegisterOrRefreshTrustedGoldenRequest) -> GoldenAdmissionExpectation: ...


# ============================================================================
# Contrato de entrada / salida
# ============================================================================


@dataclass(frozen=True, slots=True)
class RegisterOrRefreshTrustedGoldenRequest:
    """Solicitud de ``REGISTER_OR_REFRESH_TRUSTED_GOLDEN``.

    Todos los campos son candidatos: la autoridad (identidad física, tree,
    runtime, policy, SID, timestamps) se reconstruye/revalida dentro del
    servicio. Staging sólo señala qué se quiere hacer.
    """

    operation_id: str
    root: str
    admission_source: GoldenAdmissionSource
    critical_expectations: tuple[CriticalFileExpectation, ...] = ()
    expected_tree: TreeDigest | None = None
    expected_runtime: RuntimeIdentity | None = None
    source_reference: GoldenAdmissionSourceReference | None = None

    @property
    def policy_version(self) -> str:
        """Versión activa de la policy del helper; staging no puede elegirla."""
        return ACTIVE_HELPER_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class RegisterOrRefreshTrustedGoldenResult:
    """Resultado del servicio. ``success`` es True SOLO para ``REGISTERED``.

    ``message`` queda vacío en el camino normal de éxito; el único caso no
    vacío con ``success=True`` es un ``REGISTERED`` cuyo resultado terminal no
    pudo anexarse al registro de auditoría (el TGR sí quedó escrito y
    verificado por lectura, y ``REJECTED`` afirmaría lo contrario).
    """

    success: bool
    message: str
    outcome: GoldenAdmissionOutcome
    operation_id: str
    reason: GoldenAdmissionRejectionReason | None = None
    record_path: str | None = None
    tgr_entry: TrustedGoldenEntry | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, GoldenAdmissionOutcome):
            raise GoldenAdmissionServiceError("outcome debe ser GoldenAdmissionOutcome")
        if not isinstance(self.message, str):
            raise GoldenAdmissionServiceError("message debe ser string")
        if not isinstance(self.success, bool):
            raise GoldenAdmissionServiceError("success debe ser bool")
        registrado = self.outcome is GoldenAdmissionOutcome.REGISTERED
        if self.success is not registrado:
            raise GoldenAdmissionServiceError("success debe ser True exactamente para REGISTERED")
        if registrado:
            if self.reason is not None:
                raise GoldenAdmissionServiceError("REGISTERED no lleva reason de rechazo")
            if self.tgr_entry is None:
                raise GoldenAdmissionServiceError("REGISTERED exige la entrada TGR escrita")
        elif self.tgr_entry is not None:
            raise GoldenAdmissionServiceError("Sólo REGISTERED devuelve la entrada TGR")
        if self.outcome is GoldenAdmissionOutcome.REJECTED and self.reason is None:
            raise GoldenAdmissionServiceError("REJECTED exige reason tipado")
        if self.outcome is not GoldenAdmissionOutcome.REJECTED and self.reason is not None:
            raise GoldenAdmissionServiceError("Sólo REJECTED lleva reason")


def validate_golden_admission_request(request: RegisterOrRefreshTrustedGoldenRequest) -> None:
    """Valida la solicitud ANTES de cualquier efecto secundario (Fail-Closed).

    Lanza ``GoldenAdmissionRequestError`` (o un subtipo) sin tocar disco, sin
    adquirir token y sin crear el directorio de operación.
    """
    if not isinstance(request, RegisterOrRefreshTrustedGoldenRequest):
        raise GoldenAdmissionRequestError("La solicitud debe ser RegisterOrRefreshTrustedGoldenRequest")

    try:
        validate_operation_id(request.operation_id)
    except GoldenAdmissionModelError as exc:
        raise GoldenAdmissionRequestError(f"operation_id inválido: {exc}") from exc

    if not isinstance(request.root, str) or not request.root.strip():
        raise GoldenAdmissionRequestError("root debe ser una ruta no vacía")
    if "\x00" in request.root:
        raise GoldenAdmissionRequestError("root no puede contener NUL")
    if not pathlib.PureWindowsPath(request.root).is_absolute():
        raise GoldenAdmissionRequestError("root debe ser una ruta absoluta (el candidato, no la autoridad)")

    try:
        source = GoldenAdmissionSource(request.admission_source)
    except (ValueError, TypeError) as exc:
        raise GoldenAdmissionRequestError(
            f"admission_source '{request.admission_source}' no pertenece al conjunto cerrado"
        ) from exc

    try:
        digest = critical_expectations_digest(request.critical_expectations)
    except (CriticalExpectationsDigestError, GoldenAdmissionModelError, TypeError, ValueError) as exc:
        raise GoldenAdmissionRequestError(f"critical_expectations inválidas: {exc}") from exc
    if not isinstance(digest, str):
        raise GoldenAdmissionRequestError("critical_expectations no produjo un digest")

    if source is GoldenAdmissionSource.OPERATOR_TOFU:
        if request.expected_tree is not None or request.expected_runtime is not None:
            raise GoldenAdmissionRequestError(
                "OPERATOR_TOFU no admite expectativa del caller: el tree/runtime salen de la observación fresca"
            )
        if request.source_reference is not None:
            raise GoldenAdmissionRequestError(
                "OPERATOR_TOFU no admite source_reference: TOFU no se declara provenance por rellenar un campo"
            )
        return

    # INDEPENDENT_PROVENANCE
    if request.expected_tree is None and request.expected_runtime is None and request.source_reference is None:
        raise GoldenAdmissionRequestError(
            "INDEPENDENT_PROVENANCE exige una fuente: expected_tree, expected_runtime y source_reference"
        )
    if request.expected_tree is None or request.expected_runtime is None:
        raise GoldenAdmissionRequestError(
            "INDEPENDENT_PROVENANCE exige expected_tree Y expected_runtime (cobertura completa, no parcial)"
        )
    if request.source_reference is None:
        raise GoldenAdmissionRequestError("INDEPENDENT_PROVENANCE exige source_reference validada")
    if not isinstance(request.expected_tree, TreeDigest):
        raise GoldenAdmissionRequestError("expected_tree debe ser TreeDigest")
    if not isinstance(request.expected_runtime, RuntimeIdentity):
        raise GoldenAdmissionRequestError("expected_runtime debe ser RuntimeIdentity")
    if not isinstance(request.source_reference, GoldenAdmissionSourceReference):
        raise GoldenAdmissionRequestError("source_reference inválida")


# ============================================================================
# Flujo interno
# ============================================================================


class _FalloDeFlujoError(Exception):
    """Fallo tipado del flujo: se convierte en un resultado ``REJECTED``."""

    def __init__(self, reason: GoldenAdmissionRejectionReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.mensaje = message


class _ClaimAjenoError(_FalloDeFlujoError):
    """El ``operation_id`` ya pertenece a OTRA operación (claim one-use).

    Se distingue de ``_FalloDeFlujoError`` porque el desenlace debe ser
    ``REJECTED`` con ``con_registro=False``: el perdedor de la carrera no es
    dueño del registro y no debe leerlo ni escribir en él (ni observaciones ni
    resultado terminal). Colapsarlo en el handler genérico haría que
    ``_rechazado(..., con_registro=True)`` anexara un ``REJECTED`` sobre el
    registro del ganador.
    """


class _CommitDesconocidoError(Exception):
    """El resultado del replace no es determinable: es ``COMMIT_OUTCOME_UNKNOWN``.

    Existe como camino aparte de ``_FalloDeFlujoError`` para que un ``REJECTED``
    sea estructuralmente imposible una vez que el writer pudo haber corrido: el
    caller sólo puede producir ``REJECTED`` desde el otro tipo.
    """

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


def _ahora_utc() -> str:
    """Timestamp UTC ISO-8601 ``Z`` con microsegundos (§11.4)."""
    momento = _datetime.datetime.now(_datetime.UTC)
    return f"{momento:%Y-%m-%dT%H:%M:%S}.{momento.microsecond:06d}Z"


def _entrada_del_root(registry: TrustedGoldenRegistry, fisica: PhysicalRootIdentity) -> TrustedGoldenEntry | None:
    for entry in registry.entries:
        if entry.volume_serial_number == fisica.volume_serial_number and entry.root_file_id == fisica.root_file_id:
            return entry
    return None


def _entrada_mismo_canonical_root(registry: TrustedGoldenRegistry, canonical_root: str) -> TrustedGoldenEntry | None:
    clave = canonical_root.upper()
    for entry in registry.entries:
        if entry.canonical_root.upper() == clave:
            return entry
    return None


def _mismo_root_fisico(observado: PhysicalRootIdentity | None, esperado: PhysicalRootIdentity) -> bool:
    """Identidad física sin distinguir mayúsculas del canonical_root.

    El bridge normaliza el ``canonical_root`` con su propia regla y puede
    diferir en caja: exigir igualdad exacta del string produciría falsos
    ``PHYSICAL_BINDING_MISMATCH``.
    """
    if observado is None:
        return False
    return (
        observado.volume_serial_number == esperado.volume_serial_number
        and observado.root_file_id == esperado.root_file_id
        and observado.canonical_root.lower() == esperado.canonical_root.lower()
    )


def _misma_entrada(a: TrustedGoldenEntry, b: TrustedGoldenEntry) -> bool:
    """Igualdad de campo por campo entre dos entradas TGR (§11.4 before/after)."""
    return (
        a.canonical_root.lower() == b.canonical_root.lower()
        and a.volume_serial_number == b.volume_serial_number
        and a.root_file_id == b.root_file_id
        and a.tree_digest == b.tree_digest
        and a.policy_version == b.policy_version
        and a.registered_by == b.registered_by
        and a.registered_at == b.registered_at
    )


def _registered_at_unico(
    registry: TrustedGoldenRegistry,
    fisica: PhysicalRootIdentity,
    base: str,
) -> str:
    """Genera un ``registered_at`` no usado para la misma identidad física."""
    usados = {
        entry.registered_at
        for entry in registry.entries
        if entry.volume_serial_number == fisica.volume_serial_number and entry.root_file_id == fisica.root_file_id
    }
    candidato = base
    for _ in range(MAX_INTENTOS_REGISTERED_AT):
        if candidato not in usados:
            return candidato
        candidato = _sumar_microsegundo(candidato)
    raise _FalloDeFlujoError(
        GoldenAdmissionRejectionReason.TGR_CONCURRENT_CHANGE,
        f"No se obtuvo un registered_at único en {MAX_INTENTOS_REGISTERED_AT} intentos: fallo previo al replace",
    )


def _sumar_microsegundo(timestamp: str) -> str:
    momento = _datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
    futuro = momento + _datetime.timedelta(microseconds=1)
    return f"{futuro:%Y-%m-%dT%H:%M:%S}.{futuro.microsecond:06d}Z"


def _observacion_desde_bridge(res: OperatorVerifierObservationResult) -> GoldenAdmissionObservation:
    return GoldenAdmissionObservation(
        state=GoldenAdmissionState.OBSERVED,
        physical_root=res.physical_root,
        observed_tree=res.observed_tree,
        observed_runtime=res.observed_runtime,
        critical_evidences=res.critical_evidences,
        message=res.message,
    )


def _registro_de_observacion(
    obs: GoldenAdmissionObservation,
    *,
    stage: str,
    expectativas: tuple[CriticalFileExpectation, ...],
    state: GoldenAdmissionState = GoldenAdmissionState.OBSERVED,
) -> GoldenAdmissionObservationRecord:
    return GoldenAdmissionObservationRecord(
        stage=stage,
        state=state,
        physical_root=obs.physical_root,
        observed_tree=obs.observed_tree,
        observed_runtime=obs.observed_runtime,
        critical_expectations_digest=critical_expectations_digest(expectativas),
        message=obs.message,
    )


def _registro_de_verificacion(
    verificacion: OperatorVerifierVerificationResult,
    *,
    expectativas: tuple[CriticalFileExpectation, ...],
    state: GoldenAdmissionState,
) -> GoldenAdmissionObservationRecord | None:
    """Evidencia del pass RV-2; ``None`` cuando el resultado no trae medición.

    Un ``REJECTED`` del bridge llega sin ``physical_root`` ni ``observed_``:
    en ese caso no hay nada medido que registrar (no se rellena con un
    TreeDigest vacío, que pasaría la validación de sha-256 sólo si mintiera).
    """
    if verificacion.physical_root is None:
        return None
    if verificacion.observed_runtime is None:
        return None
    arbol_observado = verificacion.tree_result.observed
    if arbol_observado is None:
        return None
    return GoldenAdmissionObservationRecord(
        stage="RV2",
        state=state,
        physical_root=verificacion.physical_root,
        observed_tree=arbol_observado,
        observed_runtime=verificacion.observed_runtime,
        critical_expectations_digest=critical_expectations_digest(expectativas),
        message=verificacion.message,
    )


# ============================================================================
# Servicio
# ============================================================================


class GoldenAdmissionService:
    """Implementación del contrato ``REGISTER_OR_REFRESH_TRUSTED_GOLDEN``.

    Sólo se inyecta ``programdata_resolver`` (raíz ProgramData) como seam de
    verificación: el resto son colaboradores reales del flujo. En tests se
    reemplaza el bridge/por provider de token, nunca la lógica.
    """

    def __init__(
        self,
        *,
        bridge: OperatorVerifierBridge,
        confirmation: GoldenAdmissionConfirmationProvider | None,
        token_provider: Callable[[], OperatorPrimaryToken],
        provenance_provider: IndependentProvenanceProvider | None = None,
        programdata_resolver: Callable[[], object] | None = None,
        clock: Callable[[], str] | None = None,
        game_key: str = _GAME_KEY_DEFECTO,
    ) -> None:
        if not callable(token_provider):
            raise GoldenAdmissionServiceError("token_provider debe ser callable")
        if bridge is None:
            raise GoldenAdmissionServiceError("bridge es obligatorio: no existe observe/verify sin él")
        if provenance_provider is not None and not callable(getattr(provenance_provider, "admit", None)):
            raise GoldenAdmissionServiceError(
                "provenance_provider debe exponer admit(request) o ser None (fail-closed)"
            )
        self._bridge = bridge
        self._confirmation = confirmation
        self._token_provider = token_provider
        self._provenance_provider = provenance_provider
        self._programdata_resolver = programdata_resolver
        self._reloj = clock or _ahora_utc
        self._game_key = game_key

    # ------------------------------------------------------------------ API

    def register_or_refresh_trusted_golden(
        self,
        request: RegisterOrRefreshTrustedGoldenRequest,
    ) -> RegisterOrRefreshTrustedGoldenResult:
        """Ejecuta el flujo completo. Nunca lanza: todo fallo es un resultado tipado."""
        try:
            validate_golden_admission_request(request)
        except GoldenAdmissionRequestError as exc:
            return self._rechazado(
                request,
                GoldenAdmissionRejectionReason.REQUEST_INVALID,
                f"Solicitud inválida (cero efectos secundarios): {exc}",
                con_registro=False,
            )

        operacion_id = _id_de_la_solicitud(request)
        try:
            if self._existe_registro(operacion_id):
                return self._rechazado(
                    request,
                    GoldenAdmissionRejectionReason.REQUEST_INVALID,
                    f"La operación '{operacion_id}' ya tiene registro: el claim es one-use",
                    con_registro=False,
                )
            return self._ejecutar(request)
        except _CommitDesconocidoError as desconocido:
            return self._resultado_desconocido(_id_de_la_solicitud(request), desconocido.motivo)
        except _ClaimAjenoError as ajeno:
            # El registro pertenece a otra solicitud: REJECTED sin tocarlo.
            return self._rechazado(request, ajeno.reason, ajeno.mensaje, con_registro=False)
        except _FalloDeFlujoError as fallo:
            return self._rechazado(request, fallo.reason, fallo.mensaje, con_registro=True)
        except GoldenAdmissionRequestError as exc:
            return self._rechazado(
                request,
                GoldenAdmissionRejectionReason.REQUEST_INVALID,
                f"Solicitud inválida: {exc}",
                con_registro=True,
            )
        except GoldenAdmissionStoreError as exc:
            return self._rechazado(
                request,
                GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED,
                f"Fallo del registro de auditoría: {exc}",
                con_registro=True,
            )
        except TrustedRegistryLockBusyError as exc:
            return self._rechazado(
                request,
                GoldenAdmissionRejectionReason.TGR_CONCURRENT_CHANGE,
                f"Lock del TGR ocupado: {exc}",
                con_registro=True,
            )
        except TrustedRegistryLockError as exc:
            return self._rechazado(
                request,
                GoldenAdmissionRejectionReason.STORE_FAILED,
                f"Error del lock del TGR: {exc}",
                con_registro=True,
            )
        except GoldenAdmissionServiceError as exc:
            return self._rechazado(
                request,
                GoldenAdmissionRejectionReason.UNEXPECTED_FAILURE,
                str(exc),
                con_registro=True,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed de borde
            return self._rechazado(
                request,
                GoldenAdmissionRejectionReason.UNEXPECTED_FAILURE,
                f"Fallo inesperado (cero TGR writes): {exc}",
                con_registro=True,
            )

    # ------------------------------------------------------------ internos

    def _ejecutar(self, request: RegisterOrRefreshTrustedGoldenRequest) -> RegisterOrRefreshTrustedGoldenResult:
        operacion_id = validate_operation_id(request.operation_id)
        expectativas = request.critical_expectations

        # 1. Token original del operador (sin él, ningún pass es autoritativo).
        try:
            token = self._token_provider()
        except Exception as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.TOKEN_UNAVAILABLE,
                f"Token original del operador no disponible: {exc}",
            ) from exc

        # 2. Identidad física revalidada desde el root candidato.
        try:
            fisica = derive_physical_root(request.root)
        except (PhysicalRootError, OSError) as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.PHYSICAL_BINDING_MISMATCH,
                f"No se pudo derivar la identidad física del root: {exc}",
            ) from exc

        # 3. TGR vigente (para el diálogo old/absent → new).
        registry_previo = self._cargar_tgr()
        anterior = _entrada_del_root(registry_previo, fisica)

        source = GoldenAdmissionSource(request.admission_source)
        observacion_previa: GoldenAdmissionObservation | None = None

        if source is GoldenAdmissionSource.OPERATOR_TOFU:
            # 4a. Observación fresca read-only bajo el token original.
            observacion_previa = self._observar(token, request.root, operacion_id)
            if not _mismo_root_fisico(observacion_previa.physical_root, fisica):
                raise _FalloDeFlujoError(
                    GoldenAdmissionRejectionReason.PHYSICAL_BINDING_MISMATCH,
                    "La observación midió una identidad física distinta a la del root candidato",
                )
            # 5a. Confirmación privilegiada ANTES del receipt (§11.4 TOFU).
            self._confirmar(
                GoldenAdmissionConfirmation(
                    operation_id=operacion_id,
                    canonical_root=fisica.canonical_root,
                    volume_serial_number=fisica.volume_serial_number,
                    root_file_id=fisica.root_file_id,
                    proposed_tree=observacion_previa.observed_tree,
                    proposed_runtime=observacion_previa.observed_runtime.runtime_identity,
                    policy_version=ACTIVE_HELPER_POLICY_VERSION,
                    previous_tgr_tree_digest=None if anterior is None else anterior.tree_digest.digest,
                    admission_source=source,
                    critical_expectations=expectativas,
                    stage=GoldenAdmissionConfirmationStage.TOFU_PRE_RERUN,
                )
            )
            expectativa = GoldenAdmissionExpectation(
                state=GoldenAdmissionState.ADMITTED,
                source=source,
                expected_tree=observacion_previa.observed_tree,
                expected_runtime=observacion_previa.observed_runtime.runtime_identity,
                critical_expectations=expectativas,
                source_provenance_digest=None,
            )
        else:
            # 4b. Fuente independiente: validar y admitir ANTES del receipt.
            expectativa = self._admitir_provenance(request)
            if request.source_reference is not None and request.source_reference.digest != (
                expectativa.source_provenance_digest or ""
            ):
                raise _FalloDeFlujoError(
                    GoldenAdmissionRejectionReason.SOURCE_INVALID,
                    "source_reference.digest no coincide con la fuente admitida",
                )

        # 6. Receipt helper-issued (nonce CSPRNG, atado a la operación).
        receipt = issue_golden_admission_receipt(
            operation_id=operacion_id,
            canonical_root=fisica.canonical_root,
            volume_serial_number=fisica.volume_serial_number,
            root_file_id=fisica.root_file_id,
            tree_digest=expectativa.expected_tree,
            expected_runtime=expectativa.expected_runtime,
            admission_source=expectativa.source,
            operator_sid=self._sid_del_token(token),
            admitted_at=self._reloj(),
            critical_expectations_digest=expectativa.critical_expectations_digest,
            source_provenance_digest=expectativa.source_provenance_digest,
        )

        # 7. Claim one-use + registro de auditoría (contiene el receipt).
        observaciones_iniciales: tuple[GoldenAdmissionObservationRecord, ...] = ()
        if observacion_previa is not None:
            observaciones_iniciales = (
                _registro_de_observacion(observacion_previa, stage="OBSERVE", expectativas=expectativas),
            )
        try:
            create_admission_record(
                receipt=receipt,
                critical_expectations=expectativas,
                source_reference=None if source is GoldenAdmissionSource.OPERATOR_TOFU else request.source_reference,
                observations=observaciones_iniciales,
                programdata_resolver=self._programdata_resolver,
            )
        except GoldenAdmissionClaimAlreadyExistsError as exc:
            # Carrera perdida por el claim one-use: el registro es del ganador.
            raise _ClaimAjenoError(GoldenAdmissionRejectionReason.REQUEST_INVALID, str(exc)) from exc
        except GoldenAdmissionStoreError as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED,
                f"No se pudo crear el registro de operación: {exc}",
            ) from exc

        # 8. RV-2 fresco desde cero bajo el mismo token.
        verificacion = self._verificar(token, request.root, operacion_id, fisica, expectativas, receipt)
        if not verificacion.success:
            self._registrar_verificacion_fallida(operacion_id, verificacion, expectativas)
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.RV2_MISMATCH,
                f"RV-2 no verificó el snapshot admitido: {verificacion.message}",
            )
        if not _mismo_root_fisico(verificacion.physical_root, fisica):
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.PHYSICAL_BINDING_MISMATCH,
                "RV-2 verificó otra identidad física",
            )
        if verificacion.observed_runtime is None or (
            verificacion.observed_runtime.runtime_identity != receipt.expected_runtime
        ):
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.RUNTIME_OBSERVATION_FAILED,
                "RV-2 no reobservó exactamente el runtime admitido en el receipt",
            )
        if verificacion.tree_result.observed != receipt.tree_digest:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.RV2_MISMATCH,
                "RV-2 no reobservó exactamente el árbol admitido en el receipt",
            )
        registro_rv2 = _registro_de_verificacion(
            verificacion, expectativas=expectativas, state=GoldenAdmissionState.VERIFIED
        )
        if registro_rv2 is None:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.RV2_MISMATCH,
                "RV-2 verificó sin evidencia medible de root/runtime",
            )
        self._anexar_observacion(operacion_id, registro_rv2)

        # 9. Confirmación final (post-VERIFIED para provenance; ya hecha en TOFU).
        if source is GoldenAdmissionSource.INDEPENDENT_PROVENANCE:
            self._confirmar(
                GoldenAdmissionConfirmation(
                    operation_id=operacion_id,
                    canonical_root=fisica.canonical_root,
                    volume_serial_number=fisica.volume_serial_number,
                    root_file_id=fisica.root_file_id,
                    proposed_tree=receipt.tree_digest,
                    proposed_runtime=receipt.expected_runtime,
                    policy_version=receipt.policy_version,
                    previous_tgr_tree_digest=None if anterior is None else anterior.tree_digest.digest,
                    admission_source=source,
                    critical_expectations=expectativas,
                    stage=GoldenAdmissionConfirmationStage.PROVENANCE_POST_VERIFIED,
                )
            )

        # 10. Commit bajo el lock global del TGR.
        try:
            entrada_nueva = self._commitear(
                operacion_id=operacion_id,
                fisica=fisica,
                receipt=receipt,
                anterior=anterior,
            )
        except _CommitDesconocidoError as desconocido:
            return self._resultado_desconocido(operacion_id, desconocido.motivo)

        # 11. Resultado final: anexado DESPUÉS del replace (§11.4).
        aviso_de_auditoria = self._anexar_resultado(
            operacion_id,
            GoldenAdmissionResultRecord(
                outcome=GoldenAdmissionOutcome.REGISTERED,
                registered_at=entrada_nueva.registered_at,
                registered_by=receipt.operator_sid,
                message="",
            ),
        )
        return RegisterOrRefreshTrustedGoldenResult(
            success=True,
            message="" if aviso_de_auditoria is None else aviso_de_auditoria,
            outcome=GoldenAdmissionOutcome.REGISTERED,
            operation_id=operacion_id,
            record_path=str(self._path_del_registro(operacion_id)),
            tgr_entry=entrada_nueva,
        )

    # -------------------------------------------------------- colaboradores

    def _observar(self, token: OperatorPrimaryToken, root: str, operacion_id: str) -> GoldenAdmissionObservation:
        try:
            resultado: OperatorVerifierObservationResult = self._bridge.invoke_observe(
                token, root, operacion_id, expected_game_key=self._game_key
            )
        except OperatorVerifierBridgeError as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.OBSERVE_FAILED,
                f"Observación fresca fallida: {exc}",
            ) from exc
        except Exception as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.OBSERVE_FAILED,
                f"Observación fresca no produjo resultado: {exc}",
            ) from exc
        if resultado.observed_runtime.game_key != self._game_key:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.RUNTIME_OBSERVATION_FAILED,
                "La observación fresca midió un game_key distinto del esperado",
            )
        return _observacion_desde_bridge(resultado)

    def _verificar(
        self,
        token: OperatorPrimaryToken,
        root: str,
        operacion_id: str,
        fisica: PhysicalRootIdentity,
        expectativas: tuple[CriticalFileExpectation, ...],
        receipt: GoldenAdmissionReceipt,
    ) -> OperatorVerifierVerificationResult:
        """RV-2 fresco contra EXACTAMENTE lo que el receipt admite (§11.4)."""
        try:
            return self._bridge.invoke_verify(
                token,
                root,
                operacion_id,
                expected_physical_root=fisica,
                expected_tree=receipt.tree_digest,
                expected_runtime=receipt.expected_runtime,
                critical_expectations=expectativas,
                expected_game_key=self._game_key,
            )
        except OperatorVerifierBridgeError as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.RV2_MISMATCH,
                f"RV-2 rechazado por el bridge: {exc}",
            ) from exc
        except Exception as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.RV2_MISMATCH,
                f"RV-2 no produjo resultado: {exc}",
            ) from exc

    def _admitir_provenance(self, request: RegisterOrRefreshTrustedGoldenRequest) -> GoldenAdmissionExpectation:
        """Admite la expectativa SÓLO desde el provider de provenance (§11.4).

        ``expected_tree``/``expected_runtime``/``source_reference`` son candidatos
        del caller: se pasan al provider, nunca se promueven a autoridad. Sin
        provider cableado el desenlace es ``SOURCE_UNAVAILABLE`` fail-closed.
        """
        provider = self._provenance_provider
        if provider is None:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_UNAVAILABLE,
                "INDEPENDENT_PROVENANCE sin verificador de provenance cableado: fail-closed "
                "(cero efectos secundarios, cero TGR writes)",
            )
        if request.source_reference is None:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_UNAVAILABLE,
                "Sin source_reference no hay fuente independiente disponible",
            )
        try:
            admitida = provider.admit(request)
        except GoldenAdmissionSourceUnavailableError as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_UNAVAILABLE,
                f"Fuente independiente no disponible: {exc}",
            ) from exc
        except (GoldenAdmissionSourceError, GoldenAdmissionModelError) as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_INVALID,
                f"Fuente independiente inválida: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 — un provider que no cumple su contrato no autoriza
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_INVALID,
                f"El verificador de provenance no produjo una fuente válida: {exc}",
            ) from exc
        return self._validar_expectativa_admitida(admitida, request)

    def _validar_expectativa_admitida(
        self,
        admitida: object,
        request: RegisterOrRefreshTrustedGoldenRequest,
    ) -> GoldenAdmissionExpectation:
        """Verifica el contrato del retorno del provider: fail-closed si no lo cumple."""
        if not isinstance(admitida, GoldenAdmissionExpectation):
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_INVALID,
                "El verificador de provenance no devolvió un GoldenAdmissionExpectation",
            )
        if admitida.state is not GoldenAdmissionState.ADMITTED:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_INVALID,
                f"La expectativa admitida no está ADMITTED: {admitida.state}",
            )
        if admitida.source is not GoldenAdmissionSource.INDEPENDENT_PROVENANCE:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_INVALID,
                f"La fuente admitida no es INDEPENDENT_PROVENANCE: {admitida.source}",
            )
        if not admitida.source_provenance_digest:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_INVALID,
                "La fuente admitida no aporta source_provenance_digest",
            )
        if admitida.critical_expectations != request.critical_expectations:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.SOURCE_INVALID,
                "La fuente admitida no cubre exactamente las critical_expectations declaradas",
            )
        return admitida

    def _confirmar(self, payload: GoldenAdmissionConfirmation) -> GoldenAdmissionConfirmationReceipt:
        try:
            return require_golden_admission_outcome(self._confirmation, payload)
        except Exception as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.OPERATOR_REJECTED,
                f"Confirmación privilegiada no otorgada: {exc}",
            ) from exc

    def _sid_del_token(self, token: OperatorPrimaryToken) -> str:
        try:
            sid = token.evidence.operator_sid
        except Exception as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.TOKEN_UNAVAILABLE,
                f"El token no expone operator_sid: {exc}",
            ) from exc
        if not isinstance(sid, str) or not sid:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.TOKEN_UNAVAILABLE,
                "El token no aporta un operator_sid canónico",
            )
        return sid

    def _cargar_tgr(self) -> TrustedGoldenRegistry:
        try:
            return load_trusted_golden_registry(
                derive_trusted_registry_path(programdata_resolver=self._programdata_resolver)
            )
        except Exception as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.STORE_FAILED,
                f"No se pudo leer el TGR vigente: {exc}",
            ) from exc

    # --------------------------------------------------------------- commit

    def _commitear(
        self,
        *,
        operacion_id: str,
        fisica: PhysicalRootIdentity,
        receipt: GoldenAdmissionReceipt,
        anterior: TrustedGoldenEntry | None,
    ) -> TrustedGoldenEntry:
        """Escribe el TGR bajo lock y devuelve la entrada realmente escrita.

        Lanza ``_CommitDesconocidoError`` cuando el resultado del replace no es
        determinable: ese caso jamás se devuelve como ``REJECTED`` y su motivo
        ya incluye la revalidación del TGR contra ``before``/``after`` exactos.
        """
        registrado_en = self._reloj()
        entrada_planeada: TrustedGoldenEntry | None = None
        completado = False

        def mutador(actual: TrustedGoldenRegistry) -> TrustedGoldenRegistry:
            nonlocal entrada_planeada, completado
            vigente = _entrada_del_root(actual, fisica)
            if (None if vigente is None else vigente.registered_at) != (
                None if anterior is None else anterior.registered_at
            ):
                raise _FalloDeFlujoError(
                    GoldenAdmissionRejectionReason.TGR_CONCURRENT_CHANGE,
                    "El TGR cambió desde la lectura del diálogo: otro writer ganó la carrera",
                )
            choque = _entrada_mismo_canonical_root(actual, fisica.canonical_root)
            if choque is not None and vigente is not choque:
                raise _FalloDeFlujoError(
                    GoldenAdmissionRejectionReason.PHYSICAL_BINDING_MISMATCH,
                    "Otra identidad física ya ocupa ese canonical_root en el TGR",
                )
            entrada_nueva = TrustedGoldenEntry(
                canonical_root=fisica.canonical_root,
                volume_serial_number=fisica.volume_serial_number,
                root_file_id=fisica.root_file_id,
                tree_digest=receipt.tree_digest,
                policy_version=receipt.policy_version,
                registered_by=receipt.operator_sid,
                registered_at=_registered_at_unico(actual, fisica, registrado_en),
            )
            entradas = tuple(
                entrada_nueva
                if (
                    entry.volume_serial_number == entrada_nueva.volume_serial_number
                    and entry.root_file_id == entrada_nueva.root_file_id
                )
                else entry
                for entry in actual.entries
            )
            if vigente is None:
                entradas = (*entradas, entrada_nueva)
            # §11.4: persistir before/after y revalidar contra disco ANTES del
            # replace. Si esto falla, el writer no corre: TGR intacto.
            vinculado = bind_tgr_replacement(
                operacion_id,
                before=None if anterior is None else anterior,
                after=entrada_nueva,
                programdata_resolver=self._programdata_resolver,
            )
            revalidate_for_tgr_replace(
                operacion_id, expected=vinculado, programdata_resolver=self._programdata_resolver
            )
            entrada_planeada = entrada_nueva
            completado = True
            return TrustedGoldenRegistry(entries=entradas, schema_version=actual.schema_version)

        try:
            mutate_trusted_golden_registry(mutador, programdata_resolver=self._programdata_resolver)
        except _FalloDeFlujoError:
            # Fallo DENTRO del mutador: el writer no corrió, TGR intacto.
            raise
        except Exception as exc:
            if not completado:
                raise self._fallo_previo_al_replace(exc) from exc
            # El mutador ya devolvió: el replace pudo haber corrido. Si el TGR
            # confirma la entrada planificada, el commit ocurrió y no hay nada
            # desconocido; si no, el resultado es ambiguo y se revalida.
            entrada = self._entrada_confirmada(entrada_planeada)
            if entrada is not None:
                return entrada
            raise _CommitDesconocidoError(
                self._veredicto_de_commit_desconocido(exc, fisica=fisica, antes=anterior, planeada=entrada_planeada)
            ) from exc

        entrada_final = self._entrada_confirmada(entrada_planeada)
        if entrada_final is not None:
            return entrada_final
        raise _CommitDesconocidoError(
            self._veredicto_de_commit_desconocido(
                GoldenAdmissionCommitOutcomeUnknownError("El TGR no contiene la entrada planificada tras el replace"),
                fisica=fisica,
                antes=anterior,
                planeada=entrada_planeada,
            )
        )

    def _fallo_previo_al_replace(self, exc: Exception) -> _FalloDeFlujoError:
        """Convierte una excepción ocurrida ANTES del writer en un ``REJECTED`` tipado."""
        if isinstance(exc, TrustedRegistryLockBusyError):
            return _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.TGR_CONCURRENT_CHANGE,
                f"Lock del TGR ocupado: {exc}",
            )
        if isinstance(exc, TrustedRegistryLockError):
            return _FalloDeFlujoError(GoldenAdmissionRejectionReason.STORE_FAILED, f"Error del lock del TGR: {exc}")
        if isinstance(exc, GoldenAdmissionStoreError):
            return _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED,
                f"Fallo del registro de auditoría previo al replace: {exc}",
            )
        if isinstance(exc, TrustedRegistryError):
            return _FalloDeFlujoError(GoldenAdmissionRejectionReason.STORE_FAILED, f"Error del TGR: {exc}")
        return _FalloDeFlujoError(
            GoldenAdmissionRejectionReason.UNEXPECTED_FAILURE,
            f"Fallo previo al replace (cero TGR writes): {exc}",
        )

    def _entrada_confirmada(self, planeada: TrustedGoldenEntry | None) -> TrustedGoldenEntry | None:
        """Relee el TGR y devuelve la entrada sólo si coincide con la planificada."""
        if planeada is None:
            return None
        try:
            registry = load_trusted_golden_registry(
                derive_trusted_registry_path(programdata_resolver=self._programdata_resolver)
            )
        except Exception:  # noqa: BLE001 — un TGR ilegible no prueba nada
            return None
        for entry in registry.entries:
            if _misma_entrada(entry, planeada):
                return entry
        return None

    def _veredicto_de_commit_desconocido(
        self,
        exc: Exception,
        *,
        fisica: PhysicalRootIdentity,
        antes: TrustedGoldenEntry | None,
        planeada: TrustedGoldenEntry | None,
    ) -> str:
        """Revalida el TGR contra ``before``/``after`` exactos y reporta el estado.

        §11.4: nunca se reporta ``REJECTED`` ni se afirma que el row previo
        quedó intacto cuando el resultado del replace no es determinable.
        """
        try:
            registry = load_trusted_golden_registry(
                derive_trusted_registry_path(programdata_resolver=self._programdata_resolver)
            )
        except Exception as lectura_exc:  # noqa: BLE001 — informe, no decisión
            estado = f"TGR ilegible: {lectura_exc}"
        else:
            actual = _entrada_del_root(registry, fisica)
            if actual is None:
                estado = "TGR legible y sin entrada para esa identidad física"
            elif planeada is not None and _misma_entrada(actual, planeada):
                estado = "el TGR contiene exactamente la entrada planificada (after)"
            elif antes is not None and _misma_entrada(actual, antes):
                estado = "el TGR conserva exactamente la entrada previa (before)"
            else:
                estado = "el TGR difiere de before y de after"
        return (
            f"Resultado del commit desconocido ({exc}); revalidación del TGR protegido: {estado}. "
            "Requiere revalidación contra before/after exactos antes de permitir otra operación."
        )

    # --------------------------------------------------------------- store

    def _existe_registro(self, operacion_id: str) -> bool:
        try:
            return derive_admission_record_dir(operacion_id, programdata_resolver=self._programdata_resolver).exists()
        except Exception:  # noqa: BLE001 — sin derivación no hay registro
            return False

    def _path_del_registro(self, operacion_id: str) -> pathlib.Path:
        return derive_admission_record_dir(operacion_id, programdata_resolver=self._programdata_resolver)

    def _anexar_observacion(self, operacion_id: str, registro: GoldenAdmissionObservationRecord) -> None:
        try:
            append_observation(operacion_id, registro, programdata_resolver=self._programdata_resolver)
        except GoldenAdmissionStoreError as exc:
            raise _FalloDeFlujoError(
                GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED,
                f"No se pudo anexar la observación al registro: {exc}",
            ) from exc

    def _registrar_verificacion_fallida(
        self,
        operacion_id: str,
        verificacion: OperatorVerifierVerificationResult,
        expectativas: tuple[CriticalFileExpectation, ...],
    ) -> None:
        """Anexa la evidencia del RV-2 fallido de forma best-effort."""
        registro = _registro_de_verificacion(
            verificacion, expectativas=expectativas, state=GoldenAdmissionState.OBSERVED
        )
        if registro is None:
            return
        try:
            self._anexar_observacion(operacion_id, registro)
        except Exception:  # noqa: BLE001 — la evidencia del pass fallido es best-effort y no decide nada
            # El desenlace sigue siendo RV2_MISMATCH con el TGR intacto: la
            # evidencia del pass fallido es best-effort y no decide nada.
            logger.error("No se pudo registrar la evidencia del RV-2 fallido de %s", operacion_id)

    def _anexar_resultado(self, operacion_id: str, resultado: GoldenAdmissionResultRecord) -> str | None:
        """Anexa el resultado terminal. Devuelve el texto del fallo, si lo hubo.

        No lanza: el desenlace ya está decidido (el TGR quedó escrito o no) y
        una excepción aquí lo convertiría en ``REJECTED`` —que afirmaría
        "TGR intacto"— después de un replace real.
        """
        try:
            append_result(operacion_id, resultado, programdata_resolver=self._programdata_resolver)
        except Exception as exc:  # noqa: BLE001 — el desenlace ya está decidido; la auditoría terminal es secundaria
            logger.error("No se pudo anexar el resultado %s al registro %s: %s", resultado.outcome, operacion_id, exc)
            return f"No se pudo anexar el resultado terminal al registro de auditoría: {exc}"
        return None

    def _resultado_desconocido(self, operacion_id: str, motivo: str) -> RegisterOrRefreshTrustedGoldenResult:
        """Desenlace ``COMMIT_OUTCOME_UNKNOWN``: nunca ``REJECTED`` (§11.4)."""
        aviso = self._anexar_resultado(
            operacion_id,
            GoldenAdmissionResultRecord(
                outcome=GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN,
                message=motivo,
            ),
        )
        if aviso is not None:
            motivo = f"{motivo} / registro terminal: {aviso}"
        record_path: str | None = None
        if operacion_id and self._existe_registro(operacion_id):
            record_path = str(self._path_del_registro(operacion_id))
        return RegisterOrRefreshTrustedGoldenResult(
            success=False,
            message=motivo,
            outcome=GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN,
            operation_id=operacion_id,
            record_path=record_path,
        )

    def _rechazado(
        self,
        request: RegisterOrRefreshTrustedGoldenRequest,
        reason: GoldenAdmissionRejectionReason,
        message: str,
        *,
        con_registro: bool,
    ) -> RegisterOrRefreshTrustedGoldenResult:
        operacion_id = _id_de_la_solicitud(request)
        record_path: str | None = None
        if con_registro and operacion_id and self._existe_registro(operacion_id):
            record_path = str(self._path_del_registro(operacion_id))
            try:
                append_result(
                    operacion_id,
                    GoldenAdmissionResultRecord(
                        outcome=GoldenAdmissionOutcome.REJECTED, reason=reason, message=message
                    ),
                    programdata_resolver=self._programdata_resolver,
                )
            except Exception as exc:  # noqa: BLE001 — fallo secundario; el desenlace ya es REJECTED
                # No se puede anexar: el desenlace sigue siendo REJECTED y el
                # TGR sigue intacto; el registro queda sin resultado terminal.
                logger.warning(
                    "No se pudo anexar el resultado REJECTED al registro %s: %s",
                    operacion_id,
                    exc,
                )
                record_path = None
        return RegisterOrRefreshTrustedGoldenResult(
            success=False,
            message=message,
            outcome=GoldenAdmissionOutcome.REJECTED,
            operation_id=operacion_id,
            reason=reason,
            record_path=record_path,
        )


def _id_de_la_solicitud(request: RegisterOrRefreshTrustedGoldenRequest) -> str:
    """``operation_id`` usable en un resultado, incluso con una solicitud inválida.

    El resultado tipado jamás lanza: una solicitud que ni siquiera tiene un
    ``operation_id`` UUID debe poder reportarse como ``REQUEST_INVALID``.
    """
    valor = getattr(request, "operation_id", None)
    if isinstance(valor, str) and valor:
        try:
            return validate_operation_id(valor)
        except GoldenAdmissionModelError:
            return valor
    return ""
