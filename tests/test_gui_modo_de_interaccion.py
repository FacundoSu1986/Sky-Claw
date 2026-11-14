"""P-3c (H-12): la GUI elige el modo de interacción de la etapa 9, y «Continuar» hereda el de la corrida.

Antes la GUI despachaba ``generate_lods`` con payload vacío: SIEMPRE asistido, sin forma de pedir el desatendido que el
agente remoto ya podía pedir. Ahora el operador elige el modo en la tarjeta de DynDOLOD y el despacho lo lleva
explícito. El hermano en el mismo archivo es «Continuar DynDOLOD» (``resume_action_from_result``): también despacha
``generate_lods``, y si no heredara el modo, una corrida desatendida continuaría como asistida.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.app.gui.controllers import ritual_runner as rr
from sky_claw.local.tools.dyndolod_uia_contrato import ModoDeInteraccion

RAIZ = pathlib.Path(__file__).resolve().parents[1]
ESTRATEGIAS = RAIZ / "sky_claw" / "app" / "orchestrator" / "tool_strategies"
GUI = RAIZ / "sky_claw" / "app" / "gui"


# ------------------------------------------------------------------------------------------ el vocabulario


def test_los_modos_de_la_gui_son_exactamente_los_del_contrato() -> None:
    assert rr.MODOS_DE_INTERACCION == ("asistido", "desatendido")
    assert tuple(modo.value for modo in ModoDeInteraccion) == rr.MODOS_DE_INTERACCION
    assert rr.MODO_DE_INTERACCION_POR_DEFECTO == "asistido"


@pytest.mark.parametrize("valor", ["asistido", "desatendido"])
def test_un_modo_valido_se_conserva(valor: str) -> None:
    assert rr.modo_de_interaccion_o_defecto(valor) == valor


@pytest.mark.parametrize(
    "valor",
    [
        None,
        "",
        "Asistido",
        "DESATENDIDO",
        "unattended",
        " asistido",
        1,
        True,
        ["asistido"],
        ModoDeInteraccion.DESATENDIDO,
    ],
    ids=["none", "vacio", "capitalizado", "mayusculas", "alias", "con_espacio", "int", "bool", "lista", "enum"],
)
def test_un_valor_que_no_es_un_modo_exacto_cae_al_defecto_y_nunca_se_adivina(valor: object) -> None:
    """Adivinar ``desatendido`` sería mutar una GUI que nadie pidió mutar: lo desconocido es el modo histórico."""
    assert rr.modo_de_interaccion_o_defecto(valor) == "asistido"


# ----------------------------------------------------------------------------------- el payload de «Generar»


@pytest.mark.parametrize("modo", ["asistido", "desatendido"])
def test_generar_dyndolod_lleva_el_modo_elegido_de_forma_explicita(modo: str) -> None:
    assert rr.payload_de_generar("dyndolod", modo) == {"interaction_mode": modo}


def test_generar_dyndolod_con_un_modo_invalido_cae_al_asistido_explicito() -> None:
    assert rr.payload_de_generar("dyndolod", "otro") == {"interaction_mode": "asistido"}
    assert rr.payload_de_generar("dyndolod", None) == {"interaction_mode": "asistido"}


def test_los_rituales_sin_modo_conservan_el_payload_vacio_historico() -> None:
    sin_modo = sorted(k for k in rr.RITUAL_TOOL_MAP if k not in rr.RITUALES_CON_MODO_DE_INTERACCION)

    assert sin_modo, "el ancla no enumeró ningún ritual sin modo"
    for tool_key in sin_modo:
        assert rr.payload_de_generar(tool_key, "desatendido") is None, tool_key


def test_los_rituales_con_modo_son_exactamente_los_cuya_strategy_acepta_interaction_mode() -> None:
    """Enumera: una strategy nueva que acepte ``interaction_mode`` rompe esto hasta decidir si la GUI ofrece el selector."""
    strategies_con_modo = {
        archivo.stem
        for archivo in ESTRATEGIAS.glob("*.py")
        if any(
            isinstance(nodo, ast.Constant) and nodo.value == "interaction_mode"
            for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8")))
        )
    }
    rituales_con_modo = {rr.RITUAL_TOOL_MAP[k] for k in rr.RITUALES_CON_MODO_DE_INTERACCION}

    assert strategies_con_modo == {"generate_lods"}
    assert rituales_con_modo == {"generate_lods"}
    # y al revés: todo ritual que despacha una strategy con modo está en el conjunto
    assert {k for k, tool in rr.RITUAL_TOOL_MAP.items() if tool == "generate_lods"} == set(
        rr.RITUALES_CON_MODO_DE_INTERACCION
    )


# ------------------------------------------------------------------------- «Continuar» hereda el modo


def _handoff(**extra: object) -> dict[str, object]:
    return {"success": False, "needs_deployment": True, "texgen_mod_path": "C:/MO2/mods/TexGen Output", **extra}


@pytest.mark.parametrize("modo", ["asistido", "desatendido"])
def test_continuar_hereda_el_modo_de_la_corrida_que_produjo_el_handoff(modo: str) -> None:
    accion = rr.resume_action_from_result("dyndolod", _handoff(interaction_mode=modo))

    assert accion is not None
    assert accion["payload"] == {"run_texgen": False, "interaction_mode": modo}


@pytest.mark.parametrize(
    "extra",
    [{}, {"interaction_mode": None}, {"interaction_mode": "otro"}, {"interaction_mode": True}, {"interaction_mode": 3}],
    ids=["ausente", "none", "desconocido", "bool", "int"],
)
def test_continuar_no_inventa_un_modo_que_el_resultado_no_trae(extra: dict[str, object]) -> None:
    """Sin un modo válido en el resultado se conserva el payload histórico: el servicio decide su default."""
    accion = rr.resume_action_from_result("dyndolod", _handoff(**extra))

    assert accion is not None
    assert accion["payload"] == {"run_texgen": False}


# -------------------------------------------------------------------------------------------- el cableado


def _arbol(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def _llamadas_a(arbol: ast.AST, nombre: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == nombre)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre)
        )
    ]


def test_el_boton_generar_despacha_con_el_payload_del_modo_del_cliente() -> None:
    """``run_ritual`` del callback ``on_ritual_run`` recibe ``payload=payload_de_generar(tool_key, <modo del cliente>)``."""
    arbol = _arbol(GUI / "sky_claw_gui.py")
    asignaciones = [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "on_ritual_run"
            for t in n.targets
        )
    ]
    assert len(asignaciones) == 1
    llamadas_run = _llamadas_a(asignaciones[0], "run_ritual")
    assert len(llamadas_run) == 1
    payloads = [kw.value for kw in llamadas_run[0].keywords if kw.arg == "payload"]
    assert len(payloads) == 1, "«Generar» tiene que pasar payload= explícito"
    construccion = payloads[0]
    assert isinstance(construccion, ast.Call)
    assert isinstance(construccion.func, ast.Name) and construccion.func.id == "payload_de_generar"
    assert len(_llamadas_a(construccion, "modo_de_interaccion_del_cliente")) == 1


def test_el_modo_del_cliente_se_lee_en_el_unico_punto_con_contexto_de_cliente() -> None:
    """Hermano de ``modo_local_enabled``: se lee en el click handler (``sky_claw_gui``), no dentro de ``run_ritual``."""
    runner = _arbol(GUI / "controllers" / "ritual_runner.py")

    assert not _llamadas_a(runner, "modo_de_interaccion_del_cliente")


# ------------------------------------------------------------------- la preferencia por cliente (NiceGUI)


class _AppFalsa:
    """Doble de ``nicegui.app``: ``storage.client`` es el diccionario de la conexión."""

    class _Storage:
        def __init__(self, client: dict[str, object]) -> None:
            self.client = client

    def __init__(self, client: dict[str, object]) -> None:
        self.storage = self._Storage(client)


def test_sin_contexto_de_cliente_el_modo_es_el_historico() -> None:
    """Sin contexto (tests, tarea de fondo) ``app.storage.client`` falla: no hay elección y no se adivina ninguna."""
    from sky_claw.app.gui.views import forge_dashboard as fd

    assert fd.modo_de_interaccion_del_cliente() == "asistido"


def test_la_eleccion_del_cliente_se_lee_y_lo_invalido_cae_al_defecto(monkeypatch: pytest.MonkeyPatch) -> None:
    from sky_claw.app.gui.views import forge_dashboard as fd

    cliente: dict[str, object] = {}
    monkeypatch.setattr(fd, "app", _AppFalsa(cliente))

    assert fd.modo_de_interaccion_del_cliente() == "asistido"  # no eligió nada
    fd._fijar_modo_de_interaccion("desatendido")
    assert cliente[rr.CLIENT_KEY_MODO_DE_INTERACCION] == "desatendido"
    assert fd.modo_de_interaccion_del_cliente() == "desatendido"
    fd._fijar_modo_de_interaccion("Desatendido ")  # una escritura inválida no persiste un valor adivinado
    assert cliente[rr.CLIENT_KEY_MODO_DE_INTERACCION] == "asistido"
    cliente[rr.CLIENT_KEY_MODO_DE_INTERACCION] = "basura"  # y un valor corrupto en el almacén tampoco se interpreta
    assert fd.modo_de_interaccion_del_cliente() == "asistido"


def test_cada_modo_del_selector_tiene_etiqueta_y_ayuda_y_el_desatendido_dice_que_aprueba_el_canal_remoto() -> None:
    from sky_claw.app.gui.views import forge_dashboard as fd

    for modo in rr.MODOS_DE_INTERACCION:
        assert fd._ETIQUETA_DEL_MODO[modo] and fd._AYUDA_DEL_MODO[modo], modo
    assert set(fd._ETIQUETA_DEL_MODO) == set(fd._AYUDA_DEL_MODO) == set(rr.MODOS_DE_INTERACCION)
    # El operador no puede creer que el desatendido pregunta en esta ventana: sus aprobaciones viajan por el canal remoto.
    assert "remoto" in fd._AYUDA_DEL_MODO["desatendido"]
