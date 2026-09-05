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
  3. pasando ``self`` como service locator hacia la composición o el dispatcher
     (``test_no_pasa_self_como_service_locator``, ver #518);
  4. reimplementando el routing de tools en la facade pública en vez de delegar en
     el dispatcher extraído (``test_dispatch_tool_sigue_delegando``).

**Detección por convención, no por muestra** (review interno #553). El dominio se
detecta por convención de nombre —cualquier símbolo con ``Runner`` o ``Analyzer``
en el nombre— más un set explícito para los que no siguen convención
(``parse_active_plugins``, ``AssetConflictDetector``, servicios de la composición).
Así un runner/analyzer NUEVO o un alias (``XEditRunnerV2``, ``XEditPipelineRunner``,
``ConflictAnalyzerImpl``) queda cubierto sin editar una lista literal: se enumera la
FAMILIA por regla, no una muestra congelada.

**Ancla sobre la clase, no sobre un ``__file__``** (review interno #553). El AST se
resuelve desde ``inspect.getsourcefile(SupervisorAgent)``, así que si el módulo se
renombra/mueve el guardrail SIGUE a la clase en vez de dar un falso verde sobre el
archivo viejo; si el ancla se pierde del todo, falla ruidoso con mensaje explícito
(no un ``FileNotFoundError`` críptico).

Lo que el guardrail PERMITE deliberadamente (es la frontera SANA, no dominio):

  - delegar a servicios ya cableados (``self._wrye_bash_service.execute_pipeline(...)``);
  - construir en ``__init__`` los SEAMS que el Supervisor cablea —``AssetConflictScanner``,
    ``RecordConflictScanner``, ``PluginLimitGuard``, ``GrassRuntimeDepsProvider``—:
    eso es wiring, el dominio vive DENTRO de esas clases;
  - importar los DTOs de retorno de las facades (``ConflictReport``,
    ``AssetConflictReport``, el tipo ``AssetConflictDetector``);
  - lifecycle, bridges de eventos y facades de compatibilidad (delegación estrecha).

**Alcance deliberado.** El guardrail es un ancla estructural, no un oráculo
adversarial de análisis de flujo (metaprompt §14: proteger la frontera, no congelar
sintaxis). Cubre la forma REALISTA de cada regresión; evasiones sintácticas
rebuscadas (aliasar ``svc = self`` y pasar ``svc``; reimplementar el routing con un
``if/elif`` sobre ``tool_name`` en vez de un ``match``) quedan respaldadas por los
tests conductuales de ``tests/test_supervisor_dispatch_tool.py``, que fijan el
comportamiento observable de ``dispatch_tool`` y romperían ante un router inline.
NO congela: números de línea, forma de expresiones triviales, atributos del
constructor, ni nombres de métodos privados.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from sky_claw.app.orchestrator.supervisor import SupervisorAgent


def _resolver_fuente_del_supervisor() -> tuple[Path, ast.Module]:
    """Resuelve el archivo fuente del ``SupervisorAgent`` DESDE LA CLASE.

    Anclar sobre ``inspect.getsourcefile(SupervisorAgent)`` (y no sobre un
    ``modulo.__file__`` fijo) hace que el guardrail siga a la clase si su módulo
    se renombra o se mueve —cerrando el "falso verde por reflexión de módulo
    congelado" (review interno #553)— y que un ancla perdida falle RUIDOSA con un
    mensaje accionable en vez de un ``FileNotFoundError`` críptico.
    """
    ruta = inspect.getsourcefile(SupervisorAgent)
    if ruta is None or not Path(ruta).is_file():
        raise AssertionError(
            f"el guardrail perdió su ancla: no pude localizar el fuente de "
            f"SupervisorAgent (getsourcefile={ruta!r}). Si el módulo del Supervisor "
            f"se movió, el ancla lo sigue por la clase; revisá que la clase siga "
            f"siendo importable desde sky_claw.app.orchestrator.supervisor."
        )
    fuente = Path(ruta)
    return fuente, ast.parse(fuente.read_text(encoding="utf-8"), filename=str(fuente))


_SUPERVISOR_SRC, _SUPERVISOR_AST = _resolver_fuente_del_supervisor()


# --- Detección de dominio: FAMILIA por convención + set explícito -------------
#
# Convención: todo runner/analyzer de dominio lleva ``Runner`` o ``Analyzer`` en
# el nombre. Detectar por convención (subcadena) —en vez de una lista literal de
# nombres— cubre runners/analyzers NUEVOS y sus alias (``XEditRunnerV2``,
# ``XEditPipelineRunner``, ``ConflictAnalyzerImpl``) sin tener que editarla: se
# enumera la FAMILIA por regla, no una muestra. Ninguno de los imports/llamadas
# legítimos de ``supervisor.py`` contiene esas subcadenas (los DTOs son
# ``*Report``; los seams que cablea son ``*Scanner``/``*Guard``/``*Provider``).
_SUBCADENAS_DE_DOMINIO = ("Runner", "Analyzer")

# Símbolos de dominio que NO siguen la convención de nombre y hay que nombrar:
#  - el parser puro del load order (re-parsear plugins en el Supervisor es dominio);
_DOMINIO_EXPLICITO_IMPORTAR = frozenset({"parse_active_plugins"})
#  - además, construir el detector de assets (inline de un scan) o los servicios
#    que arma ``build_orchestration_composition`` es reabsorber dominio. NO se
#    listan los seams que el Supervisor SÍ cablea (``AssetConflictScanner``,
#    ``RecordConflictScanner``, ``PluginLimitGuard``, ``GrassRuntimeDepsProvider``):
#    construir esos es wiring legítimo. El tipo ``AssetConflictDetector`` SÍ se
#    importa (retorno de la facade); lo prohibido es CONSTRUIRLO.
_DOMINIO_EXPLICITO_INVOCAR = frozenset(
    {
        "parse_active_plugins",
        "AssetConflictDetector",
        "WryeBashPipelineService",
        "GrassCacheService",
    }
)


def _es_dominio(nombre: str, explicitos: frozenset[str]) -> bool:
    """¿``nombre`` es implementación de dominio? Por convención o por el set explícito."""
    return any(sub in nombre for sub in _SUBCADENAS_DE_DOMINIO) or nombre in explicitos


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


def test_guardrail_ancla_sobre_la_clase_supervisor() -> None:
    """El ancla apunta al archivo donde vive ``SupervisorAgent`` HOY.

    Si la clase se mueve a otro módulo, ``inspect.getsourcefile`` la sigue y el
    AST analizado es el correcto; este test lo deja explícito y falla ruidoso si
    el ancla dejara de coincidir con el módulo real de la clase (cierre del
    "falso verde por módulo congelado", review interno #553).
    """
    fuente_de_la_clase = inspect.getsourcefile(SupervisorAgent)
    assert fuente_de_la_clase is not None and Path(fuente_de_la_clase) == _SUPERVISOR_SRC, (
        "el AST analizado no corresponde al archivo donde vive SupervisorAgent: "
        f"clase en {fuente_de_la_clase!r} vs. ancla {_SUPERVISOR_SRC}."
    )
    assert _SUPERVISOR_SRC.name == "supervisor.py", (
        f"SupervisorAgent se mudó a {_SUPERVISOR_SRC.name}: el guardrail lo sigue por la "
        "clase, pero confirmá que el módulo nuevo es el hogar canónico del Supervisor."
    )


def test_no_importa_dominio_de_herramientas() -> None:
    """M3 (parcial) / regresión de import: ningún runner/analyzer/parser cruza al Supervisor."""
    filtrados = {n for n in _nombres_importados() if _es_dominio(n, _DOMINIO_EXPLICITO_IMPORTAR)}
    assert not filtrados, (
        f"supervisor.py importó implementación de dominio: {sorted(filtrados)}. "
        "Esa lógica vive en su seam (runner/analyzer/parser); el Supervisor sólo "
        "conoce sus contratos/DTOs y recibe el objeto ya construido por la composición."
    )


def test_no_invoca_dominio_de_herramientas() -> None:
    """M1/M2/M4: el Supervisor no construye runners/analyzers/detectors ni re-parsea plugins.

    Cubre construir ``XEditRunner``/``ConflictAnalyzer`` (M1/M2) —y cualquier alias
    ``*Runner``/``*Analyzer`` por convención—, el inline de un scan vía
    ``AssetConflictDetector`` (M4) y ``parse_active_plugins`` movido al Supervisor
    (M3). Permite construir los seams de composición (no son dominio).
    """
    filtrados = {n for n in _nombres_invocados() if _es_dominio(n, _DOMINIO_EXPLICITO_INVOCAR)}
    assert not filtrados, (
        f"supervisor.py invocó/construyó dominio de herramientas: {sorted(filtrados)}. "
        "Cablealo en build_orchestration_composition / su seam e inyectá el resultado; "
        "el Supervisor coordina, no fabrica runners, scanners ni parsers."
    )


def test_no_pasa_self_como_service_locator() -> None:
    """#518: pasar ``self`` a un colaborador es Service Locator.

    Se bloquea ``self`` como argumento posicional, por keyword, o expandido
    (``f(self)``, ``f(x=self)``, ``f(*self)``, ``f(**self)``). Pasar
    ``self.<colaborador>`` (un atributo concreto) es inyección explícita y NO cae
    acá. Evasiones por aliasing (``svc = self; svc.dispatch(...)``) exceden un
    ancla AST y quedan respaldadas por los tests conductuales (ver docstring del módulo).
    """
    ofensores: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if not isinstance(nodo, ast.Call):
            continue
        # posicionales (incluye ``*self``), y valores de keyword (incluye ``**self``).
        candidatos = [(arg.value if isinstance(arg, ast.Starred) else arg) for arg in nodo.args]
        candidatos += [kw.value for kw in nodo.keywords]
        if any(isinstance(arg, ast.Name) and arg.id == "self" for arg in candidatos):
            ofensores.add(ast.unparse(nodo.func))
    assert not ofensores, (
        f"supervisor.py pasa `self` a: {sorted(ofensores)}. Inyectá "
        "dependencias explícitas (self.<colaborador>), no el Supervisor entero: un "
        "callee que recibe self puede alcanzar cualquier cosa en runtime (Service Locator)."
    )


def test_dispatch_tool_sigue_delegando() -> None:
    """La facade pública ``dispatch_tool`` (contrato en contracts.py) delega en el
    dispatcher extraído en vez de reimplementar el match/case monolítico que PR1
    estranguló.

    Robusto a refactors equivalentes (M5): sólo exige que exista una llamada
    ``.dispatch(...)`` y que NO haya un ``match`` dentro del método. Renombrar el
    atributo del dispatcher o partir la expresión en dos líneas sigue pasando.
    Un router equivalente por ``if/elif`` sobre ``tool_name`` excede un ancla AST
    y queda respaldado por los tests conductuales de ``dispatch_tool``
    (``tests/test_supervisor_dispatch_tool.py``), que fijan su comportamiento.
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
