"""Frontera del Helper Privilegiado y Contrato de Elevación UAC (GP2-S3b-1, componentes A y B).

Implementa la frontera normativa de ADR 0010 §12.1/§13 (anti-Confused Deputy):

- El launch request del helper es INMUTABLE y NO transporta paths mutables:
  sólo ``operation_id`` (UUID canónico), ``staging_digest`` (SHA-256 hex), y la
  evidencia de identidad del coordinador (``coordinator_pid`` + ``coordinator_creation_time``).
- La gramática CLI del helper acepta EXACTAMENTE dos flags: ``--operation-id`` y
  ``--staging-digest``. Cualquier otro flag (``--root``, ``--golden-path``,
  ``--manifest-path``, ``--tgr-path``, ``--operations-path``, ``--lock-path``,
  ``--script``, ``--command``, ``--acl``, ``--policy-json``, ...) es rechazado
  con ``HelperArgumentError`` (fail-closed).
- La elevación se solicita ÚNICAMENTE vía ``ShellExecuteExW`` con verbo ``"runas"``.
  UAC eleva el helper; NO autoriza el plan (ADR 0010 §11.0/§13.4): este módulo
  nunca transforma un resultado UAC en autorización.

Empaquetado del helper (declaración normativa honesta):

ADR 0010 fija el NOMBRE de la imagen (``sky-claw-vault-helper.exe``) pero el
repositorio todavía no norma su ubicación de instalación ni su firma. Por eso
la resolución productiva de la imagen está marcada como ``UNRESOLVED`` y FALLA
CERRADO (``HelperImageNotProvisionedError``). Este slice entrega el contrato
puro de lanzamiento, el argument builder, la interfaz validada de resolución de
imagen y el adaptador ``ShellExecuteExW`` — no inventa ``sys.executable``,
``python -m ...`` ni paths en TEMP, porque esos cambiarían el threat model.

POSIX: todos los modelos puros son importables y testables; la primitiva de
elevación real lanza ``PrivilegedBoundaryUnsupportedError`` con cero efectos
privilegiados.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from sky_claw.local.runtime_vault.models import RuntimeVaultError

if TYPE_CHECKING:
    from sky_claw.local.runtime_vault.golden_protection_plan import GoldenProtectionPlanState

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class PrivilegedBoundaryError(RuntimeVaultError):
    """Base de excepciones de la frontera privilegiada (GP2-S3b)."""


class PrivilegedBoundaryUnsupportedError(PrivilegedBoundaryError):
    """Primitiva privilegiada nativa invocada en una plataforma no soportada."""


class PlanAuthorizationError(PrivilegedBoundaryError):
    """Fallo de un componente de autorización del helper -> REFUSE_TO_PLAN (ADR 0010 §24).

    Cubre: fallo TGR, PPSC rechazada/cancelada, fallo de binding criptográfico,
    fallo de binding de identidad del coordinador, token de operador no obtenible.
    Terminal no-mutador: cero nodos mutados, sin GoldenMutationLock, sin
    authorized_plan.json.
    """


class HelperArgumentError(PrivilegedBoundaryError):
    """La CLI del helper o el launch request violan la gramática normativa: fail-closed."""


class HelperImageNotProvisionedError(PrivilegedBoundaryError):
    """La imagen empaquetada del helper no puede resolverse con el contrato normado (packaging UNRESOLVED)."""


class HelperImageValidationError(PrivilegedBoundaryError):
    """Un candidato de imagen de helper no supera la validación estructural/nativa: fail-closed."""


class ElevationRejectedError(PrivilegedBoundaryError):
    """El usuario canceló o denegó el prompt UAC -> ELEVATION_REJECTED (sin mutación)."""


class ElevationLaunchError(PrivilegedBoundaryError):
    """ShellExecuteExW falló por una causa distinta de cancelación de usuario."""


class ElevatedHelperHandleError(PrivilegedBoundaryError):
    """Uso de un handle de proceso de helper ya cerrado o inválido."""


# ============================================================================
# Constantes Normativas
# ============================================================================

HELPER_CLI_OPERATION_ID_FLAG = "--operation-id"
HELPER_CLI_STAGING_DIGEST_FLAG = "--staging-digest"

#: Conjunto CERRADO de flags que la CLI del helper acepta (ADR 0010 §12.1).
HELPER_CLI_ALLOWED_FLAGS: frozenset[str] = frozenset(
    {
        HELPER_CLI_OPERATION_ID_FLAG,
        HELPER_CLI_STAGING_DIGEST_FLAG,
    }
)

#: Nombre de imagen normado por ADR 0010 §12 (única imagen de helper admisible).
PACKAGED_HELPER_IMAGE_NAME = "sky-claw-vault-helper.exe"

#: Estado honesto del empaquetado: la ubicación de instalación y firma de la
#: imagen de helper NO están normadas todavía en repo/ADR -> la resolución
#: productiva falla cerrado. Declarado, no escondido.
PACKAGED_HELPER_PROVISIONING_STATUS = "UNRESOLVED"

_MAX_UINT32 = (1 << 32) - 1
_MAX_UINT64 = (1 << 64) - 1
_HEX_LOWER = frozenset("0123456789abcdef")

_ERROR_CANCELLED = 1223  # "The operation was canceled by the user" (cancelación UAC)
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_SHOWNORMAL = 1


# ============================================================================
# Win32 ctypes (Windows only)
# ============================================================================

if sys.platform == "win32":
    from ctypes import wintypes

    class _ShellExecuteInfoW(ctypes.Structure):
        """SHELLEXECUTEINFOW (ABI documentada; tamaños justificados por campos puntero nativos)."""

        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),  # DUMMYUNIONNAME: hIcon (con drag) / hMonitor
            ("hProcess", wintypes.HANDLE),
        ]

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfoW)]
    _shell32.ShellExecuteExW.restype = wintypes.BOOL

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise PrivilegedBoundaryUnsupportedError(
            "La frontera de elevación privilegiada solo está soportada en Windows (ShellExecuteExW/runas)"
        )


def _is_invalid_handle(handle: Any) -> bool:
    if handle is None or handle == 0:
        return True
    return handle in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


# ============================================================================
# Modelo Inmutable de Launch Request (sin paths mutables)
# ============================================================================


def _validate_canonical_uuid(value: str | uuid.UUID, field_name: str) -> uuid.UUID:
    """Exige un UUID en formato canónico con guiones y minúsculas (fail-closed)."""
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise HelperArgumentError(f"{field_name} debe ser str o uuid.UUID")
    normalized = value.strip()
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise HelperArgumentError(f"{field_name} no es un UUID válido: '{value}'") from exc
    if normalized != str(parsed):
        raise HelperArgumentError(f"{field_name} debe ser UUID canónico con guiones en minúsculas: '{value}'")
    return parsed


def _validate_sha256_hex(value: str, field_name: str) -> str:
    """Valida y normaliza un digest SHA-256 hexadecimal de 64 caracteres."""
    if not isinstance(value, str):
        raise HelperArgumentError(f"{field_name} debe ser string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in _HEX_LOWER for ch in normalized):
        raise HelperArgumentError(f"{field_name} debe ser SHA-256 hexadecimal de 64 caracteres")
    return normalized


def _validate_positive_int(
    value: int, field_name: str, *, max_value: int, error_cls: type[PrivilegedBoundaryError]
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_cls(f"{field_name} debe ser un entero")
    if not 0 < value <= max_value:
        raise error_cls(f"{field_name} debe estar en el rango (0, {max_value}]")
    return value


@dataclass(frozen=True, slots=True)
class PrivilegedHelperLaunchRequest:
    """Solicitud inmutable de lanzamiento del helper elevado.

    El request NO transporta paths mutables ni banderas de comportamiento:
    es la única forma de informar al helper qué operación autorizar. Todos los
    paths autoritativos se re-resuelven internamente en el helper desde
    FOLDERID_ProgramData -> Sky-Claw -> runtime_vault (reutilizando S3a).

    ``coordinator_creation_time`` es el FILETIME Win32 (100 ns desde 1601-01-01,
    entero sin signo de 64 bits) del proceso coordinador, requerido para que el
    helper pueda defenderse del reuso de PID (ADR 0010 §19.1.5).
    """

    operation_id: uuid.UUID | str
    staging_digest: str
    coordinator_pid: int
    coordinator_creation_time: int

    def __post_init__(self) -> None:
        operation_id = _validate_canonical_uuid(self.operation_id, "operation_id")
        digest = _validate_sha256_hex(self.staging_digest, "staging_digest")
        pid = _validate_positive_int(
            self.coordinator_pid,
            "coordinator_pid",
            max_value=_MAX_UINT32,
            error_cls=HelperArgumentError,
        )
        creation_time = _validate_positive_int(
            self.coordinator_creation_time,
            "coordinator_creation_time",
            max_value=_MAX_UINT64,
            error_cls=HelperArgumentError,
        )
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "staging_digest", digest)
        object.__setattr__(self, "coordinator_pid", pid)
        object.__setattr__(self, "coordinator_creation_time", creation_time)

    @property
    def operation_id_str(self) -> str:
        """UUID canónico como string (minúsculas con guiones)."""
        return str(self.operation_id)


# ============================================================================
# Gramática Cerrada de la CLI del Helper
# ============================================================================


@dataclass(frozen=True, slots=True)
class HelperCliArguments:
    """Argumentos validados y parseados de la CLI normativa del helper."""

    operation_id: uuid.UUID
    staging_digest: str

    @property
    def operation_id_str(self) -> str:
        return str(self.operation_id)


def validate_helper_cli_arguments(argv: Sequence[str]) -> HelperCliArguments:
    """Parsea la CLI del helper con gramática CERRADA (fail-closed).

    Forma exacta admitida (4 tokens, orden libre de flags, cada flag una vez):

    ``--operation-id <UUID canónico> --staging-digest <SHA-256 hex>``

    Rechaza: flags desconocidos (incluye ``--root`` y cualquier flag de path),
    forma ``--flag=value``, flags duplicados, valores ausentes, argumentos
    posicionales y cualquier longitud distinta de 4 tokens.
    """
    tokens = list(argv)
    if len(tokens) != 4:
        raise HelperArgumentError(
            f"La CLI del helper exige exactamente 4 tokens (2 flags con valor); observados {len(tokens)}"
        )

    parsed: dict[str, str] = {}
    index = 0
    while index < 4:
        flag = tokens[index]
        value = tokens[index + 1]
        if flag not in HELPER_CLI_ALLOWED_FLAGS:
            raise HelperArgumentError(f"Flag no permitido en la CLI del helper: '{flag}'")
        if value.startswith("-"):
            raise HelperArgumentError(f"El flag '{flag}' carece de valor (el valor no puede comenzar con '-')")
        if flag in parsed:
            raise HelperArgumentError(f"Flag duplicado en la CLI del helper: '{flag}'")
        parsed[flag] = value
        index += 2

    # Con el conteo exacto de 4 tokens y cada flag validado, ambos flags normativos
    # deben estar presentes (un faltante implicaría un duplicado, ya rechazado).
    operation_token = parsed[HELPER_CLI_OPERATION_ID_FLAG]
    digest_token = parsed[HELPER_CLI_STAGING_DIGEST_FLAG]

    operation_id = _validate_canonical_uuid(operation_token, HELPER_CLI_OPERATION_ID_FLAG)
    digest = _validate_sha256_hex(digest_token, HELPER_CLI_STAGING_DIGEST_FLAG)
    return HelperCliArguments(operation_id=operation_id, staging_digest=digest)


def build_helper_arguments(request: PrivilegedHelperLaunchRequest) -> str:
    """Serializa los parámetros de línea de comandos del helper (forma única, anclada).

    Produce EXACTAMENTE::

        --operation-id <uuid canónico> --staging-digest <sha-256 hex minúsculas>

    Sin comillas (los valores no contienen espacios), sin flags adicionales.
    """
    if not isinstance(request, PrivilegedHelperLaunchRequest):
        raise HelperArgumentError("request debe ser PrivilegedHelperLaunchRequest")
    return (
        f"{HELPER_CLI_OPERATION_ID_FLAG} {request.operation_id_str}"
        f" {HELPER_CLI_STAGING_DIGEST_FLAG} {request.staging_digest}"
    )


# ============================================================================
# Resolución Validada de la Imagen de Helper (packaging UNRESOLVED)
# ============================================================================


def validate_helper_image_candidate(candidate: pathlib.Path | str) -> pathlib.Path:
    """Valida un candidato de imagen de helper de forma fail-closed.

    Reglas (en todas las plataformas las estructurales; las nativas solo en Windows):
    - Nombre exactamente ``PACKAGED_HELPER_IMAGE_NAME`` (case-sensitive, anti-confusión).
    - Path absoluto con volumen local (sin UNC).
    - En Windows: existe, es archivo regular y NO es reparse point (verificación
      atada a handle reutilizando la primitiva S3a).
    """
    try:
        raw = os.fspath(candidate)
    except TypeError as exc:
        raise HelperImageValidationError("El candidato de imagen debe ser str o PathLike") from exc
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise HelperImageValidationError("El candidato de imagen debe ser una ruta no vacía sin NUL")

    path = pathlib.PureWindowsPath(raw)
    if path.name != PACKAGED_HELPER_IMAGE_NAME:
        raise HelperImageValidationError(
            f"El nombre de imagen debe ser exactamente '{PACKAGED_HELPER_IMAGE_NAME}'; observado '{path.name}'"
        )
    if raw.strip().startswith(("\\\\", "//")):
        raise HelperImageValidationError("El candidato de imagen no puede ser una ruta UNC/remota")
    if not path.drive or path.root not in ("\\", "/"):
        raise HelperImageValidationError("El candidato de imagen debe ser una ruta absoluta con volumen local")

    resolved = pathlib.Path(raw)
    if sys.platform == "win32":
        from sky_claw.local.runtime_vault.trusted_namespace import (
            NamespaceReparsePointError,
            TrustedNamespaceError,
            inspect_namespace_object,
        )

        try:
            info = inspect_namespace_object(resolved)
        except NamespaceReparsePointError as exc:
            raise HelperImageValidationError(f"La imagen de helper es un reparse point: {exc}") from exc
        except TrustedNamespaceError as exc:
            raise HelperImageValidationError(f"No se pudo inspeccionar la imagen de helper: {exc}") from exc
        if info.is_directory:
            raise HelperImageValidationError("El candidato de imagen de helper es un directorio, no un archivo")
        if info.reparse_tag != 0:
            raise HelperImageValidationError(
                f"La imagen de helper posee reparse tag 0x{info.reparse_tag:08X}: fail-closed"
            )
    return resolved


def resolve_packaged_helper_image() -> pathlib.Path:
    """Resolvedor productivo de la imagen empaquetada del helper.

    El empaquetado (ubicación de instalación + identidad de firma) NO está
    normado todavía en repo/ADR (``PACKAGED_HELPER_PROVISIONING_STATUS ==
    "UNRESOLVED"``). Inventar aquí un path (``sys.executable``, el script
    actual, ``python -m``, TEMP) cambiaría el threat model: la única conducta
    legítima de este slice es FALLAR CERRADO.
    """
    _ensure_windows()
    raise HelperImageNotProvisionedError(
        "El empaquetado de la imagen de helper está UNRESOLVED: no existe una ubicación normada "
        f"para '{PACKAGED_HELPER_IMAGE_NAME}' en repo/ADR; la resolución productiva está prohibida "
        "hasta que se defina el contrato de provisioning/firma"
    )


# ============================================================================
# Adaptador ShellExecuteExW("runas") y Ownership del Handle del Helper
# ============================================================================


class ElevationVerdict(StrEnum):
    """Resultado del intento de elevación UAC (sin significado de autorización)."""

    ELEVATED = "elevated"
    REJECTED = "rejected"


class ShellRunAsRunner(Protocol):
    """Seam del adaptador UAC: recibe SOLO (lp_file, lp_parameters, lp_directory).

    El verbo (``"runas"``), la máscara y la forma de los parámetros NO son
    parámetros del seam: viven en la implementación nativa, de modo que ningún
    resolvedor ni caller puede alterarlos.
    """

    def __call__(self, *, lp_file: str, lp_parameters: str, lp_directory: str | None) -> int: ...


def _shell_execute_runas(*, lp_file: str, lp_parameters: str, lp_directory: str | None) -> int:
    """Invoca ShellExecuteExW con verbo ``runas`` y devuelve el hProcess del helper.

    - ``fMask = SEE_MASK_NOCLOSEPROCESS`` (el caller es dueño de hProcess).
    - Cancelación UAC -> ``ElevationRejectedError`` (ERROR_CANCELLED 1223).
    - Cualquier otro fallo -> ``ElevationLaunchError``.
    - hProcess inválido -> ``ElevationLaunchError`` (fail-closed).
    """
    _ensure_windows()
    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(_ShellExecuteInfoW)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = lp_file
    info.lpParameters = lp_parameters
    info.lpDirectory = lp_directory
    info.nShow = _SW_SHOWNORMAL
    info.hInstApp = None
    info.hProcess = None

    ok = _shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.get_last_error()
        if err == _ERROR_CANCELLED:
            raise ElevationRejectedError("El usuario canceló o denegó el prompt UAC (ERROR_CANCELLED)")
        raise ElevationLaunchError(f"ShellExecuteExW(runas) falló con código Win32 {err}")

    h_process = int(info.hProcess) if info.hProcess is not None else 0
    if _is_invalid_handle(h_process):
        raise ElevationLaunchError("ShellExecuteExW(runas) retornó un hProcess inválido pese a éxito")
    return h_process


class ElevatedHelperProcessHandle:
    """Ownership RAII del hProcess del helper elevado.

    Cierra exactamente una vez; todo uso posterior al cierre lanza
    ``ElevatedHelperHandleError``. Nunca expone un entero sin contrato de
    ownership: este objeto ES el contrato.
    """

    __slots__ = ("_closed", "_handle", "_image_path", "_operation_id")

    def __init__(self, handle: int, *, image_path: pathlib.Path, operation_id: uuid.UUID) -> None:
        if _is_invalid_handle(handle):
            raise ElevatedHelperHandleError("No se puede tomar ownership de un hProcess inválido")
        self._handle = int(handle)
        self._image_path = image_path
        self._operation_id = operation_id
        self._closed = False

    @property
    def image_path(self) -> pathlib.Path:
        return self._image_path

    @property
    def operation_id(self) -> uuid.UUID:
        return self._operation_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def raw_handle(self) -> int:
        """Handle prestado (borrowing) para llamadas Win32 internas: NO cerrar por el consumidor."""
        if self._closed:
            raise ElevatedHelperHandleError("Uso de hProcess de helper después de su cierre")
        return self._handle

    def close(self) -> bool:
        """Cierra el handle exactamente una vez. True si esta llamada cerró; False si ya estaba cerrado."""
        if self._closed:
            return False
        self._closed = True
        if sys.platform == "win32":
            _kernel32.CloseHandle(self._handle)
        return True

    def __enter__(self) -> ElevatedHelperProcessHandle:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class HelperImageResolverFn(Protocol):
    """Firma del resolvedor inyectable de imagen (valor-retorno siempre validado).

    Seam interno del coordinador para verificación/tests: ningún dato staged
    (Actor D) puede alcanzarlo, y su salida debe superar igualmente
    :func:`validate_helper_image_candidate` (nombre pineado, absoluto, sin
    reparse). No puede cambiar el verbo, los argumentos ni el nombre de imagen.
    """

    def __call__(self) -> pathlib.Path | str: ...


def launch_privileged_helper(
    request: PrivilegedHelperLaunchRequest,
    *,
    image_resolver: HelperImageResolverFn | None = None,
    shell_runner: ShellRunAsRunner | None = None,
) -> ElevatedHelperProcessHandle:
    """Lanza el helper elevado vía UAC de forma fail-closed.

    Secuencia contractual:
    1. Plataforma Windows obligatoria (POSIX -> ``PrivilegedBoundaryUnsupportedError``
       ANTES de tocar resolvedor o runner: cero efectos privilegiados).
    2. Resolución de imagen (por defecto ``resolve_packaged_helper_image``,
       que falla cerrado mientras el packaging esté UNRESOLVED) + validación
       estructural/nativa del candidato.
    3. Serialización fija de argumentos (``build_helper_arguments``).
    4. ``ShellExecuteExW("runas")`` vía el runner (nativo por defecto).

    UAC concedido NO implica plan autorizado: este resultado sólo prueba que un
    proceso potencialmente elevado arrancó (ADR 0010 §11.0/§13.4).
    """
    _ensure_windows()
    if not isinstance(request, PrivilegedHelperLaunchRequest):
        raise HelperArgumentError("request debe ser PrivilegedHelperLaunchRequest")

    resolver = resolve_packaged_helper_image if image_resolver is None else image_resolver
    candidate = resolver()
    image_path = validate_helper_image_candidate(candidate)

    if not isinstance(image_path, pathlib.Path):
        raise HelperImageValidationError("El resolver de imagen no produjo una ruta validable")

    parameters = build_helper_arguments(request)
    runner = _shell_execute_runas if shell_runner is None else shell_runner
    h_process = runner(lp_file=str(image_path), lp_parameters=parameters, lp_directory=None)

    return ElevatedHelperProcessHandle(
        h_process,
        image_path=image_path,
        operation_id=uuid.UUID(request.operation_id_str),
    )


def early_plan_state_for_launch_failure(exc: BaseException) -> GoldenProtectionPlanState:
    """Mapea un fallo de lanzamiento privilegiado a un estado EARLY FSM normativo.

    ``WHO_WRITES_EARLY_FSM = Unelevated Coordinator ONLY`` (ADR 0010 §19.2):
    el coordinador no elevado es quien registra este resultado en UNTRUSTED_STAGING.
    Sólo existen dos desenlaces tempranos posibles aquí:
    - ``ELEVATION_REJECTED``: el usuario canceló/denegó UAC.
    - ``CANCELLED``: cualquier otra falla previa a la existencia del helper
      (packaging no provisionado, imagen inválida, error de lanzamiento); nunca
      se fabrica un estado privilegiado desde el coordinador.
    """
    from sky_claw.local.runtime_vault.golden_protection_plan import GoldenProtectionPlanState

    if isinstance(exc, ElevationRejectedError):
        return GoldenProtectionPlanState.ELEVATION_REJECTED
    return GoldenProtectionPlanState.CANCELLED


__all__ = [
    "HELPER_CLI_ALLOWED_FLAGS",
    "HELPER_CLI_OPERATION_ID_FLAG",
    "HELPER_CLI_STAGING_DIGEST_FLAG",
    "PACKAGED_HELPER_IMAGE_NAME",
    "PACKAGED_HELPER_PROVISIONING_STATUS",
    "ElevatedHelperHandleError",
    "ElevatedHelperProcessHandle",
    "ElevationLaunchError",
    "ElevationRejectedError",
    "ElevationVerdict",
    "HelperArgumentError",
    "HelperCliArguments",
    "HelperImageNotProvisionedError",
    "HelperImageValidationError",
    "PlanAuthorizationError",
    "PrivilegedBoundaryError",
    "PrivilegedBoundaryUnsupportedError",
    "PrivilegedHelperLaunchRequest",
    "build_helper_arguments",
    "early_plan_state_for_launch_failure",
    "launch_privileged_helper",
    "resolve_packaged_helper_image",
    "validate_helper_cli_arguments",
    "validate_helper_image_candidate",
]
