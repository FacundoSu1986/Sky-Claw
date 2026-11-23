"""Guía de la etapa 9 en la GUI (#997): «Antes de arrancar» en la tarjeta de DynDOLOD y el paso actual en vivo.

Pensado para un operador que NO conoce el sistema: cada renglón dice qué pasa y qué hacer, en una línea y en
criollo; el detalle técnico (rutas, códigos) queda en un desplegable CERRADO. La lógica de display vive en funciones
puras (``vista_de_checklist`` / ``vista_de_fase``, unit-testeadas); el resto es glue fino de NiceGUI.

La vista NO decide: el checklist lo arma y lo hace cumplir el servicio. Lo único que agrega es lo que solo ella sabe —
su modo Desatendido no tiene override de etapas previas (#9)— para ofrecer el Asistido antes de que el dispatcher
rechace la corrida. No es un aviso descartable: no forma parte de la familia de avisos con cruz de #996.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

from nicegui import ui

from sky_claw.config import GUI_EXIT_WATCHDOG_GRACE_SECONDS

#: #2 del Anexo A: la GUI se apagaba sola sin que el operador supiera por qué. El plazo sale de la config (una fuente).
RECORDATORIO_DE_LA_PESTANA = (
    "Dejá esta pestaña abierta durante toda la corrida: si la cerrás, Sky-Claw se apaga solo a los "
    f"{int(GUI_EXIT_WATCHDOG_GRACE_SECONDS)} segundos."
)

#: Marcas del checklist: glifo + palabra + color. Sin emojis: el shell los reemplazó por glifos del tema porque el
#: emoji se pinta con la pila de color del SO (``test_shell_forge_inventario_de_glifos_congelado``; este módulo entra
#: en el mismo inventario por ``tests/test_gui_guia_etapa9.py``). La palabra va siempre: el color solo no alcanza.
_MARCAS: dict[str, dict[str, str]] = {
    "listo": {"glifo": "✓", "palabra": "Listo", "color": "#9bbf8e"},
    "falta": {"glifo": "■", "palabra": "Falta", "color": "#e57373"},
    "ojo": {"glifo": "▲", "palabra": "Ojo", "color": "#e0b341"},
    "sin_revisar": {"glifo": "○", "palabra": "No pude revisarlo", "color": "#8a8270"},
}
_BOTON_BLOQUEADO = "Primero arreglá lo que falta"

#: Fases en las que el operador NO tiene que tocar nada en la herramienta: el banner se ve como alerta.
_FASES_DE_ALERTA = frozenset({"esperando_tu_aprobacion", "verificando_output", "cortada"})
_FASES_DE_EXITO = frozenset({"podes_apretar_start"})


def _marca(item: dict[str, Any]) -> str:
    estado = item.get("estado")
    if estado == "ok":
        return "listo"
    if estado == "no_verificable":
        return "sin_revisar"
    return "falta" if item.get("bloquea") else "ojo"


def vista_de_checklist(checklist: dict[str, Any] | None, *, modo: str) -> dict[str, Any]:
    """``ChecklistDeEtapa9.to_dict()`` → lo que pinta la tarjeta. Puro y defensivo.

    Sin revisar todavía (``None``) o con la revisión caída, el botón NO se frena: la autoridad es el servicio, que
    corre el checklist igual al ejecutar. Lo que «falta» (un bloqueante) deshabilita el botón; en Desatendido, las
    etapas previas faltantes también, porque esa superficie no tiene a quién preguntarle (se ofrece pasar a Asistido).
    """
    vacia: dict[str, Any] = {
        "puede_arrancar": True,
        "items": [],
        "ofrecer_asistido": False,
        "boton": None,
        "error": None,
    }
    if not checklist:
        return vacia
    if checklist.get("error"):
        return {**vacia, "error": str(checklist["error"])}
    items: list[dict[str, str]] = []
    frena = False
    ofrecer_asistido = False
    for item in checklist.get("items") or []:
        marca = _marca(item)
        if item.get("bloquea"):
            frena = True
        if item.get("id") == "etapas_previas" and item.get("estado") == "falla" and modo == "desatendido":
            frena = True
            ofrecer_asistido = True
            marca = "falta"
        items.append(
            {
                "marca": marca,
                "titulo": str(item.get("titulo") or ""),
                # El arreglo solo acompaña una falla: ni a lo que pasó ni a lo que no se pudo revisar (#1007).
                "arreglo": str(item.get("arreglo") or "") if item.get("estado") == "falla" else "",
                "detalle_tecnico": str(item.get("detalle_tecnico") or ""),
            }
        )
    return {
        "puede_arrancar": not frena,
        "items": items,
        "ofrecer_asistido": ofrecer_asistido,
        "boton": _BOTON_BLOQUEADO if frena else None,
        "error": None,
    }


def _duracion(segundos: int) -> str:
    minutos, seg = divmod(max(0, int(segundos)), 60)
    return f"{minutos} min {seg} s" if minutos else f"{seg} s"


def vista_de_fase(hechos: dict[str, Any]) -> dict[str, Any]:
    """``FaseEnCurso.hechos`` → el banner del paso actual."""
    fase = str(hechos.get("fase") or "")
    return {
        "texto": str(hechos.get("texto") or ""),
        "hace": f"hace {_duracion(int(hechos.get('hace_segundos') or 0))}",
        "alerta": fase in _FASES_DE_ALERTA,
        "exito": fase in _FASES_DE_EXITO,
    }


def html_del_paso(hechos: dict[str, Any]) -> str:
    """El banner del paso actual como fragmento HTML (lo concatena el panel vivo de la etapa 9)."""
    vista = vista_de_fase(hechos)
    color = "#f0b35a" if vista["alerta"] else ("#9bbf8e" if vista["exito"] else "#e7d6ad")
    return (
        f'<div role="status" style="min-width:0; overflow-wrap:anywhere; padding:10px 12px; border-radius:4px;'
        f' border:1px solid {color}; background:rgba(0,0,0,.28);">'
        f"<div style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.14em; color:{color};\">"
        "QUÉ HACER AHORA</div>"
        f"<div style=\"font-family:'EB Garamond',serif; font-size:15px; line-height:1.45; color:#f1e7cf;\">"
        f"{html.escape(vista['texto'])}</div>"
        f"<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:#8a8270;\">"
        f"{html.escape(vista['hace'])}</div></div>"
    )


def _html_del_item(item: dict[str, str]) -> str:
    detalle = (
        '<details style="margin-top:2px;"><summary style="cursor:pointer; font-family:\'Spline Sans Mono\','
        'monospace; font-size:10px; color:#8a8270;">Ver detalle técnico</summary>'
        "<div style=\"font-family:'Spline Sans Mono',monospace; font-size:10px; line-height:1.4; color:#8a8270;"
        f' overflow-wrap:anywhere;">{html.escape(item["detalle_tecnico"])}</div></details>'
        if item["detalle_tecnico"]
        else ""
    )
    arreglo = (
        "<div style=\"font-family:'EB Garamond',serif; font-size:12.5px; line-height:1.35; color:#c9bd9f;\">"
        f"{html.escape(item['arreglo'])}</div>"
        if item["arreglo"]
        else ""
    )
    marca = _MARCAS[item["marca"]]
    return (
        '<div style="display:flex; gap:7px; align-items:flex-start; min-width:0;">'
        f"<span style=\"flex-shrink:0; font-family:'Spline Sans Mono',monospace; font-size:10.5px; color:{marca['color']};"
        f' white-space:nowrap;"><span aria-hidden="true">{marca["glifo"]}</span> {html.escape(marca["palabra"])}</span>'
        '<div style="flex:1; min-width:0; overflow-wrap:anywhere;">'
        "<div style=\"font-family:'EB Garamond',serif; font-size:13px; line-height:1.35; color:#e8e2d4;\">"
        f"{html.escape(item['titulo'])}</div>{arreglo}{detalle}</div></div>"
    )


def crear_guia_de_la_tarjeta(
    checklist: dict[str, Any] | None,
    *,
    modo: str,
    on_revisar: Callable[[], Any] | None,
    on_usar_asistido: Callable[[], Any] | None,
) -> dict[str, Any]:
    """Pinta «Antes de arrancar» en la tarjeta de DynDOLOD y devuelve la vista (el botón de la tarjeta la lee)."""
    vista = vista_de_checklist(checklist, modo=modo)
    with ui.element("div").style(
        "display:flex; flex-direction:column; gap:6px; min-width:0; padding:8px 10px; border-radius:4px;"
        "background:rgba(0,0,0,.25); border:1px solid rgba(200,168,106,.22);"
    ):
        ui.html(
            "<div style=\"font-family:'Cinzel',serif; font-size:11px; letter-spacing:.12em; color:#c8a86a;\">"
            "ANTES DE ARRANCAR</div>"
            f"<div style=\"font-family:'EB Garamond',serif; font-size:12px; line-height:1.35; color:#a39a85;\">"
            f"{html.escape(RECORDATORIO_DE_LA_PESTANA)}</div>"
        )
        if vista["error"]:
            ui.html(
                f"<div style=\"font-family:'EB Garamond',serif; font-size:12.5px; color:#e0b341;\">"
                f"{html.escape(vista['error'])}</div>"
            )
        elif not vista["items"]:
            ui.html(
                "<div style=\"font-family:'EB Garamond',serif; font-size:12.5px; color:#a39a85;\">"
                "Todavía no revisé tu instalación. Tocá «Revisar» antes de arrancar.</div>"
            )
        else:
            ui.html("".join(_html_del_item(item) for item in vista["items"]))
        with ui.row().classes("gap-2"):
            if on_revisar is not None:
                revisar = ui.element("button").classes("sc-btn").style("padding:5px 10px; font-size:11px;")
                revisar.on("click", lambda _=None: on_revisar())
                with revisar:
                    ui.html("Revisar de nuevo" if vista["items"] else "Revisar")
            if vista["ofrecer_asistido"] and on_usar_asistido is not None:
                asistido = (
                    ui.element("button").classes("sc-btn sc-btn--gold").style("padding:5px 10px; font-size:11px;")
                )
                asistido.on("click", lambda _=None: on_usar_asistido())
                with asistido:
                    ui.html("Usar Asistido")
    return vista
