"""Binding de Identidad del Coordinador (GP2-S3b-1, componente C).

El helper elevado debe demostrar que el proceso coordinador original no fue
reemplazado por reuso de PID antes de extraer su token (ADR 0010 §13.4 path 1).
Contrato normativo:

```text
coordinator_pid
+ ProcessCreationTime (FILETIME vía GetProcessTimes)
+ expected packaged image identity (vía QueryFullProcessImageNameW)
```

``PID alone != identity``: un pid aislado nunca es identidad.

Desenlace: cualquier pid ausente, proceso muerto, creation time discrepante,
imagen inesperada o identidad ilegible produce un veredicto ``REFUSE_*`` que la
fase de autorización del helper convierte en ``REFUSE_TO_PLAN`` PRE-mutación
(cero nodos mutados, sin lock, sin authorized_plan).

Limitación declarada (no escondida): en v1 la identidad de imagen se ata al
PATH de la imagen del proceso (``IMAGE_AUTHENTICITY = PATH_BINDING``). La
verificación de firma Authenticode no está implementada porque el repositorio
no tiene aún una historia de firma: NO se afirma autenticidad criptográfica de
la imagen, sólo el binding PID + creation-time + path de imagen.
"""

from __future__ import annotations

import ctypes
import ntpath
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.privileged_boundary import (
    PlanAuthorizationError,
    PrivilegedBoundaryUnsupportedError,
)

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class CoordinatorIdentityError(RuntimeVaultError):
    """Base de excepciones del binding de identidad del coordinador."""


class CoordinatorIdentityModelError(CoordinatorIdentityError):
    """El modelo de identidad esperada o el probe son inválidos: fail-closed."""


class CoordinatorIdentityBindingError(CoordinatorIdentityError, PlanAuthorizationError):
    """El coordinador no pudo ser ligado de forma segura -> REFUSE_TO_PLAN pre-mutación."""


# ============================================================================
# Constantes
# ============================================================================

#: Estado honesto de la autenticidad de imagen en v1: binding por path, sin firma.
IMAGE_AUTHENTICITY = "PATH_BINDING"

_MAX_UINT32 = (1 << 32) - 1
_MAX_UINT64 = (1 << 64) - 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


if sys.platform == "win32":
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise PrivilegedBoundaryUnsupportedError("El probe nativo de identidad de proceso solo existe en Windows")


def _is_invalid_handle(handle: object) -> bool:
    if handle is None or handle == 0:
        return True
    return handle in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


# ============================================================================
# Modelos Inmutables
# ============================================================================


def _validate_windows_absolute_image_path(value: str) -> str:
    """Valida una ruta de imagen Windows absoluta con volumen local (sin UNC)."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CoordinatorIdentityModelError("image_path debe ser una ruta Windows no vacía sin NUL")
    clean = value.strip()
    if clean.startswith(("\\\\", "//")):
        raise CoordinatorIdentityModelError("image_path UNC/remota no es admisible como identidad de imagen")
    normalized = ntpath.normpath(clean)
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith(("\\", "/")):
        raise CoordinatorIdentityModelError("image_path debe ser absoluta con volumen local (p. ej. C:\\...)")
    return normalized


def _validate_uint_range(value: int, field_name: str, *, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinatorIdentityModelError(f"{field_name} debe ser un entero")
    if not 0 < value <= max_value:
        raise CoordinatorIdentityModelError(f"{field_name} debe estar en el rango (0, {max_value}]")
    return value


@dataclass(frozen=True, slots=True)
class CoordinatorProcessIdentity:
    """Identidad esperada del coordinador: PID + ProcessCreationTime + imagen.

    Los TRES campos son obligatorios (ninguno tiene default): el binding por PID
    aislado está prohibido (defensa contra reuso de PID de ADR 0010 §19.1.5).
    """

    pid: int
    creation_time: int  # FILETIME Win32 (100 ns desde 1601-01-01), uint64
    image_path: str  # ruta absoluta Windows de la imagen esperada

    def __post_init__(self) -> None:
        pid = _validate_uint_range(self.pid, "pid", max_value=_MAX_UINT32)
        creation_time = _validate_uint_range(self.creation_time, "creation_time", max_value=_MAX_UINT64)
        image_path = _validate_windows_absolute_image_path(self.image_path)
        object.__setattr__(self, "pid", pid)
        object.__setattr__(self, "creation_time", creation_time)
        object.__setattr__(self, "image_path", image_path)


@dataclass(frozen=True, slots=True)
class ProcessIdentityProbe:
    """Resultado observado por un proveedor de probe sobre un pid.

    ``is_openable == False`` modela proceso ausente/muerto/inaccesible. Ninguna
    lectura puede quedar "a medias": si el probe no pudo leer creation time o
    imagen, el campo correspondiente es ``None`` y la clasificación RECHAZA
    (legibilidad obligatoria, fail-closed por ambigüedad).
    """

    pid: int
    is_openable: bool
    creation_time: int | None
    image_path: str | None

    def __post_init__(self) -> None:
        pid = _validate_uint_range(self.pid, "pid", max_value=_MAX_UINT32)
        object.__setattr__(self, "pid", pid)
        if not isinstance(self.is_openable, bool):
            raise CoordinatorIdentityModelError("is_openable debe ser bool")
        if self.is_openable:
            if self.creation_time is None and self.image_path is None:
                raise CoordinatorIdentityModelError(
                    "Un probe openable debe aportar al menos una lectura (creation_time o image_path)"
                )
        else:
            if self.creation_time is not None or self.image_path is not None:
                raise CoordinatorIdentityModelError("Un probe no openable no puede aportar lecturas")
        if self.creation_time is not None:
            object.__setattr__(
                self,
                "creation_time",
                _validate_uint_range(self.creation_time, "creation_time", max_value=_MAX_UINT64),
            )
        if self.image_path is not None:
            object.__setattr__(self, "image_path", _validate_windows_absolute_image_path(self.image_path))


# ============================================================================
# Clasificación Fail-Closed (pura)
# ============================================================================


class CoordinatorIdentityDisposition(StrEnum):
    """Desenlace tipado del binding de identidad del coordinador."""

    BOUND = "bound"
    REFUSE_PROCESS_MISSING = "refuse_process_missing"
    REFUSE_CREATION_TIME_UNREADABLE = "refuse_creation_time_unreadable"
    REFUSE_CREATION_TIME_MISMATCH = "refuse_creation_time_mismatch"
    REFUSE_IMAGE_UNREADABLE = "refuse_image_unreadable"
    REFUSE_IMAGE_MISMATCH = "refuse_image_mismatch"
    REFUSE_PID_MISMATCH = "refuse_pid_mismatch"


@dataclass(frozen=True, slots=True)
class CoordinatorIdentityVerdict:
    """Veredicto inmutable del binding.

    ``bound_identity`` sólo puede existir cuando la disposición es ``BOUND``
    (garantizado por estructura, no por disciplina del caller).
    """

    disposition: CoordinatorIdentityDisposition
    bound_identity: CoordinatorProcessIdentity | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CoordinatorIdentityDisposition):
            raise CoordinatorIdentityModelError("disposition debe ser CoordinatorIdentityDisposition")
        if self.disposition is CoordinatorIdentityDisposition.BOUND:
            if not isinstance(self.bound_identity, CoordinatorProcessIdentity):
                raise CoordinatorIdentityModelError("Un veredicto BOUND requiere bound_identity")
            if self.reason != "":
                raise CoordinatorIdentityModelError("Un veredicto BOUND no puede tener reason")
        else:
            if self.bound_identity is not None:
                raise CoordinatorIdentityModelError("Un veredicto REFUSE no puede tener bound_identity")
            if not self.reason:
                raise CoordinatorIdentityModelError("Un veredicto REFUSE requiere reason explicativa")

    @property
    def bound(self) -> bool:
        return self.disposition is CoordinatorIdentityDisposition.BOUND


def _normalize_image_for_compare(image_path: str) -> str:
    """Comparación canónica de paths de imagen Windows (case-insensitive, normalizada)."""
    return ntpath.normcase(ntpath.normpath(image_path))


def classify_coordinator_identity(
    expected: CoordinatorProcessIdentity,
    probe: ProcessIdentityProbe,
) -> CoordinatorIdentityVerdict:
    """Clasifica el probe contra la identidad esperada (fail-closed).

    Reglas estrictas: pid idéntico; proceso openable; creation time legible y
    EXACTO; imagen legible y EXACTA (comparación canónica Windows). Cualquier
    ambigüedad produce un ``REFUSE_*`` tipado, nunca un éxito.
    """
    if not isinstance(expected, CoordinatorProcessIdentity):
        raise CoordinatorIdentityModelError("expected debe ser CoordinatorProcessIdentity")
    if not isinstance(probe, ProcessIdentityProbe):
        raise CoordinatorIdentityModelError("probe debe ser ProcessIdentityProbe")

    if probe.pid != expected.pid:
        return CoordinatorIdentityVerdict(
            CoordinatorIdentityDisposition.REFUSE_PID_MISMATCH,
            None,
            f"El probe se ejecutó sobre pid {probe.pid} pero la identidad esperada es pid {expected.pid}",
        )

    if not probe.is_openable:
        return CoordinatorIdentityVerdict(
            CoordinatorIdentityDisposition.REFUSE_PROCESS_MISSING,
            None,
            f"El proceso coordinador pid {expected.pid} no existe, murió o es inaccesible",
        )

    if probe.creation_time is None:
        return CoordinatorIdentityVerdict(
            CoordinatorIdentityDisposition.REFUSE_CREATION_TIME_UNREADABLE,
            None,
            "No se pudo leer el ProcessCreationTime del coordinador: legibilidad obligatoria",
        )

    if probe.creation_time != expected.creation_time:
        return CoordinatorIdentityVerdict(
            CoordinatorIdentityDisposition.REFUSE_CREATION_TIME_MISMATCH,
            None,
            "El ProcessCreationTime no coincide con el coordinador original (posible reuso de PID)",
        )

    if probe.image_path is None:
        return CoordinatorIdentityVerdict(
            CoordinatorIdentityDisposition.REFUSE_IMAGE_UNREADABLE,
            None,
            "No se pudo leer la imagen del proceso coordinador: legibilidad obligatoria",
        )

    if _normalize_image_for_compare(probe.image_path) != _normalize_image_for_compare(expected.image_path):
        return CoordinatorIdentityVerdict(
            CoordinatorIdentityDisposition.REFUSE_IMAGE_MISMATCH,
            None,
            "La imagen del proceso coordinador no coincide con la imagen empaquetada esperada",
        )

    return CoordinatorIdentityVerdict(CoordinatorIdentityDisposition.BOUND, expected)


# ============================================================================
# Proveedor Nativo Win32 (OpenProcess + GetProcessTimes + QueryFullProcessImageNameW)
# ============================================================================


class CoordinatorIdentityProbeProvider(Protocol):
    """Contrato de un proveedor de probe de identidad de proceso."""

    def probe(self, pid: int) -> ProcessIdentityProbe: ...


def _filetime_to_uint64(file_time: _FileTime) -> int:
    """Combina un FILETIME Win32 (low|high DWORD) en su entero uint64."""
    return (int(file_time.dwHighDateTime) << 32) | int(file_time.dwLowDateTime)


class Win32CoordinatorIdentityProbe:
    """Probe nativo atado a handle: ownership del hProcess exactamente una vez.

    - ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)``: suficiente para
      GetProcessTimes y QueryFullProcessImageNameW (mínimo privilegio).
    - Si el proceso no puede abrirse (muerto/ausente/inaccesible), devuelve un
      probe ``is_openable=False`` (la clasificación decide: no se adivina).
    - Si el proceso abre pero la lectura de creation-time o imagen falla, el
      campo queda en ``None`` y la clasificación produce REFUSE por ilegible.
    """

    def probe(self, pid: int) -> ProcessIdentityProbe:
        _ensure_windows()
        pid_value = _validate_uint_range(pid, "pid", max_value=_MAX_UINT32)

        h_process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid_value)
        if _is_invalid_handle(h_process):
            return ProcessIdentityProbe(pid=pid_value, is_openable=False, creation_time=None, image_path=None)

        try:
            creation_time = self._read_creation_time(h_process)
            image_path = self._read_image_path(h_process)
            return ProcessIdentityProbe(
                pid=pid_value,
                is_openable=True,
                creation_time=creation_time,
                image_path=image_path,
            )
        finally:
            _kernel32.CloseHandle(h_process)

    @staticmethod
    def _read_creation_time(h_process: int) -> int | None:
        creation = _FileTime()
        exit_time = _FileTime()
        kernel_time = _FileTime()
        user_time = _FileTime()
        ok = _kernel32.GetProcessTimes(
            h_process,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        creation_int = _filetime_to_uint64(creation)
        return creation_int if creation_int > 0 else None

    @staticmethod
    def _read_image_path(h_process: int) -> str | None:
        capacity = 32768  # Longitud máxima documentada de path largo Win32 (caracteres)
        buffer = ctypes.create_unicode_buffer(capacity)
        size = wintypes.DWORD(capacity)
        ok = _kernel32.QueryFullProcessImageNameW(h_process, 0, buffer, ctypes.byref(size))
        if not ok or size.value == 0:
            return None
        return str(buffer.value)


def require_bound_coordinator_identity(
    expected: CoordinatorProcessIdentity,
    probe_provider: CoordinatorIdentityProbeProvider,
) -> CoordinatorProcessIdentity:
    """Exige un binding BOUND o lanza ``CoordinatorIdentityBindingError`` (REFUSE_TO_PLAN)."""
    verdict = classify_coordinator_identity(expected, probe_provider.probe(expected.pid))
    if not verdict.bound or verdict.bound_identity is None:
        raise CoordinatorIdentityBindingError(
            f"El coordinador no pudo ser ligado de forma segura ({verdict.disposition.value}): {verdict.reason}"
        )
    return verdict.bound_identity


__all__ = [
    "IMAGE_AUTHENTICITY",
    "CoordinatorIdentityBindingError",
    "CoordinatorIdentityDisposition",
    "CoordinatorIdentityError",
    "CoordinatorIdentityModelError",
    "CoordinatorIdentityProbeProvider",
    "CoordinatorIdentityVerdict",
    "CoordinatorProcessIdentity",
    "ProcessIdentityProbe",
    "Win32CoordinatorIdentityProbe",
    "classify_coordinator_identity",
    "require_bound_coordinator_identity",
]
