"""«Modo local» persistente: el operador elige en Ajustes no aprobar cada ritual.

Propiedad del mecanismo: *la auto-aprobación de rituales tiene UN solo seam de
lectura (``modo_local_enabled``) y todo lanzador del GUI lo consulta; la
preferencia persistida sólo cambia el DEFAULT de ese seam.* Por eso:

- no hay un segundo camino de auto-aprobación: el ancla AST enumera TODOS los
  ``auto_approve=`` de ``sky_claw_gui.py`` y exige que sean ese seam;
- «Modo local» no crece: descargas, promoción de sandbox y confirmación mid-run
  de DynDOLOD no las auto-aprueba, y acá se enumeran las tres con su getter
  encendido;
- las descargas tienen su PROPIA preferencia (egress = decisión aparte), armada
  por ContextVar sólo en la task de ``run_ritual_install``: una descarga pedida
  por el agente/Telegram corre en otra task y sigue pidiendo confirmación;
- el default sigue fail-closed: sin preferencia escrita, o con un valor que no
  sea exactamente ``True``, se pide confirmación.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from sky_claw.app.gui.controllers.ritual_runner import (
    make_gui_hitl_notify,
    ritual_auto_approve_descargas_armed,
    run_ritual_install,
)
from sky_claw.app.gui.sky_claw_gui import (
    _build_settings_data,
    cargar_modo_local_defecto,
    save_settings,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.gui.views.forge_dashboard import (
    CONFIG_KEY_DESCARGAS_AUTO,
    CONFIG_KEY_MODO_LOCAL_DEFECTO,
    PREFERENCIAS_DE_APROBACION,
    STORE_KEY_DESCARGAS_AUTO,
    STORE_KEY_MODO_LOCAL_DEFECTO,
    resolver_modo_local,
)
from sky_claw.app.security.hitl import (
    CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
    CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO,
)
from sky_claw.config import Config

_GUI = pathlib.Path(__file__).resolve().parent.parent / "sky_claw" / "app" / "gui"


def _sin_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    monkeypatch.setattr(keyring, "set_password", lambda *_a: None)
    monkeypatch.setattr(keyring, "get_password", lambda *_a: None)
    monkeypatch.setattr(keyring, "delete_password", lambda *_a: None)


# ── Seam puro: sesión explícita > preferencia persistida > fail-closed ───────────
@pytest.mark.parametrize(
    ("sesion", "defecto", "esperado"),
    [
        (None, None, False),  # nada escrito: se pide confirmación
        (None, False, False),
        (None, True, True),  # la preferencia arma el default
        (False, True, False),  # F8 en esta sesión apaga aunque el default sea ON
        (True, False, True),  # F8 en esta sesión enciende aunque el default sea OFF
        (None, "true", False),  # sólo el booleano True arma: un string no
        (None, 1, False),
        ("true", False, False),
    ],
)
def test_resolver_modo_local_enumera_sesion_y_defecto(sesion: object, defecto: object, esperado: bool) -> None:
    assert resolver_modo_local(sesion, defecto) is esperado


# ── Persistencia ────────────────────────────────────────────────────────────────
def test_el_default_de_config_es_confirmar(tmp_path: pathlib.Path) -> None:
    cfg = Config(tmp_path / "config.toml")
    assert cfg._data[CONFIG_KEY_MODO_LOCAL_DEFECTO] is False


@pytest.mark.parametrize(("texto", "esperado"), [("true", True), ("false", False)])
def test_save_settings_persiste_la_preferencia(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, texto: str, esperado: bool
) -> None:
    _sin_keyring(monkeypatch)
    ruta = tmp_path / "config.toml"

    error = save_settings(ruta, {"llm_provider": "ollama", CONFIG_KEY_MODO_LOCAL_DEFECTO: texto})

    assert error is None
    assert Config(ruta)._data[CONFIG_KEY_MODO_LOCAL_DEFECTO] is esperado
    assert _build_settings_data(ruta)["modo_local_defecto"] is esperado


def test_save_settings_sin_la_clave_no_pisa_la_preferencia(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un payload que no trae la clave (otro caller de Ajustes) no la apaga ni la enciende."""
    _sin_keyring(monkeypatch)
    ruta = tmp_path / "config.toml"
    assert save_settings(ruta, {"llm_provider": "ollama", CONFIG_KEY_MODO_LOCAL_DEFECTO: "true"}) is None

    assert save_settings(ruta, {"llm_provider": "ollama"}) is None

    assert Config(ruta)._data[CONFIG_KEY_MODO_LOCAL_DEFECTO] is True


def test_save_settings_rechaza_un_valor_que_no_es_booleano(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entrada externa validada: nada que no sea "true"/"false" llega a desarmar el gate."""
    _sin_keyring(monkeypatch)
    ruta = tmp_path / "config.toml"

    error = save_settings(ruta, {"llm_provider": "ollama", CONFIG_KEY_MODO_LOCAL_DEFECTO: "si"})

    assert error is not None
    assert Config(ruta)._data[CONFIG_KEY_MODO_LOCAL_DEFECTO] is False


def test_cargar_modo_local_defecto_publica_en_el_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sin_keyring(monkeypatch)
    ruta = tmp_path / "config.toml"
    store = ReactiveStore()

    cargar_modo_local_defecto(ruta, store)
    assert store.get(STORE_KEY_MODO_LOCAL_DEFECTO) is False

    assert save_settings(ruta, {"llm_provider": "ollama", CONFIG_KEY_MODO_LOCAL_DEFECTO: "true"}) is None
    # Sin forzar no relee (una lectura por proceso, no una por render)…
    cargar_modo_local_defecto(ruta, store)
    assert store.get(STORE_KEY_MODO_LOCAL_DEFECTO) is False
    # …y tras guardar Ajustes se fuerza.
    cargar_modo_local_defecto(ruta, store, forzar=True)
    assert store.get(STORE_KEY_MODO_LOCAL_DEFECTO) is True


def test_config_ilegible_deja_el_default_en_confirmar(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / "config.toml"
    ruta.write_text("esto no es = [toml válido", encoding="utf-8")
    store = ReactiveStore()

    cargar_modo_local_defecto(ruta, store)

    assert store.get(STORE_KEY_MODO_LOCAL_DEFECTO) is False


# ── Anclas que enumeran ─────────────────────────────────────────────────────────
def test_todo_auto_approve_del_gui_sale_del_unico_seam() -> None:
    """Enumera TODOS los ``auto_approve=`` de ``sky_claw_gui.py``: cada uno es ``modo_local_enabled()``.

    Un lanzador nuevo que lea la preferencia por su cuenta (o pase un literal)
    rompe el ancla: sería un segundo camino de auto-aprobación, que es justo la
    forma en que «arreglar un hermano y no al otro» entra en este repo.
    """
    arbol = ast.parse((_GUI / "sky_claw_gui.py").read_text(encoding="utf-8"))
    fuentes = [
        ast.unparse(kw.value)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        for kw in nodo.keywords
        if kw.arg == "auto_approve"
    ]
    assert fuentes == ["modo_local_enabled()", "modo_local_enabled()"], fuentes


def test_la_preferencia_solo_la_lee_el_seam() -> None:
    """La clave del store sólo se LEE en ``modo_local_enabled``: nadie más decide con ella."""
    lectores: list[str] = []
    for ruta in sorted(_GUI.rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for funcion in ast.walk(arbol):
            if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for nodo in ast.walk(funcion):
                if (
                    isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == "get"
                    and any(isinstance(a, ast.Name) and a.id == "STORE_KEY_MODO_LOCAL_DEFECTO" for a in nodo.args)
                ):
                    lectores.append(f"{ruta.name}:{funcion.name}")
    assert lectores == ["sky_claw_gui.py:cargar_modo_local_defecto", "forge_dashboard.py:modo_local_enabled"]


def test_la_preferencia_de_descargas_solo_la_lee_su_seam() -> None:
    lectores = [
        f"{ruta.name}:{funcion.name}"
        for ruta in sorted(_GUI.rglob("*.py"))
        for funcion in ast.walk(ast.parse(ruta.read_text(encoding="utf-8")))
        if isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef)
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Name) and nodo.id == "STORE_KEY_DESCARGAS_AUTO"
    ]
    assert lectores == ["forge_dashboard.py:descargas_auto_enabled"]


#: Qué hace el puente GUI con CADA categoría que intercepta. Igualdad exacta contra
#: lo que ``_notify`` realmente compara (censo AST abajo): una categoría nueva en
#: el puente rompe el ancla hasta que se decide acá si alguna preferencia la
#: auto-aprueba. Así entró ``dyndolod_etapas_previas_asistido`` (#906) mientras
#: este PR estaba abierto: en un merge limpio, sin un solo marcador.
_POLITICA_POR_CATEGORIA: dict[str, str] = {
    "tool_execution": "modo_local",
    "download": "preferencia_de_descargas",
    "sandbox_promotion": "siempre_manual",
    "CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA": "siempre_manual",
    "CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO": "siempre_manual",
}


def test_el_censo_de_categorias_del_puente_esta_completo() -> None:
    arbol = ast.parse((_GUI / "controllers" / "ritual_runner.py").read_text(encoding="utf-8"))
    notify = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_notify")
    comparadas: set[str] = set()
    for nodo in ast.walk(notify):
        if not isinstance(nodo, ast.Compare):
            continue
        # Los dos lados: ``category == "x"`` y ``"x" == category`` son la misma
        # comparación, y sólo mirar el izquierdo dejaría pasar la segunda en verde.
        operandos = [nodo.left, *nodo.comparators]
        if not any(isinstance(o, ast.Name) and o.id == "category" for o in operandos):
            continue
        for operando in operandos:
            for elemento in operando.elts if isinstance(operando, ast.Tuple) else [operando]:
                if isinstance(elemento, ast.Name) and elemento.id == "category":
                    continue
                comparadas.add(elemento.value if isinstance(elemento, ast.Constant) else ast.unparse(elemento))
    assert comparadas == set(_POLITICA_POR_CATEGORIA)


@pytest.mark.parametrize(
    "categoria",
    [
        "download",
        "sandbox_promotion",
        CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
        CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO,
    ],
)
async def test_lo_que_nunca_se_auto_aprueba_sigue_pidiendo_confirmacion(categoria: str) -> None:
    """Con «Modo local» encendido (getter True) estas categorías siguen parkeando modal."""
    aprobadas: list[str] = []
    parkeadas: list[dict[str, object]] = []

    async def _responder(request_id: str, aprobado: bool) -> None:
        aprobadas.append(request_id)

    notify = make_gui_hitl_notify(
        respond=_responder,
        set_pending=parkeadas.append,
        auto_approve_getter=lambda: True,
        tab_id_getter=lambda: "pestaña",
        delegate=None,
    )

    await notify(SimpleNamespace(request_id="r1", category=categoria, reason="", detail="", url=""))

    assert aprobadas == []
    assert [p["request_id"] for p in parkeadas] == ["r1"]


# ── Descargas de red: preferencia propia, scoped a la task del instalador ───────
def test_las_preferencias_de_aprobacion_estan_congeladas() -> None:
    """Igualdad literal: una preferencia nueva obliga a decidir acá su alcance."""
    assert PREFERENCIAS_DE_APROBACION == {
        "gui_auto_approve_rituals": "modo_local_defecto",
        "gui_auto_approve_downloads": "descargas_auto_defecto",
    }
    assert CONFIG_KEY_MODO_LOCAL_DEFECTO in PREFERENCIAS_DE_APROBACION
    assert PREFERENCIAS_DE_APROBACION[CONFIG_KEY_DESCARGAS_AUTO] == STORE_KEY_DESCARGAS_AUTO


@pytest.mark.parametrize("clave", sorted(PREFERENCIAS_DE_APROBACION))
def test_toda_preferencia_arranca_apagada_y_hace_el_viaje_completo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, clave: str
) -> None:
    """Default False → guardar → TOML → store, para CADA preferencia (no sólo la primera)."""
    _sin_keyring(monkeypatch)
    ruta = tmp_path / "config.toml"
    store = ReactiveStore()
    assert Config(ruta)._data[clave] is False

    assert save_settings(ruta, {"llm_provider": "ollama", clave: "true"}) is None
    cargar_modo_local_defecto(ruta, store)

    assert Config(ruta)._data[clave] is True
    for otra, store_key in PREFERENCIAS_DE_APROBACION.items():
        assert store.get(store_key) is (otra == clave), "encender una no enciende la otra"
    assert save_settings(ruta, {"llm_provider": "ollama", clave: "quizás"}) is not None


async def test_descarga_se_auto_aprueba_solo_con_su_propio_getter() -> None:
    aprobadas: list[tuple[str, bool]] = []
    parkeadas: list[dict[str, object]] = []

    async def _responder(request_id: str, aprobado: bool) -> None:
        aprobadas.append((request_id, aprobado))

    peticion = SimpleNamespace(request_id="d1", category="download", reason="", detail="", url="https://x/y.zip")
    comun = {"respond": _responder, "set_pending": parkeadas.append, "delegate": None}

    # «Modo local» encendido NO alcanza: sigue siendo egress.
    await make_gui_hitl_notify(auto_approve_getter=lambda: True, **comun)(peticion)
    assert (aprobadas, len(parkeadas)) == ([], 1)

    # Su preferencia propia sí, aun con «Modo local» apagado.
    await make_gui_hitl_notify(auto_approve_getter=lambda: False, auto_approve_downloads_getter=lambda: True, **comun)(
        peticion
    )
    assert (aprobadas, len(parkeadas)) == ([("d1", True)], 1)


class _InstaladorEspia:
    """``ensure_loot`` anota qué ve la task del instalador y qué ve una task ajena."""

    def __init__(self) -> None:
        self.visto_adentro: bool | None = None
        self.visto_por_ajena: bool | None = None
        self.ajena_puede_mirar = asyncio.Event()
        self.ajena_miro = asyncio.Event()

    async def ensure_loot(self, install_dir: object, session: object) -> object:
        self.visto_adentro = ritual_auto_approve_descargas_armed()
        self.ajena_puede_mirar.set()
        await self.ajena_miro.wait()
        return SimpleNamespace(exe_path="C:/Modding/LOOT/loot.exe", verification=None)


@pytest.mark.parametrize("preferencia", [True, False])
async def test_el_armado_de_descargas_vive_solo_en_la_task_del_instalador(
    monkeypatch: pytest.MonkeyPatch, preferencia: bool
) -> None:
    """Una descarga del agente/Telegram concurrente (otra task) nunca ve el armado de la GUI."""
    monkeypatch.setenv("LOOT_EXE", "")
    espia = _InstaladorEspia()
    contexto = SimpleNamespace(tools_installer=espia, install_dir="C:/Modding", session="sess")

    async def _task_ajena() -> None:
        await espia.ajena_puede_mirar.wait()
        espia.visto_por_ajena = ritual_auto_approve_descargas_armed()
        espia.ajena_miro.set()

    ajena = asyncio.create_task(_task_ajena())
    await run_ritual_install("loot", app_context=contexto, store=ReactiveStore(), auto_approve_downloads=preferencia)
    await ajena

    assert espia.visto_adentro is preferencia
    assert espia.visto_por_ajena is False
    assert ritual_auto_approve_descargas_armed() is False, "el armado no sobrevive al instalador"


def test_el_armado_de_descargas_del_gui_sale_de_su_unico_seam() -> None:
    """Enumera TODOS los ``auto_approve_downloads=`` de ``sky_claw_gui.py``."""
    arbol = ast.parse((_GUI / "sky_claw_gui.py").read_text(encoding="utf-8"))
    fuentes = [
        ast.unparse(kw.value)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        for kw in nodo.keywords
        if kw.arg == "auto_approve_downloads"
    ]
    assert fuentes == ["descargas_auto_enabled()"], fuentes


async def test_una_descarga_ajena_no_se_auto_aprueba_mientras_la_gui_esta_armada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De punta a punta por el puente REAL: mismo ``notify``, mismos getters que cablea el bootloader.

    Mientras ``run_ritual_install`` corre con la preferencia de descargas armada,
    una solicitud ``download`` que llega desde OTRA task (el agente LLM o Telegram
    usando el mismo ``HITLGuard``) se parkea para confirmación manual; la del
    propio instalador se auto-aprueba. Es la composición de las dos mitades que
    los tests de arriba verifican por separado.
    """
    monkeypatch.setenv("LOOT_EXE", "")
    aprobadas: list[str] = []
    parkeadas: list[str] = []

    async def _responder(request_id: str, aprobado: bool) -> None:
        aprobadas.append(request_id)

    from sky_claw.app.gui.controllers.ritual_runner import (
        ritual_auto_approve_armed,
        ritual_tab_id,
    )

    notify = make_gui_hitl_notify(
        respond=_responder,
        set_pending=lambda payload: parkeadas.append(str(payload["request_id"])),
        auto_approve_getter=ritual_auto_approve_armed,
        tab_id_getter=ritual_tab_id,
        auto_approve_downloads_getter=ritual_auto_approve_descargas_armed,
        delegate=None,
    )

    def _descarga(request_id: str) -> SimpleNamespace:
        return SimpleNamespace(request_id=request_id, category="download", reason="", detail="", url="https://x/y")

    instalador_armado = asyncio.Event()
    ajena_termino = asyncio.Event()

    async def _ensure_loot(install_dir: object, session: object) -> object:
        instalador_armado.set()
        await ajena_termino.wait()
        await notify(_descarga("del-instalador"))
        return SimpleNamespace(exe_path="C:/Modding/LOOT/loot.exe", verification=None)

    async def _agente() -> None:
        await instalador_armado.wait()
        await notify(_descarga("del-agente"))
        ajena_termino.set()

    contexto = SimpleNamespace(
        tools_installer=SimpleNamespace(ensure_loot=_ensure_loot), install_dir="C:/Modding", session="sess"
    )
    agente = asyncio.create_task(_agente())
    await run_ritual_install("loot", app_context=contexto, store=ReactiveStore(), auto_approve_downloads=True)
    await agente

    assert parkeadas == ["del-agente"], "la descarga del agente tiene que pedir confirmación"
    assert aprobadas == ["del-instalador"], "sólo la del instalador de la GUI se auto-aprueba"
