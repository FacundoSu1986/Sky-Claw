"""NP-A0i: el botón «Diagnosticar parallax» de la lista de mods (cierra #817).

La tool ``diagnosticar_parallax_perfil`` ya estaba registrada en el dispatcher de la GUI y en el registro del agente (#815), pero ningún
control la llamaba. Este módulo prueba el controlador (sin NiceGUI, como el resto de las suites del controlador de rituales): despacha
por el ÚNICO camino de la GUI al dispatcher (``ritual_runner``), publica el resultado en el slot de la pestaña que lo pidió (sin pisar el
de otra), nunca propaga una excepción al loop y formatea lo que muestra la vista. Lo visual (que el botón y el panel se dibujen) NO se
prueba acá: lo cubre el smoke de la vista.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.gui.controllers import ritual_runner
from sky_claw.app.gui.controllers.ritual_runner import (
    NOMBRE_TOOL_DIAGNOSTICO_PARALLAX,
    STORE_KEY_DIAGNOSTICO_PARALLAX,
    STORE_KEY_RITUAL_FEEDBACK,
    diagnostico_parallax_de,
    limpiar_diagnostico_parallax,
    lineas_del_diagnostico_parallax,
    run_diagnostico_parallax,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.orchestrator.tool_strategies.diagnosticar_perfil_parallax import DiagnosticarPerfilParallaxStrategy

_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parent.parent

_OK_SIN_HALLAZGOS: dict[str, Any] = {
    "success": True,
    "message": "",
    "perfil": "Default",
    "mod": "Texturas",
    "n_habilitados": 12,
    "n_texturas": 4,
    "n_hallazgos": 0,
    "hallazgos": [],
    "truncado": False,
    "impide_medir": False,
    "alcance": "Vista estructural del modlist.txt.",
}


def _supervisor(resultado: Any = None) -> MagicMock:
    sup = MagicMock()
    sup.dispatch_tool = AsyncMock(return_value=_OK_SIN_HALLAZGOS if resultado is None else resultado)
    return sup


@pytest.fixture
def store() -> ReactiveStore:
    return ReactiveStore()


async def test_despacha_la_tool_con_el_mod_y_publica_el_resultado_en_el_slot_de_la_pestana(
    store: ReactiveStore,
) -> None:
    sup = _supervisor()

    await run_diagnostico_parallax("Texturas", supervisor=sup, store=store, tab_id="t1")

    sup.dispatch_tool.assert_awaited_once_with(NOMBRE_TOOL_DIAGNOSTICO_PARALLAX, {"mod": "Texturas"})
    assert diagnostico_parallax_de(store, "t1") == {"mod": "Texturas", "resultado": _OK_SIN_HALLAZGOS}


async def test_el_resultado_de_una_pestana_no_pisa_ni_limpia_el_de_otra(store: ReactiveStore) -> None:
    await run_diagnostico_parallax(
        "A", supervisor=_supervisor({**_OK_SIN_HALLAZGOS, "mod": "A"}), store=store, tab_id="t1"
    )
    await run_diagnostico_parallax(
        "B", supervisor=_supervisor({**_OK_SIN_HALLAZGOS, "mod": "B"}), store=store, tab_id="t2"
    )

    assert diagnostico_parallax_de(store, "t1") is not None and diagnostico_parallax_de(store, "t1")["mod"] == "A"  # type: ignore[index]
    assert diagnostico_parallax_de(store, "t2")["mod"] == "B"  # type: ignore[index]

    limpiar_diagnostico_parallax(store, "t1")

    assert diagnostico_parallax_de(store, "t1") is None
    assert diagnostico_parallax_de(store, "t2") is not None


async def test_una_pestana_sin_identidad_no_ve_el_resultado_de_otra(store: ReactiveStore) -> None:
    await run_diagnostico_parallax("A", supervisor=_supervisor(), store=store, tab_id="t1")

    assert diagnostico_parallax_de(store, None) is None
    assert diagnostico_parallax_de(store, "otra") is None


async def test_sin_supervisor_avisa_y_no_publica(store: ReactiveStore) -> None:
    await run_diagnostico_parallax("A", supervisor=None, store=store, tab_id="t1")

    assert diagnostico_parallax_de(store, "t1") is None
    assert "daemon" in store.get(STORE_KEY_RITUAL_FEEDBACK)["text"]


@pytest.mark.parametrize("mod", ["", "   ", None, 3])
async def test_un_nombre_de_mod_invalido_no_despacha(store: ReactiveStore, mod: Any) -> None:
    sup = _supervisor()

    await run_diagnostico_parallax(mod, supervisor=sup, store=store, tab_id="t1")

    sup.dispatch_tool.assert_not_awaited()
    assert diagnostico_parallax_de(store, "t1") is None


async def test_si_el_despacho_falla_no_propaga_y_avisa(store: ReactiveStore) -> None:
    sup = MagicMock()
    sup.dispatch_tool = AsyncMock(side_effect=RuntimeError("boom"))

    await run_diagnostico_parallax("A", supervisor=sup, store=store, tab_id="t1")  # no debe levantar

    assert diagnostico_parallax_de(store, "t1") is None
    feedback = store.get(STORE_KEY_RITUAL_FEEDBACK)
    assert feedback["type"] == "negative" and "RuntimeError" in feedback["text"]


async def test_un_resultado_que_no_es_un_dict_no_se_publica_y_avisa(store: ReactiveStore) -> None:
    await run_diagnostico_parallax("A", supervisor=_supervisor(["no", "es", "un", "dict"]), store=store, tab_id="t1")

    assert diagnostico_parallax_de(store, "t1") is None
    assert store.get(STORE_KEY_RITUAL_FEEDBACK)["type"] == "negative"


async def test_un_click_del_operador_rearma_el_cortacircuitos_del_agente(store: ReactiveStore) -> None:
    """Igual que un ritual: repetir el mismo click a mano no se confunde con un bucle del agente."""
    sup = _supervisor()

    await run_diagnostico_parallax("A", supervisor=sup, store=store, tab_id="t1")

    sup.reset_loop_guardrail.assert_called_once_with()


def test_las_lineas_de_un_resultado_limpio_resumen_y_dicen_el_alcance() -> None:
    lineas = lineas_del_diagnostico_parallax(_OK_SIN_HALLAZGOS)

    assert any("Texturas" in linea and "4" in linea for linea in lineas)
    assert any("ningún" in linea.lower() or "sin hallazgos" in linea.lower() for linea in lineas)
    assert lineas[-1] == "Vista estructural del modlist.txt."


def test_las_lineas_de_un_resultado_con_hallazgos_los_listan_y_avisan_si_impiden_medir_o_se_recortaron() -> None:
    resultado = {
        **_OK_SIN_HALLAZGOS,
        "n_hallazgos": 120,
        "truncado": True,
        "impide_medir": True,
        "hallazgos": [{"tipo": "SALIDA TAPADA", "ruta": "textures/a_p.dds", "detalle": "la gana 'Control'"}],
    }

    lineas = lineas_del_diagnostico_parallax(resultado)

    assert any("SALIDA TAPADA" in linea and "textures/a_p.dds" in linea and "Control" in linea for linea in lineas)
    assert any("impide" in linea.lower() for linea in lineas)
    assert any("120" in linea and "1" in linea for linea in lineas)  # total real y cuántos se muestran


def test_las_lineas_de_un_fallo_muestran_el_motivo() -> None:
    lineas = lineas_del_diagnostico_parallax({"success": False, "message": "mod no habilitado: X", "alcance": "A."})

    assert any("mod no habilitado: X" in linea for linea in lineas)


@pytest.mark.parametrize("basura", [None, {}, [], "texto", {"success": True}])
def test_las_lineas_de_una_forma_inesperada_no_levantan(basura: Any) -> None:
    assert isinstance(lineas_del_diagnostico_parallax(basura), list)


# ---------------------------------------------------------------------------
# Casos de borde que la mutación encontró sin cubrir (store malformado, avisos, formas inesperadas)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basura", ["texto", 3, ["lista"], {"por_pestana": "basura"}, {"por_pestana": ["x"]}, {}])
def test_un_store_malformado_no_rompe_ni_inventa_un_diagnostico(store: ReactiveStore, basura: Any) -> None:
    store.set(STORE_KEY_DIAGNOSTICO_PARALLAX, basura)

    assert diagnostico_parallax_de(store, "t1") is None
    limpiar_diagnostico_parallax(store, "t1")  # no debe levantar


def test_el_slot_sin_dueno_nunca_se_lee_ni_se_limpia(store: ReactiveStore) -> None:
    """Un slot bajo la clave ``None`` (no debería existir) no se le entrega ni se le quita a una pestaña sin identidad."""
    store.set(STORE_KEY_DIAGNOSTICO_PARALLAX, {"por_pestana": {None: {"mod": "X", "resultado": _OK_SIN_HALLAZGOS}}})

    assert diagnostico_parallax_de(store, None) is None
    limpiar_diagnostico_parallax(store, None)
    assert store.get(STORE_KEY_DIAGNOSTICO_PARALLAX) == {
        "por_pestana": {None: {"mod": "X", "resultado": _OK_SIN_HALLAZGOS}}
    }


async def test_un_nombre_invalido_avisa_con_un_warning(store: ReactiveStore) -> None:
    await run_diagnostico_parallax("  ", supervisor=_supervisor(), store=store, tab_id="t1")

    feedback = store.get(STORE_KEY_RITUAL_FEEDBACK)
    assert feedback["type"] == "warning" and "mod" in feedback["text"]


async def test_un_supervisor_sin_cortacircuitos_tambien_funciona(store: ReactiveStore) -> None:
    sup = SimpleNamespace(dispatch_tool=AsyncMock(return_value=_OK_SIN_HALLAZGOS))  # sin reset_loop_guardrail

    await run_diagnostico_parallax("A", supervisor=sup, store=store, tab_id="t1")

    assert diagnostico_parallax_de(store, "t1") is not None


async def test_un_fallo_del_despacho_queda_en_el_log(store: ReactiveStore, caplog: pytest.LogCaptureFixture) -> None:
    sup = MagicMock()
    sup.dispatch_tool = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level("ERROR"):
        await run_diagnostico_parallax("A", supervisor=sup, store=store, tab_id="t1")

    assert any("Diagnóstico de parallax" in r.getMessage() for r in caplog.records)


async def test_sin_identidad_de_pestana_no_hay_slot_y_se_avisa_sin_perder_el_motivo(store: ReactiveStore) -> None:
    await run_diagnostico_parallax("A", supervisor=_supervisor(), store=store, tab_id=None)

    assert store.get(STORE_KEY_DIAGNOSTICO_PARALLAX) is None
    feedback = store.get(STORE_KEY_RITUAL_FEEDBACK)
    assert feedback["type"] == "warning" and "pestaña" in feedback["text"]


async def test_la_identidad_de_pestana_no_se_filtra_al_contexto_del_llamador(store: ReactiveStore) -> None:
    """El dueño se arma POR TASK y se restaura: un click no puede dejar la identidad armada para lo que corra después."""
    antes = ritual_runner.ritual_tab_id()

    await run_diagnostico_parallax("A", supervisor=_supervisor(), store=store, tab_id="t1")

    assert ritual_runner.ritual_tab_id() == antes
    assert ritual_runner._gui_pending_request_id.get() is None


async def test_restaura_el_valor_previo_del_seguimiento_de_aprobacion(store: ReactiveStore) -> None:
    """Restaurar es devolver lo que había, no dejarlo en el valor por defecto."""
    token = ritual_runner._gui_pending_request_id.set("previa")
    try:
        await run_diagnostico_parallax("A", supervisor=_supervisor(), store=store, tab_id="t1")

        assert ritual_runner._gui_pending_request_id.get() == "previa"
    finally:
        ritual_runner._gui_pending_request_id.reset(token)


async def test_no_limpia_una_aprobacion_pendiente_que_no_parkeo(store: ReactiveStore) -> None:
    """Esta tool no pide aprobación: lo que ya estaba pendiente (aun con el mismo dueño) no es suyo y no se toca."""
    ajena = {"request_id": "req-x", ritual_runner.HITL_OWNER_TAB: "t1"}
    store.set(ritual_runner.STORE_KEY_PENDING_HITL, dict(ajena))

    await run_diagnostico_parallax("A", supervisor=_supervisor(), store=store, tab_id="t1")

    assert store.get(ritual_runner.STORE_KEY_PENDING_HITL) == ajena


def test_un_resultado_sin_n_texturas_dice_cero_textura() -> None:
    lineas = lineas_del_diagnostico_parallax({"success": True, "mod": "M", "perfil": "P", "n_hallazgos": 0})

    assert any("0 textura" in linea for linea in lineas)


def test_las_lineas_no_afirman_lo_que_el_resultado_no_dice() -> None:
    """Sin ``impide_medir``, sin ``truncado`` y sin ``alcance`` no aparecen esos avisos ni una línea vacía final."""
    resultado = {
        "success": True,
        "mod": "M",
        "perfil": "P",
        "n_texturas": 2,
        "n_hallazgos": 1,
        "truncado": False,
        "impide_medir": False,
        "hallazgos": [{"tipo": "SALIDA TAPADA", "ruta": "r", "detalle": "d"}],
    }

    lineas = lineas_del_diagnostico_parallax(resultado)

    assert not any("impide" in linea.lower() for linea in lineas)
    assert not any("en total" in linea for linea in lineas)
    assert lineas[-1] != "" and all(isinstance(linea, str) and linea for linea in lineas)


def test_un_hallazgo_que_no_es_un_dict_se_ignora_sin_romper_el_resto() -> None:
    resultado = {
        "success": True,
        "mod": "M",
        "perfil": "P",
        "n_texturas": 1,
        "n_hallazgos": 2,
        "hallazgos": ["texto suelto", {"tipo": "ENTRADA TAPADA", "ruta": "r", "detalle": "d"}],
    }

    lineas = lineas_del_diagnostico_parallax(resultado)

    assert any("[ENTRADA TAPADA] r: d" in linea for linea in lineas)
    assert not any("texto suelto" in linea for linea in lineas)


def test_la_tool_se_llama_igual_en_las_tres_superficies() -> None:
    """GUI (este módulo), dispatcher y agente LLM: un nombre distinto por superficie las desincroniza sin que nadie lo note."""
    assert DiagnosticarPerfilParallaxStrategy.name == NOMBRE_TOOL_DIAGNOSTICO_PARALLAX
    registro = AsyncToolRegistry(registry=MagicMock(), mo2=MagicMock(), sync_engine=MagicMock())
    assert NOMBRE_TOOL_DIAGNOSTICO_PARALLAX in registro.tools


def _llamadas(nombre_atributo: str) -> set[str]:
    encontrados: set[str] = set()
    for archivo in (_RAIZ_DEL_REPO / "sky_claw").rglob("*.py"):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == nombre_atributo
            ):
                encontrados.add(archivo.relative_to(_RAIZ_DEL_REPO).as_posix())
    return encontrados


def test_la_gui_llega_al_dispatcher_por_un_unico_camino() -> None:
    """``dispatch_tool`` solo se llama desde ``ritual_runner``: el botón nuevo no abre un segundo camino (#817)."""
    assert _llamadas("dispatch_tool") == {"sky_claw/app/gui/controllers/ritual_runner.py"}


def test_el_slot_del_diagnostico_se_escribe_desde_un_unico_lugar() -> None:
    """Un ``store.set`` suelto sobre esta clave haría un replace global y pisaría el resultado de otra pestaña."""
    fuente = pathlib.Path(ritual_runner.__file__).read_text(encoding="utf-8")
    escrituras = [
        nodo
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "set"
        and nodo.args
        and isinstance(nodo.args[0], ast.Name)
        and nodo.args[0].id == "STORE_KEY_DIAGNOSTICO_PARALLAX"
    ]
    assert len(escrituras) == 1
    assert STORE_KEY_DIAGNOSTICO_PARALLAX == "parallax_diagnostico"
