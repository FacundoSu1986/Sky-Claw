"""Observación fresca de la Managed Source → SourceSnapshotEvidence (P1).

SFR-15: la evidencia proviene SIEMPRE de la observación fresca de la Managed
Source. No existe API donde un root arbitrario se haga pasar por fuente
autorizante: ``observe_source_snapshot`` exige un :class:`ManagedSource`
construido por el discovery (que ya verificó evidencia de proveedor), y cada
invocación vuelve a medir el disco (identidad + inventario sellado); no hay
cache que pueda ocultar un reemplazo del ejecutable (I05).
"""

from __future__ import annotations

import time

from sky_claw.local.frozen_runtime.errors import FrozenRuntimeObservationError
from sky_claw.local.frozen_runtime.models import (
    CRITICAL_EXE_BY_GAME,
    ManagedSource,
    SourceMeasurement,
    SourceSnapshotEvidence,
)
from sky_claw.local.frozen_runtime.provider_signals import read_steam_manifest_observation
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import FileIdentity, InventoryError
from sky_claw.local.runtime_vault.runtime_observation import RuntimeObservationError, observe_runtime_identity_from_root
from sky_claw.local.runtime_vault.verification import tree_digest_from_files


def medir_fuente(source: ManagedSource, *, observed_at_ns: int | None = None) -> SourceMeasurement:
    """Medición fresca y sellada de la Managed Source (identidad + árbol + metadata).

    Fail-closed: cualquier fallo de identidad o inventario se propaga como
    :class:`FrozenRuntimeObservationError`; nunca se emite una medición
    parcial. La metadata del proveedor es advisory y se registra con su estado
    de legibilidad explícito.
    """
    ts = observed_at_ns if observed_at_ns is not None else time.time_ns()
    try:
        fresh = observe_runtime_identity_from_root(source.root, expected_game_key=source.game_key)
    except RuntimeObservationError as exc:
        raise FrozenRuntimeObservationError(
            f"no se pudo observar la identidad de runtime en '{source.root}': {exc}"
        ) from exc
    try:
        files = inventory_tree(source.root)
    except InventoryError as exc:
        raise FrozenRuntimeObservationError(f"no se pudo inventariar la Managed Source '{source.root}': {exc}") from exc
    metadata = read_steam_manifest_observation(source, observed_at_ns=ts)
    return SourceMeasurement(
        runtime_identity=fresh.runtime_identity,
        files=files,
        tree_digest=tree_digest_from_files(files),
        provider_metadata=metadata,
        observed_at_ns=ts,
    )


def _archivos_criticos(game_key: str, files: tuple[FileIdentity, ...]) -> tuple[FileIdentity, ...]:
    esperado = CRITICAL_EXE_BY_GAME.get(game_key)
    if esperado is None:
        raise FrozenRuntimeObservationError(f"sin catálogo de archivos críticos para '{game_key}'")
    encontrados = tuple(f for f in files if f.rel_path.casefold() == esperado.casefold())
    if not encontrados:
        raise FrozenRuntimeObservationError(
            f"falta el archivo crítico '{esperado}' en el inventario de la Managed Source"
        )
    return encontrados


def snapshot_from_measurement(source: ManagedSource, med: SourceMeasurement) -> SourceSnapshotEvidence:
    """Construye la evidencia sellada desde una medición ya realizada."""
    return SourceSnapshotEvidence(
        provider=source.provider,
        game_key=source.game_key,
        runtime_identity=med.runtime_identity,
        tree_digest=med.tree_digest,
        files=med.files,
        critical_files=_archivos_criticos(source.game_key, med.files),
        provider_metadata=med.provider_metadata,
        observed_at_ns=med.observed_at_ns,
    )


def observe_source_snapshot(source: ManagedSource) -> SourceSnapshotEvidence:
    """Observación fresca (sin cache) de la Managed Source → evidencia sellada.

    Es la primitive que P3 reutilizará para su propio PRE/copy/POST: la
    expectativa contra la que un Candidate se compara DEBE provenir de una
    medición fresca de la Managed Source (SFR-15).
    """
    return snapshot_from_measurement(source, medir_fuente(source))
