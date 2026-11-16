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
- Identidad (#667-A4): toda la superficie pública expone la identidad LÓGICA de MO2 (el hijo
  directo de `mods_dir` tal como el gestor lo presenta) y la procedencia FÍSICA verificada
  viaja únicamente en `ParallaxRInstallationEvidence.resolved_mod_root`/`resolved_entrypoint`.
  Dos carpetas hijas que comparten destino físico (junctions o symlinks) son dos identidades
  independientes: nunca se deduplican por ruta resuelta, hash ni inodo.
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
# Caracteres reservados de Win32 y separador alternativo: un segmento lógico de selección que
# contenga cualquiera de ellos no puede identificar una carpeta hija de MO2 (incluye ADS por ':').
_INVALID_SEGMENT_CHARS: Final[frozenset[str]] = frozenset({"*", "?", "<", ">", '"', ":", "|", "\0"})
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
class _DirectModEntry:
    """Registro dual interno de una entrada de mod válida: identidad lógica MO2 y procedencia física.

    `logical_*` son coordenadas de Mod Organizer 2 bajo el `mods_dir` resuelto (lo que el
    gestor presenta y el usuario selecciona). `resolved_*` son los destinos verificados tras
    resolver reparse points dentro de la frontera autorizada.

    Invariante: nunca se deriva una coordenada de la otra. El padre físico del entrypoint
    resuelto no tiene por qué coincidir con la carpeta física del mod (un `ParallaxR.BAT`
    puede ser un symlink de archivo hacia un subdirectorio del mismo mod).
    """

    logical_mod_root: Path
    logical_target: Path
    resolved_mod_root: Path
    resolved_target: Path


@dataclass(frozen=True, slots=True)
class ParallaxRInstallationEvidence:
    """Evidencia inmutable capturada de la instalación oficial de ParallaxR.

    En emisiones productivas (`READY`) `mod_root` y `entrypoint` identifican la instalación
    LÓGICA de MO2 (la carpeta hija directa del `mods_dir` resuelto y su `ParallaxR.BAT`),
    mientras que `resolved_mod_root` y `resolved_entrypoint` registran los destinos
    físicamente inspeccionados y hasheados dentro de la frontera autorizada.

    Contrato:
    - El par `(None, None)` queda reservado a constructores heredados de cinco argumentos y
      no constituye evidencia física comprobada.
    - `__post_init__` rechaza el estado mitad-poblado, incluso vía `dataclasses.replace()`.
    - No debe asumirse `resolved_entrypoint.parent == resolved_mod_root`: el entrypoint puede
      ser un symlink de archivo hacia un subdirectorio del mod.
    - En volúmenes case-insensitive `resolved_entrypoint.name` puede preservar el casing real
      del disco, distinto de `"ParallaxR.BAT"`; el round-trip se garantiza sobre `entrypoint`.
    """

    mod_root: Path
    entrypoint: Path
    size_bytes: int
    mtime_ns: int
    sha256: str
    resolved_mod_root: Path | None = None
    resolved_entrypoint: Path | None = None

    def __post_init__(self) -> None:
        """Rechaza evidencia física parcialmente poblada (fail-closed)."""
        if (self.resolved_mod_root is None) != (self.resolved_entrypoint is None):
            raise ValueError(
                "ParallaxRInstallationEvidence requiere ambos campos resolved_mod_root y "
                "resolved_entrypoint poblados o ambos en None."
            )


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

    def __post_init__(self) -> None:
        """Valida que status sea ParallaxRAssistedStatus, success bool estricto y success is True <=> READY."""
        if not isinstance(self.status, ParallaxRAssistedStatus):
            raise TypeError(f"status debe ser ParallaxRAssistedStatus; se recibió {type(self.status).__name__}.")
        if type(self.success) is not bool:
            raise TypeError(f"success debe ser bool estricto; se recibió {type(self.success).__name__}.")
        expected_success = self.status == ParallaxRAssistedStatus.READY
        if self.success is not expected_success:
            raise ValueError(
                f"Invariante ParallaxRAssistedPreflight violado: "
                f"status={self.status.name} ({self.status.value!r}) exige success={expected_success!r}, "
                f"se recibió success={self.success!r}."
            )


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


def _es_error_de_ausencia_sin_anomalias(err: OSError) -> bool:
    """Indica si el OSError corresponde solo a ausencia conocida y no a un fallo de inspección."""
    if getattr(err, "winerror", None) in _WINERROR_INSPECTION_FAILURES:
        return False
    return err.errno in _ABSENCE_ERRNOS


def _verificar_ancestros_sin_fallo_de_inspeccion(path: Path, initial_err: OSError) -> None:
    """Confirma que la ausencia de path no oculta un ancestro roto, circular o inaccesible."""
    for ancestor in path.parents:
        try:
            ancestor.lstat()
        except OSError as lstat_err:
            if not _es_error_de_ausencia_sin_anomalias(lstat_err):
                raise lstat_err from initial_err
            continue

        # El ancestro existe en su entrada padre: su stat() y resolución estricta deben tener éxito.
        try:
            ancestor.stat()
            _resolve_path(ancestor, strict=True)
        except OSError as anc_err:
            raise anc_err from initial_err
        return


def _stat_mode_fail_closed(path: Path) -> int | None:
    """Obtiene st_mode distinguiendo ausencia real de errores de inspección Windows/POSIX."""
    try:
        return path.stat().st_mode
    except OSError as stat_err:
        if not _es_error_de_ausencia_sin_anomalias(stat_err):
            raise
        try:
            # Un symlink o junction roto falla stat() con ENOENT, pero lstat() confirma que la entrada existe.
            path.lstat()
        except OSError as lstat_err:
            if not _es_error_de_ausencia_sin_anomalias(lstat_err):
                raise lstat_err from stat_err
            _verificar_ancestros_sin_fallo_de_inspeccion(path, stat_err)
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


def _es_prefijo_de_componentes(child_path: Path, parent_dir: Path) -> bool:
    """Indica si `child_path` contiene exactamente los componentes de `parent_dir` (inclusivo).

    Compara la tupla de componentes en vez de la igualdad de rutas del lenguaje: en Windows esa
    igualdad pliega mayúsculas/minúsculas globalmente y colapsaría directorios NTFS configurados
    como case-sensitive. Ambas rutas deben ser absolutas.
    """
    if not parent_dir.is_absolute() or not child_path.is_absolute():
        return False
    parent_parts = parent_dir.parts
    return len(child_path.parts) >= len(parent_parts) and child_path.parts[: len(parent_parts)] == parent_parts


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
    if _es_prefijo_de_componentes(resolved_child, base_parent):
        return resolved_child
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
    Si el archivo es mutado o desaparece sin anomalías durante la verificación final, reintenta
    hasta max_attempts veces y lanza ParallaxREvidenceUnstableError si persiste la carrera.
    Si ocurre un error de acceso o inspección distinto de ausencia simple, propaga el OSError
    de inmediato para evitar degradar evidencia incompleta a inestabilidad.
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
            except OSError as stat_err:
                if _es_error_de_ausencia_sin_anomalias(stat_err):
                    continue
                raise

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
    build_item: Callable[[_DirectModEntry], _ItemT],
) -> tuple[tuple[_ItemT, ...], bool]:
    """Enumera entradas directas de mods_dir sin convertir errores de E/S en ausencia.

    Motor único de descubrimiento: por cada hijo directo válido emite un `_DirectModEntry` con
    las cuatro coordenadas (lógica de carpeta y archivo, física de carpeta y archivo). Preserva
    el orden de sondeo de `#944` sobre la entrada hija tal como se iteró y sobre su objetivo
    directo; la coordenada lógica se proyecta recién al construir el registro.
    """
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
            # Descendencia estricta +1: descarta la self-junction (`SelfLink -> mods_dir`, +0).
            if len(resolved_child.parts) < len(resolved_mods_dir.parts) + 1:
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
            # Descendencia estricta +2: descarta file symlinks hacia la raíz de mods_dir (+1).
            if len(resolved_target.parts) < len(resolved_mods_dir.parts) + 2:
                continue
            logical_mod_root = resolved_mods_dir / child.name
            logical_target = logical_mod_root / target_name
            items.append(
                build_item(
                    _DirectModEntry(
                        logical_mod_root=logical_mod_root,
                        logical_target=logical_target,
                        resolved_mod_root=resolved_child,
                        resolved_target=resolved_target,
                    )
                )
            )
    except OSError:
        return tuple(items), False

    return tuple(items), True


def _discover_candidate_identities(mods_dir: Path) -> tuple[_DirectModEntry, ...]:
    """Descubre candidatos de ParallaxR con identidad dual o eleva evidencia incompleta (#944)."""
    identities, complete = _discover_direct_mod_entries(
        mods_dir,
        OFFICIAL_ENTRYPOINT_NAME,
        lambda entry: entry,
    )
    if not complete:
        raise ParallaxREvidenceIncompleteError(
            "Inspección incompleta del directorio de mods al descubrir candidatos de ParallaxR.",
            partial_candidates=tuple(identity.logical_target for identity in identities),
        )
    return identities


def discover_parallaxr_candidates(mods_dir: Path) -> tuple[Path, ...]:
    """Descubre entrypoints oficiales en hijos directos de mods_dir con identidad lógica MO2.

    Operación read-only, side-effect-free respecto del filesystem y acotada. Retorna los
    entrypoints LÓGICOS (`<mods_dir resuelto>/<hijo>/ParallaxR.BAT`) sin deduplicar por destino
    físico: dos carpetas hijas que comparten destino (junctions o symlinks) son dos candidatos
    independientes. Si ocurre un error de E/S durante la inspección, eleva
    ParallaxREvidenceIncompleteError preservando los entrypoints lógicos ya observados.
    """
    return tuple(identity.logical_target for identity in _discover_candidate_identities(mods_dir))


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
        # B9 fuera de alcance: el marcador conserva su ruta FÍSICA verificada, pero hereda del
        # motor compartido el cierre de self-junctions y la profundidad mínima +2.
        lambda entry: _build_output_marker_evidence(entry.resolved_target),
    )
    if not complete:
        raise ParallaxREvidenceIncompleteError(
            "Inspección incompleta del directorio de mods al descubrir marcadores de salida.",
            partial_markers=markers,
        )
    return markers


def _validate_selection_segment(segment: str) -> bool:
    """Valida un segmento lógico de `explicit_selection` (nombre de mod o de entrypoint oficial)."""
    if not segment or segment in (".", ".."):
        return False
    if "/" in segment or "\\" in segment:
        return False
    return not any(caracter in _INVALID_SEGMENT_CHARS for caracter in segment)


def _is_same_resolved_container(candidate_container: Path, authorized_mods_dir: Path) -> bool:
    """Compara dos contenedores YA resueltos por el kernel, componente a componente.

    Se comparan los componentes y la cadena OS, nunca la igualdad de rutas del lenguaje:
    en Windows dos rutas pueden resultar iguales aunque el filesystem distinga `Mods` de
    `mods`. Ambas rutas deben estar absolutas.
    """
    if not candidate_container.is_absolute() or not authorized_mods_dir.is_absolute():
        return False
    return candidate_container.parts == authorized_mods_dir.parts and os.fspath(candidate_container) == os.fspath(
        authorized_mods_dir
    )


def _resolves_to_authorized_container(container: Path, authorized_mods_dir: Path) -> bool:
    """Resuelve estrictamente SÓLO el contenedor y lo compara con el `mods_dir` autorizado.

    Captura exclusivamente `OSError` (incluye el bucle de symlinks traducido por
    `_resolve_path`): un `RuntimeError` ajeno al bucle debe propagarse sin enmascararse.
    """
    try:
        resolved = _resolve_path(container, strict=True)
    except OSError:
        return False
    return _is_same_resolved_container(resolved, authorized_mods_dir)


def _decompose_explicit_selection(explicit_selection: Path, resolved_mods_dir: Path) -> tuple[str, str | None] | None:
    """Descompone `explicit_selection` en (segmento de mod, segmento de entrypoint | None).

    Boundary-Split: nunca se dereferencia la entrada seleccionada (el alias lógico), sólo se
    resuelve estrictamente su contenedor padre (Forma 1) o abuelo (Forma 2) y se exige igualdad
    canónica contra `resolved_mods_dir`. Se rechazan rutas vacías, `"."`, drive-relative,
    root-relative sin unidad y cualquier componente `".."`, sin normalización léxica.
    """
    if not explicit_selection.parts:
        return None
    if not explicit_selection.is_absolute() and (explicit_selection.drive or explicit_selection.root):
        return None
    if ".." in explicit_selection.parts:
        return None

    anchored = explicit_selection if explicit_selection.is_absolute() else Path.cwd() / explicit_selection
    if not anchored.is_absolute():
        return None

    # Forma 1: raíz lógica del mod (`.../<mods_dir>/<mod>`).
    mod_segment = anchored.name
    if _validate_selection_segment(mod_segment) and _resolves_to_authorized_container(
        anchored.parent, resolved_mods_dir
    ):
        return (mod_segment, None)

    # Forma 2: entrypoint lógico oficial (`.../<mods_dir>/<mod>/ParallaxR.BAT`).
    mod_segment = anchored.parent.name
    entry_segment = anchored.name
    if (
        _validate_selection_segment(mod_segment)
        and _validate_selection_segment(entry_segment)
        and _resolves_to_authorized_container(anchored.parent.parent, resolved_mods_dir)
    ):
        return (mod_segment, entry_segment)

    return None


def _match_explicit_selection(
    identities: tuple[_DirectModEntry, ...],
    selection_mod_segment: str,
    selection_entry_segment: str | None,
) -> _DirectModEntry | None:
    """Devuelve el candidato sólo si el match lógico es exacto y único (fail-closed)."""
    matches = tuple(
        identity
        for identity in identities
        if identity.logical_mod_root.name == selection_mod_segment
        and (selection_entry_segment is None or selection_entry_segment == OFFICIAL_ENTRYPOINT_NAME)
    )
    return matches[0] if len(matches) == 1 else None


def run_parallaxr_assisted_preflight(
    mods_dir: Path, explicit_selection: Path | None = None
) -> ParallaxRAssistedPreflight:
    """Ejecuta el preflight asistido de ParallaxR de forma determinista y side-effect-free.

    Toda la superficie pública del resultado habla identidad lógica MO2; la procedencia física
    verificada viaja en `ParallaxRInstallationEvidence.resolved_mod_root`/`resolved_entrypoint`
    y es la única que se usa para contención y fingerprinting anti-TOCTOU.
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

    identities: tuple[_DirectModEntry, ...] = ()
    candidate_mod_roots: tuple[Path, ...] = ()
    candidates_incomplete = False
    try:
        identities = _discover_candidate_identities(mods_dir)
    except ParallaxREvidenceIncompleteError as err:
        # #944: los parciales ya son entrypoints lógicos; su padre es la raíz lógica del mod.
        candidate_mod_roots = tuple(entrypoint.parent for entrypoint in err.partial_candidates)
        candidates_incomplete = True
    except OSError:
        candidates_incomplete = True
    else:
        candidate_mod_roots = tuple(identity.logical_mod_root for identity in identities)

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

    if not identities:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.MISSING,
            success=False,
            existing_output_candidates=existing_output,
            candidate_mod_roots=(),
            message="No se encontró ninguna instalación de ParallaxR bajo los mods de MO2.",
        )

    # El contenedor autorizado ya fue validado y resuelto por el motor de descubrimiento.
    resolved_mods_dir = identities[0].logical_mod_root.parent
    selected: _DirectModEntry | None = None

    if explicit_selection is not None:
        decomposed = _decompose_explicit_selection(explicit_selection, resolved_mods_dir)
        selected = _match_explicit_selection(identities, *decomposed) if decomposed is not None else None
        if selected is None:
            return ParallaxRAssistedPreflight(
                status=ParallaxRAssistedStatus.INVALID_SELECTION,
                success=False,
                existing_output_candidates=existing_output,
                candidate_mod_roots=candidate_mod_roots,
                message="La selección explícita no coincide con ningún candidato descubierto válido.",
            )
    elif len(identities) == 1:
        selected = identities[0]
    else:
        return ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.AMBIGUOUS,
            success=False,
            existing_output_candidates=existing_output,
            candidate_mod_roots=candidate_mod_roots,
            message=(
                f"Se detectaron múltiples ({len(identities)}) candidatos de ParallaxR. Se requiere selección explícita."
            ),
        )

    try:
        snapshot = _fingerprint_file_consistently(selected.resolved_target)
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
        mod_root=selected.logical_mod_root,
        entrypoint=selected.logical_target,
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns,
        sha256=snapshot.sha256,
        resolved_mod_root=selected.resolved_mod_root,
        resolved_entrypoint=selected.resolved_target,
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
