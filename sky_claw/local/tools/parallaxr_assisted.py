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

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeVar

OFFICIAL_ENTRYPOINT_NAME: Final[str] = "ParallaxR.BAT"
OUTPUT_MARKER_NAME: Final[str] = "ParallaxROutput.tmp"
RECOMMENDED_LAUNCHER: Final[str] = "MO2"
MANUAL_MODE: Final[str] = "manual_official_entrypoint"
DEFAULT_INSTRUCTION: Final[str] = "Ejecutá el entrypoint oficial de ParallaxR desde MO2."
_HASH_CHUNK_SIZE: Final[int] = 65536
_FINGERPRINT_MAX_ATTEMPTS: Final[int] = 2
_WINERROR_INSPECTION_FAILURES: Final[frozenset[int]] = frozenset({21, 123, 1921})
_ABSENCE_ERRNOS: Final[frozenset[int]] = frozenset({errno.ENOENT, errno.ENOTDIR})
_ItemT = TypeVar("_ItemT")


class ParallaxRAssistedStatus(StrEnum):
    """Estado resultante del preflight asistido de ParallaxR."""

    READY = "ready"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    INVALID_SELECTION = "invalid_selection"
    EVIDENCE_UNSTABLE = "evidence_unstable"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"


class ParallaxREvidenceUnstableError(RuntimeError):
    """Error cuando un archivo cambia durante la captura de evidencia."""


class ParallaxREvidenceIncompleteError(RuntimeError):
    """Error cuando una inspección de filesystem queda incompleta y no permite concluir ausencia ni unicidad."""

    def __init__(
        self,
        message: str,
        *,
        partial_candidates: tuple[Path, ...] = (),
        partial_markers: tuple[ParallaxROutputMarkerEvidence, ...] = (),
    ) -> None:
        super().__init__(message)
        self.partial_candidates: tuple[Path, ...] = partial_candidates
        self.partial_markers: tuple[ParallaxROutputMarkerEvidence, ...] = partial_markers


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


def _resolve_path(path: Path, *, strict: bool = False) -> Path:
    """Resuelve una ruta acotando la captura de RuntimeError a bucles de symlinks/junctions."""
    try:
        return path.resolve(strict=strict)
    except RuntimeError as err:
        # En Python 3.11 Windows, Path.resolve() convierte ELOOP/WinError 1921 en RuntimeError("Symlink loop from ...").
        if "symlink loop" in str(err).lower():
            raise OSError(errno.ELOOP, "Bucle detectado al resolver la ruta.") from err
        raise


def _stat_mode_fail_closed(path: Path) -> int | None:
    """Obtiene st_mode distinguiendo ausencia real de errores de inspección Windows/POSIX."""
    try:
        return path.stat().st_mode
    except OSError as stat_err:
        if getattr(stat_err, "winerror", None) in _WINERROR_INSPECTION_FAILURES:
            raise
        if stat_err.errno not in _ABSENCE_ERRNOS:
            raise
        try:
            # Un symlink o junction roto falla stat() con ENOENT, pero lstat() confirma que la entrada existe.
            path.lstat()
        except OSError as lstat_err:
            if getattr(lstat_err, "winerror", None) in _WINERROR_INSPECTION_FAILURES:
                raise lstat_err from stat_err
            if lstat_err.errno not in _ABSENCE_ERRNOS:
                raise lstat_err from stat_err
            return None
        raise


def _is_accessible_dir(path: Path) -> bool:
    """Indica si la ruta es un directorio accesible; propaga OSError cuando no puede verificarse."""
    mode = _stat_mode_fail_closed(path)
    return mode is not None and stat.S_ISDIR(mode)


def _is_accessible_regular_file(path: Path) -> bool:
    """Indica si la ruta es un archivo regular accesible; propaga OSError cuando no puede verificarse."""
    mode = _stat_mode_fail_closed(path)
    return mode is not None and stat.S_ISREG(mode)


def _resolve_if_contained(
    child_path: Path,
    parent_dir: Path,
    *,
    strict: bool = False,
    resolved_parent: Path | None = None,
) -> Path | None:
    """Resuelve child_path si permanece contenido en parent_dir; propaga OSError de resolución."""
    base_parent = resolved_parent if resolved_parent is not None else _resolve_path(parent_dir, strict=strict)
    resolved_child = _resolve_path(child_path, strict=strict)
    try:
        if resolved_child.is_relative_to(base_parent):
            return resolved_child
    except ValueError:
        return None
    return None


def validate_path_containment(child_path: Path, parent_dir: Path) -> bool:
    """Valida que una ruta resuelta permanezca físicamente contenida en el directorio padre."""
    try:
        return _resolve_if_contained(child_path, parent_dir, strict=False) is not None
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


def _discover_direct_mod_entries(
    mods_dir: Path,
    target_name: str,
    build_item: Callable[[Path], _ItemT],
) -> tuple[tuple[_ItemT, ...], bool]:
    """Enumera entradas directas de mods_dir sin convertir errores de E/S en ausencia."""
    items: list[_ItemT] = []
    try:
        if not _is_accessible_dir(mods_dir):
            return (), True
        resolved_mods_dir = _resolve_path(mods_dir, strict=True)
        for child in sorted(mods_dir.iterdir(), key=lambda p: p.name):
            if not _is_accessible_dir(child):
                continue
            resolved_child = _resolve_if_contained(
                child,
                mods_dir,
                strict=True,
                resolved_parent=resolved_mods_dir,
            )
            if resolved_child is None:
                continue
            target_candidate = child / target_name
            if not _is_accessible_regular_file(target_candidate):
                continue
            resolved_target = _resolve_if_contained(
                target_candidate,
                mods_dir,
                strict=True,
                resolved_parent=resolved_mods_dir,
            )
            if resolved_target is None:
                continue
            items.append(build_item(resolved_target))
    except OSError:
        return tuple(items), False

    return tuple(items), True


def discover_parallaxr_candidates(mods_dir: Path) -> tuple[Path, ...]:
    """Descubre candidatos de entrypoint oficial exclusivamente en hijos directos de mods_dir.

    Operación read-only, side-effect-free respecto del filesystem y acotada.
    Si ocurre un error de E/S durante la inspección, eleva ParallaxREvidenceIncompleteError
    preservando los candidatos válidos ya observados.
    """
    candidates, complete = _discover_direct_mod_entries(
        mods_dir,
        OFFICIAL_ENTRYPOINT_NAME,
        lambda resolved_entrypoint: resolved_entrypoint,
    )
    if not complete:
        raise ParallaxREvidenceIncompleteError(
            "Inspección incompleta del directorio de mods al descubrir candidatos de ParallaxR.",
            partial_candidates=candidates,
        )
    return candidates


def _build_output_marker_evidence(resolved_marker: Path) -> ParallaxROutputMarkerEvidence:
    """Construye la evidencia inmutable de un marcador de salida con snapshot anti-TOCTOU."""
    snapshot = _fingerprint_file_consistently(resolved_marker)
    return ParallaxROutputMarkerEvidence(
        path=resolved_marker,
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns,
        sha256=snapshot.sha256,
    )


def discover_output_markers(mods_dir: Path) -> tuple[ParallaxROutputMarkerEvidence, ...]:
    """Descubre marcadores de salida preexistentes en hijos directos de mods_dir.

    Operación read-only y side-effect-free respecto del filesystem. No prejuzga frescura.
    Si un marcador está en mutación continua, eleva ParallaxREvidenceUnstableError.
    Si ocurre un error de E/S durante la inspección, eleva ParallaxREvidenceIncompleteError
    preservando los marcadores válidos ya observados.
    """
    markers, complete = _discover_direct_mod_entries(
        mods_dir,
        OUTPUT_MARKER_NAME,
        _build_output_marker_evidence,
    )
    if not complete:
        raise ParallaxREvidenceIncompleteError(
            "Inspección incompleta del directorio de mods al descubrir marcadores de salida.",
            partial_markers=markers,
        )
    return markers


def run_parallaxr_assisted_preflight(
    mods_dir: Path, explicit_selection: Path | None = None
) -> ParallaxRAssistedPreflight:
    """Ejecuta el preflight asistido de ParallaxR de forma determinista y side-effect-free.

    Retorna evidencia de instalación, candidatos descubiertos y marcadores de salida existentes.
    """
    try:
        mods_dir_accessible = _is_accessible_dir(mods_dir)
    except OSError:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE,
            success=False,
            message=(
                "No se pudo inspeccionar el directorio de mods de MO2 por un error de acceso al sistema de archivos."
            ),
        )

    if not mods_dir_accessible:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.MISSING,
            success=False,
            message=f"El directorio de mods '{mods_dir}' no existe o no es accesible.",
        )

    candidates_incomplete = False
    try:
        candidates = discover_parallaxr_candidates(mods_dir)
    except ParallaxREvidenceIncompleteError as err:
        candidates = err.partial_candidates
        candidates_incomplete = True
    except OSError:
        candidates = ()
        candidates_incomplete = True

    candidate_mod_roots = tuple(c.parent for c in candidates)

    markers_incomplete = False
    markers_unstable = False
    try:
        existing_output = discover_output_markers(mods_dir)
    except ParallaxREvidenceIncompleteError as err:
        existing_output = err.partial_markers
        markers_incomplete = True
    except OSError:
        existing_output = ()
        markers_incomplete = True
    except ParallaxREvidenceUnstableError:
        existing_output = ()
        markers_unstable = True

    if candidates_incomplete or markers_incomplete:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE,
            success=False,
            existing_output_candidates=existing_output,
            candidate_mod_roots=candidate_mod_roots,
            message=(
                "La inspección del directorio de mods quedó incompleta por un error de acceso al "
                "sistema de archivos; no se puede confirmar presencia, ausencia ni unicidad."
            ),
        )

    if markers_unstable:
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
            resolved_selection = _resolve_path(explicit_selection, strict=False)
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
    except OSError:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE,
            success=False,
            existing_output_candidates=existing_output,
            candidate_mod_roots=candidate_mod_roots,
            message=(
                "No se pudo leer el entrypoint seleccionado de ParallaxR por un error de acceso al sistema de archivos."
            ),
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
