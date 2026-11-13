"""Estado «en curso» de los rituales: visible por tarjeta, con un único escritor.

Propiedad del mecanismo: *el single-flight y lo que la GUI muestra como «en
curso» se escriben JUNTOS, por las dos únicas funciones que los tocan
(``_marcar_en_curso`` / ``_liberar_en_curso``).* Antes el store sólo tenía el
booleano ``ritual_in_flight`` y ninguna vista lo leía: un DynDOLOD de varios
minutos se veía igual que en reposo. Si cada lanzador escribiera las dos claves
por su cuenta, el primero que olvidara una dejaría una tarjeta «Ejecutando»
para siempre — por eso el ancla enumera los escritores por AST.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from sky_claw.app.gui.controllers import ritual_runner as rr
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.gui.views import forge_dashboard as fd

_GUI = pathlib.Path(rr.__file__).resolve().parent.parent


# ── Runner: publica y libera ────────────────────────────────────────────────────
class _SupervisorEspia:
    def __init__(self, store: ReactiveStore, *, falla: bool = False) -> None:
        self._store = store
        self._falla = falla
        self.visto: Any = "nunca"

    async def dispatch_tool(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.visto = rr.ritual_en_curso(self._store)
        if self._falla:
            raise RuntimeError("boom")
        return {"success": True, "message": ""}


@pytest.mark.parametrize("falla", [False, True])
async def test_run_ritual_publica_que_tool_corre_y_lo_libera(falla: bool) -> None:
    store = ReactiveStore()
    supervisor = _SupervisorEspia(store, falla=falla)

    await rr.run_ritual("loot", supervisor=supervisor, store=store, auto_approve=False)

    assert supervisor.visto == {"tool_key": "loot", "fase": "ejecutando"}
    assert rr.ritual_en_curso(store) is None
    assert not store.get(rr.STORE_KEY_RITUAL_IN_FLIGHT)


@pytest.mark.parametrize("falla", [False, True])
async def test_run_ritual_install_publica_que_tool_instala_y_lo_libera(
    monkeypatch: pytest.MonkeyPatch, falla: bool
) -> None:
    monkeypatch.setenv("LOOT_EXE", "")
    store = ReactiveStore()
    visto: list[Any] = []

    async def _ensure_loot(install_dir: object, session: object) -> object:
        visto.append(rr.ritual_en_curso(store))
        if falla:
            raise RuntimeError("boom")
        return SimpleNamespace(exe_path="C:/Modding/LOOT/loot.exe", verification=None)

    contexto = SimpleNamespace(
        tools_installer=SimpleNamespace(ensure_loot=_ensure_loot), install_dir="C:/Modding", session="sess"
    )

    await rr.run_ritual_install("loot", app_context=contexto, store=store)

    assert visto == [{"tool_key": "loot", "fase": "instalando"}]
    assert rr.ritual_en_curso(store) is None
    assert not store.get(rr.STORE_KEY_RITUAL_IN_FLIGHT)


def test_un_single_flight_sin_detalle_se_lee_como_en_curso_generico() -> None:
    """Fail-safe de lectura: el booleano manda. Con él armado y sin detalle bien formado, hay «algo en curso»."""
    store = ReactiveStore()
    assert rr.ritual_en_curso(store) is None

    store.set(rr.STORE_KEY_RITUAL_IN_FLIGHT, True)
    assert rr.ritual_en_curso(store) == {"tool_key": "", "fase": ""}

    store.set(rr.STORE_KEY_RITUAL_EN_CURSO, {"tool_key": 7, "fase": "ejecutando"})
    assert rr.ritual_en_curso(store) == {"tool_key": "", "fase": ""}

    # Un detalle huérfano (sin single-flight) no pinta nada como «en curso».
    store.set(rr.STORE_KEY_RITUAL_IN_FLIGHT, False)
    store.set(rr.STORE_KEY_RITUAL_EN_CURSO, {"tool_key": "loot", "fase": "ejecutando"})
    assert rr.ritual_en_curso(store) is None


def _funciones_que_escriben(clave: str) -> set[str]:
    arbol = ast.parse(pathlib.Path(rr.__file__).read_text(encoding="utf-8"))
    escritoras: set[str] = set()
    for funcion in arbol.body:
        if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for nodo in ast.walk(funcion):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "set"
                and nodo.args
                and isinstance(nodo.args[0], ast.Name)
                and nodo.args[0].id == clave
            ):
                escritoras.add(funcion.name)
    return escritoras


@pytest.mark.parametrize("clave", ["STORE_KEY_RITUAL_IN_FLIGHT", "STORE_KEY_RITUAL_EN_CURSO"])
def test_las_dos_claves_tienen_los_mismos_dos_escritores(clave: str) -> None:
    assert _funciones_que_escriben(clave) == {"_marcar_en_curso", "_liberar_en_curso"}


# ── Vista: qué muestra cada tarjeta ─────────────────────────────────────────────
_ACCION_WB = {
    "tool_key": "wrye_bash",
    "label": "Ya construí el parche — verificar",
    "detail": "Bashed Patch",
    "payload": {},
}


@pytest.mark.parametrize(
    ("tool", "en_curso", "accion", "esperado"),
    [
        ("loot", None, None, {"insignia": None, "bloqueada": False, "accion": None}),
        # La tarjeta que corre lo dice; las demás quedan bloqueadas sin insignia.
        (
            "loot",
            {"tool_key": "loot", "fase": "ejecutando"},
            None,
            {"insignia": "ejecutando", "bloqueada": True, "accion": None},
        ),
        (
            "loot",
            {"tool_key": "loot", "fase": "instalando"},
            None,
            {"insignia": "instalando", "bloqueada": True, "accion": None},
        ),
        (
            "xedit",
            {"tool_key": "loot", "fase": "ejecutando"},
            None,
            {"insignia": None, "bloqueada": True, "accion": None},
        ),
        # Single-flight sin detalle: todo bloqueado, ninguna tarjeta afirma ser la que corre.
        ("loot", {"tool_key": "", "fase": ""}, None, {"insignia": None, "bloqueada": True, "accion": None}),
        # Fase desconocida: no se inventa una etiqueta.
        ("loot", {"tool_key": "loot", "fase": "otra"}, None, {"insignia": None, "bloqueada": True, "accion": None}),
        # Acción requerida: sólo en la tarjeta de SU tool.
        ("wrye_bash", None, _ACCION_WB, {"insignia": "accion", "bloqueada": False, "accion": _ACCION_WB}),
        ("dyndolod", None, _ACCION_WB, {"insignia": None, "bloqueada": False, "accion": None}),
        # Mientras algo corre, la acción pendiente se ve pero no se puede disparar.
        (
            "wrye_bash",
            {"tool_key": "loot", "fase": "ejecutando"},
            _ACCION_WB,
            {"insignia": "accion", "bloqueada": True, "accion": _ACCION_WB},
        ),
    ],
)
def test_estado_operativo_de_la_tarjeta_enumerado(
    tool: str, en_curso: dict[str, str] | None, accion: dict[str, Any] | None, esperado: dict[str, Any]
) -> None:
    assert fd._estado_operativo_de_tarjeta(tool, en_curso, accion) == esperado


def test_las_insignias_operativas_estan_congeladas() -> None:
    assert set(fd._INSIGNIAS_OPERATIVAS) == {"ejecutando", "instalando", "accion"}
    for insignia in fd._INSIGNIAS_OPERATIVAS.values():
        assert set(insignia) == {"texto", "color"}
        assert insignia["texto"]


def test_el_resume_se_despacha_por_un_unico_camino() -> None:
    """Toast y tarjeta ofrecen la MISMA acción: los dos pasan por ``_despachar_resume``.

    Enumera quién toca el callback de resume y quién llama al despachador. Un
    tercer botón que invoque el callback por su cuenta (sin cerrar el toast ni
    limpiar el resultado propio) rompe acá.
    """
    arbol = ast.parse((_GUI / "views" / "forge_dashboard.py").read_text(encoding="utf-8"))
    tocan_callback: set[str] = set()
    llaman_despachador: set[str] = set()
    for funcion in arbol.body:
        if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for nodo in ast.walk(funcion):
            if isinstance(nodo, ast.Name) and nodo.id == "_RITUAL_RESUME_CALLBACKS":
                tocan_callback.add(funcion.name)
            if isinstance(nodo, ast.Name) and nodo.id == "_despachar_resume" and funcion.name != "_despachar_resume":
                llaman_despachador.add(funcion.name)
    assert tocan_callback == {"render_forge_dashboard", "_despachar_resume", "_resume_disponible"}
    assert llaman_despachador == {"_ritual_feedback_panel", "_ritual_card"}


def test_despachar_resume_cierra_el_toast_y_transporta_el_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ReactiveStore()
    store.set(rr.STORE_KEY_RITUAL_FEEDBACK, {"text": "x", "type": "warning"})
    llamadas: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(fd, "get_store", lambda: store)
    monkeypatch.setattr(fd, "current_tab_id", lambda: None)
    monkeypatch.setitem(fd._RITUAL_RESUME_CALLBACKS, "resume", lambda tool, payload: llamadas.append((tool, payload)))
    accion = {"tool_key": "wrye_bash", "label": "x", "detail": "", "payload": {"fase": "verificar"}}

    fd._despachar_resume(accion)

    assert store.get(rr.STORE_KEY_RITUAL_FEEDBACK) is None
    assert llamadas == [("wrye_bash", {"fase": "verificar"})]
    assert llamadas[0][1] is not accion["payload"], "el payload viaja copiado"


def test_despachar_resume_no_consume_la_accion_antes_de_que_el_runner_la_acepte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si el runner rechaza el resume (single-flight tomado, daemon no listo), la acción sigue ahí.

    El dispatcher NO limpia el resultado propio: lo hace ``run_ritual`` después de
    sus guards. Limpiarlo antes dejaba al operador sin «Ya construí el parche —
    verificar» tras un click rechazado, obligándolo a repetir la fase 1.
    """
    store = ReactiveStore()
    resultado = {"success": False, "pendiente": True, "message": "falta tu parte", "artefacto": "Bashed Patch, 0.esp"}
    rr.publicar_resultado_de_ritual(store, tool_key="wrye_bash", resultado=resultado, tab_id="pestaña")
    accion = rr.resolve_ritual_resume_action(store, "pestaña")
    assert accion is not None
    monkeypatch.setattr(fd, "get_store", lambda: store)
    monkeypatch.setattr(fd, "current_tab_id", lambda: "pestaña")
    monkeypatch.setitem(fd._RITUAL_RESUME_CALLBACKS, "resume", lambda tool, payload: None)  # runner que rechaza

    fd._despachar_resume(accion)

    assert rr.resolve_ritual_resume_action(store, "pestaña") == accion


async def test_un_resume_rechazado_por_single_flight_conserva_la_accion() -> None:
    """La otra mitad: ``run_ritual_resume`` rechazado por el guard no toca el resultado propio."""
    store = ReactiveStore()
    resultado = {"success": False, "pendiente": True, "message": "falta tu parte", "artefacto": "Bashed Patch, 0.esp"}
    rr.publicar_resultado_de_ritual(store, tool_key="wrye_bash", resultado=resultado, tab_id="pestaña")
    accion = rr.resolve_ritual_resume_action(store, "pestaña")
    assert accion is not None
    rr._marcar_en_curso(store, "dyndolod", rr.FASE_EJECUTANDO)

    await rr.run_ritual_resume(
        "wrye_bash",
        payload=dict(accion["payload"]),
        supervisor=_SupervisorEspia(store),
        store=store,
        auto_approve=False,
        tab_id="pestaña",
    )

    assert rr.resolve_ritual_resume_action(store, "pestaña") == accion
    assert store.get(rr.STORE_KEY_RITUAL_FEEDBACK)["type"] == "warning"


def test_el_handshake_de_la_pestana_refresca_modal_y_grilla() -> None:
    """Tras un F5 el primer render no tiene ``tab_id``: todo lo que se resuelve por dueño se re-renderiza al conectar.

    Enumera los refreshables que leen estado con dueño de pestaña en el shell
    (modal HITL y grilla de rituales): los dos tienen que estar en el hook.
    """
    arbol = ast.parse((_GUI / "views" / "forge_dashboard.py").read_text(encoding="utf-8"))
    hook = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "_refrescar_al_conectar")
    refrescados = {
        nodo.func.value.id
        for nodo in ast.walk(hook)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "refresh"
        and isinstance(nodo.func.value, ast.Name)
    }
    assert refrescados == {"_hitl_modal_panel", "_rituales"}
