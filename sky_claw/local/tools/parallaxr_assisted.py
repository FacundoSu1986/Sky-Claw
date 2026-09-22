"""Modo asistido externo para ParallaxR (PR-A0).

Este módulo provee descubrimiento acotado, validación de contención de rutas,
fingerprinting de evidencia inmutable y construcción de handoffs manuales para
el entrypoint oficial ParallaxR.BAT gestionado por el usuario en MO2.

Contrato arquitectónico:
- B6-L permanece ABIERTO para DIRECT_HELPER_API. Este modo evita esa interfaz por completo.
- MaterialStepId.PARALLAXR permanece BLOCKED en el contrato de pipeline material.
- EXTERNAL_TOOL_REGISTRY no incluye ParallaxR en este corte.
- Read-only y side-effect-free respecto del filesystem: 0 mutaciones en archivos o MO2.
- Determinista para un snapshot de filesystem estable.
- Cero subprocesos, cero ejecución de código externo y cero acceso a red.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

OFFICIAL_ENTRYPOINT_NAME: Final[str] = "ParallaxR.BAT"
OUTPUT_MARKER_NAME: Final[str] = "ParallaxROutput.tmp"
RECOMMENDED_LAUNCHER: Final[str] = "MO2"
MANUAL_MODE: Final[str] = "manual_official_entrypoint"
DEFAULT_INSTRUCTION: Final[str] = "Ejecutá el entrypoint oficial de ParallaxR desde MO2."
_HASH_CHUNK_SIZE: Final[int] = 65536
_FINGERPRINT_MAX_ATTEMPTS: Final[int] = 2


class ParallaxRAssistedStatus(StrEnum):
    """Estado resultante del preflight asistido de ParallaxR."""

    READY = "ready"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INVALID_SELECTION = "invalid_selection"
    EVIDENCE_UNSTABLE = "evidence_unstable"


class ParallaxREvidenceUnstableError(RuntimeError):
    """Error cuando un archivo cambia durante la captura de evidencia."""


@dataclass(frozen=True, slots=True)
class _FileFingerprintSnapshot:
    """Snapshot coherente obtenido de una lectura con descriptor único."""

    size_bytes: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ParallaxRInstallationEvidence:
    """Evidencia inmutable capturada de la instalación oficial de ParallaxR."""

    mod_root: Path
    entrypoint: Path
    size_bytes: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ParallaxROutputMarkerEvidence:
    """Evidencia inmutable de un marcador de salida previo detectado."""

    path: Path
    size_bytes: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ParallaxRAssistedPreflight:
    """Resultado del preflight asistido de ParallaxR."""

    status: ParallaxRAssistedStatus
    success: bool
    installation: ParallaxRInstallationEvidence | None = None
    existing_output_candidates: tuple[ParallaxROutputMarkerEvidence, ...] = ()
    candidate_mod_roots: tuple[Path, ...] = ()
    message: str = ""


@dataclass(frozen=True, slots=True)
class ParallaxRManualHandoff:
    """Datos declarativos inmutables para el handoff manual del usuario."""

    mode: str
    entrypoint: Path
    mod_root: Path
    fingerprint: ParallaxRInstallationEvidence
    requires_user_launch: bool = True
    recommended_launcher: str = RECOMMENDED_LAUNCHER
    direct_helper_invocation: bool = False
    sky_claw_launches_process: bool = False
    instruction: str = DEFAULT_INSTRUCTION
    success: bool = True
    message: str = ""


def validate_path_containment(child_path: Path, parent_dir: Path) -> bool:
    """Valida que una ruta resuelta permanezca físicamente contenida en el directorio padre."""
    try:
        resolved_parent = parent_dir.resolve()
        resolved_child = child_path.resolve()
        return resolved_child.is_relative_to(resolved_parent)
    except (ValueError, OSError):
        return False


def _fingerprint_file_consistently(
    file_path: Path, max_attempts: int = _FINGERPRINT_MAX_ATTEMPTS
) -> _FileFingerprintSnapshot:
    """Calcula el fingerprint de un archivo usando un único descriptor abierto por intento.

    Garantiza coherencia fotográfica entre metadatos y hash durante la captura (anti-TOCTOU).
    Si el archivo es mutado concurrentemente, reintenta hasta max_attempts veces.
    Si la inestabilidad persiste, lanza ParallaxREvidenceUnstableError.
    """
    for _ in range(max_attempts):
        with file_path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            hasher = hashlib.sha256()
            while chunk := handle.read(_HASH_CHUNK_SIZE):
                hasher.update(chunk)
            after = os.fstat(handle.fileno())

            # Validar que el descriptor no haya cambiado mientras se leía
            descriptor_stable = (
                before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
                and before.st_ino == after.st_ino
                and before.st_dev == after.st_dev
            )
            if not descriptor_stable:
                continue

            # Validar que la ruta siga resolviendo al mismo archivo físico
            try:
                path_stat = file_path.stat()
            except OSError:
                continue

            path_matches = (
                path_stat.st_dev == after.st_dev
                and path_stat.st_ino == after.st_ino
                and path_stat.st_size == after.st_size
                and path_stat.st_mtime_ns == after.st_mtime_ns
            )
            if not path_matches:
                continue

            return _FileFingerprintSnapshot(
                size_bytes=after.st_size,
                mtime_ns=after.st_mtime_ns,
                sha256=hasher.hexdigest(),
            )

    raise ParallaxREvidenceUnstableError(
        f"El archivo '{file_path}' cambió durante la captura de evidencia tras {max_attempts} intentos."
    )


def discover_parallaxr_candidates(mods_dir: Path) -> tuple[Path, ...]:
    """Descubre candidatos de entrypoint oficial exclusivamente en hijos directos de mods_dir.

    Operación read-only, side-effect-free respecto del filesystem y acotada.
    """
    if not mods_dir.exists() or not mods_dir.is_dir():
        return ()

    candidates: list[Path] = []
    try:
        for child in sorted(mods_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            entrypoint_candidate = child / OFFICIAL_ENTRYPOINT_NAME
            if (
                entrypoint_candidate.is_file()
                and validate_path_containment(entrypoint_candidate, mods_dir)
                and validate_path_containment(child, mods_dir)
            ):
                candidates.append(entrypoint_candidate.resolve())
    except OSError:
        return ()

    return tuple(candidates)


def discover_output_markers(mods_dir: Path) -> tuple[ParallaxROutputMarkerEvidence, ...]:
    """Descubre marcadores de salida preexistentes en hijos directos de mods_dir.

    Operación read-only y side-effect-free respecto del filesystem. No prejuzga frescura.
    Si un marcador está en mutación continua, eleva ParallaxREvidenceUnstableError.
    """
    if not mods_dir.exists() or not mods_dir.is_dir():
        return ()

    markers: list[ParallaxROutputMarkerEvidence] = []
    try:
        for child in sorted(mods_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            marker_candidate = child / OUTPUT_MARKER_NAME
            if marker_candidate.is_file() and validate_path_containment(marker_candidate, mods_dir):
                resolved_marker = marker_candidate.resolve()
                snapshot = _fingerprint_file_consistently(resolved_marker)
                markers.append(
                    ParallaxROutputMarkerEvidence(
                        path=resolved_marker,
                        size_bytes=snapshot.size_bytes,
                        mtime_ns=snapshot.mtime_ns,
                        sha256=snapshot.sha256,
                    )
                )
    except OSError:
        return ()

    return tuple(markers)


def run_parallaxr_assisted_preflight(
    mods_dir: Path, explicit_selection: Path | None = None
) -> ParallaxRAssistedPreflight:
    """Ejecuta el preflight asistido de ParallaxR de forma determinista y side-effect-free.

    Retorna evidencia de instalación, candidatos descubiertos y marcadores de salida existentes.
    """
    if not mods_dir.exists() or not mods_dir.is_dir():
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.MISSING,
            success=False,
            message=f"El directorio de mods '{mods_dir}' no existe o no es accesible.",
        )

    candidates = discover_parallaxr_candidates(mods_dir)
    candidate_mod_roots = tuple(c.parent for c in candidates)

    try:
        existing_output = discover_output_markers(mods_dir)
    except ParallaxREvidenceUnstableError:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.EVIDENCE_UNSTABLE,
            success=False,
            candidate_mod_roots=candidate_mod_roots,
            message="Los marcadores de salida de ParallaxR cambiaron durante la captura de evidencia; reintente cuando la herramienta no esté activa.",
        )

    if not candidates:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.MISSING,
            success=False,
            existing_output_candidates=existing_output,
            candidate_mod_roots=(),
            message="No se encontró ninguna instalación de ParallaxR bajo los mods de MO2.",
        )

    selected_entrypoint: Path | None = None

    if explicit_selection is not None:
        try:
            resolved_selection = explicit_selection.resolve()
        except OSError:
            return ParallaxRAssistedPreflight(
                status=ParallaxRAssistedStatus.INVALID_SELECTION,
                success=False,
                existing_output_candidates=existing_output,
                candidate_mod_roots=candidate_mod_roots,
                message="La ruta de selección explícita no es válida.",
            )

        # La selección debe coincidir exactamente con un entrypoint descubierto o su mod_root
        for cand in candidates:
            if resolved_selection in (cand, cand.parent):
                selected_entrypoint = cand
                break

        if selected_entrypoint is None:
            return ParallaxRAssistedPreflight(
                status=ParallaxRAssistedStatus.INVALID_SELECTION,
                success=False,
                existing_output_candidates=existing_output,
                candidate_mod_roots=candidate_mod_roots,
                message="La selección explícita no coincide con ningún candidato descubierto válido.",
            )
    else:
        if len(candidates) == 1:
            selected_entrypoint = candidates[0]
        else:
            return ParallaxRAssistedPreflight(
                status=ParallaxRAssistedStatus.AMBIGUOUS,
                success=False,
                existing_output_candidates=existing_output,
                candidate_mod_roots=candidate_mod_roots,
                message=(
                    f"Se detectaron múltiples ({len(candidates)}) candidatos de ParallaxR. "
                    "Se requiere selección explícita."
                ),
            )

    try:
        snapshot = _fingerprint_file_consistently(selected_entrypoint)
    except ParallaxREvidenceUnstableError:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.EVIDENCE_UNSTABLE,
            success=False,
            existing_output_candidates=existing_output,
            candidate_mod_roots=candidate_mod_roots,
            message="Los archivos de ParallaxR cambiaron durante la captura de evidencia; reintente cuando la herramienta no esté siendo modificada.",
        )
    except OSError as err:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.MISSING,
            success=False,
            existing_output_candidates=existing_output,
            candidate_mod_roots=candidate_mod_roots,
            message=f"Error accediendo al entrypoint seleccionado: {err}",
        )

    evidence = ParallaxRInstallationEvidence(
        mod_root=selected_entrypoint.parent,
        entrypoint=selected_entrypoint,
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns,
        sha256=snapshot.sha256,
    )

    return ParallaxRAssistedPreflight(
        status=ParallaxRAssistedStatus.READY,
        success=True,
        installation=evidence,
        existing_output_candidates=existing_output,
        candidate_mod_roots=candidate_mod_roots,
        message="",
    )


def prepare_parallaxr_manual_handoff(
    preflight: ParallaxRAssistedPreflight,
) -> ParallaxRManualHandoff:
    """Construye una estructura de handoff manual inmutable a partir de un preflight READY.

    Transformación pura: falla cerrado si preflight no está en estado READY o success no es True.
    """
    if preflight.status != ParallaxRAssistedStatus.READY or not preflight.success or preflight.installation is None:
        raise ValueError(
            f"No se puede preparar handoff: preflight no está en estado READY "
            f"(status={preflight.status.value}, success={preflight.success})."
        )

    return ParallaxRManualHandoff(
        mode=MANUAL_MODE,
        entrypoint=preflight.installation.entrypoint,
        mod_root=preflight.installation.mod_root,
        fingerprint=preflight.installation,
        requires_user_launch=True,
        recommended_launcher=RECOMMENDED_LAUNCHER,
        direct_helper_invocation=False,
        sky_claw_launches_process=False,
        instruction=DEFAULT_INSTRUCTION,
        success=True,
        message="",
    )
