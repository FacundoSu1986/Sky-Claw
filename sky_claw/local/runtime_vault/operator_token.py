"""Adquisición del Token Primario del Operador (GP2-S3b-1, componentes D y E).

Foundation normativa de ADR 0010 §13.4 para el token del operador interactivo
original que las fases posteriores (post-verificación bajo
``CURRENT_EFFECTIVE_UNELEVATED_TOKEN``) necesitarán.

Contratos anclados:

- ``OTS_SAME_ACCOUNT``: path 1 permitido — el helper elevado abre el proceso
  coordinador (ya ligado por PID + ProcessCreationTime + imagen, componente C),
  hace ``OpenProcessToken`` + ``DuplicateTokenEx`` y obtiene un token
  **PRIMARY** con exactamente ``TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY``
  (lo que ``CreateProcessWithTokenW`` exige). El tipo final se verifica con
  ``GetTokenInformation(TokenType) == TokenPrimary``; un impersonation token
  como token final produce fail-closed.
- ``OTS_CROSS_ACCOUNT``: el path 1 NO se usa en v1. Sin un proveedor de token
  de servicio privilegiado (SYSTEM/servicio, hook modelado para v2) el desenlace
  es ``REFUSE_TO_PLAN`` antes de mutar. Este slice NO inventa servicios
  nuevos ni captura tokens ajenos.
- Privilegios: este módulo NO habilita privilegios de token de ninguna clase
  (el path same-account se sostiene en el DACL check del objeto proceso, no en
  privilegios de depuración ni de TCB).

Ownership (componente E): todo handle nativo tiene dueño claro y cierre
exactamente una vez. El token final vive dentro de :class:`OperatorPrimaryToken`
(RAII), que rechaza uso posterior al cierre y no puede ser acuñado fuera del
path de adquisición.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.privileged_boundary import (
    PlanAuthorizationError,
    PrivilegedBoundaryUnsupportedError,
)

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class OperatorTokenError(RuntimeVaultError):
    """Base de excepciones del token del operador."""


class OperatorTokenAcquisitionError(OperatorTokenError, PlanAuthorizationError):
    """No se pudo obtener el token del operador -> REFUSE_TO_PLAN pre-mutación."""


class OpenCoordinatorProcessError(OperatorTokenAcquisitionError):
    """OpenProcess sobre el coordinador falló aunque la identidad fue ligada previamente."""


class OpenCoordinatorTokenError(OperatorTokenAcquisitionError):
    """OpenProcessToken falló sobre el proceso coordinador."""


class TokenDuplicationError(OperatorTokenAcquisitionError):
    """DuplicateTokenEx falló o no produjo un handle válido."""


class OperatorTokenTypeError(OperatorTokenAcquisitionError):
    """El token final no es TokenPrimary (impersonation u otro): fail-closed."""


class OperatorIdentityEvidenceError(OperatorTokenAcquisitionError):
    """No se pudo leer la evidencia de identidad (TokenUser) del token del operador."""


class OperatorTokenOwnershipError(OperatorTokenError):
    """Uso del token después de su cierre o acuñación fuera del path de adquisición."""


# ============================================================================
# Constantes Win32 Normativas
# ============================================================================

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008

#: Derechos exactos que CreateProcessWithTokenW exige del token primario (ADR 0010 §13.4).
OPERATOR_TOKEN_REQUIRED_RIGHTS = TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY

_PROCESS_QUERY_INFORMATION = 0x0400

_SECURITY_IMPERSONATION = 2
_TOKEN_TYPE_PRIMARY = 1  # TOKEN_TYPE::TokenPrimary
_TOKEN_TYPE_IMPERSONATION = 2  # TOKEN_TYPE::TokenImpersonation
_TOKEN_INFORMATION_CLASS_TOKEN_USER = 1  # TOKEN_INFORMATION_CLASS::TokenUser
_TOKEN_INFORMATION_CLASS_TOKEN_TYPE = 8  # TOKEN_INFORMATION_CLASS::TokenType

_MAX_UINT32 = (1 << 32) - 1


if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.DuplicateTokenEx.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    _kernel32.LocalFree.restype = wintypes.LPVOID


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise PrivilegedBoundaryUnsupportedError("La adquisición nativa del token de operador solo existe en Windows")


def _is_invalid_handle(handle: object) -> bool:
    if handle is None or handle == 0:
        return True
    return handle in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


# ============================================================================
# Evidencia Inmutable de Identidad del Operador
# ============================================================================

_OPERATOR_TOKEN_TYPE_LITERAL = "primary"
_ACQUIRED_VIA_SAME_ACCOUNT = "same_account_coordinator_extraction"
_ACQUIRED_VIA_SERVICE = "privileged_service_provider"
_ACQUIRED_VIA_ALLOWED = frozenset({_ACQUIRED_VIA_SAME_ACCOUNT, _ACQUIRED_VIA_SERVICE})


@dataclass(frozen=True, slots=True)
class OperatorTokenEvidence:
    """Evidencia inmutable del token del operador válido para el contexto de autorización.

    Los literales están cerrados: nadie puede fabricar evidencia de un token
    impersonation ni de un origen inventado (fail-closed por estructura).
    """

    operator_sid: str  # String SID canónico del TokenUser (S-1-...)
    token_type: str  # siempre "primary"
    acquired_via: str  # "same_account_coordinator_extraction" | "privileged_service_provider"

    def __post_init__(self) -> None:
        if not isinstance(self.operator_sid, str) or not self.operator_sid.startswith("S-1-"):
            raise OperatorIdentityEvidenceError("operator_sid debe ser un String SID canónico 'S-1-...'")
        if self.token_type != _OPERATOR_TOKEN_TYPE_LITERAL:
            raise OperatorIdentityEvidenceError(
                f"token_type debe ser exactamente '{_OPERATOR_TOKEN_TYPE_LITERAL}'; observado '{self.token_type}'"
            )
        if self.acquired_via not in _ACQUIRED_VIA_ALLOWED:
            raise OperatorIdentityEvidenceError(
                f"acquired_via debe ser uno de {sorted(_ACQUIRED_VIA_ALLOWED)}; observado '{self.acquired_via}'"
            )


# ============================================================================
# Adaptador Win32 (seam) y Ownership Helpers
# ============================================================================


class OperatorTokenAdapter(Protocol):
    """Primitivas de kernel para la adquisición del token del operador.

    Único punto de contacto con Win32 del orquestador. Los métodos lanzan las
    excepciones tipadas de este módulo (nunca ``OSError`` cruda) y nunca
    fabrican éxito: un handle inválido de salida es un error tipado.
    """

    def open_process(self, desired_access: int, pid: int) -> int: ...
    def open_process_token(self, process_handle: int, desired_access: int) -> int: ...
    def duplicate_token_ex_primary(self, source_handle: int, desired_access: int) -> int: ...
    def get_token_type(self, token_handle: int) -> int: ...
    def read_token_user_sid(self, token_handle: int) -> str: ...
    def close_handle(self, handle: int) -> None: ...


class _Win32OperatorTokenAdapter:
    """Implementación nativa documentada del adaptador (cada Win32 con ABI explícita)."""

    def open_process(self, desired_access: int, pid: int) -> int:
        handle = _kernel32.OpenProcess(desired_access, False, pid)
        if _is_invalid_handle(handle):
            err = ctypes.get_last_error()
            raise OpenCoordinatorProcessError(f"OpenProcess(coordinador pid={pid}) falló: código Win32 {err}")
        return int(handle)

    def open_process_token(self, process_handle: int, desired_access: int) -> int:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(process_handle, desired_access, ctypes.byref(token)) or _is_invalid_handle(
            token.value
        ):
            err = ctypes.get_last_error()
            raise OpenCoordinatorTokenError(f"OpenProcessToken(coordinador) falló: código Win32 {err}")
        return int(token.value or 0)

    def duplicate_token_ex_primary(self, source_handle: int, desired_access: int) -> int:
        duplicated = wintypes.HANDLE()
        ok = _advapi32.DuplicateTokenEx(
            source_handle,
            desired_access,
            None,  # seguridad por defecto del token duplicado
            _SECURITY_IMPERSONATION,  # ignorado por DuplicateTokenEx cuando TokenType == TokenPrimary
            _TOKEN_TYPE_PRIMARY,  # NUNCA TokenImpersonation como token final del operador
            ctypes.byref(duplicated),
        )
        if not ok or _is_invalid_handle(duplicated.value):
            err = ctypes.get_last_error()
            raise TokenDuplicationError(f"DuplicateTokenEx(TokenPrimary) falló: código Win32 {err}")
        return int(duplicated.value or 0)

    def get_token_type(self, token_handle: int) -> int:
        value = wintypes.DWORD(0)
        returned = wintypes.DWORD(0)
        if not _advapi32.GetTokenInformation(
            token_handle,
            _TOKEN_INFORMATION_CLASS_TOKEN_TYPE,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(returned),
        ):
            err = ctypes.get_last_error()
            raise OperatorTokenTypeError(f"GetTokenInformation(TokenType) falló: código Win32 {err}")
        return int(value.value)

    def read_token_user_sid(self, token_handle: int) -> str:
        # Primer paso: tamaño requerido del buffer (se espera ERROR_INSUFFICIENT_BUFFER 122).
        required = wintypes.DWORD(0)
        ok = _advapi32.GetTokenInformation(
            token_handle,
            _TOKEN_INFORMATION_CLASS_TOKEN_USER,
            None,
            0,
            ctypes.byref(required),
        )
        err = ctypes.get_last_error()
        if ok or required.value == 0:
            raise OperatorIdentityEvidenceError(
                f"GetTokenInformation(TokenUser) no reportó tamaño requerido (código Win32 {err})"
            )
        buffer = (ctypes.c_ubyte * required.value)()
        if not _advapi32.GetTokenInformation(
            token_handle,
            _TOKEN_INFORMATION_CLASS_TOKEN_USER,
            ctypes.cast(buffer, wintypes.LPVOID),
            required,
            ctypes.byref(required),
        ):
            err2 = ctypes.get_last_error()
            raise OperatorIdentityEvidenceError(f"GetTokenInformation(TokenUser) falló: código Win32 {err2}")
        # TOKEN_USER: único campo es SID_AND_ATTRIBUTES { Sid, Attributes }
        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents
        sid_string = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_string)):
            err3 = ctypes.get_last_error()
            raise OperatorIdentityEvidenceError(f"ConvertSidToStringSidW(TokenUser) falló: código Win32 {err3}")
        try:
            value = sid_string.value
            if not value:
                raise OperatorIdentityEvidenceError("ConvertSidToStringSidW(TokenUser) devolvió cadena vacía")
            return str(value)
        finally:
            _kernel32.LocalFree(sid_string)

    def close_handle(self, handle: int) -> None:
        if not _is_invalid_handle(handle):
            _kernel32.CloseHandle(handle)


# ============================================================================
# RAII del Token Primario del Operador (ownership exactamente una vez)
# ============================================================================

_MINT_PROOF: Any = object()


class OperatorPrimaryToken:
    """Ownership RAII del handle del token primario del operador.

    - Sólo puede acuñarse desde :func:`acquire_operator_primary_token_from_coordinator`
      (proof privada del módulo): código externo no puede fabricar un wrapper
      sobre un entero arbitrario.
    - Cierra exactamente una vez (``close()`` idempotente-reportante).
    - Todo uso posterior al cierre lanza ``OperatorTokenOwnershipError``.
    """

    __slots__ = ("_closer", "_closed", "_evidence", "_handle")

    def __init__(
        self,
        handle: int,
        evidence: OperatorTokenEvidence,
        closer: Callable[[int], None],
        *,
        _proof: Any = None,
    ) -> None:
        if _proof is not _MINT_PROOF:
            raise OperatorTokenOwnershipError(
                "OperatorPrimaryToken sólo puede crearse desde acquire_operator_primary_token_from_coordinator"
            )
        if _is_invalid_handle(handle):
            raise OperatorTokenOwnershipError("No se puede tomar ownership de un handle de token inválido")
        if not isinstance(evidence, OperatorTokenEvidence):
            raise OperatorTokenOwnershipError("evidence debe ser OperatorTokenEvidence")
        self._handle = int(handle)
        self._evidence = evidence
        self._closer = closer
        self._closed = False

    @property
    def evidence(self) -> OperatorTokenEvidence:
        return self._evidence

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def raw_handle(self) -> int:
        """Handle prestado (borrowing) para llamadas Win32 internas: el consumidor NO lo cierra."""
        if self._closed:
            raise OperatorTokenOwnershipError("Uso del token del operador después de su cierre")
        return self._handle

    def close(self) -> bool:
        """Cierra el handle exactamente una vez. True si esta llamada cerró; False si ya estaba cerrado."""
        if self._closed:
            return False
        self._closed = True
        self._closer(self._handle)
        return True

    def __enter__(self) -> OperatorPrimaryToken:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# ============================================================================
# Adquisición OTS_SAME_ACCOUNT (coordinador previamente ligado)
# ============================================================================


def acquire_operator_primary_token_from_coordinator(
    coordinator_pid: int,
    *,
    adapter: OperatorTokenAdapter | None = None,
) -> OperatorPrimaryToken:
    """Extrae el token primario del operador desde el coordinador ligado (path 1).

    PRECONDICIÓN contractual: el ``coordinator_pid`` ya fue ligado por
    :func:`coordinator_identity.require_bound_coordinator_identity`
    (PID + ProcessCreationTime + imagen). Este módulo no re-verifica la
    identidad (separación de responsabilidades): la sesión de autorización
    encadena ambos pasos y nunca invoca esta función sin binding previo.

    Secuencia (least privilege, sin privilegios de token):
    1. ``OpenProcess(PROCESS_QUERY_INFORMATION, coordinator_pid)``.
    2. ``OpenProcessToken(handle, TOKEN_DUPLICATE)``.
    3. ``DuplicateTokenEx(TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY,
       SecurityImpersonation, TokenPrimary)``.
    4. Verificación estructural: ``GetTokenInformation(TokenType) == TokenPrimary``
       (un impersonation token final -> fail-closed, handles cerrados).
    5. Evidencia de identidad: ``GetTokenInformation(TokenUser) -> String SID``.

    Cualquier fallo cierra TODOS los handles abiertos exactamente una vez.
    """
    if adapter is None:
        _ensure_windows()
        native_adapter: OperatorTokenAdapter = _Win32OperatorTokenAdapter()
    else:
        native_adapter = adapter

    if (
        isinstance(coordinator_pid, bool)
        or not isinstance(coordinator_pid, int)
        or not 0 < coordinator_pid <= _MAX_UINT32
    ):
        raise OperatorTokenAcquisitionError(f"coordinator_pid inválido: {coordinator_pid!r}")

    closed: set[int] = set()

    def _close_once(handle: int) -> None:
        if handle not in closed:
            closed.add(handle)
            native_adapter.close_handle(handle)

    process_handle = native_adapter.open_process(_PROCESS_QUERY_INFORMATION, coordinator_pid)
    token_handle = 0
    duplicated_handle = 0
    success = False
    try:
        token_handle = native_adapter.open_process_token(process_handle, TOKEN_DUPLICATE)
        duplicated_handle = native_adapter.duplicate_token_ex_primary(token_handle, OPERATOR_TOKEN_REQUIRED_RIGHTS)

        # Verificación estructural obligatoria del tipo final (defensa en profundidad
        # anti-impersonation, oracle GP2-T22(a) subvariante del contrato del token).
        token_type = native_adapter.get_token_type(duplicated_handle)
        if token_type != _TOKEN_TYPE_PRIMARY:
            raise OperatorTokenTypeError(
                f"El token final no es TokenPrimary (observado TokenType={token_type}): fail-closed"
            )

        operator_sid = native_adapter.read_token_user_sid(duplicated_handle)
        evidence = OperatorTokenEvidence(
            operator_sid=operator_sid,
            token_type="primary",
            acquired_via=_ACQUIRED_VIA_SAME_ACCOUNT,
        )
        token = OperatorPrimaryToken(
            duplicated_handle,
            evidence,
            native_adapter.close_handle,
            _proof=_MINT_PROOF,
        )
        success = True
        return token
    finally:
        # Los handles intermedios siempre se cierran aquí, exactamente una vez;
        # el duplicado sólo se cierra cuando NO fue transferido al RAII retornado.
        if not success and duplicated_handle:
            _close_once(duplicated_handle)
        if token_handle:
            _close_once(token_handle)
        _close_once(process_handle)


# ============================================================================
# Estrategia OTS (same-account vs cross-account) — Pura
# ============================================================================


class OtsElevationCase(StrEnum):
    """Caso de elevación Over-The-Shoulder observado (ADR 0010 §13.4)."""

    SAME_ACCOUNT = "ots_same_account"
    CROSS_ACCOUNT = "ots_cross_account"


class OperatorTokenStrategy(StrEnum):
    """Estrategia normativa para obtener el token del operador original."""

    SAME_ACCOUNT_EXTRACTION = "same_account_extraction"
    SERVICE_WTS_PROVIDER = "service_wts_provider"
    REFUSE_TO_PLAN = "refuse_to_plan"


class PrivilegedServiceTokenProvider(Protocol):
    """Hook FUTURO (v2) para un proveedor privilegiado SYSTEM/servicio de token del operador.

    Sólo es válido cuando el proceso posee la autoridad normativa requerida
    (componente privilegiado SYSTEM/servicio per ADR 0010 §11.3/§13.4 vía 2).
    Este slice NO registra ninguna implementación: con ``provider is None`` la
    estrategia cross-account es siempre ``REFUSE_TO_PLAN``.
    """

    def acquire_interactive_operator_token(self) -> OperatorPrimaryToken: ...


def resolve_operator_token_strategy(
    elevation_case: OtsElevationCase,
    service_provider: PrivilegedServiceTokenProvider | None,
) -> OperatorTokenStrategy:
    """Resuelve la estrategia del token del operador (pura, fail-closed).

    - ``OTS_SAME_ACCOUNT`` -> extracción del coordinador (path 1).
    - ``OTS_CROSS_ACCOUNT`` con proveedor privilegiado -> vía servicio (hook v2).
    - ``OTS_CROSS_ACCOUNT`` sin proveedor -> ``REFUSE_TO_PLAN`` (antes de mutar).
      NUNCA se cae al path 1 en cross-account (mutante M-O2 detectado por tests).
    """
    if not isinstance(elevation_case, OtsElevationCase):
        raise OperatorTokenError("elevation_case debe ser OtsElevationCase")
    if elevation_case is OtsElevationCase.SAME_ACCOUNT:
        return OperatorTokenStrategy.SAME_ACCOUNT_EXTRACTION
    if service_provider is not None:
        return OperatorTokenStrategy.SERVICE_WTS_PROVIDER
    return OperatorTokenStrategy.REFUSE_TO_PLAN


__all__ = [
    "OPERATOR_TOKEN_REQUIRED_RIGHTS",
    "TOKEN_ASSIGN_PRIMARY",
    "TOKEN_DUPLICATE",
    "TOKEN_QUERY",
    "OpenCoordinatorProcessError",
    "OpenCoordinatorTokenError",
    "OperatorIdentityEvidenceError",
    "OperatorPrimaryToken",
    "OperatorTokenAcquisitionError",
    "OperatorTokenAdapter",
    "OperatorTokenError",
    "OperatorTokenEvidence",
    "OperatorTokenOwnershipError",
    "OperatorTokenStrategy",
    "OperatorTokenTypeError",
    "OtsElevationCase",
    "PrivilegedServiceTokenProvider",
    "TokenDuplicationError",
    "acquire_operator_primary_token_from_coordinator",
    "resolve_operator_token_strategy",
]
