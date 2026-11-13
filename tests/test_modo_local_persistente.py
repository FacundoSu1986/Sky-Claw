"""«Modo local» persistente: el operador elige en Ajustes no aprobar cada ritual.

Propiedad del mecanismo: *la auto-aprobación de rituales tiene UN solo seam de
lectura (``modo_local_enabled``) y todo lanzador del GUI lo consulta; la
preferencia persistida sólo cambia el DEFAULT de ese seam.* Por eso:

- no hay un segundo camino de auto-aprobación: el ancla AST enumera TODOS los
  ``auto_approve=`` de ``sky_claw_gui.py`` y exige que sean ese seam;
- el alcance no crece: lo que nunca se auto-aprueba (descargas, promoción de
  sandbox, confirmación mid-run de DynDOLOD) lo decide ``make_gui_hitl_notify``
  por categoría, y acá se enumeran las tres con el getter encendido;
- el default sigue fail-closed: sin preferencia escrita, o con un valor que no
  sea exactamente ``True``, se pide confirmación.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from sky_claw.app.gui.controllers.ritual_runner import make_gui_hitl_notify
from sky_claw.app.gui.sky_claw_gui import (
    _build_settings_data,
    cargar_modo_local_defecto,
    save_settings,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.gui.views.forge_dashboard import (
    CONFIG_KEY_MODO_LOCAL_DEFECTO,
    STORE_KEY_MODO_LOCAL_DEFECTO,
    resolver_modo_local,
)
from sky_claw.app.security.hitl import CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA
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


@pytest.mark.parametrize("categoria", ["download", "sandbox_promotion", CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA])
async def test_lo_que_nunca_se_auto_aprueba_sigue_pidiendo_confirmacion(categoria: str) -> None:
    """Con la preferencia encendida (getter True) estas tres categorías siguen parkeando modal."""
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
