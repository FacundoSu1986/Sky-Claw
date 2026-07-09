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
from typing import Any, Literal

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
    """One file's rollback pointer: which snapshot restores which original path.

    Mirrors :class:`~sky_claw.antigravity.db.snapshot_manager.SnapshotInfo` (the
    fields needed to reverse a mutation) so the manifest carries an actionable
    rollback plan without holding a live snapshot-manager reference.
    """

    original_path: str
    snapshot_path: str
    snapshot_id: str


class ActionManifest(BaseModel):
    """Manifest of what a single mutating Ritual *will* change, emitted BEFORE
    it executes (ADR 0002 — "caja negra de vuelo").

    Unlike :class:`PreviewManifest` (a full chain dry-run), this describes one
    Ritual: the files it will touch, the tool + version doing it, and the
    rollback plan (which snapshot restores which file). It is serialization-first
    so the same object feeds the approval gate, the journal, and the final
    flight report (T-28) without re-deriving anything.
    """

    ritual_id: str
    tool: str
    tool_version: str | None = None
    created_at: _dt.datetime = Field(default_factory=_utcnow)
    files_touched: list[str] = Field(default_factory=list)
    # Records/plugins the Ritual forwards (reuses the preview's conflict shape).
    records_forwarded: list[ConflictPair] = Field(default_factory=list)
    load_order_diff: LoadOrderDiff | None = None
    rollback_plan: list[RollbackStep] = Field(default_factory=list)
    summary: str | None = None


class FlightReport(BaseModel):
    """Informe final de vuelo de un Ritual mutante, emitido DESPUÉS de ejecutar
    (T-28, ADR 0002 — la caja negra leída después del vuelo).

    Cada sección se copia del :class:`ActionManifest` persistido en el journal
    — el informe ensambla datos existentes, no inventa nuevos: qué cambió
    (``files_touched`` / ``load_order_diff``), por qué (``summary``), quién ganó
    cada conflicto (``conflicts_resolved``) y cómo revertir (``rollback_plan``,
    apuntando a snapshots reales). Un run sin manifiesto produce un informe
    degradado explícito (``degraded`` + ``degraded_reason``), nunca un vacío
    silencioso. Serialization-first: el mismo objeto se persiste en el journal,
    alimenta la GUI y se exporta como Markdown.
    """

    # Discriminador: el manifiesto también viaja como metadata de una operación
    # del journal y también trae ritual_id — esta clave distingue ambos ops.
    kind: Literal["flight_report"] = "flight_report"
    # Identidad del Ritual; opcionales para que el informe degradado (sin
    # manifiesto que los aporte) siga siendo representable.
    ritual_id: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    created_at: _dt.datetime = Field(default_factory=_utcnow)
    # Valor del TransactionStatus del journal ("committed" / "rolled_back" /
    # "pending"), o "desconocido" si la transacción no existe al componer
    # (ver compose_flight_report_from_journal); str para no acoplar a la capa DB.
    transaction_status: str
    # Sección "qué cambió".
    files_touched: list[str] = Field(default_factory=list)
    load_order_diff: LoadOrderDiff | None = None
    # Sección "por qué" (la narrativa adicional se deriva en el renderer).
    summary: str | None = None
    # Sección "quién ganó cada conflicto".
    conflicts_resolved: list[ConflictPair] = Field(default_factory=list)
    # Sección "cómo revertir".
    rollback_plan: list[RollbackStep] = Field(default_factory=list)
    # Slot para el post-run validator (T-21); mientras no exista, el renderer
    # lo declara explícitamente como no disponible en vez de omitirlo.
    post_run_validation: dict[str, Any] | None = None
    # Degradado explícito: sin manifiesto persistido no hay vacío silencioso.
    degraded: bool = False
    degraded_reason: str | None = None


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
