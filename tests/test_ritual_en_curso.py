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
import asyncio
import contextlib
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


# ── El single-flight dura hasta el FINAL de la operación (P1 de la auditoría de #923) ──
class _Mo2(SimpleNamespace):
    """``add_mod_to_modlist`` que se puede pausar a mitad de la activación."""

    def __init__(self, *, pausar: bool) -> None:
        super().__init__(pausar=pausar, dentro=asyncio.Event(), soltar=asyncio.Event(), mods_dir=None)

    async def add_mod_to_modlist(self, nombre: str, *, profile: str) -> None:
        if self.pausar:
            self.dentro.set()
            await self.soltar.wait()


def _escenario_community_shaders(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, donde: str
) -> tuple[SimpleNamespace, ReactiveStore, asyncio.Event, asyncio.Event]:
    """Instalación de Community Shaders pausada DESPUÉS de ``ensure``: en persistencia o en activación."""
    from sky_claw.local.discovery.environment import EnvironmentSnapshot, MO2Info, SkyrimEdition, SkyrimInfo
    from sky_claw.local.tools_installer import ModInstallResult

    mo2_root = pathlib.Path("D:/Modding/MO2")
    store = ReactiveStore()
    store.set(
        fd.STORE_KEY_ENV,
        EnvironmentSnapshot(
            skyrim=SkyrimInfo(
                path=pathlib.Path("D:/Skyrim"), exe_name="SkyrimSE.exe", edition=SkyrimEdition.AE, version="1.6.1170"
            ),
            mo2=MO2Info(path=mo2_root, profiles=["Default"]),
        ),
    )
    mods = [
        ModInstallResult(
            mod_name="Community Shaders", mod_dir=mo2_root / "mods" / "CS", version="v1", already_existed=False
        )
    ]

    async def _ensure(mods_dir: object, session: object, downloader: object = None, **kwargs: object) -> list[object]:
        return mods

    mo2 = _Mo2(pausar=donde == "activacion")
    dentro_persistencia, soltar_persistencia = asyncio.Event(), asyncio.Event()

    async def _persistir(config_path: object, campo: str, valor: object) -> None:
        if donde == "persistencia":
            dentro_persistencia.set()
            await soltar_persistencia.wait()

    monkeypatch.setattr("sky_claw.local.local_config.persistir_campo_bloqueante", _persistir)
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "ollama"\n', encoding="utf-8")
    contexto = SimpleNamespace(
        tools_installer=SimpleNamespace(ensure_community_shaders=_ensure),
        install_dir="C:/Modding",
        session="sess",
        network=SimpleNamespace(downloader="dl"),
        config_path=config_path,
        mo2=mo2,
        mo2_profile="Default",
    )
    if donde == "persistencia":
        return contexto, store, dentro_persistencia, soltar_persistencia
    return contexto, store, mo2.dentro, mo2.soltar


@pytest.mark.parametrize("donde", ["persistencia", "activacion"])
async def test_community_shaders_mantiene_el_single_flight_hasta_activar_el_perfil(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, donde: str
) -> None:
    """Mientras CS persiste o activa en modlist.txt, NINGÚN otro lanzador puede pasar el guard.

    Los dos hermanos que mutan el estado (``run_ritual`` y ``run_ritual_install``)
    se intentan a mitad de la operación y los dos tienen que ser rechazados; la
    tarjeta sigue diciendo «Instalando…». Recién al terminar se libera todo.
    """
    contexto, store, dentro, soltar = _escenario_community_shaders(tmp_path, monkeypatch, donde=donde)
    tarea = asyncio.create_task(rr.run_ritual_install("community_shaders", app_context=contexto, store=store))
    await asyncio.wait_for(dentro.wait(), timeout=5)

    assert rr.ritual_en_curso(store) == {"tool_key": "community_shaders", "fase": "instalando"}
    assert store.get(rr.STORE_KEY_RITUAL_IN_FLIGHT) is True

    llamadas: list[str] = []

    async def _ensure_loot(install_dir: object, session: object) -> object:
        llamadas.append("ensure_loot")
        return SimpleNamespace(exe_path=None, verification=None)

    otro_contexto = SimpleNamespace(
        tools_installer=SimpleNamespace(ensure_loot=_ensure_loot), install_dir="C:/Modding", session="sess"
    )
    await rr.run_ritual_install("loot", app_context=otro_contexto, store=store)
    assert llamadas == [], "una segunda instalación pasó el guard mientras CS escribía el perfil"
    assert "en curso" in store.get(rr.STORE_KEY_RITUAL_FEEDBACK)["text"]

    despachos: list[str] = []

    class _Supervisor:
        async def dispatch_tool(self, nombre: str, payload: dict[str, object]) -> dict[str, object]:
            despachos.append(nombre)
            return {"success": True, "message": ""}

    await rr.run_ritual("loot", supervisor=_Supervisor(), store=store, auto_approve=False)
    assert despachos == [], "un ritual pasó el guard mientras CS escribía el perfil"
    assert rr.ritual_en_curso(store) == {"tool_key": "community_shaders", "fase": "instalando"}

    soltar.set()
    await asyncio.wait_for(tarea, timeout=5)
    assert rr.ritual_en_curso(store) is None
    assert not store.get(rr.STORE_KEY_RITUAL_IN_FLIGHT)


@pytest.mark.parametrize("donde", ["persistencia", "activacion"])
async def test_cancelar_a_mitad_de_la_activacion_libera_el_single_flight(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, donde: str
) -> None:
    contexto, store, dentro, _soltar = _escenario_community_shaders(tmp_path, monkeypatch, donde=donde)
    tarea = asyncio.create_task(rr.run_ritual_install("community_shaders", app_context=contexto, store=store))
    await asyncio.wait_for(dentro.wait(), timeout=5)

    tarea.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await tarea

    assert rr.ritual_en_curso(store) is None
    assert not store.get(rr.STORE_KEY_RITUAL_IN_FLIGHT), "la cancelación dejó el single-flight trabado"


@pytest.mark.parametrize("lanzador", ["run_ritual", "run_ritual_install"])
def test_ningun_await_posterior_a_tomar_el_single_flight_corre_fuera_del_finally_que_lo_libera(
    lanzador: str,
) -> None:
    """Ancla por AST sobre AMBOS lanzadores: todo ``await`` posterior a ``_marcar_en_curso`` está
    dentro del ``try`` cuyo ``finally`` llama a ``_liberar_en_curso``.

    Es la propiedad del P1: liberar antes de un await deja pasar a otro lanzador mientras la
    operación sigue escribiendo. Enumera todos los awaits de la función, no sólo el de CS.
    """
    arbol = ast.parse(pathlib.Path(rr.__file__).read_text(encoding="utf-8"))
    funcion = next(n for n in arbol.body if isinstance(n, ast.AsyncFunctionDef) and n.name == lanzador)

    def _llama(nodo: ast.AST, nombre: str) -> bool:
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == nombre for n in ast.walk(nodo)
        )

    marca = next(
        n.lineno
        for n in ast.walk(funcion)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_marcar_en_curso"
    )
    contenedores = [
        t
        for t in ast.walk(funcion)
        if isinstance(t, ast.Try) and any(_llama(f, "_liberar_en_curso") for f in t.finalbody)
    ]
    assert len(contenedores) == 1, f"{lanzador} debe liberar el single-flight en UN solo finally"
    cuerpo = contenedores[0].body
    desde, hasta = cuerpo[0].lineno, max(getattr(n, "end_lineno", n.lineno) for n in cuerpo)
    afuera = [
        n.lineno
        for n in ast.walk(funcion)
        if isinstance(n, ast.Await) and n.lineno > marca and not (desde <= n.lineno <= hasta)
    ]
    assert afuera == [], f"{lanzador}: await(s) tras tomar el single-flight fuera de su finally (líneas {afuera})"


# ── El guard y la toma del single-flight son atómicos respecto del event loop ──
def _llama_a(nodo: ast.AST, nombre: str) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == nombre for n in ast.walk(nodo))


def _es_punto_de_suspension(nodo: ast.AST) -> bool:
    """Todo lo que cede el control al event loop: ``await``, ``async for``, ``async with`` y comprensiones async."""
    if isinstance(nodo, ast.Await | ast.AsyncFor | ast.AsyncWith):
        return True
    comprension = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    return isinstance(nodo, comprension) and any(g.is_async for g in nodo.generators)


def test_no_hay_punto_de_suspension_entre_el_guard_y_la_toma_del_single_flight() -> None:
    """Ancla por AST: entre ``if store.get(STORE_KEY_RITUAL_IN_FLIGHT)`` y ``_marcar_en_curso`` no se cede el loop.

    El single-flight es check-then-set sobre un booleano del store. Es seguro SOLO porque
    entre el chequeo y la toma no hay ningún punto de suspensión: dos lanzadores corren en el
    mismo event loop, así que ninguno puede intercalarse ahí. Un ``await`` nuevo en ese tramo
    (p. ej. resolver algo del daemon antes de tomar) abre una ventana TOCTOU por la que pasan
    DOS corridas, y ningún test de comportamiento de un solo lanzador la ve.

    Enumera los lanzadores (toda función del módulo que toma el single-flight) y congela el
    conjunto: un lanzador nuevo rompe esta ancla hasta que su guard quede cubierto.
    """
    arbol = ast.parse(pathlib.Path(rr.__file__).read_text(encoding="utf-8"))
    funciones = [n for n in arbol.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    lanzadores = [f for f in funciones if _llama_a(f, "_marcar_en_curso")]
    assert {f.name for f in lanzadores} == {"run_ritual", "run_ritual_install"}

    for funcion in lanzadores:
        guards = [
            n
            for n in ast.walk(funcion)
            if isinstance(n, ast.If)
            and any(isinstance(c, ast.Name) and c.id == "STORE_KEY_RITUAL_IN_FLIGHT" for c in ast.walk(n.test))
        ]
        assert len(guards) == 1, f"{funcion.name} debe tener UN guard del single-flight"
        marca = next(
            n.lineno
            for n in ast.walk(funcion)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_marcar_en_curso"
        )
        assert guards[0].lineno < marca, f"{funcion.name}: el guard debe ir ANTES de tomar el single-flight"
        suspensiones = [
            n.lineno for n in ast.walk(funcion) if _es_punto_de_suspension(n) and guards[0].lineno <= n.lineno < marca
        ]
        assert suspensiones == [], (
            f"{funcion.name}: punto(s) de suspensión entre el guard y la toma del single-flight "
            f"(líneas {suspensiones}): abre una ventana TOCTOU"
        )


def test_el_anclaje_detecta_un_await_metido_entre_el_guard_y_la_toma() -> None:
    """El ancla no es vacua: sobre una función con el ``await`` en ese tramo, la misma consulta lo encuentra."""
    fuente = (
        "async def lanzador(store):\n"
        "    if store.get(STORE_KEY_RITUAL_IN_FLIGHT):\n"
        "        return\n"
        "    await algo()\n"
        "    _marcar_en_curso(store, 'x', 'y')\n"
    )
    funcion = ast.parse(fuente).body[0]
    guard = next(n for n in ast.walk(funcion) if isinstance(n, ast.If))
    marca = next(n.lineno for n in ast.walk(funcion) if isinstance(n, ast.Call) and _llama_a(n, "_marcar_en_curso"))
    assert [n.lineno for n in ast.walk(funcion) if _es_punto_de_suspension(n) and guard.lineno <= n.lineno < marca] == [
        4
    ]


# ── La × del aviso cierra el AVISO, no la acción obligatoria del flujo ──
def _manejadores_de_click_del_aviso() -> list[ast.Lambda]:
    arbol = ast.parse((_GUI / "views" / "forge_dashboard.py").read_text(encoding="utf-8"))
    panel = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "_ritual_feedback_panel")
    return [
        c.args[1]
        for c in ast.walk(panel)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "on"
        and len(c.args) == 2
        and isinstance(c.args[0], ast.Constant)
        and c.args[0].value == "click"
        and isinstance(c.args[1], ast.Lambda)
    ]


def test_la_x_del_aviso_no_consume_la_accion_de_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ejecuta el handler REAL de la × (el lambda del módulo, sin copiarlo) contra un store con una acción pendiente.

    «Ya construí el parche — verificar» y «Continuar DynDOLOD» son controles obligatorios: la ×
    cierra el aviso (``STORE_KEY_RITUAL_FEEDBACK``) y la acción sigue en la tarjeta. El lambda se
    localiza por AST y se evalúa en el namespace de la vista con ``get_store`` reemplazado, así
    que si alguien le agrega ``clear_ritual_result_owned`` (o cualquier escritura de más), cae acá.
    """
    manejadores = _manejadores_de_click_del_aviso()
    despachan = [
        m for m in manejadores if any(isinstance(n, ast.Name) and n.id == "_despachar_resume" for n in ast.walk(m))
    ]
    cierres = [m for m in manejadores if m not in despachan]
    assert len(manejadores) == 2, "el panel tiene exactamente dos handlers de click: despachar y cerrar"
    assert len(despachan) == 1
    assert len(cierres) == 1

    store = ReactiveStore()
    resultado = {"success": False, "pendiente": True, "message": "falta tu parte", "artefacto": "Bashed Patch, 0.esp"}
    rr.publicar_resultado_de_ritual(store, tool_key="wrye_bash", resultado=resultado, tab_id="pestaña")
    store.set(rr.STORE_KEY_RITUAL_FEEDBACK, {"text": "falta tu parte", "type": "warning"})
    accion = rr.resolve_ritual_resume_action(store, "pestaña")
    assert accion is not None
    refrescos: list[int] = []
    entorno = {
        **vars(fd),
        "get_store": lambda: store,
        "current_tab_id": lambda: "pestaña",
        "_ritual_feedback_panel": SimpleNamespace(refresh=lambda: refrescos.append(1)),
    }
    cerrar = eval(compile(ast.Expression(cierres[0]), "<x_del_aviso>", "eval"), entorno)  # noqa: S307 (lambda del propio módulo)

    cerrar()

    assert store.get(rr.STORE_KEY_RITUAL_FEEDBACK) is None, "la × no cerró el aviso"
    assert refrescos == [1], "la × debe re-renderizar el panel"
    assert rr.resolve_ritual_resume_action(store, "pestaña") == accion, "la × consumió la acción de resume"
