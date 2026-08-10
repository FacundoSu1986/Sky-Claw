"""Ancla del invariante del perfil MO2 de sesión.

Por qué existe: el PR #454 creó la flag ``--profile`` y un perfil único de sesión
(``_resolve_mo2_profile``), lo cableó a la familia FOMOD y dejó al resto del árbol
en el literal ``"Default"``. La auditoría post-merge de #452–#458 encontró tres
consecuencias que la suite entera (4487 tests en verde) no detectaba:

- de los ocho tools del registry que operan sobre un perfil, solo
  ``install_mod_from_archive`` recibía el de la sesión;
- ``_bootloader.py`` construía el ``SupervisorAgent`` sin ``profile_name``, así que
  agente y GUI trabajaban sobre perfiles distintos en el mismo proceso;
- ``app_context`` hardcodeaba ``profile="Default"`` en el runner de LOOT —el que
  MUTA ``plugins.txt``/``loadorder.txt``— y en la sincronización inicial.

El ancla que debía atajarlo (``test_app_context_profile.py``) falla por **muestrear**:
afirma que tres strings están en el fuente de ``_start_full_inner``, y pasaba en verde
con los ``profile="Default"`` que quedaban en esa misma función.

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
``AGENTS.md``). La propiedad que congelan es del MECANISMO, no del proceso:

    el perfil de sesión solo aísla si NINGÚN camino puede elegir otro.

De ahí las cuatro familias de abajo: la superficie que el LLM ve, el
comportamiento observable de cada tool, los literales en los call sites y los
constructores del supervisor.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry, system_tools
from sky_claw.app.security.path_validator import PathViolationError
from sky_claw.local.fomod.installer import InstallResult

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Perfil de sesión usado por los tests de comportamiento. Deliberadamente NO es
#: "Default": el defecto original era invisible justo porque el valor correcto y el
#: fallback coincidían en la instalación típica.
PERFIL = "Requiem"


# ---------------------------------------------------------------------------
# 1. Superficie: el LLM no elige el perfil
# ---------------------------------------------------------------------------


def _registry(**kwargs: Any) -> AsyncToolRegistry:
    base: dict[str, Any] = {"registry": None, "mo2": None, "sync_engine": None}
    base.update(kwargs)
    return AsyncToolRegistry(**base)


def test_ningun_params_model_expone_profile_al_llm() -> None:
    """El perfil queda fijo durante la sesión (spec de #454): exponerlo como
    argumento del tool contradice ese contrato y devuelve al LLM la capacidad de
    elegir el perfil equivocado. Un tool nuevo que lo reexponga rompe acá."""
    expuestos = {
        nombre: sorted(td.params_model.model_fields)
        for nombre, td in _registry()._tools.items()
        if td.params_model is not None and "profile" in td.params_model.model_fields
    }

    assert expuestos == {}, f"tools que devuelven la elección del perfil al LLM: {expuestos}"


def test_el_perfil_de_sesion_se_valida_al_construir_el_registry() -> None:
    """Al sacar ``profile`` de los schemas se va también su ``pattern`` de pydantic,
    que era lo que rechazaba ``"Default; rm -rf /"``. La defensa no desaparece: se
    muda al constructor, que es el único punto por el que entra ahora."""
    with pytest.raises(PathViolationError):
        _registry(mo2_profile="Default; rm -rf /")
    with pytest.raises(PathViolationError):
        _registry(mo2_profile="../escape")


# ---------------------------------------------------------------------------
# 2. Comportamiento: la familia completa usa el perfil de sesión
# ---------------------------------------------------------------------------


def _familia_por_introspeccion() -> set[str]:
    """Handlers de ``system_tools`` que reciben un perfil MO2.

    Se deriva del código, no de una lista escrita a mano: agregar un handler con
    parámetro ``profile`` mete el nombre acá solo y rompe la igualdad literal de
    :data:`FAMILIA_DEL_PERFIL` hasta que alguien decida qué hacer con él.
    """
    encontrados = set()
    for nombre, obj in vars(system_tools).items():
        if not inspect.iscoroutinefunction(obj) or nombre.startswith("_"):
            continue
        if "profile" in inspect.signature(obj).parameters:
            encontrados.add(nombre)
    return encontrados


#: Igualdad literal, no subconjunto: es la diferencia entre atajar al hermano que
#: falta y atajar solo a los que ya conocíamos.
FAMILIA_DEL_PERFIL = {
    "analyze_esp_conflicts",
    "check_load_order",
    "detect_conflicts",
    "install_mod_from_archive",
    "launch_game",
    "run_loot_sort",
    "toggle_mod",
    "uninstall_mod",
}


def test_la_familia_del_perfil_esta_congelada() -> None:
    assert _familia_por_introspeccion() == FAMILIA_DEL_PERFIL


class _FakeMO2:
    """Registra el perfil con el que se invocó CADA operación de MO2."""

    def __init__(self) -> None:
        self.root = pathlib.Path.home() / "MO2Portable"
        self.perfiles_vistos: list[tuple[str, str]] = []

    async def read_modlist(self, profile: str) -> AsyncIterator[tuple[str, bool]]:
        self.perfiles_vistos.append(("read_modlist", profile))
        return
        yield  # pragma: no cover — hace de esto un generador asíncrono vacío

    async def add_mod_to_modlist(self, mod_name: str, profile: str = "Default") -> None:
        self.perfiles_vistos.append(("add_mod_to_modlist", profile))

    async def toggle_mod_in_modlist(self, mod_name: str, profile: str, enable: bool) -> None:
        self.perfiles_vistos.append(("toggle_mod_in_modlist", profile))

    async def remove_mod_from_modlist(self, mod_name: str, profile: str) -> None:
        self.perfiles_vistos.append(("remove_mod_from_modlist", profile))

    async def delete_mod_files(self, mod_name: str) -> None:
        return None

    async def launch_game(self, profile: str) -> dict[str, str]:
        self.perfiles_vistos.append(("launch_game", profile))
        return {"status": "launched"}


class _FakeHITL:
    async def request_approval(self, **kwargs: Any) -> Any:
        from sky_claw.app.security.hitl import Decision

        return Decision.APPROVED


class _FakeFomodInstaller:
    file_state_provider = None

    async def install(self, **kwargs: Any) -> InstallResult:
        return InstallResult(mod_name="TestMod", files_copied=["a.esp"], installed=True)


class _FakeLootResult:
    success = True
    return_code = 0
    sorted_plugins: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []


class _FakeLootRunner:
    async def sort(self) -> _FakeLootResult:
        return _FakeLootResult()


class _FakeXEditRunner:
    pass


class _FakeRegistry:
    async def find_missing_masters_for_mods(self, mods: list[str]) -> list[Any]:
        return []


#: Una receta por miembro de :data:`FAMILIA_DEL_PERFIL`: con qué argumentos se
#: invoca el tool. El perfil NO aparece en ningún lado — ese es justamente el punto.
#: Un handler nuevo sin receta rompe :func:`test_toda_la_familia_tiene_receta`, y la
#: receta lo mete automáticamente en el test de comportamiento.
RECETAS: dict[str, dict[str, Any]] = {
    "analyze_esp_conflicts": {},
    "check_load_order": {},
    "detect_conflicts": {},
    "install_mod_from_archive": {
        "archive_path": str(pathlib.Path.home() / "Modding" / "TestMod.zip"),
        "selections": {},
    },
    "launch_game": {},
    "run_loot_sort": {},
    "toggle_mod": {"mod_name": "TestMod", "enable": True},
    "uninstall_mod": {"mod_name": "TestMod"},
}


def test_toda_la_familia_tiene_receta() -> None:
    assert set(RECETAS) == FAMILIA_DEL_PERFIL


@pytest.mark.parametrize("tool", sorted(RECETAS))
async def test_el_tool_usa_el_perfil_de_sesion(tool: str) -> None:
    """Ninguna operación de MO2 puede ver un perfil distinto al de la sesión.

    Se afirma sobre lo que el tool le pidió a MO2, no sobre el fuente del wiring:
    un lambda que ignore ``self._mo2_profile`` pasa cualquier ancla textual y muere
    acá.
    """
    mo2 = _FakeMO2()
    registry = _registry(
        registry=_FakeRegistry(),
        mo2=mo2,
        hitl=_FakeHITL(),
        fomod_installer=_FakeFomodInstaller(),
        loot_runner=_FakeLootRunner(),
        xedit_runner=_FakeXEditRunner(),
        mo2_profile=PERFIL,
    )

    salida = await registry.execute(tool, RECETAS[tool])

    equivocados = [(op, visto) for op, visto in mo2.perfiles_vistos if visto != PERFIL]
    assert not equivocados, f"{tool} operó sobre otro perfil: {equivocados}"
    # Los tools que devuelven el perfil en su payload también tienen que nombrar el
    # de la sesión: es lo que el LLM lee y repite al operador.
    payload = json.loads(salida)
    if isinstance(payload, dict) and "profile" in payload:
        assert payload["profile"] == PERFIL


# ---------------------------------------------------------------------------
# 3. Call sites: ningún literal de perfil en el wiring
# ---------------------------------------------------------------------------

#: Llamadas con un perfil literal, contadas por módulo. Se congela el CONTEO y no
#: solo el nombre: agregar un segundo call site dentro de un módulo ya listado es el
#: punto de extensión más probable, y con un set de nombres pasaría sin romper nada
#: (mismo criterio que ``tests/test_db_connection_invariant.py``).
LITERALES_ESPERADOS: dict[str, int] = {
    # `profile="agent-tool"` NO es un perfil de MO2: es la etiqueta de identidad con
    # la que el pipeline de Wrye Bash firma su manifiesto en el journal. Exención
    # deliberada.
    "sky_claw/app/agent/tools/system_tools.py": 1,
}

_KWARGS_DE_PERFIL = {"profile", "profile_name", "source_profile"}


def _call_sites_con_perfil_literal(archivo: pathlib.Path) -> list[tuple[int, str, str]]:
    try:
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover — el gate de ruff ya lo atajaría
        return []
    hallados = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        for kw in nodo.keywords:
            if kw.arg in _KWARGS_DE_PERFIL and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                hallados.append((nodo.lineno, kw.arg, kw.value.value))
    return hallados


def test_el_wiring_no_pasa_perfiles_literales() -> None:
    real: dict[str, int] = {}
    detalle: dict[str, list[tuple[int, str, str]]] = {}
    for archivo in sorted(PAQUETE.rglob("*.py")):
        hallados = _call_sites_con_perfil_literal(archivo)
        if hallados:
            clave = str(archivo.relative_to(RAIZ)).replace("\\", "/")
            real[clave] = len(hallados)
            detalle[clave] = hallados

    assert real == LITERALES_ESPERADOS, (
        "cambió el conjunto de call sites con perfil literal; decidí si el nuevo "
        f"participa del perfil de sesión o es una exención consciente: {detalle}"
    )


def test_ninguna_linea_de_comando_fija_el_perfil() -> None:
    """Hermano posicional: los runners que invocan un binario arman ``--profile`` y
    su valor como dos elementos de una lista, así que el ancla de kwargs de arriba no
    los ve. ``SynthesisRunner`` tenía ``["--profile", "Default"]`` mientras el
    servicio que lo construye YA resolvía el perfil activo para su resolver de
    fuentes — el patch se generaba contra un perfil y sus fuentes contra otro.

    Se barre TODO el paquete, no solo Synthesis: cualquier runner futuro que fije el
    perfil en su línea de comando rompe acá.
    """
    fijos: list[str] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.List | ast.Tuple):
                continue
            for anterior, siguiente in zip(nodo.elts, nodo.elts[1:], strict=False):
                es_flag = isinstance(anterior, ast.Constant) and anterior.value in ("--profile", "--profile-name")
                if es_flag and isinstance(siguiente, ast.Constant):
                    clave = str(archivo.relative_to(RAIZ)).replace("\\", "/")
                    fijos.append(f"{clave}:{siguiente.lineno} → {siguiente.value!r}")

    assert fijos == [], f"perfil fijo en una línea de comando: {fijos}"


def test_todo_caller_de_la_familia_pasa_el_perfil() -> None:
    """Los handlers de la familia reciben ``profile`` keyword-only y sin default:
    un caller que lo omita revienta con ``TypeError`` recién en runtime.

    Ese hueco NO lo tapa ningún gate: ``sky_claw.app.agent.*`` tiene
    ``ignore_errors = true`` en la config de mypy (``pyproject.toml``), así que el
    type-check no ve ``system_tools.py``. Y el ancla de literales de arriba solo
    mira kwargs con valor constante, así que una llamada que directamente omita el
    argumento le pasa por al lado (hallazgo de review de Qodo, #460).

    Se enumeran TODAS las llamadas del paquete a esos nombres. Solo se miran
    llamadas por nombre pelado: ``mo2.launch_game(...)`` es el método de
    ``MO2Controller``, otra función que casualmente comparte nombre.
    """
    sin_perfil: list[str] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Name):
                continue
            if nodo.func.id not in FAMILIA_DEL_PERFIL:
                continue
            if not any(kw.arg == "profile" for kw in nodo.keywords):
                clave = str(archivo.relative_to(RAIZ)).replace("\\", "/")
                sin_perfil.append(f"{clave}:{nodo.lineno} → {nodo.func.id}")

    assert sin_perfil == [], f"callers de la familia que no pasan el perfil: {sin_perfil}"


# ---------------------------------------------------------------------------
# 4. Constructores del supervisor y del resolver
# ---------------------------------------------------------------------------


def test_toda_construccion_del_resolver_declara_su_perfil() -> None:
    """``PathResolutionService`` es la fuente del perfil para LOOT, DynDOLOD,
    Pandora, Wrye Bash y Synthesis (todos leen ``get_active_profile()``).

    Hoy hay una sola construcción en el paquete y pasa el perfil del supervisor,
    pero eso es un hecho verificado una vez, no un invariante: un resolver nuevo
    construido sin perfil devolvería el fallback y esos cinco runners volverían a
    divergir del perfil de sesión sin que nada se ponga rojo (hallazgo de review de
    Qodo, #460). Este ancla lo congela.
    """
    sin_perfil: list[str] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            objetivo = nodo.func
            nombre = objetivo.attr if isinstance(objetivo, ast.Attribute) else getattr(objetivo, "id", "")
            if nombre != "PathResolutionService":
                continue
            if not any(kw.arg == "profile_name" for kw in nodo.keywords):
                clave = str(archivo.relative_to(RAIZ)).replace("\\", "/")
                sin_perfil.append(f"{clave}:{nodo.lineno}")

    assert sin_perfil == [], f"PathResolutionService construido sin perfil explícito en {sin_perfil}"


def test_ancla_api_publica_de_la_deteccion_de_runner_por_perfil() -> None:
    """La rama sin lock de ``run_loot_sort`` consume la detección de runner por
    perfil desde el alias público de ``loot_service``, que es su fuente única.

    Si un rename interno rompiera el alias, este test lo atrapa en import time y no
    en la corrida real; y si alguien duplicara la detección en ``system_tools``,
    reabriría el desfasaje que el helper existe para cerrar (mismo criterio que
    ``test_ancla_api_publica_de_los_helpers_de_lock`` para T-31).
    """
    import sky_claw.local.tools.loot_service as loot_service

    assert loot_service.es_runner_por_perfil is loot_service._is_per_profile_runner


def test_las_dos_resoluciones_de_perfil_coinciden_ante_cli_y_entorno_en_conflicto() -> None:
    """Las dos superficies resuelven el perfil por caminos distintos —``AppContext``
    para las tools del agente, ``PathResolver`` para LOOT/DynDOLOD/Pandora/Wrye
    Bash/Synthesis— y tienen que llegar al mismo valor.

    El caso que las separaba era exactamente este: ``--profile`` y ``MO2_PROFILE``
    seteados a la vez con valores distintos. ``AppContext`` daba prioridad al CLI y
    ``get_active_profile()`` a la variable de entorno, así que el perfil inyectado
    se perdía y la divergencia GUI↔agente sobrevivía al wiring correcto.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from sky_claw.app.core.path_resolver import resolver_perfil_activo
    from sky_claw.app_context import _resolve_mo2_profile

    with patch.dict("os.environ", {"MO2_PROFILE": "PerfilDelEntorno"}):
        del_app_context = _resolve_mo2_profile(SimpleNamespace(profile=PERFIL))
        del_path_resolver = resolver_perfil_activo(del_app_context)

        assert del_app_context == PERFIL
        assert del_path_resolver == PERFIL

    # Sin CLI, la variable de entorno sigue siendo la fuente en AMBOS caminos.
    with patch.dict("os.environ", {"MO2_PROFILE": "PerfilDelEntorno"}):
        sin_cli = _resolve_mo2_profile(SimpleNamespace(profile=""))

        assert sin_cli == "PerfilDelEntorno"
        assert resolver_perfil_activo(None) == "PerfilDelEntorno"


def test_toda_construccion_del_supervisor_declara_su_perfil() -> None:
    """El defecto era exactamente este: ``_bootloader.py`` construía el supervisor
    sin ``profile_name`` cuatro líneas después de que ``start_full`` resolviera el
    perfil de la sesión. Se enumeran TODAS las construcciones del paquete."""
    sin_perfil: list[str] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            objetivo = nodo.func
            nombre = objetivo.attr if isinstance(objetivo, ast.Attribute) else getattr(objetivo, "id", "")
            if nombre != "SupervisorAgent":
                continue
            declara = any(kw.arg == "profile_name" for kw in nodo.keywords) or bool(nodo.args)
            if not declara:
                clave = str(archivo.relative_to(RAIZ)).replace("\\", "/")
                sin_perfil.append(f"{clave}:{nodo.lineno}")

    assert sin_perfil == [], f"SupervisorAgent construido sin perfil explícito en {sin_perfil}"
