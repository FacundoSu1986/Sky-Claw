"""Bridge autenticado UAC -> Verifier Child IPC (GP2-P1).

Arquitectura y contratos normativos (ADR 0010 §12.3, §13.4):
- Elevated helper ejecuta bajo el token primario del operador interactivo original
  (:class:`~sky_claw.local.runtime_vault.operator_token.OperatorPrimaryToken`).
- Lanzamiento seguro: ``CreateProcessWithTokenW`` nativo
  (:class:`Win32CreateProcessWithTokenLauncher`), sin recurrir a ``CreateProcessW``,
  ``subprocess``, ``powershell`` ni ``cmd.exe`` en producción.
- Canal privado IPC: Named Pipe con nombre CSPRNG único por operación y DACL restrictiva
  (SYSTEM + Administrators + operator_sid obtenido de TokenUser).
- Protocolo inequívocamente length-prefixed:
  [4 bytes uint32 big-endian] [UTF-8 JSON cerrado] [EOF/disconnect].
  Límites normativos: MAX_REQUEST_BYTES (64 KB) y MAX_RESPONSE_BYTES (256 KB)
  verificados antes de reservar memoria.
- Autenticación robusta del peer:
  * ``GetNamedPipeClientProcessId`` == PID del hijo lanzado.
  * Same-handle verification: ProcessCreationTime e imagen del ejecutable
    revalidados sobre el process handle del hijo (inmunidad a reciclaje de PID).
  * Nonce CSPRNG de un solo uso por invocación (anti-replay).
- Exactamente una respuesta terminal: detección y rechazo de trailing bytes o múltiples frames.
- Dos modos normativos:
  * OBSERVE: deriva identidad física y mide árbol y runtime frescos; emite OBSERVED.
  * VERIFY: revalida identidad física contra expectativa autorizada y re-mide árbol y runtime
    frescos; emite VERIFIED sólo ante coincidencia total.
- Confinamiento de seams: empaquetado productivo permanece UNRESOLVED (fail-closed);
  la seam de testing ``_TestOnlyVerifierLauncher`` está confinada exclusivamente a tests.
"""

from __future__ import annotations

import ctypes
import json
import os
import pathlib
import secrets
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, cast

from sky_claw.local.runtime_vault.coordinator_identity import _normalize_image_for_compare
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    CriticalFileEvidence,
    CriticalFileExpectation,
    RuntimeIdentity,
    RuntimeVaultError,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.operator_token import (
    OperatorPrimaryToken,
    _filetime_to_uint64,
    _is_invalid_handle,
)
from sky_claw.local.runtime_vault.physical_root import (
    PhysicalRootIdentity,
    bound_physical_root,
)
from sky_claw.local.runtime_vault.privileged_boundary import PrivilegedBoundaryUnsupportedError
from sky_claw.local.runtime_vault.runtime_observation import (
    FreshRuntimeObservation,
    observe_runtime_identity_from_root,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

# ============================================================================
# Constantes Normativas de Protocolo e IPC
# ============================================================================

PROTOCOL_VERSION: Final[int] = 1

MAX_REQUEST_BYTES: Final[int] = 64 * 1024  # 64 KB
MAX_RESPONSE_BYTES: Final[int] = 256 * 1024  # 256 KB

DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_IPC_IO_TIMEOUT_SECONDS: Final[float] = 5.0
# operation_timeout_seconds provisional fijado en 120s; cubre inventario, hashing y medición
# de bibliotecas modded extensas en Skyrim. Pendiente de calibración final sobre RIG real.
DEFAULT_OPERATION_TIMEOUT_SECONDS: Final[float] = 120.0
DEFAULT_CHILD_GRACE_PERIOD_MS: Final[int] = 2000
DEFAULT_IPC_TIMEOUT_SECONDS: Final[float] = 10.0

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_FILE_FLAG_OVERLAPPED = 0x40000000

_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008

_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_CONNECTED = 535
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259

_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000

# ============================================================================
# Esquemas Cerrados del Protocolo v1
# ============================================================================

_REQUEST_OBSERVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "operation_id",
        "mode",
        "nonce",
        "canonical_root",
        "expected_game_key",
    }
)

_REQUEST_VERIFY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "operation_id",
        "mode",
        "nonce",
        "canonical_root",
        "expected_game_key",
        "expected_physical_root",
        "expected_tree",
        "expected_runtime",
        "critical_expectations",
    }
)

_PHYSICAL_ROOT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "canonical_root",
        "volume_serial_number",
        "root_file_id",
    }
)

_TREE_DIGEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        "digest",
        "files",
        "bytes",
    }
)

_RUNTIME_IDENTITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "game_key",
        "game_version",
    }
)

_FRESH_RUNTIME_KEYS: Final[frozenset[str]] = frozenset(
    {
        "game_key",
        "game_version",
        "observed_exe_path",
        "observed_at_ns",
    }
)

_CRITICAL_EXPECTATION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "rel_path",
        "expected_digest",
        "expected_size",
    }
)

_CRITICAL_EVIDENCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "rel_path",
        "state",
        "observed_digest",
        "expected_digest",
        "observed_size",
        "expected_size",
        "message",
    }
)

_RESPONSE_REJECTED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "operation_id",
        "mode",
        "nonce",
        "disposition",
        "error_type",
        "message",
    }
)

_RESPONSE_OBSERVED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "operation_id",
        "mode",
        "nonce",
        "disposition",
        "canonical_root",
        "volume_serial_number",
        "root_file_id",
        "observed_tree",
        "observed_runtime",
        "critical_evidences",
        "message",
    }
)

_RESPONSE_VERIFIED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "version",
        "operation_id",
        "mode",
        "nonce",
        "disposition",
        "canonical_root",
        "volume_serial_number",
        "root_file_id",
        "observed_tree",
        "observed_runtime",
        "critical_evidences",
        "message",
    }
)

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class OperatorVerifierBridgeError(RuntimeVaultError):
    """Base de errores del bridge verificador del operador."""


class OperatorVerifierLaunchError(OperatorVerifierBridgeError):
    """Error al lanzar el verificador hijo mediante CreateProcessWithTokenW."""

    def __init__(self, message: str, win32_code: int = 0) -> None:
        super().__init__(message)
        self.win32_code = win32_code


class VerifierImageNotProvisionedError(OperatorVerifierBridgeError):
    """El empaquetado del verificador productivo sigue UNRESOLVED (fail-closed)."""


class PeerAuthenticationError(OperatorVerifierBridgeError):
    """Fallo en la autenticación del proceso cliente conectado al pipe."""


class NonceAuthenticationError(OperatorVerifierBridgeError):
    """Nonce inválido, expirado o reutilizado en la respuesta."""


class ProtocolAbuseError(OperatorVerifierBridgeError):
    """Violación del protocolo IPC (oversized, trailing bytes, JSON malformado, etc.)."""


class ChildExitedPrematurelyError(OperatorVerifierBridgeError):
    """El verificador hijo murió o cerró el pipe antes de enviar la respuesta terminal."""


class OperatorVerifierTimeoutError(OperatorVerifierBridgeError):
    """Timeout acotado en la conexión o respuesta del verificador hijo."""


# ============================================================================
# Modos y Disposiciones Normativas
# ============================================================================


class VerifierMode(StrEnum):
    """Modo de operación del verificador."""

    OBSERVE = "OBSERVE"
    VERIFY = "VERIFY"


class VerifierDisposition(StrEnum):
    """Resultado terminal reportado por el verificador."""

    OBSERVED = "OBSERVED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


# ============================================================================
# DTOs de Evidencia y Resultados
# ============================================================================


@dataclass(frozen=True, slots=True)
class ChildProcessEvidence:
    """Evidencia de identidad inmutable del proceso hijo lanzado."""

    process_handle: int
    pid: int
    creation_time: int
    image_path: str


@dataclass(frozen=True, slots=True)
class OperatorVerifierObservationResult:
    """Resultado tipado de una invocación en modo OBSERVE."""

    disposition: VerifierDisposition
    operation_id: str
    nonce: str
    physical_root: PhysicalRootIdentity
    observed_tree: TreeDigest
    observed_runtime: FreshRuntimeObservation
    critical_evidences: tuple[CriticalFileEvidence, ...]
    message: str = ""

    def __post_init__(self) -> None:
        if self.disposition is not VerifierDisposition.OBSERVED:
            raise OperatorVerifierBridgeError(
                f"Modo OBSERVE debe producir disposition OBSERVED; observado '{self.disposition}'"
            )


@dataclass(frozen=True, slots=True)
class OperatorVerifierVerificationResult:
    """Resultado tipado de una invocación en modo VERIFY."""

    disposition: VerifierDisposition
    operation_id: str
    nonce: str
    physical_root: PhysicalRootIdentity | None
    tree_result: TreeVerificationResult
    runtime_result: RuntimeVerificationResult
    observed_runtime: FreshRuntimeObservation | None = None
    critical_evidences: tuple[CriticalFileEvidence, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        if self.disposition is VerifierDisposition.VERIFIED:
            if self.physical_root is None:
                raise OperatorVerifierBridgeError("VERIFIED exige presencia de physical_root")
            if self.observed_runtime is None:
                raise OperatorVerifierBridgeError(
                    "VERIFIED exige presencia de FreshRuntimeObservation en observed_runtime"
                )
            if self.runtime_result.observed != self.observed_runtime.runtime_identity:
                raise OperatorVerifierBridgeError(
                    "Invariante violada: runtime_result.observed no coincide con observed_runtime.runtime_identity"
                )

    @property
    def success(self) -> bool:
        """True SOLO cuando disposition es exactamente VERIFIED."""
        return self.disposition is VerifierDisposition.VERIFIED


# ============================================================================
# Win32 ABI Structures & Bindings
# ============================================================================

if sys.platform == "win32":
    from ctypes import wintypes

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_ulonglong),
            ("InternalHigh", ctypes.c_ulonglong),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_char_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _advapi32.CreateProcessWithTokenW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _advapi32.CreateProcessWithTokenW.restype = wintypes.BOOL

    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    _kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
    ]
    _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE

    _kernel32.ConnectNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.ConnectNamedPipe.restype = wintypes.BOOL

    _kernel32.GetNamedPipeClientProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.ULONG),
    ]
    _kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL

    _kernel32.CreateEventW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    _kernel32.CreateEventW.restype = wintypes.HANDLE

    _kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.ResetEvent.restype = wintypes.BOOL

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
    _kernel32.CancelIoEx.restype = wintypes.BOOL

    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL

    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL

    _kernel32.GetOverlappedResult.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_OVERLAPPED),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
    ]
    _kernel32.GetOverlappedResult.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    _kernel32.LocalFree.restype = wintypes.LPVOID

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

    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL

    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise PrivilegedBoundaryUnsupportedError("OperatorVerifierBridge sólo está soportado en Windows")


# ============================================================================
# Utilidades de Protocolo y Validación de Esquemas Cerrados
# ============================================================================


def _validate_closed_keys(data: dict[str, Any], allowed_keys: frozenset[str], context: str) -> None:
    """Valida que un diccionario cumpla estrictamente con un esquema cerrado.

    Rechaza tanto claves faltantes como claves desconocidas adicionales (fail-closed).
    """
    actual = set(data.keys())
    if actual != allowed_keys:
        missing = allowed_keys - actual
        unknown = actual - allowed_keys
        details = []
        if missing:
            details.append(f"claves faltantes: {sorted(missing)}")
        if unknown:
            details.append(f"claves desconocidas: {sorted(unknown)}")
        raise ProtocolAbuseError(f"Violación de esquema cerrado en {context}: {', '.join(details)}")


def encode_length_prefixed_frame(payload: bytes) -> bytes:
    """Enmarca un payload con un prefijo uint32 big-endian de 4 bytes."""
    length = len(payload)
    return length.to_bytes(4, byteorder="big") + payload


def read_length_prefixed_frame(handle: int, max_bytes: int) -> bytes:
    """Lee exactamente un frame enmarcado por longitud uint32 big-endian desde un HANDLE Win32.

    Valida que la longitud anunciada sea <= ``max_bytes`` antes de reservar memoria.
    """
    _ensure_windows()

    # 1. Leer exactamente 4 bytes de encabezado
    header_buf = (ctypes.c_ubyte * 4)()
    header_read = 0
    while header_read < 4:
        read_bytes = wintypes.DWORD(0)
        remaining = 4 - header_read
        ptr = ctypes.cast(ctypes.addressof(header_buf) + header_read, wintypes.LPVOID)
        ok = _kernel32.ReadFile(
            ctypes.c_void_p(handle),
            ptr,
            remaining,
            ctypes.byref(read_bytes),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in (_ERROR_BROKEN_PIPE, 232):
                if header_read == 0:
                    return b""
                raise ChildExitedPrematurelyError(
                    f"El peer cerró la conexión tras enviar sólo {header_read} bytes del encabezado"
                )
            raise ChildExitedPrematurelyError(f"ReadFile(header) falló con código Win32 {err}")

        if read_bytes.value == 0:
            if header_read == 0:
                return b""
            raise ChildExitedPrematurelyError(
                f"El peer cerró la conexión tras enviar sólo {header_read} bytes del encabezado"
            )

        header_read += read_bytes.value

    length = int.from_bytes(bytes(header_buf), byteorder="big")
    if length > max_bytes:
        raise ProtocolAbuseError(
            f"El mensaje anunciado ({length} bytes) excede el límite normativo ({max_bytes} bytes). "
            "Rechazado antes de reservar memoria"
        )
    if length == 0:
        raise ProtocolAbuseError("El mensaje anunciado tiene longitud cero")

    # 2. Leer exactamente `length` bytes
    payload_buf = (ctypes.c_ubyte * length)()
    total_read = 0
    while total_read < length:
        chunk_read = wintypes.DWORD(0)
        remaining = length - total_read
        ptr = ctypes.cast(
            ctypes.addressof(payload_buf) + total_read,
            wintypes.LPVOID,
        )
        ok = _kernel32.ReadFile(
            ctypes.c_void_p(handle),
            ptr,
            remaining,
            ctypes.byref(chunk_read),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            raise ChildExitedPrematurelyError(
                f"ReadFile(body) se interrumpió a los {total_read}/{length} bytes: código Win32 {err}"
            )
        if chunk_read.value == 0:
            raise ChildExitedPrematurelyError(f"EOF inesperado a los {total_read}/{length} bytes del payload")
        total_read += chunk_read.value

    return bytes(payload_buf)


def _cancel_and_drain_overlapped(handle: int, ov: _OVERLAPPED) -> None:
    """Cancela I/O pendiente y drena esperando su finalización real en el kernel.

    Previene use-after-free (UAF) si el driver o subsistema I/O asíncrono
    continúa escribiendo en el buffer o señalizando hEvent tras CancelIoEx.
    """
    _ensure_windows()
    _kernel32.CancelIoEx(ctypes.c_void_p(handle), ctypes.byref(ov))
    dummy_bytes = wintypes.DWORD(0)
    # bWait=True fuerza a que GetOverlappedResult espere hasta que el I/O se cancele o complete
    _kernel32.GetOverlappedResult(
        ctypes.c_void_p(handle),
        ctypes.byref(ov),
        ctypes.byref(dummy_bytes),
        True,
    )


def _write_all_sync(handle: int, data: bytes) -> None:
    """Escribe data completamente en un handle sincrónico."""
    total_written = 0
    total_len = len(data)
    while total_written < total_len:
        written = wintypes.DWORD(0)
        remaining = total_len - total_written
        c_buf = (ctypes.c_ubyte * remaining).from_buffer_copy(data[total_written:])
        ok = _kernel32.WriteFile(
            ctypes.c_void_p(handle),
            ctypes.cast(ctypes.addressof(c_buf), wintypes.LPCVOID),
            remaining,
            ctypes.byref(written),
            None,
        )
        if not ok or written.value == 0:
            break
        total_written += written.value


def _write_all_overlapped(handle: int, data: bytes, timeout_seconds: float) -> None:
    """Escribe data completamente en un handle overlapped con timeout acotado y drenado ante cancelación."""
    _ensure_windows()
    h_event = _kernel32.CreateEventW(None, True, False, None)
    if _is_invalid_handle(h_event):
        raise OperatorVerifierBridgeError("CreateEventW falló en escritura overlapped")

    try:
        total_written = 0
        total_len = len(data)
        while total_written < total_len:
            _kernel32.ResetEvent(ctypes.c_void_p(h_event))
            ov = _OVERLAPPED()
            ov.hEvent = h_event
            chunk_written = wintypes.DWORD(0)
            remaining = total_len - total_written
            chunk_bytes = data[total_written:]
            c_buf = (ctypes.c_ubyte * remaining).from_buffer_copy(chunk_bytes)
            ptr = ctypes.cast(ctypes.addressof(c_buf), wintypes.LPCVOID)

            ok = _kernel32.WriteFile(
                ctypes.c_void_p(handle),
                ptr,
                remaining,
                ctypes.byref(chunk_written),
                ctypes.byref(ov),
            )
            if not ok:
                err = ctypes.get_last_error()
                if err == _ERROR_IO_PENDING:
                    wait_ms = int(timeout_seconds * 1000)
                    wait_res = _kernel32.WaitForSingleObject(h_event, wait_ms)
                    if wait_res == _WAIT_TIMEOUT:
                        _cancel_and_drain_overlapped(handle, ov)
                        raise OperatorVerifierTimeoutError(f"La escritura en el pipe expiró tras {timeout_seconds}s")
                    elif wait_res != _WAIT_OBJECT_0:
                        _cancel_and_drain_overlapped(handle, ov)
                        raise OperatorVerifierBridgeError("Fallo en WaitForSingleObject de WriteFile")

                    res_bytes = wintypes.DWORD(0)
                    res_ok = _kernel32.GetOverlappedResult(
                        ctypes.c_void_p(handle),
                        ctypes.byref(ov),
                        ctypes.byref(res_bytes),
                        False,
                    )
                    if not res_ok:
                        res_err = ctypes.get_last_error()
                        if res_err in (_ERROR_BROKEN_PIPE, 232):
                            raise ChildExitedPrematurelyError(
                                "El verificador hijo cerró el canal durante la escritura del request"
                            )
                        raise OperatorVerifierBridgeError(
                            f"GetOverlappedResult(WriteFile) falló: código Win32 {res_err}"
                        )
                    chunk_written = res_bytes
                elif err in (_ERROR_BROKEN_PIPE, 232):
                    raise ChildExitedPrematurelyError(
                        "El verificador hijo cerró el canal antes de recibir el request completo"
                    )
                else:
                    raise OperatorVerifierBridgeError(f"WriteFile falló: código Win32 {err}")

            if chunk_written.value == 0:
                raise ChildExitedPrematurelyError("WriteFile escribió 0 bytes inesperadamente")

            total_written += chunk_written.value
    finally:
        _kernel32.CloseHandle(ctypes.c_void_p(h_event))


def _read_frame_overlapped(
    handle: int,
    max_bytes: int,
    *,
    io_timeout_seconds: float,
    operation_deadline: float | None = None,
) -> bytes:
    """Lee un frame de respuesta con prefijo uint32 big-endian mediante I/O overlapped.

    Aplica una disciplina de timeouts de dos niveles:
    - operation_deadline: tiempo límite absoluto monotónico desde que el request fue
      enviado hasta recibir la respuesta terminal (cubre scan, hash e inventario).
    - io_timeout_seconds: tiempo máximo acotado para I/O chunks y transferencias activas.
    """
    _ensure_windows()
    h_event = _kernel32.CreateEventW(None, True, False, None)
    if _is_invalid_handle(h_event):
        raise OperatorVerifierBridgeError("CreateEventW falló en lectura overlapped")

    def _read_exact(count: int, *, is_initial_wait: bool = False) -> bytes:
        buf = (ctypes.c_ubyte * count)()
        total_read = 0
        while total_read < count:
            _kernel32.ResetEvent(ctypes.c_void_p(h_event))
            ov = _OVERLAPPED()
            ov.hEvent = h_event
            chunk_read = wintypes.DWORD(0)
            remaining = count - total_read
            ptr = ctypes.cast(ctypes.addressof(buf) + total_read, wintypes.LPVOID)
            ok = _kernel32.ReadFile(
                ctypes.c_void_p(handle),
                ptr,
                remaining,
                ctypes.byref(chunk_read),
                ctypes.byref(ov),
            )
            if not ok:
                err = ctypes.get_last_error()
                if err == _ERROR_IO_PENDING:
                    now = time.monotonic()
                    if is_initial_wait and total_read == 0 and operation_deadline is not None:
                        time_left = operation_deadline - now
                        if time_left <= 0:
                            _cancel_and_drain_overlapped(handle, ov)
                            raise OperatorVerifierTimeoutError(
                                "La operación del verificador hijo excedió el tiempo límite (operation_timeout) y expiró tras 0s"
                            )
                        current_timeout = time_left
                    else:
                        current_timeout = io_timeout_seconds
                        if operation_deadline is not None:
                            rem = operation_deadline - now
                            if rem <= 0:
                                _cancel_and_drain_overlapped(handle, ov)
                                raise OperatorVerifierTimeoutError(
                                    "La operación del verificador hijo excedió el tiempo límite (operation_timeout) y expiró tras 0s"
                                )
                            current_timeout = min(current_timeout, rem)

                    wait_ms = max(1, int(current_timeout * 1000))
                    wait_res = _kernel32.WaitForSingleObject(h_event, wait_ms)
                    if wait_res == _WAIT_TIMEOUT:
                        _cancel_and_drain_overlapped(handle, ov)
                        if operation_deadline is not None and (
                            is_initial_wait or time.monotonic() >= (operation_deadline - 0.05)
                        ):
                            raise OperatorVerifierTimeoutError(
                                f"La operación del verificador hijo excedió el tiempo límite (operation_timeout) y expiró tras {current_timeout:.1f}s"
                            )
                        raise OperatorVerifierTimeoutError(
                            f"La lectura del pipe expiró tras {current_timeout:.1f}s (io_timeout)"
                        )
                    elif wait_res != _WAIT_OBJECT_0:
                        _cancel_and_drain_overlapped(handle, ov)
                        raise OperatorVerifierBridgeError("Fallo en WaitForSingleObject de ReadFile")

                    res_read = wintypes.DWORD(0)
                    res_ok = _kernel32.GetOverlappedResult(
                        ctypes.c_void_p(handle),
                        ctypes.byref(ov),
                        ctypes.byref(res_read),
                        False,
                    )
                    if not res_ok:
                        res_err = ctypes.get_last_error()
                        if res_err in (_ERROR_BROKEN_PIPE, 232):
                            if total_read == 0:
                                return b""
                            raise ChildExitedPrematurelyError(
                                f"EOF inesperado a los {total_read}/{count} bytes del frame"
                            )
                        raise OperatorVerifierBridgeError(
                            f"GetOverlappedResult(ReadFile) falló: código Win32 {res_err}"
                        )
                    chunk_read = res_read
                elif err in (_ERROR_BROKEN_PIPE, 232):
                    if total_read == 0:
                        return b""
                    raise ChildExitedPrematurelyError(f"EOF inesperado a los {total_read}/{count} bytes del frame")
                else:
                    raise OperatorVerifierBridgeError(f"ReadFile falló: código Win32 {err}")

            if chunk_read.value == 0:
                if total_read == 0:
                    return b""
                raise ChildExitedPrematurelyError(f"EOF inesperado a los {total_read}/{count} bytes")

            total_read += chunk_read.value

        return bytes(buf)

    try:
        header = _read_exact(4, is_initial_wait=True)
        if not header:
            return b""
        length = int.from_bytes(header, byteorder="big")
        if length > max_bytes:
            raise ProtocolAbuseError(
                f"El mensaje anunciado ({length} bytes) excede el límite normativo ({max_bytes} bytes). "
                "Rechazado antes de reservar memoria"
            )
        if length == 0:
            raise ProtocolAbuseError("El mensaje anunciado tiene longitud cero")
        payload = _read_exact(length, is_initial_wait=False)
        if len(payload) < length:
            raise ChildExitedPrematurelyError("EOF antes de completar el payload")
        return payload
    finally:
        _kernel32.CloseHandle(ctypes.c_void_p(h_event))


def _reap_child_process(
    child: ChildProcessEvidence,
    *,
    normal_exit: bool,
    grace_period_ms: int = DEFAULT_CHILD_GRACE_PERIOD_MS,
) -> None:
    """Gestiona el ciclo de vida del proceso hijo verifier asegurando reap y terminación.

    En salida normal (normal_exit=True):
    - Espera hasta grace_period_ms a que el hijo concluya su proceso.
    - Si no concluye dentro del grace period: termina forzosamente al hijo y
      falla cerrado levantando ProtocolAbuseError (un verificador de un solo uso
      no debe permanecer vivo tras emitir su respuesta terminal).

    En salida anormal / fallo previo (normal_exit=False):
    - Si el proceso sigue activo, lo termina inmediatamente con TerminateProcess
      y drena con WaitForSingleObject para evitar procesos huérfanos o zombis.
    """
    _ensure_windows()
    h_proc = ctypes.c_void_p(child.process_handle)
    exit_code = wintypes.DWORD(0)

    if normal_exit:
        wait_res = _kernel32.WaitForSingleObject(h_proc, grace_period_ms)
        if wait_res == _WAIT_OBJECT_0:
            return
        # El proceso no salió en el período de gracia tras enviar terminal response
        _kernel32.TerminateProcess(h_proc, 1)
        _kernel32.WaitForSingleObject(h_proc, 1000)
        raise ProtocolAbuseError(
            f"El verificador hijo (PID={child.pid}) continuó ejecutándose tras enviar "
            "la respuesta terminal (violación de ciclo de vida, fail-closed)"
        )
    else:
        # Salida anormal: verificar si sigue activo y terminarlo
        if (
            _kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)) and exit_code.value == 259  # STILL_ACTIVE
        ):
            _kernel32.TerminateProcess(h_proc, 1)
            _kernel32.WaitForSingleObject(h_proc, 1000)


# ============================================================================
# Helpers de Seguridad Win32 para Named Pipes
# ============================================================================


def _build_named_pipe_security_descriptor(operator_sid: str) -> int:
    """Construye un Security Descriptor con DACL restrictiva protegida (D:P).

    Allowlist cerrada:
    - SYSTEM (SY): GENERIC_ALL
    - Administrators (BA): GENERIC_ALL
    - Operator SID: GENERIC_READ | GENERIC_WRITE (GRGW)
    """
    _ensure_windows()
    sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;{operator_sid})"
    p_sd = wintypes.LPVOID()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,  # SDDL_REVISION_1
        ctypes.byref(p_sd),
        None,
    ):
        err = ctypes.get_last_error()
        raise OperatorVerifierBridgeError(
            f"ConvertStringSecurityDescriptorToSecurityDescriptorW falló: código Win32 {err}"
        )
    return int(p_sd.value or 0)


def _read_process_creation_time(process_handle: int) -> int | None:
    creation = _FileTime()
    dummy_exit = _FileTime()
    dummy_kernel = _FileTime()
    dummy_user = _FileTime()
    ok = _kernel32.GetProcessTimes(
        ctypes.c_void_p(process_handle),
        ctypes.byref(creation),
        ctypes.byref(dummy_exit),
        ctypes.byref(dummy_kernel),
        ctypes.byref(dummy_user),
    )
    if not ok:
        return None
    c_int = _filetime_to_uint64(creation)
    return c_int if c_int > 0 else None


def _read_process_image_path(process_handle: int) -> str | None:
    capacity = 32768
    buffer = ctypes.create_unicode_buffer(capacity)
    size = wintypes.DWORD(capacity)
    ok = _kernel32.QueryFullProcessImageNameW(
        ctypes.c_void_p(process_handle),
        0,
        buffer,
        ctypes.byref(size),
    )
    if not ok or size.value == 0:
        return None
    return str(buffer.value)


# ============================================================================
# Launcher Protocol & Implementations
# ============================================================================


class OperatorVerifierLauncher(Protocol):
    """Protocolo del componente encargado de lanzar el verificador child."""

    def launch_child(
        self,
        token: OperatorPrimaryToken,
        executable_path: pathlib.Path,
        cmdline: str,
    ) -> ChildProcessEvidence: ...


class Win32CreateProcessWithTokenLauncher:
    """Lanzador productivo mediante CreateProcessWithTokenW."""

    def launch_child(
        self,
        token: OperatorPrimaryToken,
        executable_path: pathlib.Path,
        cmdline: str,
    ) -> ChildProcessEvidence:
        _ensure_windows()
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        pi = _PROCESS_INFORMATION()

        cmd_buf = ctypes.create_unicode_buffer(cmdline)
        exe_str = str(executable_path)

        ok = _advapi32.CreateProcessWithTokenW(
            ctypes.c_void_p(token.raw_handle),
            0,  # dwLogonFlags = 0 (sin carga de perfil de red adicional, one-shot verifier)
            exe_str,
            cmd_buf,
            _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            err = ctypes.get_last_error()
            raise OperatorVerifierLaunchError(
                f"CreateProcessWithTokenW falló: código Win32 {err}",
                win32_code=err,
            )

        # Cierre inmediato del thread handle para evitar fugas
        if pi.hThread:
            _kernel32.CloseHandle(pi.hThread)

        process_handle = int(pi.hProcess)
        pid = int(pi.dwProcessId)

        creation_time = _read_process_creation_time(process_handle)
        if creation_time is None:
            _kernel32.CloseHandle(ctypes.c_void_p(process_handle))
            raise PeerAuthenticationError("No se pudo leer ProcessCreationTime del proceso lanzado")

        observed_image = _read_process_image_path(process_handle)
        if observed_image is None or _normalize_image_for_compare(observed_image) != _normalize_image_for_compare(
            exe_str
        ):
            _kernel32.CloseHandle(ctypes.c_void_p(process_handle))
            raise PeerAuthenticationError(
                f"La imagen del proceso lanzado ('{observed_image}') no coincide con la esperada ('{exe_str}')"
            )

        return ChildProcessEvidence(
            process_handle=process_handle,
            pid=pid,
            creation_time=creation_time,
            image_path=observed_image,
        )


def resolve_production_verifier_executable() -> pathlib.Path:
    """Resuelve la ruta del ejecutable empaquetado del verificador en producción.

    En GP2-P1 este empaquetado permanece UNRESOLVED (fail-closed).
    Queda terminantemente prohibido caer a sys.executable, python.exe, TEMP o cwd.
    """
    raise VerifierImageNotProvisionedError(
        "El binario empaquetado del verificador aún no está provisionado (UNRESOLVED). "
        "La ruta de producción es fail-closed"
    )


class _TestOnlyVerifierLauncher:
    """Seam de testing confinada a pruebas Win32 (no para producción).

    Lanza un subproceso Python real desechable para que el helper interactúe
    con un proceso Win32 genuino, permitiendo verificar ciclo de vida,
    WaitForSingleObject y TerminateProcess sin comprometer el proceso de test.
    """

    _is_test_verifier_launcher: Final[bool] = True

    def __init__(
        self,
        *,
        tamper_child_pid: int | None = None,
        tamper_creation_time_delta: int = 0,
        tamper_image_path: str | None = None,
        tamper_response_nonce: str | None = None,
        tamper_hang_after_response: bool = False,
        worker_code_override: str | None = None,
    ) -> None:
        self.tamper_child_pid = tamper_child_pid
        self.tamper_creation_time_delta = tamper_creation_time_delta
        self.tamper_image_path = tamper_image_path
        self.tamper_response_nonce = tamper_response_nonce
        self.tamper_hang_after_response = tamper_hang_after_response
        self.worker_code_override = worker_code_override
        self._spawned_procs: list[Any] = []

    def reap_proc(self) -> None:
        """Drena y reapea los subprocesos de prueba para evitar ResourceWarning en Python."""
        for p in list(self._spawned_procs):
            try:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            try:
                if p.stdin:
                    p.stdin.close()
                if p.stdout:
                    p.stdout.close()
                if p.stderr:
                    p.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    def launch_child(
        self,
        token: OperatorPrimaryToken,
        executable_path: pathlib.Path,
        cmdline: str,
    ) -> ChildProcessEvidence:
        _ensure_windows()
        parts = cmdline.split()
        pipe_name = ""
        for i, part in enumerate(parts):
            if part == "--pipe-name" and i + 1 < len(parts):
                pipe_name = parts[i + 1]
                break
        if not pipe_name:
            raise OperatorVerifierLaunchError("No se encontró --pipe-name en el cmdline de prueba")

        if self.worker_code_override is not None:
            script = self.worker_code_override.replace("{PIPE_NAME}", pipe_name)
        else:
            script = (
                "import os, sys\n"
                "from unittest.mock import patch\n"
                "from sky_claw.local.runtime_vault.operator_verifier_bridge import run_verifier_child_worker\n"
                "test_ver = os.environ.get('_SKYCLAW_TEST_RUNTIME_VERSION', '1.6.1170.0')\n"
                "with patch('sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version', return_value=test_ver):\n"
                f"    run_verifier_child_worker({pipe_name!r}, tamper_nonce={self.tamper_response_nonce!r}, tamper_hang_after_response={self.tamper_hang_after_response!r})\n"
            )

        import subprocess

        sys_exe = getattr(sys, "_base_executable", sys.executable)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        proc = subprocess.Popen(
            [sys_exe, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self._spawned_procs.append(proc)

        pid = proc.pid
        # SYNCHRONIZE (0x00100000) | PROCESS_TERMINATE (0x0001) | PROCESS_QUERY_INFORMATION (0x0400)
        desired_access = 0x00100000 | 0x0001 | 0x0400
        h_process = _kernel32.OpenProcess(desired_access, False, pid)
        if _is_invalid_handle(h_process):
            proc.kill()
            raise OperatorVerifierLaunchError("OpenProcess sobre subproceso de prueba falló")

        real_creation = _read_process_creation_time(int(h_process)) or 100_000_000
        reported_creation = real_creation + self.tamper_creation_time_delta

        real_image = _read_process_image_path(int(h_process)) or str(executable_path)
        reported_image = self.tamper_image_path if self.tamper_image_path is not None else real_image
        reported_pid = self.tamper_child_pid if self.tamper_child_pid is not None else pid

        return ChildProcessEvidence(
            process_handle=int(h_process),
            pid=reported_pid,
            creation_time=reported_creation,
            image_path=reported_image,
        )


# ============================================================================
# Lógica del Worker (Child Process)
# ============================================================================


def run_verifier_child_worker(
    pipe_name: str,
    *,
    tamper_nonce: str | None = None,
    tamper_hang_after_response: bool = False,
) -> None:
    """Ejecuta el protocolo interno del verificador conectándose como cliente al pipe privado."""
    _ensure_windows()

    # Abrir pipe como cliente
    h_client = _kernel32.CreateFileW(
        pipe_name,
        _GENERIC_READ | _GENERIC_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if _is_invalid_handle(h_client):
        err = ctypes.get_last_error()
        raise OperatorVerifierBridgeError(f"Worker no pudo conectar al pipe '{pipe_name}': código Win32 {err}")

    try:
        # 1. Leer request enmarcado
        req_bytes = read_length_prefixed_frame(int(h_client), MAX_REQUEST_BYTES)
        if not req_bytes:
            return

        try:
            req_data = json.loads(req_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # JSON malformado: fail-closed desconectando sin emitir respuesta ni fabricar nonces
            return

        if not isinstance(req_data, dict):
            return

        operation_id = req_data.get("operation_id")
        nonce = tamper_nonce if tamper_nonce is not None else req_data.get("nonce")
        mode = req_data.get("mode")

        # Sin operation_id o nonce no es posible autenticar una respuesta: desconectar de inmediato
        if not isinstance(operation_id, str) or not isinstance(nonce, str) or not isinstance(mode, str):
            return

        resp_payload: dict[str, Any]

        try:
            version = req_data.get("version")
            if version != PROTOCOL_VERSION:
                raise ProtocolAbuseError(f"Versión de protocolo no soportada: {version} (requerida {PROTOCOL_VERSION})")

            if mode == VerifierMode.OBSERVE:
                _validate_closed_keys(req_data, _REQUEST_OBSERVE_KEYS, "request OBSERVE")
            elif mode == VerifierMode.VERIFY:
                _validate_closed_keys(req_data, _REQUEST_VERIFY_KEYS, "request VERIFY")
                _validate_closed_keys(req_data["expected_physical_root"], _PHYSICAL_ROOT_KEYS, "expected_physical_root")
                _validate_closed_keys(req_data["expected_tree"], _TREE_DIGEST_KEYS, "expected_tree")
                _validate_closed_keys(req_data["expected_runtime"], _RUNTIME_IDENTITY_KEYS, "expected_runtime")
                for i, ce in enumerate(req_data.get("critical_expectations", [])):
                    _validate_closed_keys(ce, _CRITICAL_EXPECTATION_KEYS, f"critical_expectations[{i}]")
            else:
                raise ProtocolAbuseError(f"Modo '{mode}' no reconocido")

            canonical_root = req_data["canonical_root"]
            expected_game_key = req_data["expected_game_key"]

            if mode == VerifierMode.OBSERVE:
                # En OBSERVE: deriva identidad física y mide frescos bajo bound_physical_root (anti-TOCTOU)
                with bound_physical_root(canonical_root) as phys_identity:
                    obs_runtime = observe_runtime_identity_from_root(
                        canonical_root, expected_game_key=expected_game_key
                    )
                    files = inventory_tree(pathlib.Path(canonical_root))
                    tree_digest = tree_digest_from_files(files)

                resp_payload = {
                    "version": PROTOCOL_VERSION,
                    "operation_id": operation_id,
                    "mode": mode,
                    "nonce": nonce,
                    "disposition": VerifierDisposition.OBSERVED.value,
                    "canonical_root": phys_identity.canonical_root,
                    "volume_serial_number": phys_identity.volume_serial_number,
                    "root_file_id": phys_identity.root_file_id,
                    "observed_tree": {
                        "digest": tree_digest.digest,
                        "files": tree_digest.files,
                        "bytes": tree_digest.bytes,
                    },
                    "observed_runtime": {
                        "game_key": obs_runtime.game_key,
                        "game_version": obs_runtime.game_version,
                        "observed_exe_path": obs_runtime.observed_exe_path,
                        "observed_at_ns": obs_runtime.observed_at_ns,
                    },
                    "critical_evidences": [
                        {
                            "rel_path": f.rel_path,
                            "state": VerificationState.UNKNOWN.value,
                            "observed_digest": f.digest,
                            "expected_digest": None,
                            "observed_size": f.size,
                            "expected_size": None,
                            "message": "",
                        }
                        for f in files
                        if f.rel_path.lower().endswith(".exe")
                    ],
                    "message": "",
                }

            elif mode == VerifierMode.VERIFY:
                # En VERIFY: revalida identidad física y compara bajo bound_physical_root (anti-TOCTOU)
                expected_phys = PhysicalRootIdentity(
                    canonical_root=req_data["expected_physical_root"]["canonical_root"],
                    volume_serial_number=req_data["expected_physical_root"]["volume_serial_number"],
                    root_file_id=req_data["expected_physical_root"]["root_file_id"],
                )
                with bound_physical_root(canonical_root, expected_phys) as phys_identity:
                    obs_runtime = observe_runtime_identity_from_root(
                        canonical_root, expected_game_key=expected_game_key
                    )
                    files = inventory_tree(pathlib.Path(canonical_root))
                    observed_tree = tree_digest_from_files(files)

                expected_tree = TreeDigest(
                    digest=req_data["expected_tree"]["digest"],
                    files=req_data["expected_tree"]["files"],
                    bytes=req_data["expected_tree"]["bytes"],
                )
                expected_runtime = RuntimeIdentity(
                    game_key=req_data["expected_runtime"]["game_key"],
                    game_version=req_data["expected_runtime"]["game_version"],
                )

                tree_match = observed_tree == expected_tree
                runtime_match = obs_runtime.runtime_identity == expected_runtime

                # Revalidación de critical_expectations
                files_by_relpath = {f.rel_path: f for f in files}
                crit_evidences_list: list[dict[str, Any]] = []
                critical_matches = True
                diffs: list[str] = []

                for ce_dict in req_data.get("critical_expectations", []):
                    rel_p = ce_dict["rel_path"]
                    exp_dig = ce_dict["expected_digest"]
                    exp_sz = ce_dict.get("expected_size")

                    if rel_p in files_by_relpath:
                        obs_f = files_by_relpath[rel_p]
                        d_ok = obs_f.digest.lower() == exp_dig.lower()
                        s_ok = exp_sz is None or obs_f.size == exp_sz
                        if d_ok and s_ok:
                            st = VerificationState.VERIFIED.value
                            c_msg = ""
                        else:
                            st = VerificationState.FAILED.value
                            c_msg = "Critical file digest or size mismatch"
                            critical_matches = False
                            diffs.append(f"CriticalFile '{rel_p}' mismatch")
                        crit_evidences_list.append(
                            {
                                "rel_path": rel_p,
                                "state": st,
                                "observed_digest": obs_f.digest,
                                "expected_digest": exp_dig,
                                "observed_size": obs_f.size,
                                "expected_size": exp_sz,
                                "message": c_msg,
                            }
                        )
                    else:
                        critical_matches = False
                        diffs.append(f"CriticalFile '{rel_p}' missing")
                        crit_evidences_list.append(
                            {
                                "rel_path": rel_p,
                                "state": VerificationState.FAILED.value,
                                "observed_digest": None,
                                "expected_digest": exp_dig,
                                "observed_size": None,
                                "expected_size": exp_sz,
                                "message": f"Critical file '{rel_p}' ausente en el árbol observado",
                            }
                        )

                if not tree_match:
                    diffs.append("TreeDigest mismatch")
                if not runtime_match:
                    diffs.append("RuntimeIdentity mismatch")

                if tree_match and runtime_match and critical_matches:
                    disp = VerifierDisposition.VERIFIED.value
                    msg = ""
                else:
                    disp = VerifierDisposition.FAILED.value
                    msg = "; ".join(diffs)

                resp_payload = {
                    "version": PROTOCOL_VERSION,
                    "operation_id": operation_id,
                    "mode": mode,
                    "nonce": nonce,
                    "disposition": disp,
                    "canonical_root": phys_identity.canonical_root,
                    "volume_serial_number": phys_identity.volume_serial_number,
                    "root_file_id": phys_identity.root_file_id,
                    "observed_tree": {
                        "digest": observed_tree.digest,
                        "files": observed_tree.files,
                        "bytes": observed_tree.bytes,
                    },
                    "observed_runtime": {
                        "game_key": obs_runtime.game_key,
                        "game_version": obs_runtime.game_version,
                        "observed_exe_path": obs_runtime.observed_exe_path,
                        "observed_at_ns": obs_runtime.observed_at_ns,
                    },
                    "critical_evidences": crit_evidences_list,
                    "message": msg,
                }
        except Exception as exc:  # noqa: BLE001 — boundary del worker emite REJECTED cerrado
            resp_payload = {
                "version": PROTOCOL_VERSION,
                "operation_id": operation_id,
                "mode": str(mode),
                "nonce": nonce,
                "disposition": VerifierDisposition.REJECTED.value,
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

        frame = encode_length_prefixed_frame(json.dumps(resp_payload, ensure_ascii=False).encode("utf-8"))
        _write_all_sync(int(h_client), frame)

        if tamper_hang_after_response:
            time.sleep(10.0)
    finally:
        _kernel32.CloseHandle(ctypes.c_void_p(h_client))


# ============================================================================
# OperatorVerifierBridge Central
# ============================================================================


class OperatorVerifierBridge:
    """Coordinador UAC-elevado para invocar al Verifier no elevado vía named pipe autenticado."""

    def __init__(
        self,
        *,
        launcher: OperatorVerifierLauncher | None = None,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        io_timeout_seconds: float = DEFAULT_IPC_IO_TIMEOUT_SECONDS,
        operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS,
        timeout_seconds: float | None = None,
        grace_period_ms: int = DEFAULT_CHILD_GRACE_PERIOD_MS,
    ) -> None:
        self._launcher = launcher or Win32CreateProcessWithTokenLauncher()
        if timeout_seconds is not None:
            self._connect_timeout_seconds = timeout_seconds
            self._io_timeout_seconds = timeout_seconds
            self._operation_timeout_seconds = timeout_seconds
        else:
            self._connect_timeout_seconds = connect_timeout_seconds
            self._io_timeout_seconds = io_timeout_seconds
            self._operation_timeout_seconds = operation_timeout_seconds
        self._grace_period_ms = grace_period_ms
        self._consumed_nonces: set[str] = set()
        self._lock = threading.Lock()

    def _generate_nonce(self) -> str:
        return secrets.token_hex(32)

    def _mark_nonce_consumed(self, nonce: str) -> None:
        if nonce:
            with self._lock:
                self._consumed_nonces.add(nonce)

    def _execute_ipc_cycle(
        self,
        token: OperatorPrimaryToken,
        request_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Ejecuta el ciclo transaccional completo: pipe -> launch -> auth -> I/O -> cleanup."""
        _ensure_windows()
        if not isinstance(token, OperatorPrimaryToken):
            raise OperatorVerifierBridgeError("token debe ser un OperatorPrimaryToken normativo")
        if token.closed:
            raise OperatorVerifierBridgeError("Uso del OperatorPrimaryToken después de su cierre")

        operator_sid = token.evidence.operator_sid
        pipe_name = f"\\\\.\\pipe\\skyclaw_verifier_{secrets.token_hex(16)}"
        p_sd = _build_named_pipe_security_descriptor(operator_sid)

        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = ctypes.c_void_p(p_sd)
        sa.bInheritHandle = False

        h_pipe = _kernel32.CreateNamedPipeW(
            pipe_name,
            _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE | _FILE_FLAG_OVERLAPPED,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
            1,
            65536,
            65536,
            int(self._connect_timeout_seconds * 1000),
            ctypes.byref(sa),
        )
        if _is_invalid_handle(h_pipe):
            err = ctypes.get_last_error()
            _kernel32.LocalFree(ctypes.c_void_p(p_sd))
            raise OperatorVerifierBridgeError(f"CreateNamedPipeW falló: código Win32 {err}")

        child_evidence: ChildProcessEvidence | None = None
        h_event = _kernel32.CreateEventW(None, True, False, None)

        try:
            # 1. Resolver ejecutable y lanzar child
            if getattr(self._launcher, "_is_test_verifier_launcher", False):
                exe_path = pathlib.Path("C:\\Sky-Claw\\test_verifier.exe")
            else:
                exe_path = resolve_production_verifier_executable()

            cmdline = f'"{exe_path}" --pipe-name {pipe_name}'
            child_evidence = self._launcher.launch_child(token, exe_path, cmdline)

            # 2. Conexión del pipe con connect_timeout_seconds
            ov = _OVERLAPPED()
            ov.hEvent = h_event
            connected = False
            ok = _kernel32.ConnectNamedPipe(ctypes.c_void_p(h_pipe), ctypes.byref(ov))
            if ok:
                connected = True
            else:
                err = ctypes.get_last_error()
                if err == _ERROR_PIPE_CONNECTED:
                    connected = True
                elif err in (_ERROR_BROKEN_PIPE, 232):  # 232 = ERROR_NO_DATA
                    raise ChildExitedPrematurelyError(
                        "El verificador hijo cerró el canal prematuramente antes de la conexión"
                    )
                elif err == _ERROR_IO_PENDING:
                    wait_ms = int(self._connect_timeout_seconds * 1000)
                    wait_res = _kernel32.WaitForSingleObject(h_event, wait_ms)
                    if wait_res == _WAIT_OBJECT_0:
                        connected = True
                    elif wait_res == _WAIT_TIMEOUT:
                        _cancel_and_drain_overlapped(h_pipe, ov)
                        raise OperatorVerifierTimeoutError(
                            f"La conexión del verificador hijo expiró tras {self._connect_timeout_seconds}s"
                        )
                    else:
                        _cancel_and_drain_overlapped(h_pipe, ov)
                        raise OperatorVerifierBridgeError("Fallo en WaitForSingleObject de ConnectNamedPipe")
                else:
                    raise OperatorVerifierBridgeError(f"ConnectNamedPipe falló: código Win32 {err}")

            if not connected:
                raise OperatorVerifierBridgeError("No se pudo conectar con el verificador hijo")

            # 3. Autenticación rigurosa del Peer
            client_pid_dw = wintypes.ULONG(0)
            if not _kernel32.GetNamedPipeClientProcessId(ctypes.c_void_p(h_pipe), ctypes.byref(client_pid_dw)):
                err = ctypes.get_last_error()
                raise PeerAuthenticationError(f"GetNamedPipeClientProcessId falló: código Win32 {err}")

            peer_pid = int(client_pid_dw.value)
            if peer_pid != child_evidence.pid:
                raise PeerAuthenticationError(
                    f"Peer PID mismatch: conectado PID={peer_pid}, esperado PID={child_evidence.pid}"
                )

            # Abrir handle FRESCO e independiente sobre el peer identificado por el pipe (peer_pid)
            h_peer = _kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
                False,
                peer_pid,
            )
            if _is_invalid_handle(h_peer):
                err = ctypes.get_last_error()
                raise PeerAuthenticationError(
                    f"OpenProcess sobre el peer conectado (PID={peer_pid}) falló: código Win32 {err}"
                )

            try:
                # Comprobar que el proceso cliente sigue activo (STILL_ACTIVE)
                exit_code = wintypes.DWORD(0)
                if not _kernel32.GetExitCodeProcess(ctypes.c_void_p(h_peer), ctypes.byref(exit_code)):
                    err = ctypes.get_last_error()
                    raise PeerAuthenticationError(
                        f"GetExitCodeProcess sobre el peer conectado (PID={peer_pid}) falló: código Win32 {err}"
                    )
                if exit_code.value != _STILL_ACTIVE:
                    raise PeerAuthenticationError(
                        f"El peer conectado (PID={peer_pid}) ya no está activo (código {exit_code.value}, esperado STILL_ACTIVE)"
                    )

                obs_creation = _read_process_creation_time(int(h_peer))
                if obs_creation is None or obs_creation != child_evidence.creation_time:
                    raise PeerAuthenticationError(
                        f"ProcessCreationTime mismatch en peer conectado ({obs_creation} vs {child_evidence.creation_time}): "
                        "posible reciclaje de PID (TOCTOU mitigado)"
                    )

                obs_image = _read_process_image_path(int(h_peer))
                if obs_image is None or _normalize_image_for_compare(obs_image) != _normalize_image_for_compare(
                    child_evidence.image_path
                ):
                    raise PeerAuthenticationError(
                        f"Process image mismatch en peer conectado: observado '{obs_image}', esperado '{child_evidence.image_path}'"
                    )
            finally:
                _kernel32.CloseHandle(ctypes.c_void_p(h_peer))

            # 4. Enviar request enmarcado (io_timeout_seconds)
            req_frame = encode_length_prefixed_frame(json.dumps(request_dict, ensure_ascii=False).encode("utf-8"))
            _write_all_overlapped(int(h_pipe), req_frame, self._io_timeout_seconds)

            # 5. Leer respuesta terminal bajo el deadline de operación monotónico
            operation_deadline = time.monotonic() + self._operation_timeout_seconds
            resp_bytes = _read_frame_overlapped(
                h_pipe,
                MAX_RESPONSE_BYTES,
                io_timeout_seconds=self._io_timeout_seconds,
                operation_deadline=operation_deadline,
            )
            if not resp_bytes:
                raise ChildExitedPrematurelyError(
                    "El verificador hijo cerró el canal prematuramente sin enviar respuesta terminal"
                )

            # 6. Validar exactamente una respuesta (verificar EOF / trailing bytes)
            dummy_buf = (ctypes.c_ubyte * 1)()
            dummy_read = wintypes.DWORD(0)
            extra_ov = _OVERLAPPED()
            extra_event = _kernel32.CreateEventW(None, True, False, None)
            extra_ov.hEvent = extra_event
            try:
                extra_ok = _kernel32.ReadFile(
                    ctypes.c_void_p(h_pipe),
                    ctypes.cast(dummy_buf, wintypes.LPVOID),
                    1,
                    ctypes.byref(dummy_read),
                    ctypes.byref(extra_ov),
                )
                if extra_ok and dummy_read.value > 0:
                    raise ProtocolAbuseError(
                        "Trailing bytes detectados tras el mensaje terminal del verificador (violación de frame único)"
                    )
                if not extra_ok:
                    err = ctypes.get_last_error()
                    if err == _ERROR_IO_PENDING:
                        wait_ms = min(50, max(1, int(self._io_timeout_seconds * 1000)))
                        wait_res = _kernel32.WaitForSingleObject(extra_event, wait_ms)
                        if wait_res == _WAIT_OBJECT_0:
                            _kernel32.GetOverlappedResult(
                                ctypes.c_void_p(h_pipe),
                                ctypes.byref(extra_ov),
                                ctypes.byref(dummy_read),
                                False,
                            )
                            if dummy_read.value > 0:
                                raise ProtocolAbuseError(
                                    "Trailing bytes detectados tras el mensaje terminal del verificador"
                                )
                        else:
                            _cancel_and_drain_overlapped(h_pipe, extra_ov)
            finally:
                _kernel32.CloseHandle(ctypes.c_void_p(extra_event))

            # 7. Parsear JSON cerrado y validar esquema normativo
            try:
                raw_json = json.loads(resp_bytes.decode("utf-8"))
            except Exception as exc:
                raise ProtocolAbuseError(f"JSON de respuesta inválido o malformado: {exc}") from exc

            if not isinstance(raw_json, dict):
                raise ProtocolAbuseError("Respuesta debe ser un objeto JSON")

            resp_data: dict[str, Any] = cast(dict[str, Any], raw_json)

            resp_version = resp_data.get("version")
            if resp_version != PROTOCOL_VERSION:
                raise ProtocolAbuseError(
                    f"Versión de respuesta no soportada: {resp_version} (requerida {PROTOCOL_VERSION})"
                )

            disposition = resp_data.get("disposition")
            if disposition == VerifierDisposition.REJECTED.value:
                _validate_closed_keys(resp_data, _RESPONSE_REJECTED_KEYS, "response REJECTED")
            elif disposition == VerifierDisposition.OBSERVED.value:
                _validate_closed_keys(resp_data, _RESPONSE_OBSERVED_KEYS, "response OBSERVED")
                _validate_closed_keys(resp_data["observed_tree"], _TREE_DIGEST_KEYS, "observed_tree")
                _validate_closed_keys(resp_data["observed_runtime"], _FRESH_RUNTIME_KEYS, "observed_runtime")
                for i, c in enumerate(resp_data.get("critical_evidences", [])):
                    _validate_closed_keys(c, _CRITICAL_EVIDENCE_KEYS, f"critical_evidences[{i}]")
            elif disposition in (VerifierDisposition.VERIFIED.value, VerifierDisposition.FAILED.value):
                _validate_closed_keys(resp_data, _RESPONSE_VERIFIED_KEYS, f"response {disposition}")
                _validate_closed_keys(resp_data["observed_tree"], _TREE_DIGEST_KEYS, "observed_tree")
                _validate_closed_keys(resp_data["observed_runtime"], _FRESH_RUNTIME_KEYS, "observed_runtime")
                for i, c in enumerate(resp_data.get("critical_evidences", [])):
                    _validate_closed_keys(c, _CRITICAL_EVIDENCE_KEYS, f"critical_evidences[{i}]")
            else:
                raise ProtocolAbuseError(f"disposition desconocida en respuesta: {disposition}")

            # Salida normal: esperar a que el hijo concluya dentro de la gracia
            _reap_child_process(child_evidence, normal_exit=True, grace_period_ms=self._grace_period_ms)

            return resp_data
        except Exception:
            if child_evidence is not None:
                _reap_child_process(child_evidence, normal_exit=False)
            raise
        finally:
            reap_fn = getattr(self._launcher, "reap_proc", None)
            if callable(reap_fn):
                reap_fn()
            if child_evidence and child_evidence.process_handle:
                _kernel32.CloseHandle(ctypes.c_void_p(child_evidence.process_handle))
            if h_event:
                _kernel32.CloseHandle(ctypes.c_void_p(h_event))
            if not _is_invalid_handle(h_pipe):
                _kernel32.CloseHandle(ctypes.c_void_p(h_pipe))
            if p_sd:
                _kernel32.LocalFree(ctypes.c_void_p(p_sd))

    def invoke_observe(
        self,
        token: OperatorPrimaryToken,
        root: pathlib.Path | str,
        operation_id: str,
        *,
        expected_game_key: str = "skyrimse",
    ) -> OperatorVerifierObservationResult:
        """Ejecuta una medición en modo OBSERVE (para TOFU)."""
        nonce = self._generate_nonce()
        lexical_root = os.path.abspath(os.fspath(root))

        request_payload = {
            "version": PROTOCOL_VERSION,
            "operation_id": operation_id,
            "mode": VerifierMode.OBSERVE,
            "nonce": nonce,
            "canonical_root": lexical_root,
            "expected_game_key": expected_game_key,
        }

        try:
            resp = self._execute_ipc_cycle(token, request_payload)
            resp_nonce = resp.get("nonce")
            if resp_nonce in self._consumed_nonces:
                raise NonceAuthenticationError(f"Nonce ya consumido (posible ataque de replay): '{resp_nonce}'")
            if resp_nonce != nonce:
                raise NonceAuthenticationError(f"Nonce mismatch: esperado '{nonce}', recibido '{resp_nonce}'")
        finally:
            self._mark_nonce_consumed(nonce)

        # Validaciones del protocolo
        if resp.get("operation_id") != operation_id:
            raise ProtocolAbuseError("operation_id mismatch en la respuesta")
        if resp.get("mode") != VerifierMode.OBSERVE:
            raise ProtocolAbuseError(f"mode mismatch: esperado OBSERVE, recibido {resp.get('mode')}")

        disposition = resp.get("disposition")
        if disposition == VerifierDisposition.REJECTED.value:
            err_type = resp.get("error_type", "REJECTED")
            msg = resp.get("message", "")
            raise OperatorVerifierBridgeError(f"Operación OBSERVE rechazada [REJECTED] ({err_type}): {msg}")

        if disposition != VerifierDisposition.OBSERVED.value:
            msg = resp.get("message", "")
            raise OperatorVerifierBridgeError(
                f"Modo OBSERVE no produjo disposition OBSERVED (disposition='{disposition}', message='{msg}')"
            )

        phys_id = PhysicalRootIdentity(
            canonical_root=resp["canonical_root"],
            volume_serial_number=resp["volume_serial_number"],
            root_file_id=resp["root_file_id"],
        )
        obs_tree = TreeDigest(
            digest=resp["observed_tree"]["digest"],
            files=resp["observed_tree"]["files"],
            bytes=resp["observed_tree"]["bytes"],
        )
        obs_runtime_dto = FreshRuntimeObservation(
            game_key=resp["observed_runtime"]["game_key"],
            game_version=resp["observed_runtime"]["game_version"],
            observed_exe_path=resp["observed_runtime"]["observed_exe_path"],
            observed_at_ns=resp["observed_runtime"]["observed_at_ns"],
        )

        crit_evs: list[CriticalFileEvidence] = []
        for c in resp.get("critical_evidences", []):
            crit_evs.append(
                CriticalFileEvidence(
                    rel_path=c["rel_path"],
                    state=VerificationState(c["state"]),
                    observed_digest=c.get("observed_digest"),
                    expected_digest=c.get("expected_digest"),
                    observed_size=c.get("observed_size"),
                    expected_size=c.get("expected_size"),
                    message=c.get("message", ""),
                )
            )

        return OperatorVerifierObservationResult(
            disposition=VerifierDisposition.OBSERVED,
            operation_id=operation_id,
            nonce=nonce,
            physical_root=phys_id,
            observed_tree=obs_tree,
            observed_runtime=obs_runtime_dto,
            critical_evidences=tuple(crit_evs),
            message=resp.get("message", ""),
        )

    def invoke_verify(
        self,
        token: OperatorPrimaryToken,
        root: pathlib.Path | str,
        operation_id: str,
        *,
        expected_physical_root: PhysicalRootIdentity,
        expected_tree: TreeDigest,
        expected_runtime: RuntimeIdentity | FreshRuntimeObservation,
        critical_expectations: Sequence[CriticalFileExpectation] = (),
        expected_game_key: str = "skyrimse",
    ) -> OperatorVerifierVerificationResult:
        """Ejecuta una re-verificación fresca en modo VERIFY."""
        nonce = self._generate_nonce()
        lexical_root = os.path.abspath(os.fspath(root))
        target_expected_runtime = (
            expected_runtime.runtime_identity
            if isinstance(expected_runtime, FreshRuntimeObservation)
            else expected_runtime
        )

        request_payload = {
            "version": PROTOCOL_VERSION,
            "operation_id": operation_id,
            "mode": VerifierMode.VERIFY,
            "nonce": nonce,
            "canonical_root": lexical_root,
            "expected_game_key": expected_game_key,
            "expected_physical_root": {
                "canonical_root": expected_physical_root.canonical_root,
                "volume_serial_number": expected_physical_root.volume_serial_number,
                "root_file_id": expected_physical_root.root_file_id,
            },
            "expected_tree": {
                "digest": expected_tree.digest,
                "files": expected_tree.files,
                "bytes": expected_tree.bytes,
            },
            "expected_runtime": {
                "game_key": target_expected_runtime.game_key,
                "game_version": target_expected_runtime.game_version,
            },
            "critical_expectations": [
                {
                    "rel_path": ce.rel_path,
                    "expected_digest": ce.expected_digest,
                    "expected_size": ce.expected_size,
                }
                for ce in critical_expectations
            ],
        }

        try:
            resp = self._execute_ipc_cycle(token, request_payload)
            resp_nonce = resp.get("nonce")
            if resp_nonce in self._consumed_nonces:
                raise NonceAuthenticationError(f"Nonce ya consumido (posible ataque de replay): '{resp_nonce}'")
            if resp_nonce != nonce:
                raise NonceAuthenticationError(f"Nonce mismatch: esperado '{nonce}', recibido '{resp_nonce}'")
        finally:
            self._mark_nonce_consumed(nonce)

        if resp.get("operation_id") != operation_id:
            raise ProtocolAbuseError("operation_id mismatch en la respuesta")
        if resp.get("mode") != VerifierMode.VERIFY:
            raise ProtocolAbuseError(f"mode mismatch: esperado VERIFY, recibido {resp.get('mode')}")

        child_disposition = VerifierDisposition(resp["disposition"])
        if child_disposition == VerifierDisposition.REJECTED:
            return OperatorVerifierVerificationResult(
                disposition=child_disposition,
                operation_id=operation_id,
                nonce=nonce,
                physical_root=None,
                tree_result=TreeVerificationResult(
                    state=VerificationState.FAILED,
                    expected=expected_tree,
                    message=f"{resp.get('error_type', 'Error')}: {resp.get('message', '')}",
                ),
                runtime_result=RuntimeVerificationResult(
                    state=VerificationState.FAILED,
                    expected=target_expected_runtime,
                    message=f"{resp.get('error_type', 'Error')}: {resp.get('message', '')}",
                ),
                observed_runtime=None,
                critical_evidences=(),
                message=f"{resp.get('error_type', 'Error')}: {resp.get('message', '')}",
            )

        phys_id = PhysicalRootIdentity(
            canonical_root=resp["canonical_root"],
            volume_serial_number=resp["volume_serial_number"],
            root_file_id=resp["root_file_id"],
        )

        obs_tree = TreeDigest(
            digest=resp["observed_tree"]["digest"],
            files=resp["observed_tree"]["files"],
            bytes=resp["observed_tree"]["bytes"],
        )
        obs_runtime_dto = FreshRuntimeObservation(
            game_key=resp["observed_runtime"]["game_key"],
            game_version=resp["observed_runtime"]["game_version"],
            observed_exe_path=resp["observed_runtime"]["observed_exe_path"],
            observed_at_ns=resp["observed_runtime"]["observed_at_ns"],
        )
        obs_runtime = obs_runtime_dto.runtime_identity

        tree_ver_state = VerificationState.VERIFIED if obs_tree == expected_tree else VerificationState.FAILED
        tree_res = TreeVerificationResult(
            state=tree_ver_state,
            expected=expected_tree,
            observed=obs_tree,
        )

        runtime_ver_state = (
            VerificationState.VERIFIED if obs_runtime == target_expected_runtime else VerificationState.FAILED
        )
        runtime_res = RuntimeVerificationResult(
            state=runtime_ver_state,
            expected=target_expected_runtime,
            observed=obs_runtime,
        )

        crit_evs: list[CriticalFileEvidence] = []
        for c in resp.get("critical_evidences", []):
            crit_evs.append(
                CriticalFileEvidence(
                    rel_path=c["rel_path"],
                    state=VerificationState(c["state"]),
                    observed_digest=c.get("observed_digest"),
                    expected_digest=c.get("expected_digest"),
                    observed_size=c.get("observed_size"),
                    expected_size=c.get("expected_size"),
                    message=c.get("message", ""),
                )
            )

        # El elevated helper deriva independientemente el veredicto VERIFIED final
        physical_match = (
            phys_id.canonical_root.lower() == expected_physical_root.canonical_root.lower()
            and phys_id.volume_serial_number == expected_physical_root.volume_serial_number
            and phys_id.root_file_id == expected_physical_root.root_file_id
        )
        tree_match = obs_tree == expected_tree
        runtime_match = obs_runtime == target_expected_runtime
        crit_by_path = {ev.rel_path.lower(): ev for ev in crit_evs}
        critical_match = len(critical_expectations) == len(crit_evs) and all(
            exp.rel_path.lower() in crit_by_path
            and crit_by_path[exp.rel_path.lower()].state == VerificationState.VERIFIED
            for exp in critical_expectations
        )

        helper_verified = physical_match and tree_match and runtime_match and critical_match

        # Invariante: si el child reportó VERIFIED pero el helper determinó que no cumple, fail-closed
        if child_disposition == VerifierDisposition.VERIFIED and not helper_verified:
            raise ProtocolAbuseError(
                f"Inconsistencia en verificador hijo: child reportó VERIFIED pero la verificación "
                f"independiente del helper falló (physical={physical_match}, tree={tree_match}, "
                f"runtime={runtime_match}, critical={critical_match})"
            )

        final_disposition = VerifierDisposition.VERIFIED if helper_verified else VerifierDisposition.FAILED

        return OperatorVerifierVerificationResult(
            disposition=final_disposition,
            operation_id=operation_id,
            nonce=nonce,
            physical_root=phys_id,
            tree_result=tree_res,
            runtime_result=runtime_res,
            observed_runtime=obs_runtime_dto,
            critical_evidences=tuple(crit_evs),
            message=resp.get("message", ""),
        )
