"""Tests for the Operations Hub preview-manifest view-model builder.

The NiceGUI panel itself is thin browser glue; the display logic lives in the
pure ``build_preview_view_model`` helper, which is what we pin down here.
"""

from __future__ import annotations

from sky_claw.antigravity.gui.views.sections.preview_manifest_panel import build_preview_view_model
from sky_claw.antigravity.orchestrator.preview.manifest import (
    ConflictPair,
    ConflictPreview,
    LoadOrderDiff,
    LODPlan,
    PreviewManifest,
    StageChangeSet,
)


def _manifest_dict() -> dict:
    manifest = PreviewManifest(
        workflow_id="wf",
        stages=[
            StageChangeSet(
                stage="loot",
                executed_for_real=True,
                load_order_diff=LoadOrderDiff.from_orders(["B.esp", "A.esm"], ["A.esm", "B.esp"]),
            ),
            StageChangeSet(
                stage="xedit",
                executed_for_real=False,
                conflicts=ConflictPreview(
                    target_plugin="P.esp",
                    total_conflicts=3,
                    critical=1,
                    minor=2,
                    pairs=[ConflictPair(winner="A.esm", losers=["B.esp"], record_type="NPC_", form_id="001")],
                    proposed_resolution="execute_xedit_script",
                ),
            ),
            StageChangeSet(
                stage="dyndolod",
                executed_for_real=False,
                lod_plan=LODPlan(preset="High", would_generate=["DynDOLOD.esp"], output_dirs=["DynDOLOD Output"]),
            ),
        ],
        warnings=["Plugin count 250/254 approaching the limit"],
    )
    return manifest.model_dump(mode="json")


def test_view_model_load_order_diff() -> None:
    vm = build_preview_view_model(_manifest_dict())

    assert vm["workflow_id"] == "wf"
    assert vm["load_order"]["changed"] is True
    assert vm["load_order"]["before"] == ["B.esp", "A.esm"]
    assert vm["load_order"]["after"] == ["A.esm", "B.esp"]
    # 1-based, human-readable move text.
    texts = {m["text"] for m in vm["load_order"]["moves"]}
    assert "B.esp: 1 → 2" in texts
    assert "A.esm: 2 → 1" in texts


def test_view_model_conflicts_and_lod() -> None:
    vm = build_preview_view_model(_manifest_dict())

    assert vm["conflicts"]["total"] == 3
    assert vm["conflicts"]["critical"] == 1
    assert vm["conflicts"]["minor"] == 2
    assert vm["conflicts"]["proposed"] == "execute_xedit_script"
    assert vm["conflicts"]["rows"][0]["winner"] == "A.esm"
    assert vm["conflicts"]["rows"][0]["losers"] == "B.esp"

    assert vm["lod"] is not None
    assert vm["lod"]["preset"] == "High"
    assert "DynDOLOD.esp" in vm["lod"]["would_generate"]

    assert vm["warnings"] == ["Plugin count 250/254 approaching the limit"]


def test_view_model_handles_empty_manifest() -> None:
    vm = build_preview_view_model({"workflow_id": "x", "stages": [], "warnings": []})

    assert vm["load_order"]["changed"] is False
    assert vm["load_order"]["moves"] == []
    assert vm["conflicts"]["total"] == 0
    assert vm["lod"] is None
    assert vm["warnings"] == []


def test_view_model_tolerates_partial_move_entry() -> None:
    """A move dict missing index keys must not raise (defensive .get())."""
    manifest = {
        "workflow_id": "x",
        "stages": [
            {
                "stage": "loot",
                "executed_for_real": True,
                "load_order_diff": {
                    "before": ["A.esp"],
                    "after": ["B.esp"],
                    "moves": [{"plugin": "A.esp"}],  # missing from_index / to_index
                },
            }
        ],
        "warnings": [],
    }

    vm = build_preview_view_model(manifest)  # must not raise KeyError

    move = vm["load_order"]["moves"][0]
    assert move["plugin"] == "A.esp"
    # Unknown positions render as placeholders, NOT a misleading "1 → 1".
    assert move["text"] == "A.esp: ? → ?"
