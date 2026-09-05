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
volver al módulo del Supervisor de cuatro maneras, y cada test cierra una:

  1. importando implementación de dominio —un runner, el analyzer, el parser del
     load order, un servicio de la composición— sea por nombre, por forma-módulo
     (``import sky_claw.local...``) o por ``import *`` (``test_no_importa_dominio_de_herramientas``);
  2. invocando/construyendo dominio in-situ —un runner/analyzer, el detector de
     assets, o cualquier servicio que produce la composición— (``test_no_invoca_dominio_de_herramientas``);
  3. pasando ``self`` como service locator hacia la composición o el dispatcher
     (``test_no_pasa_self_como_service_locator``, ver #518);
  4. reimplementando el routing de tools en la facade pública —``match``, ``if/elif``
     sobre ``tool_name``, tabla ``[tool_name]``, ``getattr(..., tool_name)``— en vez
     de delegar en el dispatcher (``test_dispatch_tool_sigue_delegando``).

**Enumerar la FAMILIA, no una muestra** (reviews internos de #553). El dominio se
detecta por:
  - convención de nombre (cualquier símbolo con ``Runner``/``Analyzer``): cubre
    runners/analyzers NUEVOS y alias (``XEditRunnerV2``, ``ConflictAnalyzerImpl``);
  - introspección de la composición: los tipos que produce
    ``OrchestrationComposition`` (los 7 servicios + dispatcher/deps/middleware/máquina
    de estados) se derivan de sus anotaciones, así que un servicio NUEVO agregado a
    la composición queda cubierto sin editar este test;
  - un set explícito para los no-convencionales (``parse_active_plugins``,
    ``AssetConflictDetector``);
  - la CAPA de dominio por ruta de módulo (``sky_claw.local.*`` + el parser
    ``active_plugins``) para las formas-módulo y ``import *`` que un denylist por
    nombre no ve.

**Ancla sobre la CLASE, no sobre un ``__file__``.** El AST se resuelve desde
``inspect.getsourcefile(SupervisorAgent)`` y las búsquedas de método se acotan a la
``ClassDef`` de ``SupervisorAgent``: si el módulo se renombra/mueve el guardrail
SIGUE a la clase (no da falso verde sobre el archivo viejo), y no valida un
``dispatch_tool`` homónimo de otra clase/helper; si el ancla se pierde, falla
ruidoso con mensaje explícito.

Lo que el guardrail PERMITE deliberadamente (frontera SANA, no dominio):
  - delegar a servicios ya cableados (``self._wrye_bash_service.execute_pipeline(...)``);
  - construir en ``__init__`` los SEAMS que el Supervisor cablea —``AssetConflictScanner``,
    ``RecordConflictScanner``, ``PluginLimitGuard``, ``GrassRuntimeDepsProvider``,
    ``PathResolutionService``— (wiring, no dominio: el dominio vive DENTRO de ellos);
  - importar DTOs de retorno de las facades (``ConflictReport``,
    ``AssetConflictReport``, el tipo ``AssetConflictDetector``);
  - lifecycle, bridges de eventos y facades de compatibilidad.

**Alcance deliberado.** Es un ancla estructural, no un oráculo de análisis de flujo
(metaprompt §14: proteger la frontera, no congelar sintaxis). Evasiones por aliasing
de datos (``svc = self; svc.dispatch(...)``) exceden un ancla AST y quedan
respaldadas por los tests conductuales de ``tests/test_supervisor_dispatch_tool.py``,
que fijan el comportamiento observable de ``dispatch_tool``. NO congela: números de
línea, forma de expresiones triviales, atributos del constructor, ni métodos privados.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from sky_claw.app.orchestrator.orchestration_composition import OrchestrationComposition
from sky_claw.app.orchestrator.supervisor import SupervisorAgent


def _resolver_fuente_del_supervisor() -> tuple[Path, ast.Module]:
    """Resuelve el archivo fuente del ``SupervisorAgent`` DESDE LA CLASE.

    Anclar sobre ``inspect.getsourcefile(SupervisorAgent)`` (y no sobre un
    ``modulo.__file__`` fijo) hace que el guardrail siga a la clase si su módulo
    se renombra o se mueve, y que un ancla perdida falle RUIDOSA con un mensaje
    accionable en vez de un ``FileNotFoundError`` críptico.
    """
    ruta = inspect.getsourcefile(SupervisorAgent)
    if ruta is None or not Path(ruta).is_file():
        raise AssertionError(
            f"el guardrail perdió su ancla: no pude localizar el fuente de "
            f"SupervisorAgent (getsourcefile={ruta!r}). El ancla sigue a la clase; "
            f"revisá que siga siendo importable desde sky_claw.app.orchestrator.supervisor."
        )
    fuente = Path(ruta)
    return fuente, ast.parse(fuente.read_text(encoding="utf-8"), filename=str(fuente))


_SUPERVISOR_SRC, _SUPERVISOR_AST = _resolver_fuente_del_supervisor()


def _clase_supervisor() -> ast.ClassDef:
    """La ``ClassDef`` de ``SupervisorAgent`` en el AST anclado (falla ruidoso si no está)."""
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            return nodo
    raise AssertionError("no se encontró la ClassDef de SupervisorAgent en el AST anclado")


# --- Detección de dominio: FAMILIA por convención + introspección + explícito ---
#
# Convención: todo runner/analyzer lleva ``Runner``/``Analyzer`` en el nombre.
_SUBCADENAS_DE_DOMINIO = ("Runner", "Analyzer")


def _tipos_de_la_composicion() -> frozenset[str]:
    """Nombres de los tipos que PRODUCE ``build_orchestration_composition``.

    El Supervisor RECIBE todos estos objetos de la composición (``composition.X``)
    y no debe construir ninguno. Se derivan por INTROSPECCIÓN de las anotaciones
    del dataclass ``OrchestrationComposition`` (no una lista a mano): los 7
    servicios de pipeline + dispatcher/deps/middleware/máquina de estados. Un
    servicio/componente NUEVO en la composición queda cubierto sin tocar este test
    —enumera la familia, no una muestra (review interno #553).
    """
    nombres: set[str] = set()
    for anotacion in OrchestrationComposition.__annotations__.values():
        texto = anotacion if isinstance(anotacion, str) else getattr(anotacion, "__name__", str(anotacion))
        # Identificadores Capitalizados = nombres de clase (ignora genéricos/None).
        nombres.update(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", texto))
    return frozenset(nombres)


_COMPOSICION = _tipos_de_la_composicion()

# No-convencionales que igual son dominio: el parser puro del load order y —sólo
# para CONSTRUCCIÓN— el detector de assets (su tipo SÍ se importa como retorno de
# la facade; construirlo in-situ es inline de un scan).
_DOMINIO_EXPLICITO_IMPORTAR = frozenset({"parse_active_plugins"}) | _COMPOSICION
_DOMINIO_EXPLICITO_INVOCAR = frozenset({"parse_active_plugins", "AssetConflictDetector"}) | _COMPOSICION

# Capa de dominio por RUTA de módulo — para formas que un denylist por nombre no
# ve: ``import sky_claw.local.xedit.runner as x`` y ``from ...local... import *``.
# El Supervisor sólo importa DTOs puntuales de esa capa con ``from X import Nombre``
# (que sí analiza el check por nombre); nunca la importa en forma-módulo ni con star.
_PAQUETES_DE_DOMINIO = ("sky_claw.local.",)
_MODULOS_DE_DOMINIO_EXTRA = frozenset({"sky_claw.app.orchestrator.active_plugins"})


def _es_dominio_por_nombre(nombre: str, explicitos: frozenset[str]) -> bool:
    return any(sub in nombre for sub in _SUBCADENAS_DE_DOMINIO) or nombre in explicitos


def _es_modulo_de_dominio(ruta: str) -> bool:
    return ruta.startswith(_PAQUETES_DE_DOMINIO) or ruta in _MODULOS_DE_DOMINIO_EXTRA


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


def _metodo_de_clase(clase: ast.ClassDef, nombre: str) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    """Método DIRECTO de ``clase`` por nombre (no funciones anidadas ni de otras clases)."""
    for nodo in clase.body:
        if isinstance(nodo, (ast.AsyncFunctionDef, ast.FunctionDef)) and nodo.name == nombre:
            return nodo
    return None


def _referencia_tool_name(sub: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "tool_name" for n in ast.walk(sub))


def test_guardrail_ancla_sobre_la_clase_supervisor() -> None:
    """El ancla apunta al archivo donde vive ``SupervisorAgent`` HOY.

    Si la clase se mueve a otro módulo, ``inspect.getsourcefile`` la sigue; este
    test lo deja explícito y falla ruidoso si el ancla dejara de coincidir con el
    módulo real de la clase (cierre del "falso verde por módulo congelado").
    """
    fuente_de_la_clase = inspect.getsourcefile(SupervisorAgent)
    assert fuente_de_la_clase is not None and Path(fuente_de_la_clase) == _SUPERVISOR_SRC, (
        "el AST analizado no corresponde al archivo donde vive SupervisorAgent: "
        f"clase en {fuente_de_la_clase!r} vs. ancla {_SUPERVISOR_SRC}."
    )
    assert _clase_supervisor() is not None  # además, la ClassDef existe en ese archivo


def test_no_importa_dominio_de_herramientas() -> None:
    """Regresión de import (M3 parcial): ningún runner/analyzer/parser/servicio de
    dominio cruza al Supervisor — por nombre, por forma-módulo o por ``import *``.

    - ``from X import Nombre``: se chequea el NOMBRE (convención + introspección de
      la composición + explícito), así que los DTOs (``ConflictReport``,
      ``AssetConflictReport``, el tipo ``AssetConflictDetector``) siguen permitidos.
    - ``import sky_claw.local...`` (forma-módulo) y ``from ...local... import *``: se
      prohíben por RUTA de módulo, formas que el denylist por nombre no vería.
    """
    ofensores: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, ast.Import):
            ofensores.update(a.name for a in nodo.names if _es_modulo_de_dominio(a.name))
        elif isinstance(nodo, ast.ImportFrom):
            modulo = nodo.module or ""
            for alias in nodo.names:
                if alias.name == "*":
                    if _es_modulo_de_dominio(modulo):
                        ofensores.add(f"{modulo}.* (star import de la capa de dominio)")
                elif _es_dominio_por_nombre(alias.name, _DOMINIO_EXPLICITO_IMPORTAR):
                    ofensores.add(alias.name)
    assert not ofensores, (
        f"supervisor.py importó implementación de dominio: {sorted(ofensores)}. "
        "Esa lógica vive en su seam (runner/analyzer/parser/servicio); el Supervisor "
        "sólo conoce sus contratos/DTOs y recibe el objeto ya construido por la composición."
    )


def test_no_invoca_dominio_de_herramientas() -> None:
    """M1/M2/M4: el Supervisor no construye runners/analyzers/detectors, ni ningún
    servicio de la composición, ni re-parsea plugins.

    Cubre por convención (``*Runner``/``*Analyzer`` y sus alias), por introspección
    de ``OrchestrationComposition`` (los 7 servicios + componentes), y explícito
    (``AssetConflictDetector`` inline de scan, ``parse_active_plugins``). Permite
    construir los seams que el Supervisor cablea (no son dominio) y servicios
    no-dominio como ``PathResolutionService`` (no es tipo de la composición).
    """
    filtrados = {n for n in _nombres_invocados() if _es_dominio_por_nombre(n, _DOMINIO_EXPLICITO_INVOCAR)}
    assert not filtrados, (
        f"supervisor.py invocó/construyó dominio de herramientas: {sorted(filtrados)}. "
        "Cablealo en build_orchestration_composition / su seam e inyectá el resultado; "
        "el Supervisor coordina, no fabrica runners, servicios, scanners ni parsers."
    )


def test_no_pasa_self_como_service_locator() -> None:
    """#518: pasar ``self`` a un colaborador es Service Locator.

    Bloquea ``self`` posicional, por keyword, o expandido (``f(self)``, ``f(x=self)``,
    ``f(*self)``, ``f(**self)``). Pasar ``self.<colaborador>`` (atributo concreto) es
    inyección explícita y NO cae acá. Evasiones por aliasing (``svc = self``) exceden
    un ancla AST y quedan respaldadas por los tests conductuales.
    """
    ofensores: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if not isinstance(nodo, ast.Call):
            continue
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
    dispatcher extraído en vez de reimplementar routing inline.

    Se acota a la ``ClassDef`` de ``SupervisorAgent`` (no valida un ``dispatch_tool``
    homónimo de otra clase/helper). Exige una llamada ``.dispatch(...)`` y RECHAZA
    todo routing keyed por ``tool_name`` dentro del método: ``match``, ``if``/``elif``
    o ternario cuyo test referencia ``tool_name``, tabla ``[tool_name]`` y
    ``getattr(..., tool_name)``. Un refactor equivalente que sólo delega (partir la
    expresión, renombrar el atributo) sigue pasando; volver a rutear por tool_name
    falla. Formas de aliasing más rebuscadas quedan respaldadas por los tests
    conductuales de ``dispatch_tool``.
    """
    clase = _clase_supervisor()
    fn = _metodo_de_clase(clase, "dispatch_tool")
    assert fn is not None, "desapareció el método SupervisorAgent.dispatch_tool (contrato de contracts.py roto)"

    delega = any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "dispatch"
        for nodo in ast.walk(fn)
    )
    assert delega, (
        "dispatch_tool dejó de delegar en el dispatcher (no hay llamada .dispatch(...)): "
        "el routing de tools vive en tool_strategies/ vía OrchestrationToolDispatcher."
    )

    routers: set[str] = set()
    for nodo in ast.walk(fn):
        if isinstance(nodo, ast.Match):
            routers.add("match")
        elif isinstance(nodo, (ast.If, ast.IfExp)) and _referencia_tool_name(nodo.test):
            routers.add("if/elif/ternario sobre tool_name")
        elif isinstance(nodo, ast.Subscript) and _referencia_tool_name(nodo.slice):
            routers.add("tabla[tool_name]")
        elif (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Name)
            and nodo.func.id == "getattr"
            and any(_referencia_tool_name(a) for a in nodo.args)
        ):
            routers.add("getattr(..., tool_name)")
    assert not routers, (
        f"dispatch_tool reintrodujo routing inline por tool_name ({sorted(routers)}). "
        "El dispatch —selección de estrategia por tool_name— es responsabilidad del "
        "OrchestrationToolDispatcher, no del Supervisor."
    )
