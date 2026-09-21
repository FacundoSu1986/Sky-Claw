"""Primitive nativa de inspección de quiescencia para Runtime Vault.

SECURITY RULE (QUIESCENCE PROBE CONTRACT - ADR 0010 §4.1 / §4.1.3 / §12.2):
- Detección determinista de writers y file mappings escribibles preexistentes
  mediante un probe handle transitorio con dwShareMode = 0 (exclusivo).
- Apertura atada a handle con FILE_FLAG_BACKUP_SEMANTICS y FILE_FLAG_OPEN_REPARSE_POINT.
- Cierre inmediato del handle tras la verificación positiva (en bloque finally).
- Política de reintentos acotada: máximo 5 intentos totales ante ERROR_SHARING_VIOLATION
  con backoff exponencial y jitter inyectable.
- Cero reintentos silenciosos ante cualquier error distinto de ERROR_SHARING_VIOLATION.
- Cero mutaciones sobre filesystem o ACLs.
- Fail-closed tipado en entornos no-Windows (QuiescenceUnsupportedError) sin NameError
  ni fallos al importar.
- FASE NORMATIVA: esta primitiva está reservada para el helper privilegiado de aplicación
  (ADR 0010 §4.1 / §12.2), donde los derechos write/delete están garantizados bajo elevación.
  No se invoca durante la planificación no elevada (GP2-S2) para evitar falsos ACCESS_DENIED
  sobre árboles legítimamente WRITE_PROTECTED.
"""

from __future__ import annotations

import os
import pathlib
import random
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from sky_claw.local.runtime_vault.golden_protection_plan import GoldenProtectionNodeKind
from sky_claw.local.runtime_vault.models import RuntimeVaultError

# ============================================================================
# Constantes Normativas Win32 (ADR 0010 §4.1)
# ============================================================================

# Máscaras de acceso solicitadas por el probe:
# Archivos regulares: FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | DELETE
PROBE_FILE_DESIRED_ACCESS: int = (
    0x0002  # FILE_WRITE_DATA
    | 0x0004  # FILE_APPEND_DATA
    | 0x0100  # FILE_WRITE_ATTRIBUTES
    | 0x0010  # FILE_WRITE_EA
    | 0x00010000  # DELETE
)  # 0x00010116

# Directorios: PROBE_FILE_DESIRED_ACCESS | FILE_DELETE_CHILD
# (FILE_ADD_FILE es 0x0002 y FILE_ADD_SUBDIRECTORY es 0x0004, ya presentes en PROBE_FILE_DESIRED_ACCESS)
PROBE_DIR_DESIRED_ACCESS: int = PROBE_FILE_DESIRED_ACCESS | 0x0040  # FILE_DELETE_CHILD  # 0x00010156

PROBE_SHARE_MODE: int = 0  # CERO: exclusión absoluta bidireccional contra handles de datos
PROBE_CREATION_DISPOSITION: int = 3  # OPEN_EXISTING
PROBE_FLAGS: int = 0x02000000 | 0x00200000  # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT

ERROR_SHARING_VIOLATION: int = 32
MAX_PROBE_RETRIES: int = 5  # Exactamente 5 intentos totales (intento 0 a 4)
DEFAULT_BASE_BACKOFF_SECONDS: float = 0.05

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class QuiescenceError(RuntimeVaultError):
    """Base para errores durante la inspección de quiescencia."""


class QuiescenceUnsupportedError(QuiescenceError):
    """Plataforma no soportada (no Windows) para quiescencia nativa."""


class QuiescenceViolationError(QuiescenceError):
    """Conflicto de concurrencia (ERROR_SHARING_VIOLATION) tras agotar los intentos totales."""

    def __init__(self, message: str, *, blocked_paths: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.blocked_paths: tuple[str, ...] = tuple(blocked_paths)


# ============================================================================
# Configuración Win32 (Solo Windows)
# ============================================================================

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
else:
    _kernel32 = None  # type: ignore[assignment]
    _INVALID_HANDLE_VALUE = -1


# ============================================================================
# Funciones Públicas
# ============================================================================


def default_quiescence_jitter(delay: float) -> float:
    """Añade hasta un 10% de jitter aleatorio no determinista al retraso base."""
    return delay + random.uniform(0.0, delay * 0.1)


def probe_node_quiescence(
    path: str | os.PathLike[str],
    node_kind: GoldenProtectionNodeKind,
    *,
    max_attempts: int = MAX_PROBE_RETRIES,
    base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
    jitter_fn: Callable[[float], float] = default_quiescence_jitter,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Abre un probe handle transitorio exclusivo (dwShareMode=0) sobre un nodo.

    El probe se cierra inmediatamente tras la apertura exitosa.
    Si se detecta ERROR_SHARING_VIOLATION, reintenta con backoff exponencial
    y jitter hasta exactamente ``max_attempts`` totales.

    Cualquier error distinto de ERROR_SHARING_VIOLATION falla de inmediato (fail closed).
    """
    if sys.platform != "win32":
        raise QuiescenceUnsupportedError("Plataforma no soportada: quiescence probe requiere Windows")

    if max_attempts <= 0:
        raise ValueError("max_attempts debe ser al menos 1")

    str_path = os.path.abspath(os.fspath(path))
    desired_access = (
        PROBE_DIR_DESIRED_ACCESS if node_kind is GoldenProtectionNodeKind.DIR else PROBE_FILE_DESIRED_ACCESS
    )

    last_err: int = 0

    for attempt in range(max_attempts):
        h = _kernel32.CreateFileW(
            str_path,
            desired_access,
            PROBE_SHARE_MODE,
            None,
            PROBE_CREATION_DISPOSITION,
            PROBE_FLAGS,
            None,
        )

        if h != _INVALID_HANDLE_VALUE and h != -1 and h != 0:
            try:
                pass
            finally:
                _kernel32.CloseHandle(h)
            return

        last_err = ctypes.get_last_error()
        if last_err == ERROR_SHARING_VIOLATION:
            if attempt < max_attempts - 1:
                delay = base_backoff_seconds * (2**attempt)
                actual_delay = jitter_fn(delay)
                sleeper(actual_delay)
                continue
            raise QuiescenceViolationError(
                f"Conflicto de quiescencia en '{str_path}': "
                f"ERROR_SHARING_VIOLATION persistente tras agotar {max_attempts} intentos",
                blocked_paths=(str_path,),
            )

        raise QuiescenceError(f"CreateFileW falló durante el probe de quiescencia en '{str_path}': código {last_err}")


def probe_tree_quiescence(
    root: str | os.PathLike[str],
    nodes: Sequence[Any],
    *,
    max_attempts: int = MAX_PROBE_RETRIES,
    base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
    jitter_fn: Callable[[float], float] = default_quiescence_jitter,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Ejecuta el probe de quiescencia sobre cada nodo del árbol secuencialmente.

    Cierra cada probe handle inmediatamente al verificar cada nodo.
    Si múltiples nodos presentan ERROR_SHARING_VIOLATION persistente, acumula
    todas las rutas bloqueadas y lanza un único QuiescenceViolationError estructurado
    al finalizar el recorrido del árbol completo.
    """
    if sys.platform != "win32":
        raise QuiescenceUnsupportedError("Plataforma no soportada: quiescence probe requiere Windows")

    root_path = pathlib.Path(os.path.abspath(os.fspath(root)))
    blocked_nodes: list[str] = []

    for node in nodes:
        # Acepta NativeNodeEvidence o NodeSecurityBackup
        backup = getattr(node, "backup", node)
        rel = backup.relative_path
        node_path = root_path if rel in ("", ".") else root_path / rel
        try:
            probe_node_quiescence(
                node_path,
                backup.node_kind,
                max_attempts=max_attempts,
                base_backoff_seconds=base_backoff_seconds,
                jitter_fn=jitter_fn,
                sleeper=sleeper,
            )
        except QuiescenceViolationError:
            blocked_nodes.append(rel)

    if blocked_nodes:
        nodos_str = ", ".join(f"'{p}'" for p in blocked_nodes)
        raise QuiescenceViolationError(
            f"Conflicto de quiescencia: {len(blocked_nodes)} nodo(s) con ERROR_SHARING_VIOLATION "
            f"persistente tras {max_attempts} intentos: {nodos_str}",
            blocked_paths=tuple(blocked_nodes),
        )


__all__ = [
    "DEFAULT_BASE_BACKOFF_SECONDS",
    "ERROR_SHARING_VIOLATION",
    "MAX_PROBE_RETRIES",
    "PROBE_CREATION_DISPOSITION",
    "PROBE_DIR_DESIRED_ACCESS",
    "PROBE_FILE_DESIRED_ACCESS",
    "PROBE_FLAGS",
    "PROBE_SHARE_MODE",
    "QuiescenceError",
    "QuiescenceUnsupportedError",
    "QuiescenceViolationError",
    "default_quiescence_jitter",
    "probe_node_quiescence",
    "probe_tree_quiescence",
]
