"""NGIO y Community Shaders: instalar en disco NO es activar — todas las superficies.

``ensure_community_shaders`` deja los mods en ``<mo2>/mods/``; sin ``+Nombre`` en el
``modlist.txt`` del perfil USVFS no los monta y el juego nunca ve ``CommunityShaders.dll``.
La activación la hace el CALLER, y hay dos: el agente LLM (``setup_tools``) y la GUI
(``run_ritual_install``). Este archivo corre el MISMO cuerpo de escenarios contra las dos,
con un ``MO2Controller`` REAL sobre un ``modlist.txt`` en disco: un doble append-only
(``_FakeMO2``) no puede fallar y por eso dejó pasar el defecto que motivó este archivo
(un ``-Community Shaders`` preexistente se ignoraba y se reportaba éxito).

NGIO (solo agente: no tiene ritual de GUI) comparte con CS el helper ``activar_en_modlist``
(``sky_claw/local/mo2/activacion_modlist.py``, capa neutral: ni el agente ni la GUI importan al otro) y
corre los mismos escenarios. Propiedad: *«instalado y activado» solo se afirma si el perfil quedó con TODAS las
entradas habilitadas, en cualquiera de las dos superficies, y esa decisión se toma en UN solo sitio.*
Anclas por enumeración al final: quién llama a ``add_mod_to_modlist`` (y desde qué función), quién
llama al helper, qué instaladores devuelven mods y qué ramas de cada superficie activan están
congelados por igualdad literal; un cuarto bucle de activación, o un instalador de mods que no
active, rompe el test hasta que se le escriba su receta. Los textos de la GUI se congelan por
caracterización (los dos extremos del veredicto) porque los lee una persona.
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import pathlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.agent.tools.external_tools import _AVISO_PERSISTENCIA, setup_tools
from sky_claw.app.gui.controllers.ritual_runner import (
    _MOTIVO_GUI_POR_ESTADO,
    RITUAL_INSTALLER_MAP,
    run_ritual_install,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.discovery.environment import EnvironmentSnapshot, MO2Info, SkyrimEdition, SkyrimInfo
from sky_claw.local.local_config import LocalConfig
from sky_claw.local.mo2.activacion_modlist import EstadoActivacion
from sky_claw.local.mo2.vfs import MO2Controller
from sky_claw.local.tools_installer import ModInstallResult, ToolsInstaller

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
    tipo: str = ""  # solo la GUI lo informa («positive» / «warning»); el agente lo expresa con ``exito``


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
    return _Salida(exito=feedback["type"] == "positive", texto=feedback["text"], tipo=feedback["type"])


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


# ── La GUI: los textos que lee una persona no cambian ────────────────────────────
# Caracterización: se escribieron contra el bloque inline ANTERIOR a unificar la activación y
# tienen que seguir pasando idénticos después. Cubren los DOS extremos del veredicto (el cartel
# verde y cada motivo del warning), que es lo único que el operador ve; el resto de los
# escenarios solo mira `exito`.
_REINTENTO_GUI = "Reintentá o activá los mods a mano en MO2."


async def test_la_gui_celebra_con_el_texto_de_siempre(tmp_path: pathlib.Path) -> None:
    inst = _instancia(tmp_path, "")

    salida = await _por_la_gui(inst)

    assert salida.tipo == "positive"
    assert salida.texto == f"«community_shaders» instalado y activado en el perfil «{PERFIL}»: {', '.join(MODS_CS)}."


@pytest.mark.parametrize(
    ("kwargs", "componentes", "motivo"),
    [
        ({"falla_en": MODS_CS[0]}, ", ".join(MODS_CS), "falló la escritura en modlist.txt"),  # fallo
        ({"falla_en": MODS_CS[1]}, ", ".join(MODS_CS), "falló la escritura en modlist.txt"),  # parcial
        ({"con_mo2": False}, ", ".join(MODS_CS), "no hay MO2 o perfil en el contexto de esta sesión"),
        ({"instalados": ()}, "sin componentes", "el instalador no devolvió componentes"),
    ],
    ids=["fallo", "parcial", "no_disponible", "vacio"],
)
async def test_la_gui_advierte_con_el_texto_de_siempre(
    tmp_path: pathlib.Path, kwargs: dict[str, Any], componentes: str, motivo: str
) -> None:
    inst = _instancia(tmp_path, "", **kwargs)

    salida = await _por_la_gui(inst)

    assert salida.tipo == "warning"
    assert salida.texto == f"«community_shaders» instalado en disco ({componentes}), pero {motivo}. {_REINTENTO_GUI}"


def test_la_gui_tiene_un_motivo_por_cada_estado_que_no_es_exito() -> None:
    """Igualdad literal con el vocabulario del helper: un estado nuevo sin cartel rompe acá, no en el rig.

    Los textos se congelan JUNTO con las claves porque son lo que lee el operador (ver arriba).
    """
    assert set(_MOTIVO_GUI_POR_ESTADO) == set(get_args(EstadoActivacion)) - {"ok"}
    assert _MOTIVO_GUI_POR_ESTADO == {
        "parcial": "falló la escritura en modlist.txt",
        "fallo": "falló la escritura en modlist.txt",
        "no_disponible": "no hay MO2 o perfil en el contexto de esta sesión",
        "vacio": "el instalador no devolvió componentes",
    }


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


# ── Anclas por enumeración ───────────────────────────────────────────────────────
# La propiedad que cierran, enunciada como propiedad del MECANISMO: *«¿quedó activado?» se decide
# en UN solo sitio (`activar_en_modlist`) y toda superficie que instala mods bajo `mods/` lo
# consume*. Antes el bloque vivía copiado en el agente y en la GUI; este archivo corría los mismos
# escenarios contra las dos, pero nada impedía que un tercer instalador (o un cambio de estado en
# una sola copia) los separara de nuevo: el ancla anterior sólo pedía que ambos archivos llamaran a
# `add_mod_to_modlist` (`>=`), no que compartieran el bucle.
#
# Todo se detecta por AST sobre el árbol REAL y se compara por igualdad literal. Escribir por qué
# un llamador queda afuera no cuenta como verificarlo: cada excluido está nombrado en el diccionario
# y un llamador nuevo rompe el test hasta que alguien lo clasifique.
_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_AGENTE = "sky_claw/app/agent/tools/external_tools.py"
_GUI = "sky_claw/app/gui/controllers/ritual_runner.py"
_HELPER = "sky_claw/local/mo2/activacion_modlist.py"
_MODULO_HELPER = "sky_claw.local.mo2.activacion_modlist"
_NOMBRE_HELPER = "activar_en_modlist"
_MUTADOR = "add_mod_to_modlist"

# (ruta, función calificada) → qué es. Un llamador del mutador que no esté acá, o acá y ausente del
# árbol, rompe `test_los_unicos_llamadores_de_add_mod_to_modlist_estan_enumerados`.
LLAMADORES_DE_ADD_MOD_TO_MODLIST: dict[tuple[str, str], str] = {
    (_HELPER, "activar_en_modlist"): "EL bucle de activación tras instalar herramientas (NGIO y CS), agente y GUI",
    # Un solo mod, con su propio contrato (`sanitize_for_prompt`, `errors`, lock de instalación):
    # no es un lote ni comparte el vocabulario de estados. Queda afuera A PROPÓSITO y nombrado.
    ("sky_claw/app/agent/tools/system_tools.py", "_instalar_con_contrato"): "FOMOD de UN mod (contrato propio)",
    # Config mod del grass cache sobre el perfil CLON, con `highest_priority=True`: otra operación.
    ("sky_claw/local/mo2/grass_profile.py", "GrassProfileManager.build_config_mod"): "mod de config de grass (clon)",
    # El propio controlador: `add_mod_to_modlist_highest_priority` delega en `add_mod_to_modlist`.
    ("sky_claw/local/mo2/vfs.py", "MO2Controller.add_mod_to_modlist_highest_priority"): "delegación del controlador",
}

# Quiénes consumen el helper. La GUI lo llama desde `run_ritual_install` y el agente desde las DOS
# ramas de `setup_tools` (la misma función: el detalle por rama lo congela el test de más abajo).
CONSUMIDORES_DEL_HELPER = frozenset({(_AGENTE, "setup_tools"), (_GUI, "run_ritual_install")})

# Instaladores de ToolsInstaller que devuelven `list[ModInstallResult]` (mods bajo `mods/`, sin exe):
# son los que HAY que activar. Se descubren por introspección de las anotaciones y se congelan.
INSTALADORES_QUE_DEVUELVEN_MODS = frozenset({"ensure_ngio", "ensure_community_shaders"})
# Claves de `RITUAL_INSTALLER_MAP` cuyo instalador devuelve mods (NGIO no tiene ritual de GUI).
RITUALES_DE_GUI_QUE_INSTALAN_MODS = frozenset({"community_shaders"})
# Ramas de `setup_tools` que instalan bajo `mods/` (usan `target_mods_dir`).
RAMAS_QUE_INSTALAN_EN_MODS = frozenset({"ngio", "community_shaders"})


class _Visitante(ast.NodeVisitor):
    """Recorre un árbol anotando, por llamada a ``nombre``, la función calificada que la contiene.

    Reconoce ``<expr>.nombre(...)`` y ``nombre(...)``. ``en_bucle`` registra las llamadas que viven
    dentro de un ``for``/``while``/comprensión de ESA función: un bucle manual es el defecto.
    """

    def __init__(self, nombre: str) -> None:
        self._nombre = nombre
        self._pila: list[str] = []
        self._bucles = 0
        self.llamadores: set[str] = set()
        self.en_bucle: set[str] = set()

    def _funcion(self, nodo: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._pila.append(nodo.name)
        previo, self._bucles = self._bucles, 0  # el bucle de una función externa no cuenta para una interna
        self.generic_visit(nodo)
        self._bucles = previo
        self._pila.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _funcion  # noqa: N815

    def _bucle(self, nodo: ast.AST) -> None:
        self._bucles += 1
        self.generic_visit(nodo)
        self._bucles -= 1

    visit_For = visit_AsyncFor = visit_While = _bucle  # noqa: N815
    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _bucle  # noqa: N815

    def visit_Call(self, nodo: ast.Call) -> None:
        f = nodo.func
        llamado = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        if llamado == self._nombre:
            calificado = ".".join(self._pila) or "<módulo>"
            self.llamadores.add(calificado)
            if self._bucles:
                self.en_bucle.add(calificado)
        self.generic_visit(nodo)


def _analizar(fuente: str, nombre: str) -> _Visitante:
    visitante = _Visitante(nombre)
    visitante.visit(ast.parse(fuente))
    return visitante


@functools.cache
def _fuentes_de_produccion() -> dict[str, str]:
    return {
        py.relative_to(_RAIZ).as_posix(): py.read_text(encoding="utf-8")
        for py in sorted((_RAIZ / "sky_claw").rglob("*.py"))
    }


def _llamadores_en_el_repo(nombre: str, *, solo_en_bucle: bool = False) -> set[tuple[str, str]]:
    salida: set[tuple[str, str]] = set()
    for ruta, fuente in _fuentes_de_produccion().items():
        if nombre not in fuente:  # barato: casi ningún módulo lo nombra
            continue
        visitante = _analizar(fuente, nombre)
        salida |= {(ruta, q) for q in (visitante.en_bucle if solo_en_bucle else visitante.llamadores)}
    return salida


def test_los_unicos_llamadores_de_add_mod_to_modlist_estan_enumerados() -> None:
    """Un cuarto sitio que active mods por su cuenta rompe esto, aunque copie el helper al pie de la letra."""
    assert _llamadores_en_el_repo(_MUTADOR) == set(LLAMADORES_DE_ADD_MOD_TO_MODLIST)


def test_el_unico_bucle_sobre_add_mod_to_modlist_es_el_del_helper() -> None:
    """«Prohibido el bucle manual fuera del helper»: el bucle es lo que decide parcial/fallo/ok.

    Los otros tres llamadores activan UN mod por llamada; si alguno pasara a iterar, estaría
    reimplementando la máquina de estados del helper.
    """
    assert _llamadores_en_el_repo(_MUTADOR, solo_en_bucle=True) == {(_HELPER, "activar_en_modlist")}


def test_los_consumidores_del_helper_estan_enumerados() -> None:
    assert _llamadores_en_el_repo(_NOMBRE_HELPER) == set(CONSUMIDORES_DEL_HELPER)


def _modulos_importados(ruta: str) -> dict[str, set[str]]:
    """Módulo importado → nombres traídos (``import x`` da ``{x: set()}``), contando imports anidados."""
    importados: dict[str, set[str]] = {}
    for nodo in ast.walk(ast.parse(_fuentes_de_produccion()[ruta])):
        if isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            importados.setdefault(nodo.module, set()).update(a.name for a in nodo.names)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                importados.setdefault(alias.name, set())
    return importados


def test_las_dos_superficies_importan_el_helper_de_la_capa_neutral_y_no_se_importan_entre_si() -> None:
    """El helper vive en ``sky_claw.local.mo2``; ni la GUI importa al agente ni el agente a la GUI.

    Si la GUI importara el helper desde ``external_tools`` (privado o no), la capa de presentación
    quedaría acoplada a la del agente LLM, que es justo lo que esta unificación evita.
    """
    for ruta in (_AGENTE, _GUI):
        assert _modulos_importados(ruta).get(_MODULO_HELPER) == {_NOMBRE_HELPER}, ruta
    assert not [m for m in _modulos_importados(_GUI) if m.startswith("sky_claw.app.agent")]
    assert not [m for m in _modulos_importados(_AGENTE) if m.startswith("sky_claw.app.gui")]


def _instaladores_que_devuelven_mods() -> set[str]:
    return {
        nombre
        for nombre, funcion in inspect.getmembers(ToolsInstaller, inspect.iscoroutinefunction)
        if nombre.startswith("ensure_") and "ModInstallResult" in str(inspect.signature(funcion).return_annotation)
    }


def test_los_instaladores_que_devuelven_mods_estan_enumerados_y_los_llama_el_agente() -> None:
    """Un ``ensure_*`` nuevo que devuelva ``list[ModInstallResult]`` rompe el ancla hasta que se lo cablee.

    - Agente: llama ``tools_installer.ensure_*`` de forma directa, así que el AST lo ve.
    - GUI: despacha dinámicamente (``getattr(installer, RITUAL_INSTALLER_MAP[...])``), que el AST no
      ve; se ancla el mapa. NGIO NO está en el mapa (no tiene ritual de GUI): si alguien lo agrega,
      este test rompe hasta que la GUI active también.
    """
    assert _instaladores_que_devuelven_mods() == INSTALADORES_QUE_DEVUELVEN_MODS
    llamadores = {ruta for m in INSTALADORES_QUE_DEVUELVEN_MODS for ruta, _ in _llamadores_en_el_repo(m)}
    assert llamadores == {_AGENTE}
    en_gui = {clave for clave, metodo in RITUAL_INSTALLER_MAP.items() if metodo in INSTALADORES_QUE_DEVUELVEN_MODS}
    assert en_gui == RITUALES_DE_GUI_QUE_INSTALAN_MODS
    assert "ngio" not in RITUAL_INSTALLER_MAP
    assert {s for _, s in CASOS} == {"agente", "gui"}
    assert {h for h, _ in CASOS} == set(RAMAS_QUE_INSTALAN_EN_MODS)


# ── Ramas por herramienta: agente (setup_tools) y GUI (run_ritual_install) ───────
def _ramas(ruta: str, funcion: str, variable: str) -> list[tuple[str, ast.If]]:
    """Las ``if <variable> == "<clave>"`` de una función, como lista (la GUI repite la clave en dos ``if``)."""
    arbol = ast.parse(_fuentes_de_produccion()[ruta])
    cuerpo = next(
        n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == funcion
    )
    ramas: list[tuple[str, ast.If]] = []
    for nodo in ast.walk(cuerpo):
        if not (isinstance(nodo, ast.If) and isinstance(nodo.test, ast.Compare)):
            continue
        izq, comp = nodo.test.left, nodo.test.comparators[0]
        if (
            isinstance(izq, ast.Name)
            and izq.id == variable
            and isinstance(comp, ast.Constant)
            and isinstance(comp.value, str)
        ):
            ramas.append((comp.value, nodo))
    return ramas


def _usa(rama: ast.If, nombre: str) -> bool:
    """``nombre`` aparece en el cuerpo PROPIO de la rama (no en el ``elif`` que cuelga del orelse)."""
    return any(isinstance(n, ast.Name) and n.id == nombre for s in rama.body for n in ast.walk(s))


def _llama(rama: ast.If, nombre: str) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == nombre
        for s in rama.body
        for n in ast.walk(s)
    )


def test_toda_rama_de_setup_tools_que_instala_en_mods_activa_con_el_helper_compartido() -> None:
    # Una rama "instala bajo mods/" si usa `target_mods_dir`. loot/xedit/pandora/bodyslide instalan
    # ejecutables en `install_dir` y no tienen nada que activar. Igualdad literal: una rama nueva
    # que instale mods rompe el test hasta que se la clasifique, y entonces tiene que llamar al helper.
    ramas = dict(_ramas(_AGENTE, "setup_tools", "tool_name_lower"))
    instalan = {clave for clave, rama in ramas.items() if _usa(rama, "target_mods_dir")}
    assert instalan == RAMAS_QUE_INSTALAN_EN_MODS
    for clave in sorted(instalan):
        rama = ramas[clave]
        assert _llama(rama, _NOMBRE_HELPER), f"la rama {clave!r} instala en mods/ y no activa con el helper"
        # Y el éxito sale del helper, no de un literal: `**activacion.como_campos()` en el dict de éxito.
        desempaca = any(
            isinstance(n, ast.Dict)
            and any(
                k is None
                and isinstance(v, ast.Call)
                and isinstance(v.func, ast.Attribute)
                and v.func.attr == "como_campos"
                for k, v in zip(n.keys, n.values, strict=True)
            )
            for s in rama.body
            for n in ast.walk(s)
        )
        assert desempaca, f"la rama {clave!r} no toma success/message de la activación"


def test_toda_rama_del_ritual_de_gui_que_instala_mods_activa_con_el_helper_compartido() -> None:
    """Hermano en el MISMO archivo: la GUI repetía la clave en dos ``if`` (despacho y veredicto) y sólo el segundo activa."""
    ramas = _ramas(_GUI, "run_ritual_install", "tool_key")
    activan = {clave for clave, rama in ramas if _llama(rama, _NOMBRE_HELPER)}
    assert activan == RITUALES_DE_GUI_QUE_INSTALAN_MODS
    # Ninguna rama de la GUI vuelve a activar por su cuenta: ya lo ancla el censo de llamadores del
    # mutador (arriba); acá sólo se ata la rama de `community_shaders` al cartel del helper.
    veredicto = next(rama for clave, rama in ramas if _llama(rama, _NOMBRE_HELPER))
    assert _usa(veredicto, "_MOTIVO_GUI_POR_ESTADO")


# ── El detector detecta ──────────────────────────────────────────────────────────
# Un ancla que no falla ante el defecto que persigue es peor que no tenerla: estos casos le dan
# al detector el defecto concreto (un bucle manual, un llamador nuevo, una importación cruzada).
_FUENTE_CON_BUCLE_MANUAL = """
async def instalar_ngio_a_mano(mods, mo2, perfil):
    for m in mods:
        await mo2.add_mod_to_modlist(m.mod_name, profile=perfil)
"""


def test_el_detector_ve_un_bucle_manual_en_un_modulo_nuevo() -> None:
    visitante = _analizar(_FUENTE_CON_BUCLE_MANUAL, _MUTADOR)

    assert visitante.llamadores == {"instalar_ngio_a_mano"}
    assert visitante.en_bucle == {"instalar_ngio_a_mano"}


def test_el_detector_distingue_la_llamada_suelta_del_bucle_y_califica_los_metodos() -> None:
    fuente = """
class Controlador:
    async def uno(self, mo2):
        await mo2.add_mod_to_modlist("X", profile="P")

    async def varios(self, mo2, nombres):
        activados = [await mo2.add_mod_to_modlist(n, profile="P") for n in nombres]
"""

    visitante = _analizar(fuente, _MUTADOR)

    assert visitante.llamadores == {"Controlador.uno", "Controlador.varios"}
    assert visitante.en_bucle == {"Controlador.varios"}


def test_el_detector_no_confunde_el_bucle_de_la_funcion_externa_con_el_de_la_interna() -> None:
    fuente = """
async def externa(mo2, nombres):
    for n in nombres:
        async def interna():
            await mo2.add_mod_to_modlist("X", profile="P")
"""

    visitante = _analizar(fuente, _MUTADOR)

    assert visitante.llamadores == {"externa.interna"}
    assert visitante.en_bucle == set()


def test_el_detector_ve_la_llamada_por_nombre_suelto_y_por_atributo() -> None:
    fuente = """
async def a(x):
    await activar_en_modlist(x)

async def b(x):
    await activacion_modlist.activar_en_modlist(x)
"""

    assert _analizar(fuente, _NOMBRE_HELPER).llamadores == {"a", "b"}
