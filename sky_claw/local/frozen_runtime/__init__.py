"""Frozen Runtime — P1: Managed Source discovery, identidad y estabilización.

Contrato ADR 0012:

- Feature: Frozen Runtime. Término genérico: Managed Source. Proveedor
  inicial: Steam (AppID 489830, game skyrimse).
- P1 es READ-ONLY sobre la Managed Source: sin writes, sin ACL, sin helper
  privilegiado, sin bloquear Steam (SFR-11/12; USES_* = NO).
- ``SourceSnapshotEvidence`` proviene sólo de la observación de la Managed
  Source (SFR-15); la estabilidad es un contrato acotado a la ventana
  observada (Q-04); ``INDETERMINATE != STABLE``.
"""

from __future__ import annotations

from sky_claw.local.frozen_runtime.discovery import discover_managed_source
from sky_claw.local.frozen_runtime.errors import (
    AmbiguousManagedSourceError,
    DuplicateKeyVdfError,
    FrozenRuntimeError,
    FrozenRuntimeObservationError,
    FrozenRuntimeStorageError,
    GenerationCollisionError,
    InvalidGenerationIdError,
    MalformedVdfError,
    ProviderEvidenceError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.generation_id import construir_generation_id, validar_generation_id
from sky_claw.local.frozen_runtime.generations import (
    descubrir_generations,
    generacion_id_desde_evidencia,
    leer_generation_metadata,
    registrar_generation_metadata,
    verificar_generation,
)
from sky_claw.local.frozen_runtime.independence import verify_generation_independence
from sky_claw.local.frozen_runtime.models import (
    DiscoveryState,
    ManagedSource,
    ManagedSourceDiscoveryResult,
    ManagedSourceProvider,
    ProviderActivitySignals,
    ProviderMetadataObservation,
    SourceMeasurement,
    SourceSnapshotEvidence,
    SourceStabilityResult,
    StabilityState,
    StableSourceObservation,
)
from sky_claw.local.frozen_runtime.observation import observe_source_snapshot
from sky_claw.local.frozen_runtime.stabilization import (
    DEFAULT_QUIET_WINDOW_SECONDS,
    assess_managed_source_stability,
    obtain_stable_source_snapshot,
)
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state, save_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import (
    default_storage_root,
    initialize_frozen_runtime_storage,
    same_volume,
)
from sky_claw.local.frozen_runtime.storage_models import (
    FrozenRuntimeState,
    FrozenRuntimeStateLoadResult,
    GenerationInventory,
    GenerationMetadata,
    GenerationRecord,
    GenerationVerificationResult,
    GenerationVerificationState,
    IndependenceState,
    PhysicalIndependenceResult,
    SharedObjectEvidence,
    StorageAdmissionResult,
    StorageAdmissionState,
    StorageInitResult,
)

__all__ = [
    "AmbiguousManagedSourceError",
    "DEFAULT_QUIET_WINDOW_SECONDS",
    "DiscoveryState",
    "DuplicateKeyVdfError",
    "FrozenRuntimeError",
    "FrozenRuntimeObservationError",
    "FrozenRuntimeStorageError",
    "GenerationCollisionError",
    "GenerationInventory",
    "GenerationMetadata",
    "GenerationRecord",
    "GenerationVerificationResult",
    "GenerationVerificationState",
    "IndependenceState",
    "InvalidGenerationIdError",
    "MalformedVdfError",
    "ManagedSource",
    "ManagedSourceDiscoveryResult",
    "ManagedSourceProvider",
    "PhysicalIndependenceResult",
    "ProviderActivitySignals",
    "ProviderEvidenceError",
    "ProviderMetadataObservation",
    "SharedObjectEvidence",
    "SourceMeasurement",
    "SourceSnapshotEvidence",
    "SourceStabilityResult",
    "StabilityState",
    "StableSourceObservation",
    "StateCorruptError",
    "StateSchemaError",
    "StorageAdmissionResult",
    "StorageAdmissionState",
    "StorageInitResult",
    "FrozenRuntimeState",
    "FrozenRuntimeStateLoadResult",
    "assess_managed_source_stability",
    "construir_generation_id",
    "descubrir_generations",
    "discover_managed_source",
    "default_storage_root",
    "generacion_id_desde_evidencia",
    "initialize_frozen_runtime_storage",
    "leer_generation_metadata",
    "load_frozen_runtime_state",
    "observe_source_snapshot",
    "obtain_stable_source_snapshot",
    "registrar_generation_metadata",
    "same_volume",
    "save_frozen_runtime_state",
    "validar_generation_id",
    "verify_generation_independence",
    "verificar_generation",
]
