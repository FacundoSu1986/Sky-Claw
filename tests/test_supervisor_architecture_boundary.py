"""Guardrail arquitectónico del cierre del Strangler Fig de ``SupervisorAgent`` (PR7).

PR1–PR6 estrangularon la lógica de dominio de herramientas fuera del
``SupervisorAgent`` hacia seams dedicados (``OrchestrationToolDispatcher``,
``build_orchestration_composition``, ``GrassRuntimeDepsProvider``,
``AssetConflictScanner``, ``RecordConflictScanner``, ``PluginLimitGuard``). PR7 no
extrae nada nuevo —ya no queda lógica de dominio residual en el Supervisor: sus
responsabilidades son lifecycle, bridges de eventos, wiring de composición y
facades de compatibilidad— sino que **congela la frontera** para que ese dominio
no vuelva a entrar.

La frontera se define por CÓMO reingresaría el dominio, no por la sintaxis exacta
del código de hoy (el review de #545 revirtió un ancla que fijaba forma AST
trivial: acá no se repite ese error). El dominio de herramientas sólo puede
volver al ``supervisor.py`` de cuatro maneras, y cada test cierra una:

  1. importando un símbolo de IMPLEMENTACIÓN de dominio —un runner, el analyzer de
     conflictos, el parser del load order— (``test_no_importa_dominio_de_herramientas``);
  2. invocando/construyendo dominio in-situ —``XEditRunner()``, ``ConflictAnalyzer()``,
     ``parse_active_plugins(...)``, o el inline de un scan vía ``AssetConflictDetector()``—
     (``test_no_invoca_dominio_de_herramientas``);
  3. pasando ``self`` pelado como service locator hacia la composición o el
     dispatcher (``test_no_pasa_self_como_service_locator``, ver #518);
  4. reimplementando el routing de tools en la facade pública en vez de delegar en
     el dispatcher extraído (``test_dispatch_tool_sigue_delegando``).

Lo que el guardrail PERMITE deliberadamente (es la frontera SANA, no dominio):

  - delegar a servicios ya cableados (``self._wrye_bash_service.execute_pipeline(...)``);
  - construir en ``__init__`` los SEAMS que el Supervisor cablea —``AssetConflictScanner``,
    ``RecordConflictScanner``, ``PluginLimitGuard``, ``GrassRuntimeDepsProvider``—:
    eso es wiring, el dominio vive DENTRO de esas clases;
  - importar los DTOs de retorno de las facades (``ConflictReport``,
    ``AssetConflictReport``, el tipo ``AssetConflictDetector``);
  - lifecycle, bridges de eventos y facades de compatibilidad (delegación estrecha).

NO congela: números de línea, la forma de expresiones triviales, el conjunto de
atributos del constructor, ni nombres de métodos privados — sólo la frontera. Un
refactor equivalente de una delegación legítima debe seguir pasando (ver el test
de ``dispatch_tool``, tolerante a renombres y a partir la expresión).
"""

from __future__ import annotations

import ast
from pathlib import Path

from sky_claw.app.orchestrator import supervisor as supervisor_mod

_SUPERVISOR_SRC = Path(supervisor_mod.__file__)
_SUPERVISOR_AST = ast.parse(_SUPERVISOR_SRC.read_text(encoding="utf-8"), filename=str(_SUPERVISOR_SRC))


# Símbolos de IMPLEMENTACIÓN de dominio (verificados en el repo). Importar
# cualquiera al supervisor.py significa que el dominio volvió a cruzar la
# frontera. El denylist es por NOMBRE de símbolo (no por módulo) a propósito: el
# supervisor importa DTOs de estos mismos módulos —``ConflictReport`` de
# ``conflict_analyzer``, ``AssetConflictReport`` de ``assets``— y esos SÍ son
# legítimos (tipos de retorno de las facades).
_DOMINIO_PROHIBIDO_IMPORTAR = frozenset(
    {
        "XEditRunner",  # sky_claw/local/xedit/runner.py
        "ConflictAnalyzer",  # sky_claw/local/xedit/conflict_analyzer.py (NO ConflictReport)
        "LOOTRunner",  # sky_claw/local/loot/cli.py
        "DynDOLODRunner",  # sky_claw/local/tools/dyndolod_runner.py
        "SynthesisRunner",  # sky_claw/local/tools/synthesis_runner.py
        "PandoraRunner",  # sky_claw/local/tools/pandora_runner.py
        "GrassCacheRunner",  # sky_claw/local/tools/grass_cache_runner.py
        "WryeBashRunner",  # sky_claw/local/tools/wrye_bash_runner.py
        "parse_active_plugins",  # sky_claw/app/orchestrator/active_plugins.py (re-parseo de plugins)
    }
)

# Callees que el Supervisor no debe INVOCAR: construir un runner/analyzer/detector
# de dominio, construir un servicio que arma la composición, o llamar al parser de
# plugins. Se chequea el nombre TERMINAL del callee, así que atrapa tanto
# ``Foo(...)`` como ``modulo.Foo(...)`` sin depender de la forma del import.
#
# NO incluye los seams que el Supervisor SÍ cablea en ``__init__``
# (``AssetConflictScanner``, ``RecordConflictScanner``, ``PluginLimitGuard``,
# ``GrassRuntimeDepsProvider``): construir esos es wiring legítimo — el dominio
# vive dentro de ellos, no en el Supervisor.
_DOMINIO_PROHIBIDO_INVOCAR = frozenset(
    (_DOMINIO_PROHIBIDO_IMPORTAR - {"WryeBashRunner"})
    | {
        "AssetConflictDetector",  # inline de scan_asset_conflicts (el tipo SÍ se importa; construirlo NO)
        "WryeBashPipelineService",  # la arma build_orchestration_composition, no el Supervisor
        "GrassCacheService",  # idem
        "WryeBashRunner",  # explícito para dejar el set legible
    }
)


def _nombres_importados() -> set[str]:
    """Todo símbolo (from-import) o módulo (import) que trae supervisor.py."""
    nombres: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, ast.ImportFrom):
            nombres.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Import):
            nombres.update(alias.name.split(".")[-1] for alias in nodo.names)
    return nombres


def _nombres_invocados() -> set[str]:
    """Nombre terminal del callee de cada llamada: ``Foo(...)`` -> ``Foo``,
    ``mod.Foo(...)`` -> ``Foo``. Captura construcción/invocación sin importar la
    forma del import (directo o calificado)."""
    invocados: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, ast.Call):
            callee = nodo.func
            if isinstance(callee, ast.Name):
                invocados.add(callee.id)
            elif isinstance(callee, ast.Attribute):
                invocados.add(callee.attr)
    return invocados


def _funcion(nombre: str) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, (ast.AsyncFunctionDef, ast.FunctionDef)) and nodo.name == nombre:
            return nodo
    return None


def test_no_importa_dominio_de_herramientas() -> None:
    """M3 (parcial) / regresión de import: ningún runner/analyzer/parser cruza al Supervisor."""
    filtrados = _DOMINIO_PROHIBIDO_IMPORTAR & _nombres_importados()
    assert not filtrados, (
        f"supervisor.py importó implementación de dominio: {sorted(filtrados)}. "
        "Esa lógica vive en su seam (runner/analyzer/parser); el Supervisor sólo "
        "conoce sus contratos/DTOs y recibe el objeto ya construido por la composición."
    )


def test_no_invoca_dominio_de_herramientas() -> None:
    """M1/M2/M4: el Supervisor no construye runners/analyzers/detectors ni re-parsea plugins.

    Cubre construir ``XEditRunner``/``ConflictAnalyzer`` (M1/M2), inline de un scan
    vía ``AssetConflictDetector`` (M4) y ``parse_active_plugins`` movido al
    Supervisor (M3). Permite construir los seams de composición (no están en el
    denylist).
    """
    filtrados = _DOMINIO_PROHIBIDO_INVOCAR & _nombres_invocados()
    assert not filtrados, (
        f"supervisor.py invocó/construyó dominio de herramientas: {sorted(filtrados)}. "
        "Cablealo en build_orchestration_composition / su seam e inyectá el resultado; "
        "el Supervisor coordina, no fabrica runners, scanners ni parsers."
    )


def test_no_pasa_self_como_service_locator() -> None:
    """#518: pasar ``self`` pelado a un colaborador es Service Locator.

    Pasar ``self.<colaborador>`` (un atributo concreto) es inyección explícita y
    NO cae acá — sólo miramos el ``Name`` pelado ``self`` como argumento.
    """
    ofensores: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if not isinstance(nodo, ast.Call):
            continue
        argumentos = [*nodo.args, *(kw.value for kw in nodo.keywords)]
        if any(isinstance(arg, ast.Name) and arg.id == "self" for arg in argumentos):
            ofensores.add(ast.unparse(nodo.func))
    assert not ofensores, (
        f"supervisor.py pasa `self` pelado a: {sorted(ofensores)}. Inyectá "
        "dependencias explícitas (self.<colaborador>), no el Supervisor entero: un "
        "callee que recibe self puede alcanzar cualquier cosa en runtime (Service Locator)."
    )


def test_dispatch_tool_sigue_delegando() -> None:
    """La facade pública ``dispatch_tool`` (contrato en contracts.py) delega en el
    dispatcher extraído en vez de reimplementar el match/case monolítico que PR1
    estranguló.

    Robusto a refactors equivalentes (M5): sólo exige que exista una llamada
    ``.dispatch(...)`` y que NO haya un ``match`` dentro del método. Renombrar el
    atributo del dispatcher o partir la expresión en dos líneas sigue pasando;
    volver a un árbol de decisión sobre ``tool_name`` falla.
    """
    fn = _funcion("dispatch_tool")
    assert fn is not None, "desapareció la facade pública dispatch_tool (contrato de contracts.py roto)"

    delega = any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "dispatch"
        for nodo in ast.walk(fn)
    )
    assert delega, (
        "dispatch_tool dejó de delegar en el dispatcher (no hay llamada .dispatch(...)): "
        "el routing de tools vive en tool_strategies/ vía OrchestrationToolDispatcher, "
        "no en un match/case dentro del Supervisor."
    )
    tiene_match = any(isinstance(nodo, ast.Match) for nodo in ast.walk(fn))
    assert not tiene_match, (
        "dispatch_tool volvió a contener un `match` — routing de tools inline. "
        "El dispatch es responsabilidad del OrchestrationToolDispatcher."
    )
