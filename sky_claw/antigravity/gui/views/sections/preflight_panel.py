"""Operations Hub panel for the aggregated preflight semaphore (T-16).

El panel consume el contrato ``PreflightReport.to_dict()`` que expone el
agregador :class:`sky_claw.local.validators.preflight.PreflightService` — el
semáforo verde/amarillo/rojo que gobierna a los Rituales mutantes (T-15). Es la
"puerta de entrada": se muestra ANTES de lanzar cualquier Ritual, con un banner
del veredicto agregado (incluido el bloqueo cuando ``blocks_mutations``) y un
bloque por sensor (vfs, LOOT, masters, límites, overwrite, permisos, composición)
con su severidad, resumen y los ``details`` accionables.

VIEW PURO — la lógica de display vive en :func:`build_preflight_view_model` (un
transform puro, unit-testeado); :func:`create_preflight_panel` es glue fino de
NiceGUI.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

#: Etiqueta del banner agregado por estado del semáforo. El banner nunca sale
#: vacío: un estado desconocido cae al mensaje verde.
_BANNER_LABELS: dict[str, str] = {
    "green": "Listo para lanzar: preflight en verde.",
    "yellow": "Precaución: revisá las advertencias antes de lanzar.",
    "red": "Bloqueado: resolvé los checks en rojo antes de lanzar el Ritual.",
}

#: Estilo (punto de color del check + fondo/borde del banner) por estado. Mismo
#: lenguaje visual que el resto del Operations Hub.
_STATUS_STYLES: dict[str, dict[str, str]] = {
    "green": {"dot": "text-[#22c55e]", "banner": "bg-[#052e16] text-[#86efac] border-[#166534]"},
    "yellow": {"dot": "text-[#f59e0b]", "banner": "bg-[#3a2a00] text-[#fcd34d] border-[#92400e]"},
    "red": {"dot": "text-[#ef4444]", "banner": "bg-[#3f1212] text-[#fca5a5] border-[#991b1b]"},
}
_DEFAULT_STYLE: dict[str, str] = {"dot": "text-[#9ca3af]", "banner": "bg-[#0f0f0f] text-[#9ca3af] border-[#1f2937]"}


def build_preflight_view_model(report: dict[str, Any]) -> dict[str, Any]:
    """Transforma un ``PreflightReport.to_dict()`` en un modelo listo para render.

    Puro y defensivo: campos ausentes colapsan a vacíos/verde, así el panel
    renderiza aunque el reporte llegue parcial, y el banner agregado nunca sale
    vacío (criterio "visible antes de lanzar cualquier Ritual").
    """
    status = report.get("status") or "green"
    blocks = bool(report.get("blocks_mutations", False))
    checks = [
        {
            "name": check.get("name", ""),
            "status": check.get("status") or "green",
            "summary": check.get("summary") or "",
            # ``details`` puede faltar en un payload parcial; nunca ``None``.
            "details": list(check.get("details") or []),
        }
        for check in report.get("checks") or []
    ]
    return {
        "status": status,
        "blocks_mutations": blocks,
        "banner": {
            "status": status,
            "blocks": blocks,
            "label": _BANNER_LABELS.get(status, _BANNER_LABELS["green"]),
        },
        "checks": checks,
    }


def create_preflight_panel(report: dict[str, Any]) -> None:
    """Renderiza el semáforo de preflight antes de lanzar un Ritual mutante (T-16).

    Muestra el banner del veredicto agregado + un bloque por sensor (punto de
    color según severidad, ``summary`` y los ``details`` accionables como
    bullets). VIEW PURO — la lógica vive en :func:`build_preflight_view_model`;
    esta función es glue fino de NiceGUI.
    """
    vm = build_preflight_view_model(report)
    banner_style = _STATUS_STYLES.get(vm["status"], _DEFAULT_STYLE)["banner"]

    with ui.element("div").classes("bg-[#0f0f0f] border border-[#1f2937] rounded-2xl p-6 gap-4"):
        ui.label("Preflight").classes("text-white font-bold text-lg")

        # --- Banner agregado: el veredicto del semáforo de un vistazo ---
        with ui.element("div").classes(f"{banner_style} border rounded-xl px-4 py-2"):
            ui.label(vm["banner"]["label"]).classes("text-sm font-medium")

        # --- Un bloque por sensor: severidad + resumen + remediación ---
        for check in vm["checks"]:
            dot = _STATUS_STYLES.get(check["status"], _DEFAULT_STYLE)["dot"]
            with ui.element("div").classes("border-l-2 border-[#374151] pl-3 mt-2 gap-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.label("●").classes(f"{dot} text-xs")
                    ui.label(f"{check['name']}: {check['summary']}").classes("text-[#d1d5db] text-sm font-medium")
                for detail in check["details"]:
                    ui.label(detail).classes("text-[#9ca3af] text-xs")
