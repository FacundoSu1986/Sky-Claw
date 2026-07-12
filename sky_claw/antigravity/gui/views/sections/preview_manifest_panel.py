"""Operations Hub panel for the dry-run PreviewManifest.

The panel is fed by the ``ops.hitl.preview`` event that
:class:`ChainPreviewService` publishes (the WebSocket fan-out already forwards
``ops.hitl.*``).  It shows the load-order diff, the detected conflicts, the LOD
plan, and any warnings, with Approve / Reject buttons that drive the HITL gate.

VIEW PURO — the display logic lives in :func:`build_preview_view_model` (a pure,
unit-tested transform); :func:`create_preview_manifest_panel` is thin NiceGUI glue.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from ..components import create_cta_button


def _pos(index: int | None) -> str:
    """1-based position string, or ``"?"`` when the index is unknown (partial payload)."""
    return str(index + 1) if isinstance(index, int) else "?"


def build_preview_view_model(manifest: dict[str, Any]) -> dict[str, Any]:
    """Transform a serialized :class:`PreviewManifest` into a display-ready model.

    Pure and defensive: missing stages/fields collapse to empty/zero values so
    the panel renders regardless of which stages a preview produced.
    """
    stages: list[dict[str, Any]] = manifest.get("stages") or []
    by_stage = {stage.get("stage"): stage for stage in stages}

    diff = (by_stage.get("loot") or {}).get("load_order_diff") or {}
    moves = [
        {
            "plugin": move.get("plugin", "?"),
            "from_index": move.get("from_index"),
            "to_index": move.get("to_index"),
            # 1-based positions; "?" when an index is unknown (partial payload), so
            # a partial move never looks like a real "1 → 1" no-op.
            "text": f"{move.get('plugin', '?')}: {_pos(move.get('from_index'))} → {_pos(move.get('to_index'))}",
        }
        for move in diff.get("moves", [])
    ]
    load_order = {
        "changed": diff.get("before", []) != diff.get("after", []),
        "before": diff.get("before", []),
        "after": diff.get("after", []),
        "moves": moves,
    }

    conf = (by_stage.get("xedit") or {}).get("conflicts") or {}
    conflicts = {
        "total": conf.get("total_conflicts", 0),
        "critical": conf.get("critical", 0),
        "minor": conf.get("minor", 0),
        "proposed": conf.get("proposed_resolution") or "",
        "target_plugin": conf.get("target_plugin") or "",
        "rows": [
            {
                "winner": pair.get("winner", ""),
                "losers": ", ".join(pair.get("losers", [])),
                "record_type": pair.get("record_type") or "",
                "form_id": pair.get("form_id") or "",
            }
            for pair in conf.get("pairs", [])
        ],
        # Capa advisory del asistente de parcheo (T-20·2): qué enfoque conviene
        # por grupo de conflictos y por qué. Sin esto el panel dejaría caer la
        # recomendación en silencio (review Codex #272).
        "recommendations": [
            {
                "record_type": rec.get("record_type") or "",
                "approach": rec.get("approach") or "",
                "rationale": rec.get("rationale") or "",
                "severity": rec.get("severity") or "",
                "conflict_count": rec.get("conflict_count", 0),
                "form_ids": rec.get("form_ids", []),
            }
            for rec in conf.get("recommendations", [])
        ],
    }

    plan = (by_stage.get("dyndolod") or {}).get("lod_plan")
    lod = (
        {
            "preset": plan.get("preset", ""),
            "would_generate": plan.get("would_generate", []),
            "estimated_assets": plan.get("estimated_assets", 0),
            "output_dirs": plan.get("output_dirs", []),
        }
        if plan
        else None
    )

    return {
        "workflow_id": manifest.get("workflow_id", ""),
        "summary": manifest.get("summary") or "",
        "stages": [
            {"stage": stage.get("stage"), "executed_for_real": stage.get("executed_for_real", False)}
            for stage in stages
        ],
        "load_order": load_order,
        "conflicts": conflicts,
        "lod": lod,
        "warnings": manifest.get("warnings") or [],
    }


def create_preview_manifest_panel(
    manifest: dict[str, Any],
    on_approve: Callable[[], None] | None = None,
    on_reject: Callable[[], None] | None = None,
) -> None:
    """Render the dry-run preview manifest with Approve / Reject controls."""
    vm = build_preview_view_model(manifest)

    with ui.element("div").classes("bg-[#0f0f0f] border border-[#1f2937] rounded-2xl p-6 gap-4"):
        ui.label("Dry-run preview").classes("text-white font-bold text-lg")
        if vm["summary"]:
            ui.label(vm["summary"]).classes("text-[#9ca3af] text-sm mb-2")

        # --- Load order diff ---
        ui.label("Load order").classes("text-white font-semibold mt-2")
        if vm["load_order"]["changed"]:
            for move in vm["load_order"]["moves"]:
                ui.label(move["text"]).classes("text-[#d1d5db] text-sm font-mono")
        else:
            ui.label("No reordering").classes("text-[#6b7280] text-sm")

        # --- Conflicts ---
        conflicts = vm["conflicts"]
        ui.label(
            f"Conflicts: {conflicts['total']} ({conflicts['critical']} critical) "
            f"→ {conflicts['proposed'] or 'no patch'}"
        ).classes("text-white font-semibold mt-2")
        for row in conflicts["rows"]:
            ui.label(f"{row['record_type']} {row['form_id']}: {row['winner']} wins over {row['losers']}").classes(
                "text-[#d1d5db] text-sm"
            )

        # --- Recommended strategy (asistente de parcheo T-20·2) ---
        if conflicts["recommendations"]:
            ui.label("Recommended strategy").classes("text-white font-semibold mt-2")
            for rec in conflicts["recommendations"]:
                ui.label(
                    f"{rec['record_type']} ({rec['conflict_count']}) → {rec['approach']}: {rec['rationale']}"
                ).classes("text-[#d1d5db] text-sm")

        # --- LOD plan ---
        if vm["lod"] is not None:
            ui.label(
                f"LODs (preset {vm['lod']['preset']}): would generate {', '.join(vm['lod']['would_generate'])}"
            ).classes("text-white font-semibold mt-2")

        # --- Warnings ---
        for warning in vm["warnings"]:
            ui.label(f"⚠ {warning}").classes("text-[#f59e0b] text-sm")

        # --- HITL controls ---
        with ui.row().classes("items-center gap-3 mt-4"):
            create_cta_button(text="Approve & run", on_click=on_approve, variant="primary")
            create_cta_button(text="Reject", on_click=on_reject, variant="secondary")
