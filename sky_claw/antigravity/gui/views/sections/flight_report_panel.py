"""Operations Hub panel for the post-run flight report (T-28, ADR 0002).

Cierra el lazo "informe" del flujo must-have: los 6 rituales mutantes (LOOT,
xEdit, Synthesis, DynDOLOD, Pandora, Wrye Bash) persisten un
:class:`~sky_claw.antigravity.orchestrator.preview.manifest.FlightReport` en el
journal DESPUÉS de ejecutar (T-28); este panel lo hace *visible* al operador —
la caja negra leída después del vuelo. Muestra un banner con el veredicto de la
transacción (aplicado / revertido / pendiente) y las secciones que el propio
modelo declara: **qué cambió** (archivos + orden), **por qué** (summary),
**quién ganó cada conflicto** (records forwardeados) y **cómo revertir** (plan de
rollback apuntando a snapshots reales). Un informe degradado (sin manifiesto) se
declara explícito, nunca un vacío silencioso.

VIEW PURO — la lógica de display vive en :func:`build_flight_report_view_model`
(un transform puro, unit-testeado); :func:`create_flight_report_panel` es glue
fino de NiceGUI.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

#: Etiqueta del banner por estado de la transacción del journal. Nunca sale vacío:
#: un estado desconocido cae a la etiqueta neutra.
_STATUS_LABELS: dict[str, str] = {
    "committed": "Aplicado: los cambios quedaron en el perfil.",
    "rolled_back": "Revertido: la mutación se deshizo (rollback).",
    "pending": "Pendiente: la transacción no se cerró.",
}
_DEFAULT_STATUS_LABEL = "Estado de la transacción desconocido."

#: Estilo del banner (fondo/borde/texto) por estado. Aplicado = verde; revertido =
#: rojo; pendiente/desconocido = ámbar. Mismo lenguaje visual que el panel de
#: preflight (T-16) para consistencia del Operations Hub.
_STATUS_STYLES: dict[str, str] = {
    "committed": "bg-[#052e16] text-[#86efac] border-[#166534]",
    "rolled_back": "bg-[#3f1212] text-[#fca5a5] border-[#991b1b]",
    "pending": "bg-[#3a2a00] text-[#fcd34d] border-[#92400e]",
}
_DEFAULT_STATUS_STYLE = "bg-[#0f0f0f] text-[#9ca3af] border-[#1f2937]"


def build_flight_report_view_model(report: dict[str, Any]) -> dict[str, Any]:
    """Transforma un ``FlightReport`` serializado en un modelo listo para render.

    Puro y defensivo: campos ausentes colapsan a vacíos/etiquetas por defecto, así
    el panel renderiza aunque el informe llegue parcial o degradado, y el banner
    nunca sale vacío. Espeja el criterio de ``build_preflight_view_model``.
    """
    status = report.get("transaction_status") or "desconocido"
    tool = report.get("tool") or "Ritual desconocido"

    diff = report.get("load_order_diff") or {}
    moves = [
        {
            "plugin": m.get("plugin", ""),
            "from_index": m.get("from_index"),
            "to_index": m.get("to_index"),
        }
        for m in (diff.get("moves") or [])
    ]
    # ``moves`` solo registra plugins presentes en AMBOS órdenes (ver
    # LoadOrderDiff.from_orders): un plugin agregado/quitado no aparece ahí. Para no
    # reportar "sin cambios" cuando el orden sí cambió (review Codex #326), se derivan
    # los altas/bajas y ``order_changed`` desde before/after.
    before = list(diff.get("before") or [])
    after = list(diff.get("after") or [])
    added = [p for p in after if p not in before]
    removed = [p for p in before if p not in after]
    order_changed = before != after
    files_touched = list(report.get("files_touched") or [])

    conflicts = [
        {
            "winner": c.get("winner", ""),
            "losers": list(c.get("losers") or []),
            "record_type": c.get("record_type") or "",
            "form_id": c.get("form_id") or "",
        }
        for c in (report.get("conflicts_resolved") or [])
    ]

    # El input accionable de recuperación es ``snapshot_path`` (el archivo que
    # restaura el original); ``snapshot_id`` solo es el identificador opaco. Se
    # incluyen ambos, igual que el renderer Markdown (review Codex #326).
    rollback = [
        {
            "original_path": step.get("original_path", ""),
            "snapshot_path": step.get("snapshot_path", ""),
            "snapshot_id": step.get("snapshot_id", ""),
        }
        for step in (report.get("rollback_plan") or [])
    ]

    # El payload real del post-run (PostRunValidationReport.to_dict, T-21) NO tiene
    # un ``status`` de tope: trae kind/has_findings/preflight/headers_checked/
    # header_issues. Se enumera clave: valor como el renderer Markdown, así los
    # hallazgos (has_findings, preflight.status) le llegan al operador (review Codex
    # #326). ``None`` = el validador no viajó → el panel lo declara "no disponible".
    post_run_raw = report.get("post_run_validation")
    post_run: list[str] | None = (
        [f"{clave}: {valor}" for clave, valor in post_run_raw.items()] if isinstance(post_run_raw, dict) else None
    )

    return {
        "header": {
            "tool": tool,
            "ritual_id": report.get("ritual_id") or "",
            "tool_version": report.get("tool_version") or "",
            "created_at": report.get("created_at") or "",
            "status": status,
            "status_label": _STATUS_LABELS.get(status, _DEFAULT_STATUS_LABEL),
            "degraded": bool(report.get("degraded", False)),
            "degraded_reason": report.get("degraded_reason") or "",
        },
        "changed": {
            "files_touched": files_touched,
            "moves": moves,
            "added": added,
            "removed": removed,
            "order_changed": order_changed,
            "has_changes": bool(files_touched or moves or order_changed),
        },
        "summary": report.get("summary") or "",
        "conflicts_resolved": conflicts,
        "rollback": rollback,
        "post_run": post_run,
    }


def create_flight_report_panel(report: dict[str, Any]) -> None:
    """Renderiza el informe de vuelo de un Ritual mutante ya ejecutado (T-28).

    Banner del veredicto de la transacción + las cuatro secciones de la caja negra
    (qué cambió / por qué / quién ganó / cómo revertir) + el slot del post-run
    validator. VIEW PURO — la lógica vive en :func:`build_flight_report_view_model`;
    esta función es glue fino de NiceGUI.
    """
    vm = build_flight_report_view_model(report)
    header = vm["header"]
    banner_style = _STATUS_STYLES.get(header["status"], _DEFAULT_STATUS_STYLE)

    with ui.element("div").classes("bg-[#0f0f0f] border border-[#1f2937] rounded-2xl p-6 gap-4"):
        # --- Encabezado: qué ritual, cuándo ---
        titulo = f"Informe de vuelo · {header['tool']}"
        if header["ritual_id"]:
            titulo += f" ({header['ritual_id']})"
        ui.label(titulo).classes("text-white font-bold text-lg")
        if header["created_at"]:
            ui.label(header["created_at"]).classes("text-[#6b7280] text-xs")

        # --- Banner del veredicto de la transacción ---
        with ui.element("div").classes(f"{banner_style} border rounded-xl px-4 py-2"):
            ui.label(header["status_label"]).classes("text-sm font-medium")

        # --- Aviso de informe degradado (sin manifiesto) ---
        if header["degraded"]:
            with ui.element("div").classes("bg-[#3a2a00] text-[#fcd34d] border border-[#92400e] rounded-xl px-4 py-2"):
                ui.label(f"⚠ Informe degradado: {header['degraded_reason']}").classes("text-xs")

        changed = vm["changed"]

        # --- Qué cambió: archivos tocados + movimientos/altas/bajas de orden ---
        with ui.element("div").classes("border-l-2 border-[#374151] pl-3 mt-2 gap-1"):
            ui.label("Qué cambió").classes("text-[#d1d5db] text-sm font-semibold")
            if not changed["has_changes"]:
                ui.label("Sin archivos tocados ni cambios de orden.").classes("text-[#9ca3af] text-xs")
            for path in changed["files_touched"]:
                ui.label(path).classes("text-[#9ca3af] text-xs")
            for move in changed["moves"]:
                ui.label(f"{move['plugin']}: {move['from_index']} → {move['to_index']}").classes(
                    "text-[#9ca3af] text-xs"
                )
            for plugin in changed["added"]:
                ui.label(f"+ {plugin} (agregado al orden)").classes("text-[#9ca3af] text-xs")
            for plugin in changed["removed"]:
                ui.label(f"− {plugin} (quitado del orden)").classes("text-[#9ca3af] text-xs")

        # --- Por qué (siempre visible, con empty-state — espejo del renderer Markdown) ---
        with ui.element("div").classes("border-l-2 border-[#374151] pl-3 mt-2 gap-1"):
            ui.label("Por qué").classes("text-[#d1d5db] text-sm font-semibold")
            ui.label(vm["summary"] or "Sin resumen registrado en el manifiesto.").classes("text-[#9ca3af] text-xs")

        # --- Quién ganó cada conflicto (siempre visible, con empty-state) ---
        with ui.element("div").classes("border-l-2 border-[#374151] pl-3 mt-2 gap-1"):
            ui.label("Quién ganó cada conflicto").classes("text-[#d1d5db] text-sm font-semibold")
            if not vm["conflicts_resolved"]:
                ui.label("Sin conflictos forwardeados en este Ritual.").classes("text-[#9ca3af] text-xs")
            for c in vm["conflicts_resolved"]:
                etiqueta = c["record_type"] or "conflicto"
                if c["form_id"]:
                    etiqueta += f" {c['form_id']}"
                perdedores = ", ".join(c["losers"]) or "—"
                ui.label(f"{etiqueta}: {c['winner']} ganó sobre {perdedores}").classes("text-[#9ca3af] text-xs")

        # --- Cómo revertir (siempre visible: distinguir "sin snapshots" de "no cargó") ---
        with ui.element("div").classes("border-l-2 border-[#374151] pl-3 mt-2 gap-1"):
            ui.label("Cómo revertir").classes("text-[#d1d5db] text-sm font-semibold")
            if not vm["rollback"]:
                ui.label("Sin plan de rollback registrado (no hay snapshots que restaurar).").classes(
                    "text-[#9ca3af] text-xs"
                )
            for step in vm["rollback"]:
                # El path del snapshot es el input accionable de recuperación.
                ui.label(f"{step['original_path']} ← {step['snapshot_path']} (snapshot {step['snapshot_id']})").classes(
                    "text-[#9ca3af] text-xs"
                )

        # --- Validación post-run (T-21): enumerada, declarada explícita aunque no esté ---
        with ui.element("div").classes("border-l-2 border-[#374151] pl-3 mt-2 gap-1"):
            ui.label("Validación post-run").classes("text-[#d1d5db] text-sm font-semibold")
            if vm["post_run"] is None:
                ui.label("No disponible — validador post-run (T-21) pendiente.").classes("text-[#9ca3af] text-xs")
            for linea in vm["post_run"] or []:
                ui.label(linea).classes("text-[#9ca3af] text-xs")
