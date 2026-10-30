"""NGIO y Community Shaders: instalar en disco NO es activar — todas las superficies.

``ensure_community_shaders`` deja los mods en ``<mo2>/mods/``; sin ``+Nombre`` en el
``modlist.txt`` del perfil USVFS no los monta y el juego nunca ve ``CommunityShaders.dll``.
La activación la hace el CALLER, y hay dos: el agente LLM (``setup_tools``) y la GUI
(``run_ritual_install``). Este archivo corre el MISMO cuerpo de escenarios contra las dos,
con un ``MO2Controller`` REAL sobre un ``modlist.txt`` en disco: un doble append-only
(``_FakeMO2``) no puede fallar y por eso dejó pasar el defecto que motivó este archivo
(un ``-Community Shaders`` preexistente se ignoraba y se reportaba éxito).

NGIO (solo agente: no tiene ritual de GUI) comparte el helper ``_activar_en_modlist`` con CS y
corre los mismos escenarios. Propiedad: *«instalado y activado» solo se afirma si el perfil quedó con TODAS las
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

from sky_claw.app.agent.tools.external_tools import _AVISO_PERSISTENCIA, setup_tools
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
# Cada caso es (herramienta, superficie). NGIO no tiene ritual de GUI (`RITUAL_INSTALLER_MAP`
# no lo lista; lo congela el ancla del final), así que solo corre por el agente.
MODS_NGIO = ("No Grass In Objects NG", "Address Library for SKSE Plugins")  # edición SE
MODS_POR_HERRAMIENTA = {"ngio": MODS_NGIO, "community_shaders": MODS_CS}
CASOS = (("ngio", "agente"), ("community_shaders", "agente"), ("community_shaders", "gui"))
CASOS_IDS = [f"{h}-{s}" for h, s in CASOS]


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
    instalados: tuple[str, ...] | None = None,
    herramienta: str = "community_shaders",
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
    if instalados is None:
        instalados = MODS_POR_HERRAMIENTA[herramienta]
    return _Instancia(raiz, raiz / "mods", modlist, juego, mo2, instalados)


def _resultados(inst: _Instancia) -> list[ModInstallResult]:
    return [
        ModInstallResult(mod_name=n, mod_dir=inst.mods_dir / n, version="v1", already_existed=False)
        for n in inst.instalados
    ]


def _lineas(inst: _Instancia) -> list[str]:
    texto = inst.modlist.read_text(encoding="utf-8-sig")
    return [ln.strip() for ln in texto.splitlines() if ln.strip()]


async def _por_el_agente(
    inst: _Instancia, monkeypatch: pytest.MonkeyPatch, herramienta: str = "community_shaders"
) -> tuple[_Salida, dict[str, Any]]:
    async def _no_guarda(*_a: Any, **_k: Any) -> None:
        return None

    # La persistencia no es lo que se prueba acá (tiene su archivo): que no interfiera.
    monkeypatch.setattr("sky_claw.local.local_config.guardar_config_bloqueante", _no_guarda)
    instalador = MagicMock()
    setattr(instalador, f"ensure_{herramienta}", AsyncMock(return_value=_resultados(inst)))
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
            tools=[herramienta],
            gateway=MagicMock(),
            session=MagicMock(spec=aiohttp.ClientSession),
            mods_dir=inst.mods_dir,
            mo2=inst.mo2,
            profile=PERFIL,
        )
    entrada = json.loads(crudo)[herramienta]
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


async def _correr(herramienta: str, superficie: str, inst: _Instancia, monkeypatch: pytest.MonkeyPatch) -> _Salida:
    if superficie == "agente":
        salida, _ = await _por_el_agente(inst, monkeypatch, herramienta)
        return salida
    return await _por_la_gui(inst)


# ── Comportamiento: todas las superficies, los mismos escenarios ─────────────────
_PARAM = pytest.mark.parametrize(("herramienta", "superficie"), CASOS, ids=CASOS_IDS)


def _nombre_en_modlist(herramienta: str) -> str:
    """Un mod de la herramienta que sirve de objeto del escenario «deshabilitado»."""
    return {"ngio": "No Grass In Objects NG", "community_shaders": "Community Shaders"}[herramienta]


@_PARAM
async def test_con_el_mod_deshabilitado_en_el_perfil_queda_habilitado_en_su_lugar(
    herramienta: str, superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EL escenario del PR #825: el perfil ya tiene ``-<mod>``.

    Sin el fix de ``add_mod_to_modlist`` la entrada se ignoraba y las superficies
    afirmaban «activado» con el mod todavía deshabilitado en MO2.
    """
    mod = _nombre_en_modlist(herramienta)
    inst = _instancia(tmp_path, f"+Otra\n-{mod}\n", herramienta=herramienta)

    salida = await _correr(herramienta, superficie, inst, monkeypatch)

    lineas = _lineas(inst)
    assert salida.exito, salida.texto
    assert f"-{mod}" not in lineas
    assert lineas.index(f"+{mod}") == 1  # in place, detrás de «Otra»
    assert set(lineas) == {"+Otra", *(f"+{m}" for m in inst.instalados)}
    assert len(lineas) == len(set(lineas)), "ninguna entrada repetida"


@_PARAM
async def test_sin_entradas_previas_agrega_todas_habilitadas(
    herramienta: str, superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = _instancia(tmp_path, "", herramienta=herramienta)

    salida = await _correr(herramienta, superficie, inst, monkeypatch)

    assert salida.exito, salida.texto
    assert _lineas(inst) == [f"+{m}" for m in inst.instalados]


@_PARAM
async def test_si_la_escritura_falla_a_mitad_no_hay_exito_visual(
    herramienta: str, superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activación parcial (falla el 2.º): ninguna superficie afirma éxito."""
    segundo = MODS_POR_HERRAMIENTA[herramienta][1]
    inst = _instancia(tmp_path, "", falla_en=segundo, herramienta=herramienta)

    salida = await _correr(herramienta, superficie, inst, monkeypatch)

    assert not salida.exito
    assert _lineas(inst) == [f"+{inst.instalados[0]}"], "solo lo activado antes de la falla quedó escrito"


@_PARAM
async def test_sin_mo2_en_el_contexto_degrada_y_no_toca_el_perfil(
    herramienta: str, superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrato: sin ``mo2``/``profile`` el resultado es ``no_disponible`` y ``success: False``
    (para NGIO es un cambio deliberado: antes devolvía ``success: True`` sin activar)."""
    inst = _instancia(tmp_path, "+Otra\n", con_mo2=False, herramienta=herramienta)

    salida = await _correr(herramienta, superficie, inst, monkeypatch)

    assert not salida.exito
    assert _lineas(inst) == ["+Otra"]


# `vacio` solo se ejerce con CS: `ensure_ngio` arma siempre NGIO + Address Library (o lanza),
# así que `[]` es inalcanzable para NGIO. El guard vive en el helper compartido y CS lo cubre.
@pytest.mark.parametrize(("herramienta", "superficie"), [c for c in CASOS if c[0] == "community_shaders"])
async def test_si_el_instalador_no_devuelve_componentes_no_hay_exito_vacuo(
    herramienta: str, superficie: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[]`` no es «todo activado»: ``len(activados) == len(mods)`` valdría ``0 == 0``."""
    inst = _instancia(tmp_path, "+Otra\n", instalados=(), herramienta=herramienta)

    salida = await _correr(herramienta, superficie, inst, monkeypatch)

    assert not salida.exito
    assert _lineas(inst) == ["+Otra"]


# ── Contrato del agente: el vocabulario de ``activacion`` ────────────────────────
@pytest.mark.parametrize("herramienta", ["ngio", "community_shaders"])
@pytest.mark.parametrize(
    ("falla_en", "con_mo2", "vacia", "activacion", "n_activados"),
    [
        (None, True, False, "ok", 99),  # 99 = todos
        (1, True, False, "parcial", 1),
        (0, True, False, "fallo", 0),
        (None, False, False, "no_disponible", 0),
        (None, True, True, "vacio", 0),
    ],
)
async def test_el_agente_reporta_activacion_y_activados_sin_pisar_success(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    herramienta: str,
    falla_en: int | None,
    con_mo2: bool,
    vacia: bool,
    activacion: str,
    n_activados: int,
) -> None:
    """``success`` es exactamente ``activacion == "ok"`` y ``message`` es vacío solo en éxito."""
    if vacia and herramienta == "ngio":
        pytest.skip("`ensure_ngio` nunca devuelve []: rama inalcanzable para NGIO")
    mods = MODS_POR_HERRAMIENTA[herramienta]
    inst = _instancia(
        tmp_path,
        "",
        falla_en=None if falla_en is None else mods[falla_en],
        con_mo2=con_mo2,
        instalados=() if vacia else mods,
        herramienta=herramienta,
    )

    salida, entrada = await _por_el_agente(inst, monkeypatch, herramienta)

    esperados = list(mods[: n_activados if n_activados != 99 else len(mods)]) if not vacia else []
    assert entrada["activacion"] == activacion
    assert entrada["activados"] == esperados
    assert entrada["success"] is (activacion == "ok")
    assert (salida.texto == "") is (activacion == "ok")


# ── Aviso de persistencia sobre una entrada ya fallida (fix B) ───────────────────
@pytest.mark.parametrize("herramienta", ["ngio", "community_shaders"])
async def test_activacion_fallida_y_guardado_fallido_reportan_ambos_motivos(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, herramienta: str
) -> None:
    """Si la activación falla Y el guardado de config lanza, el mensaje no oculta el segundo."""
    inst = _instancia(tmp_path, "", falla_en=MODS_POR_HERRAMIENTA[herramienta][0], herramienta=herramienta)

    async def _explota(*_a: Any, **_k: Any) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr("sky_claw.local.local_config.guardar_config_bloqueante", _explota)
    instalador = MagicMock()
    setattr(instalador, f"ensure_{herramienta}", AsyncMock(return_value=_resultados(inst)))
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
            tools=[herramienta],
            gateway=MagicMock(),
            session=MagicMock(spec=aiohttp.ClientSession),
            mods_dir=inst.mods_dir,
            mo2=inst.mo2,
            profile=PERFIL,
        )

    entrada = json.loads(crudo)[herramienta]
    assert entrada["success"] is False
    assert entrada["activacion"] == "fallo"
    assert "disco lleno" in entrada["message"], "motivo de la activación conservado"
    assert _AVISO_PERSISTENCIA in entrada["message"], "motivo del guardado anexado"


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
      pertenencia. NGIO NO está en el mapa (no tiene ritual de GUI): si alguien lo agrega,
      este test rompe hasta que la GUI active también.
    Cada superficie tiene que invocar ``add_mod_to_modlist`` y tener su escenario arriba.
    """
    assert _modulos_que_llaman("ensure_community_shaders") == {_AGENTE}
    assert RITUAL_INSTALLER_MAP["community_shaders"] == "ensure_community_shaders"
    assert "ngio" not in RITUAL_INSTALLER_MAP
    assert _modulos_que_llaman("ensure_ngio") == {_AGENTE}
    assert _modulos_que_llaman("add_mod_to_modlist") >= {_AGENTE, _GUI}
    assert {s for _, s in CASOS} == {"agente", "gui"}
    assert {h for h, _ in CASOS} == set(RAMAS_QUE_INSTALAN_EN_MODS)


# ── Ancla por enumeración: las ramas de `setup_tools` que instalan bajo mods/ ────
# Una rama "instala bajo mods/" si usa `target_mods_dir` (el directorio `mods/` de MO2 que
# le pasa a su `ensure_*`). loot/xedit/pandora/bodyslide instalan ejecutables en `install_dir`
# y no tienen nada que activar. Igualdad literal: una rama nueva que instale mods rompe el
# test hasta que se la clasifique, y entonces tiene que llamar al helper compartido.
RAMAS_QUE_INSTALAN_EN_MODS = frozenset({"ngio", "community_shaders"})


def _ramas_de_setup_tools() -> dict[str, ast.If]:
    arbol = ast.parse((_RAIZ / _AGENTE).read_text(encoding="utf-8"))
    funcion = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "setup_tools")
    ramas: dict[str, ast.If] = {}
    for nodo in ast.walk(funcion):
        if not (isinstance(nodo, ast.If) and isinstance(nodo.test, ast.Compare)):
            continue
        izq, comp = nodo.test.left, nodo.test.comparators[0]
        if (
            isinstance(izq, ast.Name)
            and izq.id == "tool_name_lower"
            and isinstance(comp, ast.Constant)
            and isinstance(comp.value, str)
        ):
            ramas[comp.value] = nodo
    return ramas


def _usa(rama: ast.If, nombre: str) -> bool:
    """``nombre`` aparece en el cuerpo PROPIO de la rama (no en el ``elif`` que cuelga del orelse)."""
    return any(isinstance(n, ast.Name) and n.id == nombre for s in rama.body for n in ast.walk(s))


def test_toda_rama_de_setup_tools_que_instala_en_mods_activa_con_el_helper_compartido() -> None:
    ramas = _ramas_de_setup_tools()
    instalan = {clave for clave, rama in ramas.items() if _usa(rama, "target_mods_dir")}
    assert instalan == RAMAS_QUE_INSTALAN_EN_MODS
    for clave in sorted(instalan):
        rama = ramas[clave]
        assert _usa(rama, "_activar_en_modlist"), f"la rama {clave!r} instala en mods/ y no activa"
        # Y el éxito sale del helper, no de un literal: `**activacion_campos` en el dict de éxito.
        desempaca = any(
            isinstance(n, ast.Dict)
            and any(
                k is None and isinstance(v, ast.Name) and v.id == "activacion_campos"
                for k, v in zip(n.keys, n.values, strict=True)
            )
            for s in rama.body
            for n in ast.walk(s)
        )
        assert desempaca, f"la rama {clave!r} no toma success/message de la activación"
