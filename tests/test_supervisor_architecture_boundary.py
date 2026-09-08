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
trivial: acá no se repite ese error). Cada test cierra una vía de reingreso, y se
enumera la FAMILIA por regla, no una muestra (reviews internos + review Codex de #553):

  1. **Import de la capa de dominio** (``test_no_importa_dominio_de_herramientas``).
     La capa ``sky_claw.local.*`` (+ el parser ``active_plugins``) está CERRADA: el
     Supervisor sólo puede importar de ahí un allowlist chico de DTOs/tipos de
     retorno de sus facades; cualquier otro símbolo —runner, analyzer, servicio, o
     un helper de nombre NEUTRO (``load_plugins``, ``resolve_load_order``, …)— es un
     ofensor, sin depender de convención de nombre. Se cubren: ``from X import Nombre``,
     ``import X`` (forma-módulo, con o sin alias), ``from X import *``, imports RELATIVOS
     (``from ...local import y``) y el **import DINÁMICO** con string constante
     (``importlib.import_module("sky_claw.local.x")`` / ``__import__("...")``), cuya
     ruta viaja como ``Constant`` y no como nodo ``Import`` (review Codex #553).
  2. **Construcción/invocación de dominio** (``test_no_invoca_dominio_de_herramientas``):
     runners/analyzers por convención de nombre, los tipos que PRODUCE la
     composición (por introspección de ``OrchestrationComposition``) y un set
     explícito (``AssetConflictDetector`` inline de scan, ``parse_active_plugins``).
     Los **alias de import** de un símbolo construible de dominio se normalizan al
     original antes de aplicar el denylist: ``AssetConflictDetector as Detector`` +
     ``Detector()`` no esquiva la regla (review Codex #553).
  3. **Service Locator** (``test_no_pasa_self_como_service_locator``, #518): pasar
     ``self`` —posicional, keyword, expandido (``*self``/``**self``), su estado
     (``vars(self)``, ``self.__dict__``, ``self.__class__``) o ``self`` CAPTURADO
     dentro de un contenedor/closure (``lambda: self``, ``{"k": self}``, ``[self]``)—
     a un colaborador (review Codex #553). ``self.<colaborador>`` sigue permitido.
  4. **Routing inline** (``test_dispatch_tool_sigue_delegando``): en la facade
     pública ``tool_name`` sólo puede fluir como argumento a ``.dispatch(...)``;
     cualquier otro uso (``match``/``if``/``for``/``[tool_name]``/``getattr(...)``/
     ``except``→fallback) es routing reabsorbido. El receptor de ``.dispatch`` debe
     ser ``self._tool_dispatcher`` o un alias LOCAL asignado desde él —un alias de
     OTRO colaborador (``router = self._legacy_router``) no cuenta (review Codex #553).

**Ancla sobre la CLASE.** El AST se resuelve desde ``inspect.getsourcefile(SupervisorAgent)``
y las búsquedas de método se acotan a su ``ClassDef``: si el módulo se
renombra/mueve el guardrail SIGUE a la clase (no da falso verde sobre el archivo
viejo) y no valida un ``dispatch_tool`` homónimo; si el ancla se pierde, falla
ruidoso.

Lo que PERMITE deliberadamente: delegar a servicios cableados
(``self._wrye_bash_service.execute_pipeline(...)``); construir en ``__init__`` los
SEAMS que cablea (``AssetConflictScanner``/``RecordConflictScanner``/``PluginLimitGuard``/
``GrassRuntimeDepsProvider``/``PathResolutionService``); importar los DTOs
declarados; lifecycle, bridges y facades de compatibilidad.

**Alcance deliberado.** Ancla estructural, no oráculo de análisis de flujo
(metaprompt §14). La resolución de alias que hace el guardrail es de UN nivel y
acotada a su función/módulo (asignaciones directas), no un motor de data-flow.
**Limitación aceptada y explícita** (no la vendo como cubierta): un alias de DATOS
del PROPIO Supervisor —``svc = self; svc.dispatch(...)`` / ``inst = self``— NO lo
detecta este ancla estático; rastrear que ``svc`` ES ``self`` a varios saltos
exigiría data-flow. No es una brecha peligrosa: un alias que preserva el
comportamiento sigue delegando en el dispatcher (no reabsorbe dominio), y uno que
sí reabsorbe routing altera el comportamiento observable que fijan los tests
conductuales de ``tests/test_supervisor_dispatch_tool.py`` —pero el ancla por sí
sola no lo prohíbe. NO congela: números de línea, forma de expresiones triviales,
atributos del constructor, ni métodos privados.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from sky_claw.app.orchestrator.orchestration_composition import OrchestrationComposition
from sky_claw.app.orchestrator.supervisor import SupervisorAgent

_PAQUETE_SUPERVISOR = SupervisorAgent.__module__.rpartition(".")[0]  # sky_claw.app.orchestrator


def _resolver_fuente_del_supervisor() -> tuple[Path, ast.Module]:
    """Resuelve el archivo fuente del ``SupervisorAgent`` DESDE LA CLASE (falla ruidoso)."""
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


# --- Familia de dominio: convención + introspección de la composición ---------
_SUBCADENAS_DE_DOMINIO = ("Runner", "Analyzer")


def _tipos_de_la_composicion() -> frozenset[str]:
    """Nombres de los tipos que PRODUCE ``build_orchestration_composition``.

    El Supervisor RECIBE todos estos objetos (``composition.X``) y no debe construir
    ninguno. Se derivan por INTROSPECCIÓN de las anotaciones del dataclass
    ``OrchestrationComposition`` (no una lista a mano): 7 servicios + dispatcher/
    deps/middleware/máquina de estados. Un servicio NUEVO queda cubierto sin tocar
    este test —enumera la familia, no una muestra.
    """
    nombres: set[str] = set()
    for anotacion in OrchestrationComposition.__annotations__.values():
        texto = anotacion if isinstance(anotacion, str) else getattr(anotacion, "__name__", str(anotacion))
        nombres.update(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", texto))  # identificadores de clase
    return frozenset(nombres)


_COMPOSICION = _tipos_de_la_composicion()

_DOMINIO_EXPLICITO_INVOCAR = frozenset({"parse_active_plugins", "AssetConflictDetector"}) | _COMPOSICION

# --- Capa de dominio por RUTA de módulo (cerrada salvo DTOs declarados) --------
_RAIZ_DOMINIO = "sky_claw.local"
_MODULOS_DE_DOMINIO_EXTRA = frozenset({"sky_claw.app.orchestrator.active_plugins"})

# Únicos símbolos que el Supervisor puede importar de la capa de dominio: DTOs y
# tipos de retorno de sus facades. Cualquier otro nombre (runner/analyzer/servicio
# o un helper de nombre neutro) importado de esa capa es un ofensor —así el ancla
# no depende de que el dominio se llame ``*Runner``. Este set es la frontera: si
# una facade nueva necesita otro DTO de dominio, se agrega acá explícitamente.
_DTOS_PERMITIDOS_DE_DOMINIO = frozenset(
    {"LLMCallable", "AssetConflictDetector", "AssetConflictReport", "ConflictReport"}
)


def _es_dominio_por_nombre(nombre: str) -> bool:
    """¿``nombre`` es dominio por convención o por ser un tipo de la composición?"""
    return any(sub in nombre for sub in _SUBCADENAS_DE_DOMINIO) or nombre in _DOMINIO_EXPLICITO_INVOCAR


def _es_modulo_de_dominio(ruta: str) -> bool:
    """¿``ruta`` (dotted) es el paquete de dominio, un submódulo suyo, o un extra?"""
    return ruta == _RAIZ_DOMINIO or ruta.startswith(_RAIZ_DOMINIO + ".") or ruta in _MODULOS_DE_DOMINIO_EXTRA


def _modulo_de_import_dinamico(nodo: ast.AST) -> str | None:
    """Ruta (dotted) de un import DINÁMICO con string constante, si ``nodo`` lo es.

    Cubre ``importlib.import_module("sky_claw.local.x")`` y ``__import__("...")``: la
    ruta viaja como ``Constant`` str dentro de un ``ast.Call``, no como nodo
    ``Import``/``ImportFrom``, así que el guard de imports estáticos no la ve (review
    Codex #553). Sólo se resuelve el caso de ruta CONSTANTE —una ruta computada en
    runtime excede un ancla estática y no es un reingreso "naive".
    """
    if not isinstance(nodo, ast.Call):
        return None
    callee = nodo.func
    es_import_module = isinstance(callee, ast.Attribute) and callee.attr == "import_module"
    es_dunder_import = isinstance(callee, ast.Name) and callee.id == "__import__"
    if not (es_import_module or es_dunder_import):
        return None
    if nodo.args and isinstance(nodo.args[0], ast.Constant) and isinstance(nodo.args[0].value, str):
        return nodo.args[0].value
    return None


def _es_self_tool_dispatcher(nodo: ast.AST) -> bool:
    """¿``nodo`` es exactamente ``self._tool_dispatcher``?"""
    return (
        isinstance(nodo, ast.Attribute)
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "self"
        and nodo.attr == "_tool_dispatcher"
    )


def _alias_locales_del_dispatcher(fn: ast.AST) -> set[str]:
    """Nombres locales asignados desde ``self._tool_dispatcher`` dentro de ``fn``.

    Resuelve el alias local del refactor equivalente (M8: ``d = self._tool_dispatcher``)
    SIN volverse un motor de data-flow: es un scan de asignaciones de UN nivel dentro
    de la función. Un alias de OTRO colaborador (``router = self._legacy_router``) NO
    entra, así que ``router.dispatch(...)`` deja de contar como delegación al
    dispatcher extraído (review Codex #553).
    """
    alias: set[str] = set()
    for nodo in ast.walk(fn):
        if isinstance(nodo, ast.Assign) and _es_self_tool_dispatcher(nodo.value):
            for objetivo in nodo.targets:
                if isinstance(objetivo, ast.Name):
                    alias.add(objetivo.id)
        elif (
            isinstance(nodo, ast.AnnAssign)
            and nodo.value is not None
            and _es_self_tool_dispatcher(nodo.value)
            and isinstance(nodo.target, ast.Name)
        ):
            alias.add(nodo.target.id)
    return alias


def _delega_al_dispatcher(nodo: ast.AST, alias_locales: set[str]) -> bool:
    """¿``nodo`` es una llamada de delegación al dispatcher extraído?

    Acepta ``self._tool_dispatcher.dispatch(...)`` y ``<alias>.dispatch(...)`` donde
    ``<alias>`` es un nombre LOCAL asignado desde ``self._tool_dispatcher`` (refactor
    equivalente M8). NO acepta ``self.<otro_servicio>.dispatch(...)`` ni un alias de
    otro colaborador: delegar en un colaborador distinto no es delegar en el
    dispatcher (reviews interno + Codex #553).
    """
    if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "dispatch"):
        return False
    receptor = nodo.func.value
    if _es_self_tool_dispatcher(receptor):
        return True  # self._tool_dispatcher.dispatch(...)
    return isinstance(receptor, ast.Name) and receptor.id in alias_locales  # alias local del dispatcher (M8)


def _resolver_modulo(nodo: ast.ImportFrom) -> str:
    """Ruta absoluta del módulo de un ``ImportFrom``, resolviendo imports relativos.

    ``from ...local import x`` (``level>0``) se resuelve contra el paquete del
    Supervisor para que la regla por ruta de módulo también los vea.
    """
    if not nodo.level:
        return nodo.module or ""
    partes = _PAQUETE_SUPERVISOR.split(".")
    raiz = partes[: len(partes) - (nodo.level - 1)]  # level 1 = paquete actual
    return ".".join([*raiz, *([nodo.module] if nodo.module else [])])


def _alias_de_simbolos_de_dominio(arbol: ast.Module) -> dict[str, str]:
    """Mapa ``binding local -> símbolo original`` para imports construibles de dominio.

    ``from sky_claw.local.assets import AssetConflictDetector as Detector`` liga
    ``Detector`` al símbolo original ``AssetConflictDetector``; sin resolver el alias
    ``Detector()`` esquivaría el denylist de invocación (review Codex #553). Sólo
    interesan los símbolos que YA son dominio-por-nombre (construir un DTO permitido
    como ``AssetConflictDetector`` es reingreso; un DTO puro como ``ConflictReport``
    no —importarlo Y construirlo es legítimo, no fabrica un runner/detector).
    """
    mapa: dict[str, str] = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and _es_modulo_de_dominio(_resolver_modulo(nodo)):
            for alias in nodo.names:
                if alias.asname and _es_dominio_por_nombre(alias.name):
                    mapa[alias.asname] = alias.name
    return mapa


def _accede_estado_de_self(nodo: ast.AST) -> bool:
    """¿En cualquier parte del subárbol se accede a ``self.__dict__`` / ``self.__class__``?

    Recursivo a propósito: atrapa derivados como ``self.__dict__.copy()`` o
    ``dict(self.__dict__)``, no sólo el acceso directo (review interno #553). NO
    marca ``self.<colaborador>`` (atributos normales), que es inyección explícita.
    """
    return any(
        isinstance(desc, ast.Attribute)
        and isinstance(desc.value, ast.Name)
        and desc.value.id == "self"
        and desc.attr in {"__dict__", "__class__"}
        for desc in ast.walk(nodo)
    )


def _captura_self_anidado(nodo: ast.AST) -> bool:
    """¿El subárbol captura ``self`` PELADO (no como ``self.<attr>``) en cualquier nivel?

    Atrapa ``self`` suelto y también capturado dentro de un contenedor o closure:
    ``f(self)``, ``f(*self)``, ``vars(self)``, ``f(lambda: self)``, ``f({"k": self})``,
    ``f([self])`` — el colaborador recibe el Supervisor entero y puede usarlo como
    service locator (review Codex #553). ``self.<colaborador>`` NO cae acá: ese
    ``self`` es la BASE de un ``Attribute`` (inyección explícita de una dependencia),
    no un valor suelto.
    """
    bases_de_atributo = {
        id(desc.value)
        for desc in ast.walk(nodo)
        if isinstance(desc, ast.Attribute) and isinstance(desc.value, ast.Name) and desc.value.id == "self"
    }
    return any(
        isinstance(desc, ast.Name) and desc.id == "self" and id(desc) not in bases_de_atributo
        for desc in ast.walk(nodo)
    )


def _expone_self(nodo: ast.expr) -> bool:
    """¿La expresión pasa ``self`` o su estado interno a un colaborador?"""
    real = nodo.value if isinstance(nodo, ast.Starred) else nodo
    # self.__dict__ / self.__class__ (incl. self.__dict__.copy()): estado interno.
    if _accede_estado_de_self(real):
        return True
    # self pelado o capturado en contenedor/closure: self, *self, **self, vars(self),
    # lambda: self, {"k": self}, [self]. self.<colaborador> queda permitido.
    return _captura_self_anidado(real)


def _nombres_invocados() -> set[str]:
    """Nombre terminal del callee de cada llamada (``Foo(...)`` / ``mod.Foo(...)`` -> ``Foo``)."""
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


def test_guardrail_ancla_sobre_la_clase_supervisor() -> None:
    """El ancla apunta al archivo donde vive ``SupervisorAgent`` HOY (falla ruidoso si no)."""
    fuente_de_la_clase = inspect.getsourcefile(SupervisorAgent)
    assert fuente_de_la_clase is not None and Path(fuente_de_la_clase) == _SUPERVISOR_SRC, (
        "el AST analizado no corresponde al archivo donde vive SupervisorAgent: "
        f"clase en {fuente_de_la_clase!r} vs. ancla {_SUPERVISOR_SRC}."
    )
    assert _clase_supervisor() is not None


def test_no_importa_dominio_de_herramientas() -> None:
    """La capa de dominio está CERRADA: sólo DTOs declarados cruzan al Supervisor.

    Cubre ``from X import Nombre``, ``import X`` (forma-módulo), ``from X import *``,
    imports relativos y el import DINÁMICO con string constante. De la capa de
    dominio (``sky_claw.local.*`` + ``active_plugins``) sólo se admiten los nombres de
    ``_DTOS_PERMITIDOS_DE_DOMINIO`` —cualquier otro (runner/analyzer/servicio, o un
    helper de nombre neutro) es ofensor sin depender de convención. De módulos
    NO-dominio se rechazan runners/analyzers y tipos de la composición por nombre.
    """
    ofensores: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, ast.Import):
            ofensores.update(a.name for a in nodo.names if _es_modulo_de_dominio(a.name))
        elif isinstance(nodo, ast.ImportFrom):
            modulo = _resolver_modulo(nodo)
            if _es_modulo_de_dominio(modulo):
                for alias in nodo.names:
                    if alias.name == "*":
                        ofensores.add(f"{modulo}.* (star import de la capa de dominio)")
                    elif alias.name not in _DTOS_PERMITIDOS_DE_DOMINIO:
                        ofensores.add(f"{modulo}.{alias.name}")
            else:
                for alias in nodo.names:
                    if alias.name == "*":
                        continue
                    # ``from sky_claw import local`` importa un SUBPAQUETE de dominio
                    # por nombre: se detecta por la ruta combinada, no por el símbolo.
                    subpaquete = f"{modulo}.{alias.name}" if modulo else alias.name
                    if _es_modulo_de_dominio(subpaquete):
                        ofensores.add(subpaquete)
                    elif _es_dominio_por_nombre(alias.name):
                        ofensores.add(alias.name)
        else:
            # Import dinámico (importlib.import_module / __import__) con ruta constante.
            ruta_dinamica = _modulo_de_import_dinamico(nodo)
            if ruta_dinamica is not None and _es_modulo_de_dominio(ruta_dinamica):
                ofensores.add(f"{ruta_dinamica} (import dinámico)")
    assert not ofensores, (
        f"supervisor.py importó implementación de dominio: {sorted(ofensores)}. "
        "La capa sky_claw.local.* está cerrada salvo los DTOs declarados; el resto "
        "(runner/analyzer/servicio/helper) vive en su seam y se recibe ya construido."
    )


def test_allowlist_de_dtos_sin_entradas_muertas() -> None:
    """El allowlist de DTOs de dominio no acumula entradas muertas.

    Cada nombre de ``_DTOS_PERMITIDOS_DE_DOMINIO`` debe corresponder a un símbolo que
    el Supervisor HOY importa de la capa de dominio. Así el allowlist no puede crecer
    en silencio (un nombre agregado "para callar el test" que no se importa rompe acá)
    — cierra el punto ciego de mantenimiento que señaló el review interno.
    """
    importados_de_dominio: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, ast.ImportFrom) and _es_modulo_de_dominio(_resolver_modulo(nodo)):
            importados_de_dominio.update(a.name for a in nodo.names)
    muertas = _DTOS_PERMITIDOS_DE_DOMINIO - importados_de_dominio
    assert not muertas, (
        f"entradas del allowlist de DTOs que ya no se importan de la capa de dominio: {sorted(muertas)}. "
        "Quitalas: un allowlist con nombres muertos es un punto ciego (dejaría pasar un import de "
        "dominio con ese nombre sin que nadie lo note)."
    )


def test_no_invoca_dominio_de_herramientas() -> None:
    """M1/M2/M4: el Supervisor no construye runners/analyzers/detectors, ni ningún
    servicio de la composición, ni re-parsea plugins.

    Por convención (``*Runner``/``*Analyzer`` y alias), por introspección de
    ``OrchestrationComposition`` (los 7 servicios + componentes) y explícito
    (``AssetConflictDetector`` inline de scan, ``parse_active_plugins``). Un símbolo
    construible de dominio IMPORTADO con alias se normaliza al original antes de
    aplicar el denylist (``AssetConflictDetector as Detector`` -> ``Detector()`` es
    reingreso, review Codex #553). Permite los seams que el Supervisor cablea y
    servicios no-dominio como ``PathResolutionService`` (no es tipo de la composición).
    """
    invocados = _nombres_invocados()
    alias_de_dominio = _alias_de_simbolos_de_dominio(_SUPERVISOR_AST)
    filtrados = {n for n in invocados if _es_dominio_por_nombre(n)}
    filtrados |= {f"{n} (alias de {alias_de_dominio[n]})" for n in invocados if n in alias_de_dominio}
    assert not filtrados, (
        f"supervisor.py invocó/construyó dominio de herramientas: {sorted(filtrados)}. "
        "Cablealo en build_orchestration_composition / su seam e inyectá el resultado; "
        "el Supervisor coordina, no fabrica runners, servicios, scanners ni parsers."
    )


def test_no_pasa_self_como_service_locator() -> None:
    """#518: pasar ``self`` (o su estado) a un colaborador es Service Locator.

    Bloquea ``self`` posicional/keyword/expandido (``f(self)``, ``f(x=self)``,
    ``f(*self)``, ``f(**self)``), su estado interno (``vars(self)``, ``self.__dict__``,
    ``self.__class__``) y ``self`` CAPTURADO dentro de un contenedor/closure
    (``lambda: self``, ``{"k": self}``, ``[self]`` — review Codex #553). Pasar
    ``self.<colaborador>`` (atributo concreto) es inyección explícita y NO cae acá. El
    aliasing de datos del propio Supervisor (``svc = self``) excede un ancla AST y
    queda respaldado por los conductuales.
    """
    ofensores: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if not isinstance(nodo, ast.Call):
            continue
        argumentos: list[ast.expr] = [*nodo.args, *(kw.value for kw in nodo.keywords)]
        if any(_expone_self(arg) for arg in argumentos):
            ofensores.add(ast.unparse(nodo.func))
    assert not ofensores, (
        f"supervisor.py pasa `self` (o su estado) a: {sorted(ofensores)}. Inyectá "
        "dependencias explícitas (self.<colaborador>), no el Supervisor entero: un "
        "callee que recibe self puede alcanzar cualquier cosa en runtime (Service Locator)."
    )


def test_dispatch_tool_sigue_delegando() -> None:
    """La facade pública ``dispatch_tool`` (contrato en contracts.py) delega en el
    dispatcher extraído: ``tool_name`` sólo puede fluir como argumento a
    ``.dispatch(...)``.

    Acotado a la ``ClassDef`` de ``SupervisorAgent``. Regla EXHAUSTIVA (no muestreo
    de formas): toda referencia a ``tool_name`` en el método debe ser un argumento de
    una llamada ``.dispatch(...)``; cualquier otro uso —``match``, ``if``/``elif``,
    ternario, ``for tool_name in ...``, ``tabla[tool_name]``, ``getattr(_, tool_name)``,
    ``except``→fallback por tool_name— es routing reabsorbido y falla. El receptor de
    ``.dispatch`` debe ser ``self._tool_dispatcher`` o un alias LOCAL asignado desde él;
    un alias de otro colaborador no cuenta (review Codex #553). Un refactor equivalente
    que sólo delega (partir la expresión, renombrar el atributo del dispatcher) sigue
    pasando.
    """
    clase = _clase_supervisor()
    fn = _metodo_de_clase(clase, "dispatch_tool")
    assert fn is not None, "desapareció el método SupervisorAgent.dispatch_tool (contrato de contracts.py roto)"

    alias_del_dispatcher = _alias_locales_del_dispatcher(fn)
    llamadas_dispatch = [nodo for nodo in ast.walk(fn) if _delega_al_dispatcher(nodo, alias_del_dispatcher)]
    assert llamadas_dispatch, (
        "dispatch_tool dejó de delegar en self._tool_dispatcher.dispatch(...) (o un alias local de él): "
        "el routing de tools vive en tool_strategies/ vía OrchestrationToolDispatcher, y delegar "
        "en otro colaborador (self.<otro_servicio>.dispatch o un alias suyo) no cuenta."
    )

    # ``tool_name`` sólo puede aparecer como argumento de un ``.dispatch(...)``.
    permitidos: set[int] = set()
    for llamada in llamadas_dispatch:
        for arg in llamada.args:
            real = arg.value if isinstance(arg, ast.Starred) else arg
            if isinstance(real, ast.Name) and real.id == "tool_name":
                permitidos.add(id(real))
        for kw in llamada.keywords:
            if isinstance(kw.value, ast.Name) and kw.value.id == "tool_name":
                permitidos.add(id(kw.value))
    fuera = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "tool_name" and id(n) not in permitidos]
    assert not fuera, (
        f"dispatch_tool usa tool_name fuera de la delegación ({len(fuera)} referencia(s) "
        "en match/if/for/subscript/getattr/except…). tool_name sólo debe fluir como "
        "argumento a _tool_dispatcher.dispatch(...); la selección de estrategia por "
        "tool_name es responsabilidad del OrchestrationToolDispatcher, no del Supervisor."
    )


# --- Anclas de regresión de los helpers de frontera (review Codex #553) --------
# Fijan que las cuatro vías de bypass que reportó Codex quedan detectadas, contra
# fragmentos SINTÉTICOS (no dependen de que supervisor.py hoy las tenga).


def test_import_dinamico_de_dominio_se_detecta() -> None:
    """Codex #553: import dinámico con string constante de dominio es reingreso."""
    for fuente in (
        'importlib.import_module("sky_claw.local.plugins")',
        '__import__("sky_claw.local.xedit.runner")',
    ):
        ruta = _modulo_de_import_dinamico(ast.parse(fuente, mode="eval").body)
        assert ruta is not None and _es_modulo_de_dominio(ruta), fuente
    # Un import dinámico NO-dominio se resuelve pero no se marca como ofensor.
    ruta_no_dominio = _modulo_de_import_dinamico(ast.parse('importlib.import_module("json")', mode="eval").body)
    assert ruta_no_dominio == "json" and not _es_modulo_de_dominio(ruta_no_dominio)
    # Una ruta NO constante no se resuelve (no es reingreso naive).
    assert _modulo_de_import_dinamico(ast.parse("importlib.import_module(nombre)", mode="eval").body) is None


def test_alias_de_simbolo_de_dominio_construible_se_normaliza() -> None:
    """Codex #553: ``AssetConflictDetector as Detector`` liga un símbolo construible."""
    arbol = ast.parse("from sky_claw.local.assets import AssetConflictDetector as Detector, AssetConflictReport as Rep")
    # Sólo el símbolo construible-de-dominio (Detector) se mapea; el DTO puro (Rep) no.
    assert _alias_de_simbolos_de_dominio(arbol) == {"Detector": "AssetConflictDetector"}


def test_captura_self_anidado_atrapa_contenedores_y_closures() -> None:
    """Codex #553: ``self`` envuelto en lambda/dict/list expone el Supervisor entero."""
    for fuente in ("lambda: self", "{'s': self}", "[self]", "(self,)", "self", "vars(self)"):
        assert _captura_self_anidado(ast.parse(fuente, mode="eval").body), fuente
    # self.<colaborador> NO es captura: ese self es base de un Attribute (inyección).
    for permitido in ("self.foo", "self.foo.bar", "self._tool_dispatcher"):
        assert not _captura_self_anidado(ast.parse(permitido, mode="eval").body), permitido


def test_alias_local_del_dispatcher_distingue_colaboradores() -> None:
    """Codex #553: sólo un alias local de ``self._tool_dispatcher`` cuenta como delegación."""
    fn = ast.parse(
        "async def dispatch_tool(self, tool_name, payload):\n"
        "    d = self._tool_dispatcher\n"
        "    router = self._legacy_router\n"
        "    router.dispatch(tool_name, payload)\n"
        "    return d.dispatch(tool_name, payload)\n"
    ).body[0]
    alias = _alias_locales_del_dispatcher(fn)
    assert alias == {"d"}  # router (otro colaborador) NO entra
    resultados = {
        ast.unparse(nodo.func): _delega_al_dispatcher(nodo, alias)
        for nodo in ast.walk(fn)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "dispatch"
    }
    assert resultados == {"d.dispatch": True, "router.dispatch": False}
