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
    MalformedVdfError,
    ProviderEvidenceError,
)
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

__all__ = [
    "AmbiguousManagedSourceError",
    "DEFAULT_QUIET_WINDOW_SECONDS",
    "DiscoveryState",
    "DuplicateKeyVdfError",
    "FrozenRuntimeError",
    "FrozenRuntimeObservationError",
    "MalformedVdfError",
    "ManagedSource",
    "ManagedSourceDiscoveryResult",
    "ManagedSourceProvider",
    "ProviderActivitySignals",
    "ProviderEvidenceError",
    "ProviderMetadataObservation",
    "SourceMeasurement",
    "SourceSnapshotEvidence",
    "SourceStabilityResult",
    "StabilityState",
    "StableSourceObservation",
    "assess_managed_source_stability",
    "discover_managed_source",
    "observe_source_snapshot",
    "obtain_stable_source_snapshot",
]
