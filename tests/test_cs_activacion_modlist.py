"""Community Shaders: instalar en disco NO es activar — las DOS superficies.

``ensure_community_shaders`` deja los mods en ``<mo2>/mods/``; sin ``+Nombre`` en el
``modlist.txt`` del perfil USVFS no los monta y el juego nunca ve ``CommunityShaders.dll``.
La activación la hace el CALLER, y hay dos: el agente LLM (``setup_tools``) y la GUI
(``run_ritual_install``). Este archivo corre el MISMO cuerpo de escenarios contra las dos,
con un ``MO2Controller`` REAL sobre un ``modlist.txt`` en disco: un doble append-only
(``_FakeMO2``) no puede fallar y por eso dejó pasar el defecto que motivó este archivo
(un ``-Community Shaders`` preexistente se ignoraba y se reportaba éxito).

Propiedad: *«instalado y activado» solo se afirma si el perfil quedó con TODAS las
entradas habilitadas, en cualquiera de las dos superficies.* Ancla por enumeración al
final: quién instala CS está congelado por igualdad literal y todo instalador tiene que
activar.
"""

from __future__ import annotations

import ast
import json
import pathlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.agent.tools.external_tools import setup_tools
from sky_claw.app.gui.controllers.ritual_runner import RITUAL_INSTALLER_MAP, run_ritual_install
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.discovery.environment import EnvironmentSnapshot, MO2Info, SkyrimEdition, SkyrimInfo
from sky_claw.local.local_config import LocalConfig
from sky_claw.local.mo2.vfs import MO2Controller
from sky_claw.local.tools_installer import ModInstallResult

PERFIL = "Sesion"
# Orden en que ``ensure_community_shaders`` los devuelve (dependencias primero).
MODS_CS = ("Address Library for SKSE Plugins", "SSE Engine Fixes", "Community Shaders")
SUPERFICIES = ("agente", "gui")


# ── Infraestructura ──────────────────────────────────────────────────────────────
class _MO2QueFallaEn(MO2Controller):
    """``MO2Controller`` real cuya escritura del modlist falla para UN mod."""

    def __init__(self, *args: Any, falla_en: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._falla_en = falla_en

    async def add_mod_to_modlist(self, mod_name: str, *args: Any, **kwargs: Any) -> None:
        if mod_name == self._falla_en:
            raise OSError("disco lleno")
        await super().add_mod_to_modlist(mod_name, *args, **kwargs)


@dataclass(frozen=True)
class _Instancia:
    raiz: pathlib.Path
    mods_dir: pathlib.Path
    modlist: pathlib.Path
    juego: pathlib.Path
    mo2: MO2Controller | None
    instalados: tuple[str, ...]


@dataclass(frozen=True)
class _Salida:
    """Lo que cada superficie le muestra a quien la usa, normalizado."""

    exito: bool
    texto: str


def _instancia(
    tmp_path: pathlib.Path,
    modlist_inicial: str,
    *,
    falla_en: str | None = None,
    con_mo2: bool = True,
    instalados: tuple[str, ...] = MODS_CS,
) -> _Instancia:
    raiz = tmp_path / "MO2"
    perfil = raiz / "profiles" / PERFIL
    perfil.mkdir(parents=True)
    modlist = perfil / "modlist.txt"
    modlist.write_text(modlist_inicial, encoding="utf-8-sig")
    juego = tmp_path / "Skyrim"
    juego.mkdir()
    (juego / "SkyrimSE.exe").write_bytes(b"exe")
    mo2 = _MO2QueFallaEn(raiz, path_validator=PathValidator(roots=[tmp_path]), falla_en=falla_en) if con_mo2 else None
    return _Instancia(raiz, raiz / "mods", modlist, juego, mo2, instalados)


def _resultados(inst: _Instancia) -> list[ModInstallResult]:
    return [
        ModInstallResult(mod_name=n, mod_dir=inst.mods_dir / n, version="v1", already_existed=False)
        for n in inst.instalados
    ]


def _lineas(inst: _Instancia) -> list[str]:
    texto = inst.modlist.read_text(encoding="utf-8-sig")
    return [ln.strip() for ln in texto.splitlines() if ln.strip()]


async def _por_el_agente(inst: _Instancia, monkeypatch: pytest.MonkeyPatch) -> tuple[_Salida, dict[str, Any]]:
    async def _no_guarda(*_a: Any, **_k: Any) -> None:
        return None

    # La persistencia no es lo que se prueba acá (tiene su archivo): que no interfiera.
    monkeypatch.setattr("sky_claw.local.local_config.guardar_config_bloqueante", _no_guarda)
    instalador = MagicMock()
    instalador.ensure_community_shaders = AsyncMock(return_value=_resultados(inst))
    local_cfg = LocalConfig(mo2_root=str(inst.raiz), skyrim_path=str(inst.juego))
    with (
        patch("sky_claw.local.discovery.scanner.detect_skyrim_edition", return_value=MagicMock(value="SE")),
        patch("sky_claw.local.discovery.scanner.read_skyrim_version", return_value="1.5.97"),
    ):
        crudo = await setup_tools(
            tools_installer=instalador,
            install_dir=inst.raiz,
            local_cfg=local_cfg,
            config_path=inst.raiz / "cfg.toml",
            downloader=MagicMock(),
            tools=["community_shaders"],
            gateway=MagicMock(),
            session=MagicMock(spec=aiohttp.ClientSession),
            mods_dir=inst.mods_dir,
            mo2=inst.mo2,
            profile=PERFIL,
        )
    entrada = json.loads(crudo)["community_shaders"]
    return _Salida(exito=entrada["success"], texto=entrada["message"]), entrada


class _InstaladorGui:
    def __init__(self, resultados: list[ModInstallResult]) -> None:
        self._resultados = resultados

    async def ensure_community_shaders(self, install_dir: Any, session: Any, downloader: Any, **kw: Any) -> Any:
        return self._resultados


async def _por_la_gui(inst: _Instancia) -> _Salida:
    snapshot = EnvironmentSnapshot(
        skyrim=SkyrimInfo(path=inst.juego, exe_name="SkyrimSE.exe", edition=SkyrimEdition.SE, version="1.5.97"),
        mo2=MO2Info(path=inst.raiz, profiles=[PERFIL], mods_dir=inst.mods_dir),
    )
    store = ReactiveStore()
    store.set(STORE_KEY_ENV, snapshot)
    config_path = inst.raiz.parent / "config.toml"
    config_path.write_text('llm_provider = "anthropic"\n', encoding="utf-8")
    ctx = SimpleNamespace(
        tools_installer=_InstaladorGui(_resultados(inst)),
        install_dir=None,
        session="sess",
        network=SimpleNamespace(downloader="dl"),
        config_path=config_path,
        mo2=inst.mo2,
        mo2_profile=PERFIL,
    )
    await run_ritual_install("community_shaders", app_context=ctx, store=store)
    feedback = store.get("ritual_feedback")
    assert feedback is not None, "la GUI siempre deja un veredicto visible"
    return _Salida(exito=feedback["type"] == "positive", texto=feedback["text"])


async def _correr(superficie: str, inst: _Instancia, monkeypatch: pytest.MonkeyPatch) -> _Salida:
    if superficie == "agente":
        salida, _ = await _por_el_agente(inst, monkeypatch)
        return salida
    return await _por_la_gui(inst)


# ── Comportamiento: las dos superficies, los mismos escenarios ───────────────────
@pytest.mark.parametrize("superficie", SUPERFICIES)
async def test_con_cs_deshabilitado_en_el_perfil_queda_habilitado_en_su_lugar(
    superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EL escenario del PR: el perfil ya tiene ``-Community Shaders``.

    Sin el fix de ``add_mod_to_modlist`` la entrada se ignoraba y las dos superficies
    afirmaban «activado» con el mod todavía deshabilitado en MO2.
    """
    inst = _instancia(tmp_path, "+Otra\n-Community Shaders\n")

    salida = await _correr(superficie, inst, monkeypatch)

    lineas = _lineas(inst)
    assert salida.exito, salida.texto
    assert "-Community Shaders" not in lineas
    assert lineas.index("+Community Shaders") == 1  # in place, detrás de «Otra»
    assert set(lineas) == {"+Otra", *(f"+{m}" for m in MODS_CS)}
    assert len(lineas) == len(set(lineas)), "ninguna entrada repetida"


@pytest.mark.parametrize("superficie", SUPERFICIES)
async def test_sin_entradas_previas_agrega_las_tres_habilitadas(
    superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = _instancia(tmp_path, "")

    salida = await _correr(superficie, inst, monkeypatch)

    assert salida.exito, salida.texto
    assert _lineas(inst) == [f"+{m}" for m in MODS_CS]


@pytest.mark.parametrize("superficie", SUPERFICIES)
async def test_si_la_escritura_falla_a_mitad_no_hay_exito_visual(
    superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activación parcial (falla el 2.º de 3): ninguna superficie afirma éxito."""
    inst = _instancia(tmp_path, "", falla_en="SSE Engine Fixes")

    salida = await _correr(superficie, inst, monkeypatch)

    assert not salida.exito
    assert _lineas(inst) == [f"+{MODS_CS[0]}"], "solo lo activado antes de la falla quedó escrito"


@pytest.mark.parametrize("superficie", SUPERFICIES)
async def test_sin_mo2_en_el_contexto_degrada_y_no_toca_el_perfil(
    superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = _instancia(tmp_path, "+Otra\n", con_mo2=False)

    salida = await _correr(superficie, inst, monkeypatch)

    assert not salida.exito
    assert _lineas(inst) == ["+Otra"]


@pytest.mark.parametrize("superficie", SUPERFICIES)
async def test_si_el_instalador_no_devuelve_componentes_no_hay_exito_vacuo(
    superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[]`` no es «todo activado»: ``len(activados) == len(mods)`` valdría ``0 == 0``."""
    inst = _instancia(tmp_path, "+Otra\n", instalados=())

    salida = await _correr(superficie, inst, monkeypatch)

    assert not salida.exito
    assert _lineas(inst) == ["+Otra"]


# ── Contrato del agente: el vocabulario de ``activacion`` ────────────────────────
@pytest.mark.parametrize(
    ("falla_en", "con_mo2", "instalados", "activacion", "activados"),
    [
        (None, True, MODS_CS, "ok", list(MODS_CS)),
        ("SSE Engine Fixes", True, MODS_CS, "parcial", [MODS_CS[0]]),
        (MODS_CS[0], True, MODS_CS, "fallo", []),
        (None, False, MODS_CS, "no_disponible", []),
        (None, True, (), "vacio", []),
    ],
)
async def test_el_agente_reporta_activacion_y_activados_sin_pisar_success(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    falla_en: str | None,
    con_mo2: bool,
    instalados: tuple[str, ...],
    activacion: str,
    activados: list[str],
) -> None:
    """``success`` es exactamente ``activacion == "ok"`` y ``message`` es vacío solo en éxito."""
    inst = _instancia(tmp_path, "", falla_en=falla_en, con_mo2=con_mo2, instalados=instalados)

    salida, entrada = await _por_el_agente(inst, monkeypatch)

    assert entrada["activacion"] == activacion
    assert entrada["activados"] == activados
    assert entrada["success"] is (activacion == "ok")
    assert (salida.texto == "") is (activacion == "ok")


# ── Ancla por enumeración: quién instala CS tiene que activar ────────────────────
_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_AGENTE = "sky_claw/app/agent/tools/external_tools.py"
_GUI = "sky_claw/app/gui/controllers/ritual_runner.py"


def _llamadas_a_metodos(ruta: str) -> set[str]:
    """Nombres de los métodos invocados como ``<expr>.<nombre>(...)`` en un módulo."""
    arbol = ast.parse((_RAIZ / ruta).read_text(encoding="utf-8"))
    return {
        nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
    }


def _modulos_que_llaman(metodo: str) -> set[str]:
    return {
        py.relative_to(_RAIZ).as_posix()
        for py in (_RAIZ / "sky_claw").rglob("*.py")
        if metodo in _llamadas_a_metodos(py.relative_to(_RAIZ).as_posix())
    }


def test_las_superficies_que_instalan_cs_estan_enumeradas_y_todas_activan() -> None:
    """Un instalador de CS nuevo que no active rompe este test hasta que se le escriba su receta.

    - Agente: llama ``tools_installer.ensure_community_shaders`` de forma directa, así que
      el AST lo ve y el conjunto se congela por igualdad literal.
    - GUI: despacha dinámicamente (``getattr(installer, RITUAL_INSTALLER_MAP[...])``), que el
      AST no ve; se ancla el mapa y el consumidor, y ``forge_dashboard`` solo lo mira por
      pertenencia.
    Cada superficie tiene que invocar ``add_mod_to_modlist`` y tener su escenario arriba.
    """
    assert _modulos_que_llaman("ensure_community_shaders") == {_AGENTE}
    assert RITUAL_INSTALLER_MAP["community_shaders"] == "ensure_community_shaders"
    assert _modulos_que_llaman("add_mod_to_modlist") >= {_AGENTE, _GUI}
    assert set(SUPERFICIES) == {"agente", "gui"}
