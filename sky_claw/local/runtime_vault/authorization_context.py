"""Contexto de Autorización Privilegiada — foundation (GP2-S3b-1, componente H + §13).

Objeto inmutable que S4 podrá consumir para demostrar que la frontera
privilegiada fue establecida de forma confinada y fail-closed:

```text
elevated boundary established  (launch request bien formado)
+ coordinator bound            (PID + ProcessCreationTime + imagen)
+ operator token valid         (PRIMARY, derechos 0x000B, evidencia SID)
+ PPSC confirmed               (siete campos normativos, CONFIRMED explícito)
+ GoldenMutationLock acquired  (share=0, metadata normativa)
```

Lo que este contexto NO prueba (y nunca debe insinuar): Golden mutado, TGR
comparado, ``authorized_plan.json`` durable, WAL listo, ``MUTATING(K)``,
``COMMITTED`` ni post-verificación ejecutada.

Separación normativa: la EVIDENCIA inmutable (dataclass serializable) y los
HANDLES vivos (OperatorPrimaryToken + GoldenMutationLockHandle) viajan en
objetos distintos (``PrivilegedBoundarySession``): un handle jamás viaja dentro
de una dataclass inmutable.

Authorized Plan Foundation (§13): sólo header/bindings inmutables + digest puro
en memoria. Marcado explícitamente como ``NOT_DURABLE_AUTHORIZED_PLAN /
NOT_MUTATION_AUTHORITY``: este módulo NO escribe nada (sin ``open``, sin
``WriteFile``, sin ``os.replace``, sin ``FILE_FLAG_WRITE_THROUGH``); el writer
durable con gates completos pertenece a S4.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sky_claw.local.runtime_vault.coordinator_identity import (
    CoordinatorIdentityProbeProvider,
    CoordinatorProcessIdentity,
    require_bound_coordinator_identity,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockIdentity,
    GoldenMutationLockHandle,
    acquire_golden_mutation_lock,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError, TreeDigest
from sky_claw.local.runtime_vault.operator_token import (
    OperatorPrimaryToken,
    OperatorTokenEvidence,
    OperatorTokenStrategy,
    OtsElevationCase,
    PrivilegedServiceTokenProvider,
    acquire_operator_primary_token_from_coordinator,
    resolve_operator_token_strategy,
)
from sky_claw.local.runtime_vault.ppsc import (
    PrivilegedPlanConfirmation,
    PrivilegedPlanConfirmationProvider,
    PrivilegedPlanConfirmationReceipt,
    require_ppsc_outcome,
)
from sky_claw.local.runtime_vault.privileged_boundary import (
    HelperArgumentError,
    PlanAuthorizationError,
    PrivilegedHelperLaunchRequest,
)

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class AuthorizationContextError(RuntimeVaultError):
    """Base de excepciones del contexto de autorización privilegiada."""


class AuthorizationContextModelError(AuthorizationContextError):
    """El contexto o sus componentes violan invariantes de estructura/binding: fail-closed."""


class AuthorizationSessionError(AuthorizationContextError):
    """Uso de la sesión de frontera después de su cierre."""


class AuthorizationCleanupError(AuthorizationContextError):
    """El establecimiento falló Y el cleanup de recursos también falló (causalidad explícita).

    Nunca esconde el error original: ``__cause__`` conserva el fallo de
    establecimiento y los atributos conservan los fallos de cleanup. Si ambos
    cleanups tuvieron éxito, esta excepción NO se levanta (se re-lanza el error
    de establecimiento original tal cual).
    """

    def __init__(
        self,
        *args: Any,
        lock_cleanup_error: BaseException | None = None,
        token_cleanup_error: BaseException | None = None,
    ) -> None:
        super().__init__(*args)
        self.lock_cleanup_error = lock_cleanup_error
        self.token_cleanup_error = token_cleanup_error


# ============================================================================
# Contexto Inmutable de Autorización (evidencia, NO handles)
# ============================================================================

_HEX_LOWER = frozenset("0123456789abcdef")


def _validate_canonical_operation_id(value: str) -> str:
    if not isinstance(value, str):
        raise AuthorizationContextModelError("operation_id debe ser string")
    normalized = value.strip()
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise AuthorizationContextModelError(f"operation_id '{value}' no es un UUID válido") from exc
    if normalized != str(parsed):
        raise AuthorizationContextModelError("operation_id debe ser UUID canónico con guiones en minúsculas")
    return normalized


def _validate_sha256_hex(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise AuthorizationContextModelError(f"{field_name} debe ser string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in _HEX_LOWER for ch in normalized):
        raise AuthorizationContextModelError(f"{field_name} debe ser SHA-256 hexadecimal de 64 caracteres")
    return normalized


@dataclass(frozen=True, slots=True)
class PrivilegedAuthorizationContext:
    """Evidencia inmutable de la frontera privilegiada establecida.

    EXACTAMENTE seis campos (AUTH-04: ningún path arbitrario del caller;
    ``canonical_root`` vive únicamente dentro del payload PPSC normativo).

    Bindings cruzados exigidos por construcción (defensa anti confused-deputy):
    - ``operation_id`` == payload PPSC.operation_id == lock_identity.operation_id
    - payload PPSC.volume_serial_number == lock_identity.volume_serial_number
    - payload PPSC.root_file_id == lock_identity.root_file_id

    Lo que prueba: boundary bien formado + coordinador ligado + token de
    operador PRIMARY válido + PPSC CONFIRMED + GoldenMutationLock adquirido.
    Lo que NO prueba: Golden mutado, plan durable, WAL, COMMITTED.
    """

    operation_id: str
    staging_digest: str
    coordinator_identity: CoordinatorProcessIdentity
    operator_identity: OperatorTokenEvidence
    ppsc_confirmation: PrivilegedPlanConfirmationReceipt
    lock_identity: GoldenLockIdentity

    def __post_init__(self) -> None:
        op_id = _validate_canonical_operation_id(self.operation_id)
        digest = _validate_sha256_hex(self.staging_digest, "staging_digest")
        if not isinstance(self.coordinator_identity, CoordinatorProcessIdentity):
            raise AuthorizationContextModelError("coordinator_identity debe ser CoordinatorProcessIdentity")
        if not isinstance(self.operator_identity, OperatorTokenEvidence):
            raise AuthorizationContextModelError("operator_identity debe ser OperatorTokenEvidence")
        if not isinstance(self.ppsc_confirmation, PrivilegedPlanConfirmationReceipt):
            raise AuthorizationContextModelError("ppsc_confirmation debe ser PrivilegedPlanConfirmationReceipt")
        if not isinstance(self.lock_identity, GoldenLockIdentity):
            raise AuthorizationContextModelError("lock_identity debe ser GoldenLockIdentity")

        payload = self.ppsc_confirmation.payload
        if payload.operation_id != op_id:
            raise AuthorizationContextModelError("operation_id no coincide con el payload PPSC confirmado")
        if self.lock_identity.operation_id != op_id:
            raise AuthorizationContextModelError("operation_id no coincide con la identidad del lock adquirido")
        if payload.volume_serial_number != self.lock_identity.volume_serial_number:
            raise AuthorizationContextModelError(
                "volume_serial_number del lock no coincide con el payload PPSC confirmado"
            )
        if payload.root_file_id != self.lock_identity.root_file_id:
            raise AuthorizationContextModelError("root_file_id del lock no coincide con el payload PPSC confirmado")

        object.__setattr__(self, "operation_id", op_id)
        object.__setattr__(self, "staging_digest", digest)


# ============================================================================
# Sesión de Frontera (ownership de handles vivos)
# ============================================================================


class PrivilegedBoundarySession:
    """Ownership de los handles vivos de la frontera establecida.

    Orden de cierre: primero el GoldenMutationLock (release + metadata RELEASED
    + flush + close) y después el token del operador (close). Cada recurso se
    cierra exactamente una vez aunque el primero falle; los errores de cierre
    se colectan y el primero se relanza tipado.
    """

    __slots__ = ("_closed", "_lock", "_token")

    def __init__(self, *, operator_token: OperatorPrimaryToken, lock: GoldenMutationLockHandle) -> None:
        if not isinstance(operator_token, OperatorPrimaryToken):
            raise AuthorizationSessionError("operator_token debe ser OperatorPrimaryToken")
        if not isinstance(lock, GoldenMutationLockHandle):
            raise AuthorizationSessionError("lock debe ser GoldenMutationLockHandle")
        self._token = operator_token
        self._lock = lock
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def operator_token(self) -> OperatorPrimaryToken:
        if self._closed:
            raise AuthorizationSessionError("La sesión de frontera ya fue cerrada")
        return self._token

    @property
    def lock(self) -> GoldenMutationLockHandle:
        if self._closed:
            raise AuthorizationSessionError("La sesión de frontera ya fue cerrada")
        return self._lock

    def close(self) -> bool:
        """Cierra la sesión exactamente una vez (lock luego token)."""
        if self._closed:
            return False
        self._closed = True
        first_error: BaseException | None = None
        try:
            self._lock.release()
        except BaseException as exc:  # noqa: BLE001 — boundary deliberada: se colecta y se re-lanza tras cerrar todo
            first_error = exc
        try:
            self._token.close()
        except BaseException as exc:  # noqa: BLE001 — boundary deliberada: se colecta y se re-lanza tras cerrar todo
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error
        return True

    def __enter__(self) -> PrivilegedBoundarySession:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# ============================================================================
# Establecimiento de la Frontera (orquestación fail-closed)
# ============================================================================

TokenAcquirerFn = Callable[[int], OperatorPrimaryToken]
LockAcquirerFn = Callable[[int, int, str], GoldenMutationLockHandle]

#: Orden normativo verificado por tests (PPSC antes del lock, ADR 0010 §12.2 pasos 4 y 6).
#: Representa los cinco stages de autoridad/recursos; NO es una FSM y no tiene
#: un sexto stage.
AUTHORIZATION_ESTABLISHMENT_ORDER: tuple[str, ...] = (
    "coordinator_identity_binding",
    "operator_token_strategy",
    "operator_token_acquisition",
    "ppsc_confirmation",
    "golden_mutation_lock",
)

#: Validación estructural pura que ANTECEDE a ``AUTHORIZATION_ESTABLISHMENT_ORDER``.
#: Comprueba únicamente los campos del request/payload y su igualdad cruzada
#: (volumen, root_file_id, operation_id): es fail-fast y SIN EFECTOS LATERALES
#: — ante un mismatch no abre proceso, no adquiere token, no llama a la PPSC y
#: no toma el GoldenMutationLock. No es un sexto stage de autoridad: es un gate
#: de coherencia de entrada puro.
PURE_INPUT_BINDING_VALIDATION: str = (
    "pure_input_binding_validation (precedes AUTHORIZATION_ESTABLISHMENT_ORDER; "
    "side-effect-free: no process open, no token, no PPSC, no lock)"
)


def _default_token_acquirer(pid: int) -> OperatorPrimaryToken:
    return acquire_operator_primary_token_from_coordinator(pid)


def _default_lock_acquirer(volume_serial_number: int, root_file_id: int, operation_id: str) -> GoldenMutationLockHandle:
    return acquire_golden_mutation_lock(volume_serial_number, root_file_id, operation_id)


def establish_privileged_authorization(
    *,
    launch_request: PrivilegedHelperLaunchRequest,
    expected_coordinator: CoordinatorProcessIdentity,
    coordinator_probe_provider: CoordinatorIdentityProbeProvider,
    elevation_case: OtsElevationCase,
    ppsc_provider: PrivilegedPlanConfirmationProvider,
    ppsc_payload: PrivilegedPlanConfirmation,
    volume_serial_number: int,
    root_file_id: int,
    service_token_provider: PrivilegedServiceTokenProvider | None = None,
    token_acquirer: TokenAcquirerFn | None = None,
    lock_acquirer: LockAcquirerFn | None = None,
) -> tuple[PrivilegedAuthorizationContext, PrivilegedBoundarySession]:
    """Establece la frontera privilegiada y devuelve (contexto, sesión).

    Orden (fail-closed en cada paso; ningún paso posterior se ejecuta tras un
    fallo, y todo recurso adquirido se libera exactamente una vez):

    1. Binding de identidad del coordinador (PID + creation time + imagen).
    2. Estrategia del token del operador (OTS). CROSS_ACCOUNT sin proveedor
       privilegiado -> ``PlanAuthorizationError`` (REFUSE_TO_PLAN) ANTES de
       tocar token, PPSC o lock.
    3. Adquisición del token primario del operador.
    4. PPSC (exige CONFIRMED explícito; UAC nunca sustituye este paso).
    5. GoldenMutationLock (un solo intento; contención/huérfano tipado).

    Un plan confirmado para la identidad física A NUNCA puede encadenarse con
    un lock sobre la identidad física B (binding cruzado exigido).
    """
    if not isinstance(launch_request, PrivilegedHelperLaunchRequest):
        raise HelperArgumentError("launch_request debe ser PrivilegedHelperLaunchRequest")
    if not isinstance(ppsc_payload, PrivilegedPlanConfirmation):
        raise PlanAuthorizationError("ppsc_payload debe ser PrivilegedPlanConfirmation normativo")

    # Binding preventivo: el lock se adquiere sobre la MISMA identidad física
    # que el operador confirmó vía PPSC (anti confused-deputy A/B).
    if ppsc_payload.volume_serial_number != volume_serial_number or ppsc_payload.root_file_id != root_file_id:
        raise PlanAuthorizationError(
            "La identidad física del lock solicitado no coincide con el payload PPSC confirmado: REFUSE_TO_PLAN"
        )
    if ppsc_payload.operation_id != launch_request.operation_id_str:
        raise PlanAuthorizationError("operation_id del launch request no coincide con el payload PPSC: REFUSE_TO_PLAN")

    # Paso 1: binding de identidad del coordinador (nunca PID aislado).
    bound_identity = require_bound_coordinator_identity(expected_coordinator, coordinator_probe_provider)

    # Paso 2: estrategia del token del operador (cross-account sin servicio -> refuse).
    strategy = resolve_operator_token_strategy(elevation_case, service_token_provider)
    if strategy is OperatorTokenStrategy.REFUSE_TO_PLAN:
        raise PlanAuthorizationError(
            "OTS_CROSS_ACCOUNT sin proveedor privilegiado SYSTEM/servicio: el token del operador original "
            "no está disponible -> REFUSE_TO_PLAN (antes de mutar, sin lock, sin MUTATING(1))"
        )

    # Paso 3-5: adquisición del token primario del operador, PPSC y lock.
    # Ownership explícito: ante CUALQUIER excepción previa al retorno exitoso,
    # el lock adquirido se libera exactamente una vez y el token adquirido se
    # cierra exactamente una vez (lock.release() primero, token.close() después;
    # un fallo de cleanup del lock NUNCA impide cerrar el token). Si el cleanup
    # también falla, la causalidad de ambos errores se conserva tipada
    # (AuthorizationCleanupError con __cause__ = fallo de establecimiento).
    token: OperatorPrimaryToken | None = None
    lock: GoldenMutationLockHandle | None = None
    try:
        if strategy is OperatorTokenStrategy.SAME_ACCOUNT_EXTRACTION:
            acquirer = _default_token_acquirer if token_acquirer is None else token_acquirer
            token = acquirer(bound_identity.pid)
        else:  # SERVICE_WTS_PROVIDER (hook v2; no existe implementación en este slice)
            if service_token_provider is None:
                raise PlanAuthorizationError(
                    "La estrategia SERVICE_WTS_PROVIDER exige un proveedor registrado; no hay ninguno en v1"
                )
            token = service_token_provider.acquire_interactive_operator_token()
        if not isinstance(token, OperatorPrimaryToken):
            raise PlanAuthorizationError("El acquirer del token del operador no devolvió OperatorPrimaryToken")

        # Paso 4: PPSC (CONFIRMED explícito o REFUSE_TO_PLAN; NUNCA defaults).
        receipt = require_ppsc_outcome(ppsc_provider, ppsc_payload)

        # Paso 5: GoldenMutationLock sobre la identidad física confirmada.
        lock_fn = _default_lock_acquirer if lock_acquirer is None else lock_acquirer
        lock = lock_fn(volume_serial_number, root_file_id, launch_request.operation_id_str)

        context = PrivilegedAuthorizationContext(
            operation_id=launch_request.operation_id_str,
            staging_digest=launch_request.staging_digest,
            coordinator_identity=bound_identity,
            operator_identity=token.evidence,
            ppsc_confirmation=receipt,
            lock_identity=lock.identity,
        )
        return context, PrivilegedBoundarySession(operator_token=token, lock=lock)
    except BaseException as establishment_error:
        lock_cleanup_error: BaseException | None = None
        token_cleanup_error: BaseException | None = None
        if lock is not None and not lock.closed:
            try:
                lock.release()
            except Exception as release_exception:  # noqa: BLE001 (cleanup nunca puede abortar el del token)
                lock_cleanup_error = release_exception
        if token is not None and not token.closed:
            try:
                token.close()
            except Exception as close_exception:  # noqa: BLE001 (cleanup nunca debe tragar errores silenciosamente)
                token_cleanup_error = close_exception
        if lock_cleanup_error is not None or token_cleanup_error is not None:
            raise AuthorizationCleanupError(
                "El establecimiento de la frontera falló Y el cleanup de recursos también falló "
                f"(lock_cleanup={'falló' if lock_cleanup_error is not None else 'ok'}, "
                f"token_cleanup={'falló' if token_cleanup_error is not None else 'ok'})",
                lock_cleanup_error=lock_cleanup_error,
                token_cleanup_error=token_cleanup_error,
            ) from establishment_error
        raise


# ============================================================================
# Authorized Plan Foundation (§13) — en memoria, NOT_DURABLE / NOT_MUTATION_AUTHORITY
# ============================================================================

#: Estado normativo honesto de la foundation del plan autorizado en este slice.
AUTHORIZED_PLAN_FOUNDATION_STATUS = "NOT_DURABLE_AUTHORIZED_PLAN_NOT_MUTATION_AUTHORITY"

FOUNDATION_HEADER_KEYS: tuple[str, ...] = (
    "operation_id",
    "canonical_root",
    "volume_serial_number",
    "root_file_id",
    "tree_digest",
    "node_count",
    "policy_version",
    "staging_digest",
)


@dataclass(frozen=True, slots=True)
class AuthorizedPlanFoundationHeader:
    """Header/bindings inmutables del futuro plan autorizado (foundation §13).

    NO es el plan autoritativo: es la pieza pura (serialización canónica +
    digest) sobre la que S4 construirá el writer durable con las gates
    normativas completas. No contiene la tabla de nodos (pertenece al FULL
    AUTHORIZED PLAN de §12.2 paso 7) ni backing store.
    """

    operation_id: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    tree_digest: TreeDigest
    node_count: int
    policy_version: str
    staging_digest: str

    def __post_init__(self) -> None:
        op_id = _validate_canonical_operation_id(self.operation_id)
        digest = _validate_sha256_hex(self.staging_digest, "staging_digest")
        if not isinstance(self.tree_digest, TreeDigest):
            raise AuthorizationContextModelError("tree_digest debe ser TreeDigest")
        tree_digest_norm = _validate_sha256_hex(self.tree_digest.digest, "tree_digest.digest")
        payload = PrivilegedPlanConfirmation(
            operation_id=op_id,
            canonical_root=self.canonical_root,
            volume_serial_number=self.volume_serial_number,
            root_file_id=self.root_file_id,
            tree_digest=TreeDigest(
                digest=tree_digest_norm,
                files=self.tree_digest.files,
                bytes=self.tree_digest.bytes,
            ),
            node_count=self.node_count,
            policy_version=self.policy_version,
        )
        object.__setattr__(self, "operation_id", op_id)
        object.__setattr__(self, "staging_digest", digest)
        object.__setattr__(self, "canonical_root", payload.canonical_root)
        object.__setattr__(self, "volume_serial_number", payload.volume_serial_number)
        object.__setattr__(self, "root_file_id", payload.root_file_id)
        object.__setattr__(self, "tree_digest", payload.tree_digest)
        object.__setattr__(self, "node_count", payload.node_count)
        object.__setattr__(self, "policy_version", payload.policy_version)


def serialize_authorized_plan_foundation_header(header: AuthorizedPlanFoundationHeader) -> bytes:
    """Serialización canónica determinista del header (claves ordenadas, UTF-8)."""
    if not isinstance(header, AuthorizedPlanFoundationHeader):
        raise AuthorizationContextModelError("header debe ser AuthorizedPlanFoundationHeader")
    payload = {
        "node_count": header.node_count,
        "operation_id": header.operation_id,
        "policy_version": header.policy_version,
        "root_file_id": header.root_file_id,
        "staging_digest": header.staging_digest,
        "tree_digest": {
            "bytes": header.tree_digest.bytes,
            "digest": header.tree_digest.digest,
            "files": header.tree_digest.files,
        },
        "volume_serial_number": header.volume_serial_number,
        "canonical_root": header.canonical_root,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_foundation_plan_digest(header: AuthorizedPlanFoundationHeader) -> str:
    """Digest SHA-256 puro del header foundation (función pura, sin I/O)."""
    return hashlib.sha256(serialize_authorized_plan_foundation_header(header)).hexdigest()


def build_foundation_header_from_context(context: PrivilegedAuthorizationContext) -> AuthorizedPlanFoundationHeader:
    """Deriva el header foundation desde un contexto establecido (sin I/O)."""
    if not isinstance(context, PrivilegedAuthorizationContext):
        raise AuthorizationContextModelError("context debe ser PrivilegedAuthorizationContext")
    payload = context.ppsc_confirmation.payload
    return AuthorizedPlanFoundationHeader(
        operation_id=context.operation_id,
        canonical_root=payload.canonical_root,
        volume_serial_number=payload.volume_serial_number,
        root_file_id=payload.root_file_id,
        tree_digest=payload.tree_digest,
        node_count=payload.node_count,
        policy_version=payload.policy_version,
        staging_digest=context.staging_digest,
    )


__all__ = [
    "AUTHORIZATION_ESTABLISHMENT_ORDER",
    "AuthorizationCleanupError",
    "PURE_INPUT_BINDING_VALIDATION",
    "AUTHORIZED_PLAN_FOUNDATION_STATUS",
    "FOUNDATION_HEADER_KEYS",
    "AuthorizationContextError",
    "AuthorizationContextModelError",
    "AuthorizationSessionError",
    "AuthorizedPlanFoundationHeader",
    "PrivilegedAuthorizationContext",
    "PrivilegedBoundarySession",
    "build_foundation_header_from_context",
    "compute_foundation_plan_digest",
    "establish_privileged_authorization",
    "serialize_authorized_plan_foundation_header",
]
