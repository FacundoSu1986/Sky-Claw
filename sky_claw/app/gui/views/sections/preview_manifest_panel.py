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


def _build_surgery_rows(conf: dict[str, Any]) -> list[dict[str, Any]]:
    """Fusiona cada par de conflicto con su recomendación y su alerta de flag.

    Es la "cirugía" del panel (T-29): por cada ``pair`` une —

    - la recomendación del asistente de parcheo (T-20) por ``form_id`` (con
      ``record_type`` como fallback) → el parche sugerido y su severidad, y
    - la alerta de flag (T-19b) por ``form_id`` → el flag en riesgo, el "por qué"
      de la regla y qué plugins lo definen (lo que se pierde),

    de modo que el operador entienda winner/loser + riesgo + por qué + qué se
    pierde + parche SIN abrir xEdit. Defensivo: un par sin recomendación/alerta
    que matchee degrada a los campos base (los advisory quedan vacíos, nunca
    ``None``).

    El match por ``form_id`` (no solo por ``record_type``) evita cruzar el parche
    de un record con el de otro cuando un tipo tiene varias recomendaciones con
    distintos ``form_ids``; ``record_type`` queda solo como respaldo.
    """
    # Índices: recomendación por form_id (preciso) y por record_type (fallback),
    # y alerta de flag por form_id. En todos, la primera aparición gana.
    rec_by_form_id: dict[str, dict[str, Any]] = {}
    rec_by_type: dict[str, dict[str, Any]] = {}
    alert_by_form_id: dict[str, dict[str, Any]] = {}
    for rec in conf.get("recommendations") or []:
        record_type = rec.get("record_type") or ""
        if record_type and record_type not in rec_by_type:
            rec_by_type[record_type] = rec
        for rec_form_id in rec.get("form_ids") or []:
            if rec_form_id and rec_form_id not in rec_by_form_id:
                rec_by_form_id[rec_form_id] = rec
        for alert in rec.get("flag_alerts") or []:
            alert_form_id = alert.get("form_id") or ""
            if alert_form_id and alert_form_id not in alert_by_form_id:
                alert_by_form_id[alert_form_id] = alert

    rows: list[dict[str, Any]] = []
    for pair in conf.get("pairs") or []:
        winner = pair.get("winner", "")
        losers = pair.get("losers", [])
        record_type = pair.get("record_type") or ""
        form_id = pair.get("form_id") or ""
        rec = rec_by_form_id.get(form_id) or rec_by_type.get(record_type)
        alert = alert_by_form_id.get(form_id) or {}
        rows.append(
            {
                "record_type": record_type,
                "form_id": form_id,
                "winner": winner,
                "losers": ", ".join(losers),
                # Severidad/riesgo: la de la alerta de flag (por-record) manda; si no
                # hay alerta, la del grupo de la recomendación. Deja priorizar sin xEdit.
                "severity": alert.get("severity") or (rec.get("severity", "") if rec else ""),
                "flag": alert.get("flag", ""),
                "why": alert.get("explanation", ""),
                # "Qué se pierde": los plugins que SÍ definen el flag que el ganador no preserva.
                "lost_from": ", ".join(alert.get("defined_by", [])),
                "suggested_patch": (f"{rec['approach']}: {rec['rationale']}" if rec else ""),
                # Target del botón "Abrir en xEdit": plugin ganador + losers del conflicto.
                "plugins": [p for p in [winner, *losers] if p],
            }
        )
    return rows


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
    # Capa advisory del asistente de parcheo (T-20·2): qué enfoque conviene por
    # grupo de conflictos y por qué.
    recommendations = [
        {
            "record_type": rec.get("record_type") or "",
            "approach": rec.get("approach") or "",
            "rationale": rec.get("rationale") or "",
            "severity": rec.get("severity") or "",
            "conflict_count": rec.get("conflict_count", 0),
            "form_ids": rec.get("form_ids", []),
        }
        for rec in conf.get("recommendations", [])
    ]
    # Vista de "cirugía" por subrecord (T-29): fusiona par + recomendación + alerta
    # de flag para que el operador decida el forwardeo sin abrir xEdit.
    surgery = _build_surgery_rows(conf)
    covered_types = {row["record_type"] for row in surgery}
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
        "recommendations": recommendations,
        "surgery": surgery,
        # Recomendaciones cuyo record_type no aparece en ninguna fila de cirugía:
        # el preview solo mete pairs *críticos*, así que un preview solo-menor (p.
        # ej. LVLI→bashed_patch) deja pairs vacío pero igual recomienda. Sin esto,
        # esas recomendaciones desaparecerían del panel (regresión, review Codex #278).
        "uncovered_recommendations": [rec for rec in recommendations if rec["record_type"] not in covered_types],
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
    on_open_xedit: Callable[[list[str]], None] | None = None,
) -> None:
    """Render the dry-run preview manifest with Approve / Reject controls.

    ``on_open_xedit`` recibe los plugins de un conflicto (winner + losers) cuando
    el operador pulsa "Abrir en xEdit" para forwardear a mano (T-29). Es un
    callback **síncrono**: ``create_cta_button`` lo invoca sin ``await``, así que
    el controller debe envolver la corrutina de :meth:`XEditRunner.launch_interactive`
    y agendarla (p. ej. ``asyncio.create_task`` / ``background_tasks.create``); si
    se pasara el método async crudo, el clic solo crearía y descartaría la corrutina
    y xEdit nunca se lanzaría.
    """
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

        # --- Conflicts: cirugía por subrecord (T-29) ---
        # Cada conflicto muestra winner/loser + el flag en riesgo y su "por qué"
        # (T-19b) + el parche sugerido (T-20), y un botón para forwardear a mano en
        # xEdit — el operador decide sin salir del panel.
        conflicts = vm["conflicts"]
        ui.label(
            f"Conflicts: {conflicts['total']} ({conflicts['critical']} critical) "
            f"→ {conflicts['proposed'] or 'no patch'}"
        ).classes("text-white font-semibold mt-2")
        for row in conflicts["surgery"]:
            with ui.element("div").classes("border-l-2 border-[#374151] pl-3 mt-2 gap-1"):
                # La severidad va en la línea del conflicto para priorizar de un vistazo.
                severity = f" [{row['severity']}]" if row["severity"] else ""
                ui.label(
                    f"{row['record_type']} {row['form_id']}{severity}: {row['winner']} wins over {row['losers']}"
                ).classes("text-[#d1d5db] text-sm font-medium")
                if row["flag"]:
                    ui.label(f"⚠ pierde «{row['flag']}»: {row['why']}").classes("text-[#f59e0b] text-sm")
                    if row["lost_from"]:
                        ui.label(f"lo definían: {row['lost_from']}").classes("text-[#9ca3af] text-xs")
                if row["suggested_patch"]:
                    ui.label(f"→ {row['suggested_patch']}").classes("text-[#9ca3af] text-sm")
                if on_open_xedit is not None and row["plugins"]:
                    create_cta_button(
                        text="Abrir en xEdit",
                        on_click=lambda plugins=row["plugins"]: on_open_xedit(plugins),
                        variant="secondary",
                    )

        # --- Recommended strategy (recomendaciones sin fila de cirugía) ---
        # Un preview solo-menor deja pairs (críticos) vacío pero igual recomienda
        # (p. ej. bashed_patch); esta sección evita que esas recomendaciones
        # desaparezcan al no tener conflicto crítico que las ancle.
        if conflicts["uncovered_recommendations"]:
            ui.label("Recommended strategy").classes("text-white font-semibold mt-2")
            for rec in conflicts["uncovered_recommendations"]:
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
