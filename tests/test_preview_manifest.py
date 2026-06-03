"""Tests for the dry-run/preview manifest Pydantic models.

The manifest is the typed contract shown to the operator before the real
LOOT->xEdit->DynDOLOD chain runs.  It must serialize losslessly (GUI +
audit logging) and present the load-order diff respecting Skyrim master
rules (.esm > .esl > .esp).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sky_claw.antigravity.orchestrator.preview.manifest import (
    ConflictPair,
    ConflictPreview,
    LoadOrderDiff,
    LODPlan,
    PreviewManifest,
    StageChangeSet,
    sort_by_master_rules,
)

# ---------------------------------------------------------------------------
# sort_by_master_rules
# ---------------------------------------------------------------------------


def test_sort_by_master_rules_orders_esm_then_esl_then_esp() -> None:
    plugins = ["B.esp", "A.esm", "C.esl", "D.esp", "E.esm"]
    assert sort_by_master_rules(plugins) == ["A.esm", "E.esm", "C.esl", "B.esp", "D.esp"]


def test_sort_by_master_rules_is_stable_within_group() -> None:
    # Within the same rank we must preserve input order (NOT alphabetise).
    plugins = ["z.esp", "a.esp", "m.esp"]
    assert sort_by_master_rules(plugins) == ["z.esp", "a.esp", "m.esp"]


def test_sort_by_master_rules_is_case_insensitive_on_extension() -> None:
    plugins = ["b.ESP", "a.ESM"]
    assert sort_by_master_rules(plugins) == ["a.ESM", "b.ESP"]


# ---------------------------------------------------------------------------
# LoadOrderDiff
# ---------------------------------------------------------------------------


def test_load_order_diff_from_orders_computes_moves() -> None:
    before = ["A.esm", "B.esp", "C.esp"]
    after = ["A.esm", "C.esp", "B.esp"]
    diff = LoadOrderDiff.from_orders(before, after)

    assert diff.changed is True
    moved = {m.plugin: (m.from_index, m.to_index) for m in diff.moves}
    assert moved == {"B.esp": (1, 2), "C.esp": (2, 1)}


def test_load_order_diff_no_change_has_no_moves() -> None:
    order = ["A.esm", "B.esp"]
    diff = LoadOrderDiff.from_orders(order, order)

    assert diff.changed is False
    assert diff.moves == []


# ---------------------------------------------------------------------------
# PreviewManifest serialization
# ---------------------------------------------------------------------------


def _sample_manifest() -> PreviewManifest:
    return PreviewManifest(
        workflow_id="wf-preview-1",
        stages=[
            StageChangeSet(
                stage="loot",
                executed_for_real=True,
                files_touched=["plugins.txt"],
                load_order_diff=LoadOrderDiff.from_orders(["B.esp", "A.esm"], ["A.esm", "B.esp"]),
            ),
            StageChangeSet(
                stage="xedit",
                executed_for_real=True,
                conflicts=ConflictPreview(
                    target_plugin="Patch.esp",
                    total_conflicts=2,
                    critical=1,
                    minor=1,
                    pairs=[
                        ConflictPair(winner="A.esm", losers=["B.esp"], record_type="WEAP"),
                    ],
                    proposed_resolution="create_merged_patch",
                ),
            ),
            StageChangeSet(
                stage="dyndolod",
                executed_for_real=False,
                lod_plan=LODPlan(
                    preset="High",
                    would_generate=["DynDOLOD.esp"],
                    estimated_assets=1200,
                    output_dirs=["DynDOLOD Output"],
                ),
            ),
        ],
        warnings=["Plugin count 250/254 approaching the 255 hard limit"],
    )


def test_preview_manifest_round_trip() -> None:
    manifest = _sample_manifest()

    raw = manifest.model_dump_json()
    restored = PreviewManifest.model_validate_json(raw)

    assert restored == manifest
    # Spot-check the nested plan-only and conflict payloads survive the trip.
    assert restored.stages[2].lod_plan is not None
    assert restored.stages[2].lod_plan.preset == "High"
    assert restored.stages[2].executed_for_real is False
    assert restored.stages[1].conflicts is not None
    assert restored.stages[1].conflicts.proposed_resolution == "create_merged_patch"


def test_stage_change_set_rejects_unknown_stage() -> None:
    with pytest.raises(ValidationError):
        StageChangeSet(stage="bogus", executed_for_real=True)  # type: ignore[arg-type]
