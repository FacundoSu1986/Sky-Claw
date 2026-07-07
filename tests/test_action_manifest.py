"""Tests del contrato ``ActionManifest`` — manifiesto por acción (T-26a).

El manifiesto es la "caja negra de vuelo" de un Ritual mutante: antes de
ejecutar declara qué archivos tocará, qué records forwardea, con qué
herramienta+versión y cuál es el plan de rollback (qué snapshot restaura qué
archivo). Debe serializar sin pérdida (journal + GUI + HITL) reusando los
modelos ya existentes del subsistema de preview.
"""

from __future__ import annotations

from sky_claw.antigravity.orchestrator.preview.manifest import (
    ActionManifest,
    ConflictPair,
    RollbackStep,
)

# ---------------------------------------------------------------------------
# Construcción y defaults
# ---------------------------------------------------------------------------


def test_action_manifest_minimo_tiene_defaults_vacios() -> None:
    manifest = ActionManifest(workflow_id="wf-1", ritual="loot_sort", tool="LOOT")

    assert manifest.workflow_id == "wf-1"
    assert manifest.ritual == "loot_sort"
    assert manifest.tool == "LOOT"
    assert manifest.tool_version is None
    assert manifest.files_to_touch == []
    assert manifest.records_forwarded == []
    assert manifest.rollback_plan == []
    # created_at se completa solo (timezone-aware).
    assert manifest.created_at.tzinfo is not None


def test_rollback_step_mapea_snapshot_a_archivo() -> None:
    step = RollbackStep(target_file="plugins.txt", snapshot_id="snap-42")

    assert step.target_file == "plugins.txt"
    assert step.snapshot_id == "snap-42"


# ---------------------------------------------------------------------------
# Serialización sin pérdida (round-trip)
# ---------------------------------------------------------------------------


def test_action_manifest_round_trip_json_sin_perdida() -> None:
    original = ActionManifest(
        workflow_id="wf-7",
        ritual="xedit_patch",
        tool="xEdit",
        tool_version="4.1.5",
        files_to_touch=["SkyClaw_Patch.esp"],
        records_forwarded=[
            ConflictPair(winner="A.esp", losers=["B.esp"], record_type="SPEL", form_id="0x00012345"),
        ],
        rollback_plan=[RollbackStep(target_file="SkyClaw_Patch.esp", snapshot_id="snap-1")],
        summary="Forward del ganador SPEL",
    )

    restored = ActionManifest.model_validate_json(original.model_dump_json())

    assert restored == original
    assert restored.records_forwarded[0].record_type == "SPEL"
    assert restored.rollback_plan[0].snapshot_id == "snap-1"


def test_records_forwarded_reusa_conflict_pair() -> None:
    manifest = ActionManifest(
        workflow_id="wf-9",
        ritual="xedit_patch",
        tool="xEdit",
        records_forwarded=[ConflictPair(winner="W.esp", losers=["L1.esp", "L2.esp"])],
    )

    assert isinstance(manifest.records_forwarded[0], ConflictPair)
    assert manifest.records_forwarded[0].losers == ["L1.esp", "L2.esp"]


# ---------------------------------------------------------------------------
# describe(): resumen legible para el gate / GUI
# ---------------------------------------------------------------------------


def test_describe_incluye_ritual_herramienta_y_conteos() -> None:
    manifest = ActionManifest(
        workflow_id="wf-3",
        ritual="loot_sort",
        tool="LOOT",
        tool_version="0.29.0",
        files_to_touch=["plugins.txt", "loadorder.txt"],
        rollback_plan=[
            RollbackStep(target_file="plugins.txt", snapshot_id="s1"),
            RollbackStep(target_file="loadorder.txt", snapshot_id="s2"),
        ],
    )

    texto = manifest.describe()

    assert "loot_sort" in texto
    assert "LOOT" in texto
    # Menciona cuántos archivos se tocan y que hay plan de rollback.
    assert "2" in texto
