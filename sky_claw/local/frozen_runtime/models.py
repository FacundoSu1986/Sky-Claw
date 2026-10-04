"""Modelos inmutables de Frozen Runtime — P1 (discovery / identidad / estabilidad).

Contrato ADR 0012:
- ``SourceSnapshotEvidence`` es evidencia de la Managed Source observada; nunca
  autoridad por sí misma (SFR-15: no es Golden ni GP2). Se produce únicamente
  desde la observación fresca de la Managed Source, jamás desde un Candidate.
- ``provider_metadata`` es auxiliar/advisory; no define autoridad.
- La estabilidad es acotada: STABLE significa "la fuente estuvo estable durante
  la ventana observada"; no promete estabilidad futura (Q-04).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from enum import StrEnum

from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError
from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest

GAME_KEYS_SUPPORTED: tuple[str, ...] = ("skyrimse",)
CRITICAL_EXE_BY_GAME: dict[str, str] = {"skyrimse": "SkyrimSE.exe"}
GAME_DIR_NAME_BY_KEY: dict[str, str] = {"skyrimse": "Skyrim Special Edition"}


class ManagedSourceProvider(StrEnum):
    """Proveedores de Managed Source. P1 implementa sólo Steam (sin framework)."""

    STEAM = "steam"


class DiscoveryState(StrEnum):
    """Desenlaces tipados del discovery de Managed Source."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


class StabilityState(StrEnum):
    """Veredictos de estabilidad de la ventana de observación.

    ``INDETERMINATE != STABLE``: no poder demostrar estabilidad jamás se
    promueve a estable (Q-04).
    """

    STABLE = "stable"
    UNSTABLE = "unstable"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ManagedSource:
    """Instalación mutable del juego administrada por un proveedor externo.

    P1 soporta ``provider="steam"`` (AppID 489830) y ``game_key="skyrimse"``.
    La evidencia del proveedor la establece el discovery; este modelo no la
    fabrica: una ruta arbitraria no se convierte en Managed Source por
    construcción.
    """

    provider: ManagedSourceProvider
    game_key: str
    appid: str
    root: pathlib.Path
    library_steamapps: pathlib.Path
    library_root: pathlib.Path

    def __post_init__(self) -> None:
        if self.game_key not in GAME_KEYS_SUPPORTED:
            raise FrozenRuntimeError(f"game_key no soportado en P1: '{self.game_key}'")
        if not self.appid:
            raise FrozenRuntimeError("appid no puede ser vacío")
        if not self.root.is_absolute():
            raise FrozenRuntimeError(f"root debe ser una ruta absoluta: '{self.root}'")
        # Layout Steam: <library>/steamapps/common/<game dir>/<root>
        if self.library_steamapps != self.root.parent.parent:
            raise FrozenRuntimeError(
                f"library_steamapps '{self.library_steamapps}' debe ser '<library>/steamapps' del root"
            )
        if self.library_root != self.library_steamapps.parent:
            raise FrozenRuntimeError(f"library_root '{self.library_root}' debe ser el directorio padre de steamapps")


@dataclass(frozen=True, slots=True)
class ManagedSourceDiscoveryResult:
    """Resultado tipado del discovery (FOUND / NOT_FOUND / AMBIGUOUS / INVALID)."""

    state: DiscoveryState
    message: str = ""
    source: ManagedSource | None = None
    candidates: tuple[pathlib.Path, ...] = ()

    @property
    def success(self) -> bool:
        return self.state is DiscoveryState.FOUND


@dataclass(frozen=True, slots=True)
class ProviderMetadataObservation:
    """Metadata advisory del proveedor (nunca autoridad de runtime).

    Para Steam: appid, buildid, state_flags, bytes de descarga, update_result
    y ubicación del manifest. Un campo ilegible se registra explícitamente
    (``manifest_readable=False``); no se inventa ni se interpola.
    """

    provider: ManagedSourceProvider
    appid: str
    buildid: str | None = None
    state_flags: str | None = None
    bytes_to_download: int | None = None
    bytes_downloaded: int | None = None
    update_result: str | None = None
    install_dir: str | None = None
    manifest_path: pathlib.Path | None = None
    manifest_readable: bool = True
    manifest_parse_error: str | None = None
    observed_at_ns: int = 0


@dataclass(frozen=True, slots=True)
class ProviderActivitySignals:
    """Señales advisory de actividad de actualización del proveedor.

    Ninguna señal es autoridad; el conjunto alimenta un gate fail-closed:
    cualquier señal de actividad ⇒ la fuente nunca es STABLE (S11).
    """

    provider: ManagedSourceProvider
    manifest_readable: bool
    manifest_parse_error: str | None
    state_flags: str | None
    bytes_to_download: int | None
    bytes_downloaded: int | None
    update_result: str | None
    buildid: str | None
    downloading_dir_nonempty: bool
    temp_dir_nonempty: bool
    observed_at_ns: int

    @property
    def update_in_progress(self) -> bool:
        """True si CUALQUIER señal advisory indica actualización activa.

        Heurísticas documentadas en ``provider_signals.py``; la ausencia de
        manifest legible no se trata acá (la decide el caller como
        INDETERMINATE).
        """
        if self.state_flags is not None and self.state_flags.strip() not in ("", "4"):
            return True
        if (
            self.bytes_to_download is not None
            and self.bytes_downloaded is not None
            and self.bytes_to_download > self.bytes_downloaded
        ):
            return True
        if self.update_result is not None and self.update_result.strip() not in ("", "0"):
            return True
        return self.downloading_dir_nonempty or self.temp_dir_nonempty


@dataclass(frozen=True, slots=True)
class SourceSnapshotEvidence:
    """Evidencia sellada de la Managed Source observada (SFR-15).

    Producida exclusivamente por observación fresca de la Managed Source; no
    existe API donde un root arbitrario se haga pasar por fuente autorizante.
    No es Golden ni autoridad GP2.
    """

    provider: ManagedSourceProvider
    game_key: str
    runtime_identity: RuntimeIdentity
    tree_digest: TreeDigest
    files: tuple[FileIdentity, ...]
    critical_files: tuple[FileIdentity, ...]
    provider_metadata: ProviderMetadataObservation
    observed_at_ns: int

    def __post_init__(self) -> None:
        if not self.critical_files:
            raise FrozenRuntimeError("SourceSnapshotEvidence exige al menos una evidencia crítica")
        if not self.files:
            raise FrozenRuntimeError("SourceSnapshotEvidence exige un inventario no vacío")


@dataclass(frozen=True, slots=True)
class SourceMeasurement:
    """Medición sellada de la Managed Source en un instante (PRE o POST).

    Es la materia prima de la ventana de estabilización: dos mediciones
    comparables, con la metadata advisory del proveedor observada en el mismo
    instante.
    """

    runtime_identity: RuntimeIdentity
    files: tuple[FileIdentity, ...]
    tree_digest: TreeDigest
    provider_metadata: ProviderMetadataObservation
    observed_at_ns: int


@dataclass(frozen=True, slots=True)
class SourceStabilityResult:
    """Veredicto de estabilidad de la ventana PRE/POST observada."""

    state: StabilityState
    message: str = ""
    pre_tree_digest: TreeDigest | None = None
    post_tree_digest: TreeDigest | None = None
    pre_runtime_identity: RuntimeIdentity | None = None
    post_runtime_identity: RuntimeIdentity | None = None
    pre_provider: ProviderActivitySignals | None = None
    post_provider: ProviderActivitySignals | None = None
    observed_at_ns: int = 0

    @property
    def success(self) -> bool:
        return self.state is StabilityState.STABLE


@dataclass(frozen=True, slots=True)
class StableSourceObservation:
    """Veredicto de estabilidad + evidencia de la MISMA ventana causal.

    ``snapshot`` sólo existe si ``stability.state is STABLE``; su contenido es
    la medición POST de la ventana, de modo que evidencia y veredicto no pueden
    desincronizarse (acota el TOCTOU STABLE → snapshot).
    """

    source: ManagedSource
    stability: SourceStabilityResult
    snapshot: SourceSnapshotEvidence | None = None

    @property
    def success(self) -> bool:
        return self.stability.state is StabilityState.STABLE and self.snapshot is not None
