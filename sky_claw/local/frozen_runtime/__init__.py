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

from sky_claw.local.frozen_runtime.candidate_id import (
    default_candidate_id_factory,
    nuevo_candidate_id,
    validar_candidate_id,
)
from sky_claw.local.frozen_runtime.candidates import (
    CANDIDATE_SCHEMA_VERSION,
    CandidateResult,
    candidate_dir,
    candidate_metadata_path,
    candidates_state_dir,
    crear_candidate,
    descubrir_candidates,
    leer_metadata_candidate,
    payload_dir,
    serializar_metadata_candidate,
    verificar_candidate,
)
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.discovery import discover_managed_source
from sky_claw.local.frozen_runtime.errors import (
    AmbiguousManagedSourceError,
    CandidateCopyError,
    CandidateCorruptMetadataError,
    CandidateError,
    CandidateVerificationError,
    DuplicateKeyVdfError,
    FrozenRuntimeError,
    FrozenRuntimeObservationError,
    FrozenRuntimeStorageError,
    GenerationCollisionError,
    InvalidCandidateIdError,
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
from sky_claw.local.frozen_runtime.independence import (
    verify_generation_independence,
    verify_generation_physical_integrity,
)
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    DirectoryMembershipEvidence,
    SealedTreeObservation,
    TreeObservationCoherenceError,
    canonicalizar_directorio,
    capturar_membership_directorios,
    construir_evidencia_membership,
    exigir_coherencia_archivos_membership,
    observar_arbol_sellado,
)
from sky_claw.local.frozen_runtime.models import (
    DiscoveryState,
    ManagedSource,
    ManagedSourceDiscoveryResult,
    ManagedSourceProvider,
    ProviderActivitySignals,
    ProviderMetadataObservation,
    ProviderObservationState,
    SourceMeasurement,
    SourceSnapshotEvidence,
    SourceStabilityResult,
    StabilityState,
    StableSourceObservation,
)
from sky_claw.local.frozen_runtime.observation import observe_source_snapshot
from sky_claw.local.frozen_runtime.provider_signals import evaluate_provider_observation
from sky_claw.local.frozen_runtime.stabilization import (
    DEFAULT_QUIET_WINDOW_SECONDS,
    assess_managed_source_stability,
    obtain_stable_source_snapshot,
)
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state, save_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import (
    admitir_directorio_storage,
    default_storage_root,
    initialize_frozen_runtime_storage,
    same_volume,
)
from sky_claw.local.frozen_runtime.storage_models import (
    CandidateInventory,
    CandidateMetadata,
    CandidateRecord,
    CandidateSourceEvidence,
    CandidateState,
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
    "CANDIDATE_SCHEMA_VERSION",
    "CandidateCopyError",
    "CandidateCorruptMetadataError",
    "CandidateError",
    "CandidateInventory",
    "CandidateMetadata",
    "CandidateRecord",
    "CandidateResult",
    "CandidateSourceEvidence",
    "CandidateState",
    "CandidateVerificationError",
    "DEFAULT_QUIET_WINDOW_SECONDS",
    "DirectoryMembershipError",
    "DirectoryMembershipEvidence",
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
    "InvalidCandidateIdError",
    "InvalidGenerationIdError",
    "MalformedVdfError",
    "ManagedSource",
    "ManagedSourceDiscoveryResult",
    "ManagedSourceProvider",
    "PhysicalIndependenceResult",
    "ProviderActivitySignals",
    "ProviderEvidenceError",
    "ProviderMetadataObservation",
    "ProviderObservationState",
    "SealedTreeObservation",
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
    "TreeObservationCoherenceError",
    "admitir_directorio_storage",
    "assess_managed_source_stability",
    "candidate_dir",
    "candidate_metadata_path",
    "candidates_state_dir",
    "capturar_membership_directorios",
    "canonicalizar_directorio",
    "construir_evidencia_membership",
    "construir_generation_id",
    "copiar_arbol_independiente",
    "crear_candidate",
    "default_candidate_id_factory",
    "descubrir_candidates",
    "descubrir_generations",
    "discover_managed_source",
    "default_storage_root",
    "evaluate_provider_observation",
    "exigir_coherencia_archivos_membership",
    "generacion_id_desde_evidencia",
    "initialize_frozen_runtime_storage",
    "leer_generation_metadata",
    "leer_metadata_candidate",
    "load_frozen_runtime_state",
    "nuevo_candidate_id",
    "observar_arbol_sellado",
    "observe_source_snapshot",
    "obtain_stable_source_snapshot",
    "payload_dir",
    "registrar_generation_metadata",
    "same_volume",
    "save_frozen_runtime_state",
    "serializar_metadata_candidate",
    "validar_candidate_id",
    "validar_generation_id",
    "verify_generation_independence",
    "verify_generation_physical_integrity",
    "verificar_candidate",
    "verificar_generation",
]
