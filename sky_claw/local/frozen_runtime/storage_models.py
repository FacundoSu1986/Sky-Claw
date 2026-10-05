"""Modelos inmutables de storage de Frozen Runtime — P2 (Generation model).

Contratos ADR 0012:

- ``FrozenRuntimeState`` registra la **Desired Active Generation** (SFR-16);
  NO registra Effective Runtime: que MO2/SKSE ejecuten esa generación se
  demuestra en P5, y este archivo jamás afirma por sí solo promoción exitosa.
- Una Generation publicada es lógicamente inmutable (SFR-17); el estado se
  deriva de evidencia fresca, no de flags persistentes que envejecen.
- SFR-18: la independencia física Generation↔Managed Source es contrato
  verificable (symlink/junction/reparse/hardlink/alias/contención).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from enum import StrEnum

from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest


class IndependenceState(StrEnum):
    """Veredictos de independencia física (SFR-18)."""

    INDEPENDENT = "independent"
    VIOLATED = "violated"
    INDETERMINATE = "indeterminate"


class GenerationVerificationState(StrEnum):
    """Clasificación tipada de una Generation conocida.

    Se deriva de evidencia fresca (inventario sellado + identidad), no de
    flags persistentes: ``DRIFTED != VALID`` y jamás es target de rollback sin
    re-verificación exitosa.
    """

    VALID = "valid"
    DRIFTED = "drifted"
    INVALID = "invalid"
    UNKNOWN = "unknown"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class SharedObjectEvidence:
    """Evidencia de un objeto NTFS compartido entre Generation y Managed Source."""

    rel_path_generation: str
    rel_path_source: str
    volume_serial: int
    file_index: int


@dataclass(frozen=True, slots=True)
class PhysicalIndependenceResult:
    """Resultado tipado de la verificación de independencia física (SFR-18)."""

    state: IndependenceState
    message: str = ""
    shared_objects: tuple[SharedObjectEvidence, ...] = ()

    @property
    def success(self) -> bool:
        return self.state is IndependenceState.INDEPENDENT


@dataclass(frozen=True, slots=True)
class GenerationMetadata:
    """Metadata inmutable de una Generation publicada (``state/generations/<id>.json``).

    Vive FUERA del árbol de la Generation a propósito: así el árbol de la
    generación permanece byte-idéntico al snapshot del que nació (el digest no
    cambia al publicar) y la verificación no necesita filtrados especiales.
    ``provider_metadata`` es auxiliar (nunca autoridad; el buildid no define
    la identidad de la Generation).
    """

    schema_version: int
    generation_id: str
    display_version: str
    runtime_identity: RuntimeIdentity
    tree_digest: TreeDigest
    critical_files: tuple[FileIdentity, ...]
    provider: str
    provider_appid: str | None = None
    provider_buildid: str | None = None
    created_at_ns: int = 0


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    """Una Generation descubierta bajo ``versions/``: clasificada, no activada."""

    generation_id: str | None
    directory: pathlib.Path
    metadata: GenerationMetadata | None
    state: GenerationVerificationState
    message: str = ""


@dataclass(frozen=True, slots=True)
class GenerationInventory:
    """Listado tipado de ``versions/`` (sin activar nada)."""

    root: pathlib.Path
    records: tuple[GenerationRecord, ...]


@dataclass(frozen=True, slots=True)
class GenerationVerificationResult:
    """Resultado de la verificación on-demand de una Generation (P2 hook)."""

    state: GenerationVerificationState
    message: str = ""
    recorded: GenerationMetadata | None = None
    observed_digest: TreeDigest | None = None
    observed_identity: RuntimeIdentity | None = None

    @property
    def success(self) -> bool:
        return self.state is GenerationVerificationState.VALID


class StorageAdmissionState(StrEnum):
    """Desenlace de la admisión del root de storage (fail-closed)."""

    ADMITTED = "admitted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class StorageAdmissionResult:
    """Resultado tipado de la admisión de rutas del storage."""

    state: StorageAdmissionState
    message: str = ""
    root: pathlib.Path | None = None

    @property
    def success(self) -> bool:
        return self.state is StorageAdmissionState.ADMITTED


@dataclass(frozen=True, slots=True)
class StorageInitResult:
    """Resultado de la inicialización (idempotente, no destructiva)."""

    admission: StorageAdmissionResult
    root: pathlib.Path
    created_dirs: tuple[pathlib.Path, ...] = ()
    state_initialized: bool = False

    @property
    def success(self) -> bool:
        return self.admission.success


@dataclass(frozen=True, slots=True)
class FrozenRuntimeState:
    """Intención persistente de Sky-Claw (schema v1).

    ``desired_active_generation=None`` = arranque limpio (sin Generation
    activa todavía). Es el Desired de SFR-16: NO registra Effective Runtime.
    """

    schema_version: int
    desired_active_generation: str | None
    updated_at_ns: int


@dataclass(frozen=True, slots=True)
class FrozenRuntimeStateLoadResult:
    """Carga tipada del estado: distingue ausente de corrupto.

    ``found=False, state=None`` = estado ausente (pre-inicialización o
    arranque limpio). Un estado presente pero corrupto LANZA (fail-closed);
    nunca se interpreta como ausente.
    """

    found: bool
    state: FrozenRuntimeState | None
    message: str = ""
