"""Observación determinista y fresca del runtime del juego (FreshRuntimeObservation).

Contrato normativo ADR 0010 §11.4:
- OBSERVATION != AUTHORITY: La observación sólo mide el estado del disco;
  no autoriza ni emite expectativas.
- FRESH RUNTIME OBSERVATION: Cada invocación (tanto en OBSERVE como en VERIFY)
  debe medir el runtime de forma fresca e independiente desde el root físico.
- No reutiliza valores previos ni acepta `observed_runtime` inyectado por el caller
  o desde staging (R01).
- Es imposible copiar `expected_runtime` como `observed_runtime` (R02).
- Cualquier cambio en el ejecutable entre pasadas es detectado en la remediación (R03).
- Si el runtime no puede medirse (ejecutable ausente o versión ilegible),
  falla cerrado con error tipado y nunca emite VERIFIED (R04).
- Anti-ambigüedad: Determina el ejecutable esperado según ``expected_game_key``.
  Si coexisten múltiples ejecutables de runtime en la raíz, falla cerrado con
  :class:`AmbiguousRuntimeError` en lugar de priorizar oportunísticamente.
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass

from sky_claw.local.discovery.scanner import read_skyrim_version
from sky_claw.local.runtime_vault.models import RuntimeIdentity, RuntimeVaultError

# ============================================================================
# Excepciones Tipadas
# ============================================================================


class RuntimeObservationError(RuntimeVaultError):
    """Base de errores de observación de runtime."""


class ExecutableNotFoundError(RuntimeObservationError):
    """El ejecutable de runtime esperado no existe en el root especificado."""


class UnreadableRuntimeVersionError(RuntimeObservationError):
    """No se pudo leer la versión PE del ejecutable de runtime."""


class AmbiguousRuntimeError(RuntimeObservationError):
    """Coexisten múltiples ejecutables de runtime incompatibles en el root."""


# ============================================================================
# Modelos Inmutables
# ============================================================================


@dataclass(frozen=True, slots=True)
class FreshRuntimeObservation:
    """Observación fresca e inmutable del runtime obtenida directamente del disco."""

    game_key: str
    game_version: str
    observed_exe_path: str
    observed_at_ns: int

    def __post_init__(self) -> None:
        if not self.game_key or not isinstance(self.game_key, str):
            raise RuntimeObservationError("game_key no puede ser vacío")
        if not self.game_version or not isinstance(self.game_version, str):
            raise RuntimeObservationError("game_version no puede ser vacío")
        if not self.observed_exe_path or not isinstance(self.observed_exe_path, str):
            raise RuntimeObservationError("observed_exe_path no puede ser vacío")
        if not isinstance(self.observed_at_ns, int) or self.observed_at_ns <= 0:
            raise RuntimeObservationError("observed_at_ns debe ser un timestamp positivo en nanosegundos")

    @property
    def runtime_identity(self) -> RuntimeIdentity:
        """Convierte la observación en el DTO canónico RuntimeIdentity."""
        return RuntimeIdentity(game_key=self.game_key, game_version=self.game_version)


# ============================================================================
# Catálogo Normativo de Ejecutables
# ============================================================================

_KNOWN_RUNTIME_EXECUTABLES: dict[str, str] = {
    "skyrimse": "SkyrimSE.exe",
    "skyrim": "Skyrim.exe",
    "skyrimvr": "SkyrimVR.exe",
}

_ALL_CANDIDATE_NAMES_LOWER: frozenset[str] = frozenset(name.lower() for name in _KNOWN_RUNTIME_EXECUTABLES.values())


# ============================================================================
# Función de Medición Fresca
# ============================================================================


def observe_runtime_identity_from_root(
    root: pathlib.Path | str,
    *,
    expected_game_key: str = "skyrimse",
) -> FreshRuntimeObservation:
    """Mide directamente el runtime del juego inspeccionando el ejecutable en *root*.

    Args:
        root: Raíz física donde reside la instalación del juego.
        expected_game_key: Clave canónica del juego esperada (ej. ``"skyrimse"``).

    Returns:
        :class:`FreshRuntimeObservation` con versión y timestamp fresco.

    Raises:
        RuntimeObservationError: Si ``expected_game_key`` no es reconocido.
        AmbiguousRuntimeError: Si coexisten múltiples ejecutables de runtime en el root.
        ExecutableNotFoundError: Si el ejecutable esperado no existe.
        UnreadableRuntimeVersionError: Si el recurso de versión PE no se pudo leer.
    """
    if expected_game_key not in _KNOWN_RUNTIME_EXECUTABLES:
        raise RuntimeObservationError(
            f"game_key no reconocido: '{expected_game_key}'. Soportados: {sorted(_KNOWN_RUNTIME_EXECUTABLES)}"
        )

    expected_exe_name = _KNOWN_RUNTIME_EXECUTABLES[expected_game_key]
    expected_exe_lower = expected_exe_name.lower()

    root_path = pathlib.Path(os.path.abspath(os.fspath(root)))
    if not root_path.is_dir():
        raise ExecutableNotFoundError(f"La ruta '{root_path}' no existe o no es un directorio")

    # Escanear el root para detectar ejecutables de runtime (sin recursión profunda)
    found_candidates: list[pathlib.Path] = []
    try:
        for entry in os.scandir(root_path):
            if entry.is_file():
                name_lower = entry.name.lower()
                if name_lower in _ALL_CANDIDATE_NAMES_LOWER:
                    found_candidates.append(pathlib.Path(entry.path))
    except OSError as exc:
        raise RuntimeObservationError(f"No se pudo escanear el directorio '{root_path}': {exc}") from exc

    # Regla anti-ambigüedad: si hay más de un ejecutable conocido en la raíz -> fail-closed
    if len(found_candidates) > 1:
        nombres = [p.name for p in found_candidates]
        raise AmbiguousRuntimeError(
            f"Coexisten múltiples ejecutables de runtime en '{root_path}': {sorted(nombres)}. "
            "No se puede resolver el runtime de forma determinista (fail-closed)"
        )

    target_exe: pathlib.Path | None = None
    for cand in found_candidates:
        if cand.name.lower() == expected_exe_lower:
            target_exe = cand
            break

    if target_exe is None:
        raise ExecutableNotFoundError(
            f"No se encontró el ejecutable '{expected_exe_name}' esperado para game_key '{expected_game_key}' "
            f"en '{root_path}'"
        )

    # Medir la versión del PE directamente
    raw_version = read_skyrim_version(target_exe)
    if not raw_version or not raw_version.strip():
        raise UnreadableRuntimeVersionError(
            f"No se pudo leer la versión de ProductVersion PE del ejecutable '{target_exe}'"
        )

    observed_at = time.time_ns()
    return FreshRuntimeObservation(
        game_key=expected_game_key,
        game_version=raw_version.strip(),
        observed_exe_path=str(target_exe),
        observed_at_ns=observed_at,
    )
