"""Comportamiento del control de elegibilidad de parallax de la lista de mods (#821)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.gui.controllers.ritual_runner import (
    STORE_KEY_RITUAL_FEEDBACK,
    diagnostico_parallax_de,
    limpiar_diagnostico_parallax,
    run_diagnostico_parallax,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.gui.views import forge_dashboard, mod_list

_TIMEOUT = {"success": False, "message": "Plazo agotado. Usá cli elegibilidad.", "alcance": "Lectura estructural."}


async def test_publica_el_progreso_y_muestra_el_plazo_agotado_como_resultado() -> None:
    store = ReactiveStore()
    inicio = asyncio.Event()
    continuar = asyncio.Event()

    async def responder(nombre: str, argumentos: dict[str, str]) -> dict:
        assert nombre == "evaluar_parallax_elegibilidad"
        assert argumentos == {"mod": "Texturas"}
        inicio.set()
        await continuar.wait()
        return _TIMEOUT

    supervisor = MagicMock(dispatch_tool=AsyncMock(side_effect=responder))
    tarea = asyncio.create_task(
        run_diagnostico_parallax("Texturas", supervisor=supervisor, store=store, tab_id="a", elegibilidad=True)
    )
    try:
        # Si falla antes de alcanzar el despacho, deja que pytest muestre esa excepción.
        await asyncio.wait_for(inicio.wait(), timeout=2)
        assert diagnostico_parallax_de(store, "a") == {"mod": "Texturas", "elegibilidad": True, "evaluando": True}
        assert diagnostico_parallax_de(store, "b") is None
        await run_diagnostico_parallax("Otro", supervisor=supervisor, store=store, tab_id="a", elegibilidad=True)
        supervisor.dispatch_tool.assert_awaited_once()
    finally:
        continuar.set()
        await tarea

    assert diagnostico_parallax_de(store, "a") == {"mod": "Texturas", "elegibilidad": True, "resultado": _TIMEOUT}
    assert store.get(STORE_KEY_RITUAL_FEEDBACK) is None


@pytest.mark.parametrize("cerrar", [True, False], ids=["cerrar", "cancelar"])
async def test_el_trabajo_desechado_no_reaparece_en_la_pestana(cerrar: bool) -> None:
    store = ReactiveStore()
    inicio = asyncio.Event()
    continuar = asyncio.Event()

    async def responder(*_args) -> dict:
        inicio.set()
        await continuar.wait()
        return _TIMEOUT

    supervisor = MagicMock(dispatch_tool=AsyncMock(side_effect=responder))
    tarea = asyncio.create_task(
        run_diagnostico_parallax("Texturas", supervisor=supervisor, store=store, tab_id="a", elegibilidad=True)
    )
    try:
        await asyncio.wait_for(inicio.wait(), timeout=2)
    finally:
        if cerrar:
            limpiar_diagnostico_parallax(store, "a")
            continuar.set()
            await tarea
        else:
            tarea.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tarea
    assert diagnostico_parallax_de(store, "a") is None


def test_el_boton_de_la_fila_llama_al_callback_con_su_mod(monkeypatch: pytest.MonkeyPatch) -> None:
    ui = MagicMock()
    monkeypatch.setattr(mod_list, "ui", ui)
    evaluar = MagicMock()

    mod_list._build_mod_row({"name": "Texturas"}, on_toggle=None, on_evaluate_eligibility=evaluar)

    ui.button.assert_called_once()
    ui.button.call_args.kwargs["on_click"]()
    evaluar.assert_called_once_with("Texturas")
    ui.button.return_value.props.return_value.props.assert_called_with('aria-label="Evaluar elegibilidad de parallax"')


def test_el_panel_muestra_alcance_y_detalles_como_texto(monkeypatch: pytest.MonkeyPatch) -> None:
    ui = MagicMock()
    monkeypatch.setattr(forge_dashboard, "ui", ui)
    ruta = "<img src=x onerror=alert(1)>.dds"
    razon = "<em>falta una malla</em>"
    resultado = {
        "success": True,
        "message": "",
        "alcance": "SI no prueba lo que se ve en el juego.",
        "n_texturas": 51,
        "truncado": True,
        "texturas": [
            {"ruta_de_salida": ruta, "efecto": "SI", "razon": "Una forma elegible", "mallas": ["<b>malla</b>.nif"]},
            {"ruta_de_salida": "a.dds", "efecto": "NO_VERIFICADO", "razon": razon},
            {"ruta_de_salida": "b.dds", "efecto": "NO", "razon": "No hay consumo"},
        ],
    }

    forge_dashboard._render_parallax_elegibilidad({"mod": "<b>Mod</b>", "resultado": resultado})

    etiquetas = [llamada.args[0] for llamada in ui.label.call_args_list]
    assert etiquetas[0] == resultado["alcance"]
    assert all(any(texto in etiqueta for etiqueta in etiquetas) for texto in (ruta, razon, "<b>malla</b>.nif", "51"))
    assert any("NO_VERIFICADO" in etiqueta and ".bsa" in etiqueta for etiqueta in etiquetas)
    ui.html.assert_not_called()


async def test_un_diagnostico_anterior_no_pisa_la_evaluacion_del_mismo_tab() -> None:
    store = ReactiveStore()
    inicio_diagnostico = asyncio.Event()
    terminar_diagnostico = asyncio.Event()

    async def responder(nombre: str, _argumentos: dict[str, str]) -> dict:
        if nombre == "diagnosticar_parallax_perfil":
            inicio_diagnostico.set()
            await terminar_diagnostico.wait()
            return {"success": True, "message": ""}
        return {"status": "error", "reason": "Error de evaluación"}

    supervisor = MagicMock(dispatch_tool=AsyncMock(side_effect=responder))
    diagnostico = asyncio.create_task(run_diagnostico_parallax("A", supervisor=supervisor, store=store, tab_id="a"))
    try:
        await asyncio.wait_for(inicio_diagnostico.wait(), timeout=2)
        await run_diagnostico_parallax("B", supervisor=supervisor, store=store, tab_id="a", elegibilidad=True)
    finally:
        terminar_diagnostico.set()
        await diagnostico

    entrada = diagnostico_parallax_de(store, "a")
    assert entrada is not None and entrada["mod"] == "B"
    assert entrada["resultado"]["success"] is False
    assert entrada["resultado"]["message"] == "Error de evaluación"


async def test_un_diagnostico_durante_evaluacion_pendiente_no_la_pisa() -> None:
    store = ReactiveStore()
    inicio_elegibilidad = asyncio.Event()
    terminar_elegibilidad = asyncio.Event()

    async def responder(nombre: str, _argumentos: dict[str, str]) -> dict:
        if nombre == "evaluar_parallax_elegibilidad":
            inicio_elegibilidad.set()
            await terminar_elegibilidad.wait()
            return {"success": True, "message": "", "alcance": "solo archivos sueltos", "texturas": []}
        return {"success": True, "message": "", "n_texturas": 1, "n_hallazgos": 0, "hallazgos": [], "perfil": "P"}

    supervisor = MagicMock(dispatch_tool=AsyncMock(side_effect=responder))
    elegibilidad = asyncio.create_task(
        run_diagnostico_parallax("E", supervisor=supervisor, store=store, tab_id="a", elegibilidad=True)
    )
    try:
        await asyncio.wait_for(inicio_elegibilidad.wait(), timeout=2)
        await run_diagnostico_parallax("D", supervisor=supervisor, store=store, tab_id="a")
    finally:
        terminar_elegibilidad.set()
        await elegibilidad

    entrada = diagnostico_parallax_de(store, "a")
    assert entrada is not None and entrada["mod"] == "E"
    assert entrada.get("elegibilidad") is True
    assert supervisor.dispatch_tool.await_count == 1
