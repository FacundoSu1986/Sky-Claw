"""Runtime Vault (RV-1): identidad de runtime + verificación de árboles.

Core autocontenido y sin dependencias de #493: la identidad de runtime llega
como datos de entrada (nunca se escanea el host) y la identidad de un árbol es
(digest, files, bytes) derivada de (relpath, size, sha256) por archivo, sin
mtime y sin root físico. La única dependencia fuera de stdlib es
:mod:`sky_claw.app.security.links` (módulo de ``main``), la primitiva canónica
de detección de enlaces, reutilizada sin modificarla.

Follow-up registrado: ``FOLLOW_UP_DEDUP_AFTER_PR493`` — cuando el PR #493
aterrice en main, evaluar unificar el digest de árbol con
``sky_claw/local/tools/artifact_digest.py``; hasta entonces este paquete se
mantiene independiente.
"""

from __future__ import annotations

from sky_claw.local.runtime_vault.clone import (
    create_runtime_clone,
    verify_physical_independence,
)
from sky_claw.local.runtime_vault.golden import (
    verify_critical_files,
    verify_golden_master,
)
from sky_claw.local.runtime_vault.golden_protection_plan import (
    DuplicateFileIdError,
    GoldenProtectionNodeKind,
    GoldenProtectionPlan,
    GoldenProtectionPlanError,
    GoldenProtectionPlanState,
    GoldenProtectionSealChecks,
    NodeSecurityBackup,
    PlanCreationError,
    SealedGoldenProtectionPlan,
    SecurityBackupIntegrityError,
    prepare_golden_protection_plan,
    seal_golden_protection_plan,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.locking import destination_lock
from sky_claw.local.runtime_vault.models import (
    CriticalFileEvidence,
    CriticalFileExpectation,
    DestinationExistsError,
    FileIdentity,
    GoldenMasterCandidate,
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    InventoryError,
    InventoryLinkError,
    PhysicalIndependenceResult,
    RuntimeCloneDescriptor,
    RuntimeCloneError,
    RuntimeCloneResult,
    RuntimeIdentity,
    RuntimeVaultError,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.protection import (
    GoldenProtectionEvidence,
    GoldenProtectionInputError,
    GoldenProtectionResult,
    GoldenProtectionRight,
    GoldenProtectionState,
    NodeProtectionObservation,
    classify_protection,
    inspect_golden_protection,
)
from sky_claw.local.runtime_vault.verification import (
    tree_digest_from_files,
    verify_runtime_identity,
    verify_tree,
)

__all__ = [
    "CriticalFileEvidence",
    "CriticalFileExpectation",
    "DestinationExistsError",
    "DuplicateFileIdError",
    "FileIdentity",
    "GoldenMasterCandidate",
    "GoldenMasterDescriptor",
    "GoldenMasterVerificationResult",
    "GoldenProtectionEvidence",
    "GoldenProtectionInputError",
    "GoldenProtectionNodeKind",
    "GoldenProtectionPlan",
    "GoldenProtectionPlanError",
    "GoldenProtectionPlanState",
    "GoldenProtectionResult",
    "GoldenProtectionRight",
    "GoldenProtectionSealChecks",
    "GoldenProtectionState",
    "InventoryError",
    "InventoryLinkError",
    "NodeProtectionObservation",
    "NodeSecurityBackup",
    "PhysicalIndependenceResult",
    "PlanCreationError",
    "RuntimeCloneDescriptor",
    "RuntimeCloneError",
    "RuntimeCloneResult",
    "RuntimeIdentity",
    "RuntimeVaultError",
    "RuntimeVerificationResult",
    "SealedGoldenProtectionPlan",
    "SecurityBackupIntegrityError",
    "TreeDigest",
    "TreeVerificationResult",
    "VerificationState",
    "classify_protection",
    "create_runtime_clone",
    "destination_lock",
    "inspect_golden_protection",
    "inventory_tree",
    "prepare_golden_protection_plan",
    "seal_golden_protection_plan",
    "tree_digest_from_files",
    "verify_critical_files",
    "verify_golden_master",
    "verify_physical_independence",
    "verify_runtime_identity",
    "verify_tree",
]
