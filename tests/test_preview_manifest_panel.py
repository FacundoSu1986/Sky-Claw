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
    PatchRecommendationView,
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
                    recommendations=[
                        PatchRecommendationView(
                            approach="xedit_manual",
                            record_type="NPC_",
                            rationale="conflicto de alto riesgo (narrativa/IA)",
                            severity="critical",
                            conflict_count=1,
                            form_ids=["001"],
                        ),
                    ],
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

    # T-20·2: la recomendación del asistente de parcheo se surface (no se cae en
    # silencio en el panel principal del Operations Hub).
    recs = vm["conflicts"]["recommendations"]
    assert len(recs) == 1
    assert recs[0]["record_type"] == "NPC_"
    assert recs[0]["approach"] == "xedit_manual"
    assert recs[0]["conflict_count"] == 1
    assert "narrativa" in recs[0]["rationale"]

    assert vm["lod"] is not None
    assert vm["lod"]["preset"] == "High"
    assert "DynDOLOD.esp" in vm["lod"]["would_generate"]

    assert vm["warnings"] == ["Plugin count 250/254 approaching the limit"]


def _spel_surgery_manifest_dict() -> dict:
    """Un conflicto SPEL con la alerta de ``Manual Cost Calc`` (T-19b) adjunta a la
    recomendación del asistente (T-20), como llega serializado al panel."""
    alerta = {
        "form_id": "0A1B2C:03",
        "editor_id": "SustainedFlames",
        "record_type": "SPEL",
        "flag": "Manual Cost Calc",
        "winner": "Winner.esp",
        "defined_by": ["Base.esm", "Loser.esp"],
        "severity": "critical",
        "explanation": "sin este flag el motor recalcula el coste por duración → coste astronómico",
    }
    manifest = PreviewManifest(
        workflow_id="wf",
        stages=[
            StageChangeSet(
                stage="xedit",
                executed_for_real=False,
                conflicts=ConflictPreview(
                    target_plugin="P.esp",
                    total_conflicts=1,
                    critical=1,
                    pairs=[
                        ConflictPair(
                            winner="Winner.esp",
                            losers=["Loser.esp"],
                            record_type="SPEL",
                            form_id="0A1B2C:03",
                        )
                    ],
                    proposed_resolution="execute_xedit_script",
                    recommendations=[
                        PatchRecommendationView(
                            approach="xedit_manual",
                            record_type="SPEL",
                            rationale="conflicto crítico de flags (Manual Cost Calc en riesgo)",
                            severity="critical",
                            conflict_count=1,
                            form_ids=["0A1B2C:03"],
                            flag_alerts=[alerta],
                        ),
                    ],
                ),
            ),
        ],
        warnings=[],
    )
    return manifest.model_dump(mode="json")


def test_surgery_rows_fuse_pair_recommendation_and_flag_alert() -> None:
    """Criterio de aceptación T-29: un conflicto SPEL con ``Manual Cost Calc`` en
    riesgo se entiende desde el panel — winner/loser + por qué + qué se pierde +
    parche sugerido — SIN abrir xEdit.

    La fila de cirugía fusiona el par de conflicto con su recomendación (por
    ``record_type``) y su alerta de flag (por ``form_id``).
    """
    vm = build_preview_view_model(_spel_surgery_manifest_dict())

    surgery = vm["conflicts"]["surgery"]
    assert len(surgery) == 1
    row = surgery[0]
    assert row["record_type"] == "SPEL"
    assert row["form_id"] == "0A1B2C:03"
    assert row["winner"] == "Winner.esp"
    assert row["losers"] == "Loser.esp"
    # La severidad/riesgo del conflicto (de la alerta de flag), para priorizar.
    assert row["severity"] == "critical"
    # El flag en riesgo y el "por qué" de la regla (T-19b).
    assert row["flag"] == "Manual Cost Calc"
    assert "coste" in row["why"]
    # Qué se pierde: los plugins que SÍ definen el flag (defined_by).
    assert row["lost_from"] == "Base.esm, Loser.esp"
    # El parche sugerido viene del asistente (T-20).
    assert "xedit_manual" in row["suggested_patch"]
    # Target del botón "Abrir en xEdit": el plugin del conflicto + sus losers.
    assert row["plugins"] == ["Winner.esp", "Loser.esp"]


def test_surgery_rows_match_recommendation_by_form_id() -> None:
    """Con varias recomendaciones del mismo ``record_type`` pero distintos
    ``form_ids``, cada par toma la SUYA por ``form_id`` (no la primera del tipo):
    el parche y la severidad no se cruzan entre records (review Copilot #278)."""
    conf = {
        "total_conflicts": 2,
        "pairs": [
            {"winner": "A.esp", "losers": ["B.esp"], "record_type": "WEAP", "form_id": "001"},
            {"winner": "C.esp", "losers": ["D.esp"], "record_type": "WEAP", "form_id": "002"},
        ],
        "recommendations": [
            {
                "approach": "smash",
                "record_type": "WEAP",
                "rationale": "para 001",
                "severity": "minor",
                "conflict_count": 1,
                "form_ids": ["001"],
                "flag_alerts": [],
            },
            {
                "approach": "xedit_manual",
                "record_type": "WEAP",
                "rationale": "para 002",
                "severity": "critical",
                "conflict_count": 1,
                "form_ids": ["002"],
                "flag_alerts": [],
            },
        ],
    }
    manifest = {
        "workflow_id": "x",
        "stages": [{"stage": "xedit", "executed_for_real": False, "conflicts": conf}],
        "warnings": [],
    }

    surgery = build_preview_view_model(manifest)["conflicts"]["surgery"]
    by_form = {row["form_id"]: row for row in surgery}
    # Cada record recibe el parche de SU recomendación (match por form_id).
    assert by_form["001"]["suggested_patch"] == "smash: para 001"
    assert by_form["002"]["suggested_patch"] == "xedit_manual: para 002"
    # Y su propia severidad (fallback a la recomendación cuando no hay alerta).
    assert by_form["001"]["severity"] == "minor"
    assert by_form["002"]["severity"] == "critical"


def test_surgery_rows_degrade_without_recommendation() -> None:
    """Un par sin recomendación ni alerta que matchee degrada a los campos base
    (winner/losers + plugins), con los campos advisory vacíos — nunca rompe."""
    manifest = {
        "workflow_id": "x",
        "stages": [
            {
                "stage": "xedit",
                "executed_for_real": False,
                "conflicts": {
                    "total_conflicts": 1,
                    "pairs": [{"winner": "A.esm", "losers": ["B.esp"], "record_type": "WEAP", "form_id": "010"}],
                    "recommendations": [],
                },
            }
        ],
        "warnings": [],
    }

    vm = build_preview_view_model(manifest)

    surgery = vm["conflicts"]["surgery"]
    assert len(surgery) == 1
    row = surgery[0]
    assert row["winner"] == "A.esm"
    assert row["losers"] == "B.esp"
    assert row["severity"] == ""
    assert row["flag"] == ""
    assert row["why"] == ""
    assert row["lost_from"] == ""
    assert row["suggested_patch"] == ""
    assert row["plugins"] == ["A.esm", "B.esp"]


def test_view_model_handles_empty_manifest() -> None:
    vm = build_preview_view_model({"workflow_id": "x", "stages": [], "warnings": []})

    assert vm["load_order"]["changed"] is False
    assert vm["load_order"]["moves"] == []
    assert vm["conflicts"]["total"] == 0
    assert vm["conflicts"]["recommendations"] == []
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
