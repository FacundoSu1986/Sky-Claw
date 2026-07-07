"""Typed manifest of everything a dry-run of the modding chain *would* change.

These Pydantic models are the contract surfaced to the operator (and to the
audit log) before the real LOOT->xEdit->DynDOLOD chain is executed.  They are
deliberately serialization-first (``model_dump_json`` / ``model_validate_json``)
so the same object feeds the Operations Hub UI, the HITL approval prompt, and
structured logging without any regex over free LLM text.

Master-order note
-----------------
Skyrim loads ``.esm`` (master) plugins first, then light ``.esl`` masters, then
regular ``.esp`` plugins.  :func:`sort_by_master_rules` presents a plugin list
in that order (stable within a rank) so the load-order diff is readable.
"""

from __future__ import annotations

import datetime as _dt
from typing import Literal

from pydantic import BaseModel, Field

StageName = Literal["loot", "xedit", "dyndolod", "bashed"]


def _utcnow() -> _dt.datetime:
    """Timezone-aware UTC now (kept as a named fn for deterministic patching)."""
    return _dt.datetime.now(_dt.UTC)


def sort_by_master_rules(plugins: list[str]) -> list[str]:
    """Return ``plugins`` ordered ``.esm`` > ``.esl`` > ``.esp`` (stable within rank).

    This is a *presentation* helper for the load-order diff.  The master flag is
    approximated by file extension (the common case); plugins keep their relative
    input order inside each rank, so a stable sort is intentional.
    """

    def rank(name: str) -> int:
        lowered = name.lower()
        if lowered.endswith(".esm"):
            return 0
        if lowered.endswith(".esl"):
            return 1
        return 2  # .esp and anything else load last

    return sorted(plugins, key=rank)


class PluginMove(BaseModel):
    """A single plugin whose index changed between the old and new load order."""

    plugin: str
    from_index: int
    to_index: int


class LoadOrderDiff(BaseModel):
    """Before/after load order plus the per-plugin moves between them."""

    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    moves: list[PluginMove] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when the new order differs from the old one."""
        return self.before != self.after

    @classmethod
    def from_orders(cls, before: list[str], after: list[str]) -> LoadOrderDiff:
        """Build a diff, computing the moves for plugins present in both orders."""
        before_index = {plugin: i for i, plugin in enumerate(before)}
        moves: list[PluginMove] = []
        for new_index, plugin in enumerate(after):
            old_index = before_index.get(plugin)
            if old_index is not None and old_index != new_index:
                moves.append(PluginMove(plugin=plugin, from_index=old_index, to_index=new_index))
        return cls(before=list(before), after=list(after), moves=moves)


class ConflictPair(BaseModel):
    """One conflict: the winning plugin and the overridden losers."""

    winner: str
    losers: list[str] = Field(default_factory=list)
    record_type: str | None = None
    form_id: str | None = None


class ConflictPreview(BaseModel):
    """What xEdit's read-only scan found and how a patch would resolve it."""

    target_plugin: str | None = None
    total_conflicts: int = 0
    critical: int = 0
    minor: int = 0
    pairs: list[ConflictPair] = Field(default_factory=list)
    # The patch strategy that *would* run if approved (plan-only; not executed).
    proposed_resolution: str | None = None


class LODPlan(BaseModel):
    """Estimate of what DynDOLOD *would* generate (plan-only; exes never run)."""

    preset: str
    would_generate: list[str] = Field(default_factory=list)
    estimated_assets: int = 0
    output_dirs: list[str] = Field(default_factory=list)


class StageChangeSet(BaseModel):
    """The changes one stage of the chain would apply.

    ``executed_for_real`` distinguishes stages run for real inside the
    force-rollback transaction (LOOT sort, xEdit read-only scan) from
    plan-only stages whose expensive mutation was skipped (xEdit patch,
    DynDOLOD, Bashed Patch).
    """

    stage: StageName
    executed_for_real: bool
    files_touched: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Only the payload relevant to ``stage`` is populated.
    load_order_diff: LoadOrderDiff | None = None
    conflicts: ConflictPreview | None = None
    lod_plan: LODPlan | None = None
    summary: str | None = None


class RollbackStep(BaseModel):
    """Un paso del plan de rollback: qué snapshot restaura qué archivo."""

    target_file: str
    snapshot_id: str


class ActionManifest(BaseModel):
    """Manifiesto de UNA acción (Ritual mutante) antes de ejecutarla.

    Es la "caja negra de vuelo" por acción: declara qué archivos tocará el
    Ritual, qué records forwardea, con qué herramienta+versión, y cuál es el
    plan de rollback (qué snapshot restaura qué archivo). A diferencia de
    :class:`PreviewManifest` (que describe el dry-run de la cadena completa),
    esto describe una única acción y es el contrato que el approval gate
    persiste y muestra al operador antes de ejecutar.
    """

    workflow_id: str
    ritual: str
    tool: str
    tool_version: str | None = None
    created_at: _dt.datetime = Field(default_factory=_utcnow)
    files_to_touch: list[str] = Field(default_factory=list)
    # Reusa ConflictPair: "estos records forwardeo, este gana, estos pierden".
    records_forwarded: list[ConflictPair] = Field(default_factory=list)
    rollback_plan: list[RollbackStep] = Field(default_factory=list)
    summary: str | None = None

    def describe(self) -> str:
        """Resumen de una línea para el gate/GUI (evidencia, no magia)."""
        version = f" {self.tool_version}" if self.tool_version else ""
        return (
            f"{self.ritual} vía {self.tool}{version}: "
            f"{len(self.files_to_touch)} archivo(s), "
            f"{len(self.records_forwarded)} record(s) forwardeado(s), "
            f"rollback de {len(self.rollback_plan)} archivo(s)."
        )


class PreviewManifest(BaseModel):
    """Top-level manifest of a full dry-run of the modding chain."""

    workflow_id: str
    created_at: _dt.datetime = Field(default_factory=_utcnow)
    stages: list[StageChangeSet] = Field(default_factory=list)
    # Convenience pointer to the LOOT stage's diff for the UI header.
    load_order_diff: LoadOrderDiff | None = None
    warnings: list[str] = Field(default_factory=list)
    summary: str | None = None

    def stage_names(self) -> list[str]:
        """Ordered list of stage identifiers present in this manifest."""
        return [stage.stage for stage in self.stages]
