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

from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    FileIdentity,
    InventoryError,
    InventoryLinkError,
    RuntimeIdentity,
    RuntimeVaultError,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.verification import (
    tree_digest_from_files,
    verify_runtime_identity,
    verify_tree,
)

__all__ = [
    "FileIdentity",
    "InventoryError",
    "InventoryLinkError",
    "RuntimeIdentity",
    "RuntimeVaultError",
    "RuntimeVerificationResult",
    "TreeDigest",
    "TreeVerificationResult",
    "VerificationState",
    "inventory_tree",
    "tree_digest_from_files",
    "verify_runtime_identity",
    "verify_tree",
]
