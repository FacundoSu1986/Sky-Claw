"""Guardrail arquitectónico del cierre del Strangler Fig de ``SupervisorAgent``.

PR1–PR6 extrajeron el dominio de herramientas. PR7 no crea otro seam: congela
la frontera para que ``SupervisorAgent`` conserve únicamente lifecycle, wiring,
bridges de eventos/interfaz y fachadas de compatibilidad.

La regla es deliberadamente estructural: bloquea mecanismos de reingreso que el
Supervisor no necesita en vez de intentar reconstruir data-flow arbitrario.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from sky_claw.app.orchestrator.orchestration_composition import OrchestrationComposition
from sky_claw.app.orchestrator.supervisor import SupervisorAgent

_PAQUETE_SUPERVISOR = SupervisorAgent.__module__.rpartition(".")[0]
_RAIZ_DOMINIO = "sky_claw.local"
_MODULOS_DE_DOMINIO_EXTRA = frozenset({"sky_claw.app.orchestrator.active_plugins"})
_SUBCADENAS_DE_DOMINIO = ("Runner", "Analyzer")
_DTOS_PERMITIDOS_DE_DOMINIO = frozenset(
    {"LLMCallable", "AssetConflictDetector", "AssetConflictReport", "ConflictReport"}
)
_ACCESORES_INTERNOS_DE_SELF = frozenset({"__dict__", "__class__", "__getattribute__", "__getattr__"})


def _resolver_fuente_del_supervisor() -> tuple[Path, ast.Module]:
    """Resuelve el fuente desde la clase, no desde un nombre congelado."""
    ruta = inspect.getsourcefile(SupervisorAgent)
    if ruta is None or not Path(ruta).is_file():
        raise AssertionError(f"no pude localizar el fuente de SupervisorAgent: {ruta!r}")
    fuente = Path(ruta)
    return fuente, ast.parse(fuente.read_text(encoding="utf-8"), filename=str(fuente))


_SUPERVISOR_SRC, _SUPERVISOR_AST = _resolver_fuente_del_supervisor()


def _clase_supervisor(arbol: ast.Module = _SUPERVISOR_AST) -> ast.ClassDef:
    """Devuelve la ``ClassDef`` de ``SupervisorAgent`` o falla ruidosamente."""
    for nodo in arbol.body:
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            return nodo
    raise AssertionError("no se encontró la ClassDef de SupervisorAgent")


def _metodo_de_clase(
    clase: ast.ClassDef,
    nombre: str,
) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    """Busca un método directo de la clase, excluyendo homónimos externos."""
    for nodo in clase.body:
        if isinstance(nodo, (ast.AsyncFunctionDef, ast.FunctionDef)) and nodo.name == nombre:
            return nodo
    return None


def _tipos_de_la_composicion() -> frozenset[str]:
    """Deriva los tipos producidos por ``OrchestrationComposition``."""
    nombres: set[str] = set()
    for anotacion in OrchestrationComposition.__annotations__.values():
        texto = anotacion if isinstance(anotacion, str) else getattr(anotacion, "__name__", str(anotacion))
        nombres.update(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", texto))
    return frozenset(nombres)


_COMPOSICION = _tipos_de_la_composicion()
_DOMINIO_EXPLICITO_INVOCAR = frozenset({"parse_active_plugins", "AssetConflictDetector"}) | _COMPOSICION


def _es_dominio_por_nombre(nombre: str) -> bool:
    """Identifica runners/analyzers y símbolos producidos por la composición."""
    return any(sub in nombre for sub in _SUBCADENAS_DE_DOMINIO) or nombre in _DOMINIO_EXPLICITO_INVOCAR


def _facades_publicas_de_dominio() -> frozenset[str]:
    """Deriva properties públicas cuyo retorno es un tipo de dominio construible."""
    nombres: set[str] = set()
    for nombre, miembro in vars(SupervisorAgent).items():
        if not isinstance(miembro, property) or miembro.fget is None:
            continue
        retorno = miembro.fget.__annotations__.get("return")
        if retorno is None:
            continue
        texto = retorno if isinstance(retorno, str) else getattr(retorno, "__name__", str(retorno))
        if _es_dominio_por_nombre(texto):
            nombres.add(nombre)
    return frozenset(nombres)


_FACADES_PUBLICAS_DE_DOMINIO = _facades_publicas_de_dominio()


def _es_modulo_de_dominio(ruta: str) -> bool:
    """Identifica la raíz de dominio, sus submódulos y extras cerrados."""
    return ruta == _RAIZ_DOMINIO or ruta.startswith(_RAIZ_DOMINIO + ".") or ruta in _MODULOS_DE_DOMINIO_EXTRA


def _resolver_modulo(nodo: ast.ImportFrom) -> str:
    """Resuelve un ``ImportFrom`` absoluto o relativo respecto del Supervisor."""
    if not nodo.level:
        return nodo.module or ""
    partes = _PAQUETE_SUPERVISOR.split(".")
    raiz = partes[: len(partes) - (nodo.level - 1)]
    return ".".join([*raiz, *([nodo.module] if nodo.module else [])])


def _targets_nombre(nodo: ast.Assign | ast.AnnAssign) -> list[str]:
    """Nombres simples escritos por una asignación."""
    objetivos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
    return [objetivo.id for objetivo in objetivos if isinstance(objetivo, ast.Name)]


def _ofensores_imports(arbol: ast.Module) -> set[str]:
    """Bloquea imports de implementación y carga dinámica en el Supervisor."""
    ofensores: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if alias.name == "importlib" or _es_modulo_de_dominio(alias.name):
                    ofensores.add(alias.name)
            continue

        if isinstance(nodo, ast.ImportFrom):
            modulo = _resolver_modulo(nodo)
            for alias in nodo.names:
                if modulo == "importlib" and alias.name == "import_module":
                    ofensores.add(f"{modulo}.{alias.name}")
                    continue
                if modulo == "builtins" and alias.name == "__import__":
                    ofensores.add(f"{modulo}.{alias.name}")
                    continue
                if _es_modulo_de_dominio(modulo):
                    if alias.name == "*" or alias.name not in _DTOS_PERMITIDOS_DE_DOMINIO:
                        ofensores.add(f"{modulo}.{alias.name}")
                    continue
                subpaquete = f"{modulo}.{alias.name}" if modulo else alias.name
                if _es_modulo_de_dominio(subpaquete):
                    ofensores.add(subpaquete)
                elif _es_dominio_por_nombre(alias.name) and alias.name not in _DTOS_PERMITIDOS_DE_DOMINIO:
                    ofensores.add(alias.name)
            continue

        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "__import__":
            ofensores.add("__import__")
    return ofensores


def _bindings_construibles_de_dominio(arbol: ast.Module) -> frozenset[str]:
    """Bindings importados cuyo uso ejecutable reabsorbería dominio."""
    bindings: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.ImportFrom):
            continue
        modulo = _resolver_modulo(nodo)
        for alias in nodo.names:
            if _es_modulo_de_dominio(modulo) and _es_dominio_por_nombre(alias.name):
                bindings.add(alias.asname or alias.name)
    return frozenset(bindings)


def _es_self_facade_de_dominio(nodo: ast.AST) -> bool:
    """Reconoce ``self.<property pública que retorna dominio>``."""
    return (
        isinstance(nodo, ast.Attribute)
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "self"
        and nodo.attr in _FACADES_PUBLICAS_DE_DOMINIO
    )


def _ofensores_invocaciones(arbol: ast.Module) -> set[str]:
    """Bloquea construcción, aliasing e invocación de dominio en el Supervisor."""
    bindings = _bindings_construibles_de_dominio(arbol)
    ofensores: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Assign, ast.AnnAssign)) and nodo.value is not None:
            valor = nodo.value
            alias_de_constructor = isinstance(valor, ast.Name) and (
                valor.id in bindings or _es_dominio_por_nombre(valor.id)
            )
            if alias_de_constructor or _es_self_facade_de_dominio(valor):
                destinos = _targets_nombre(nodo)
                ofensores.add(f"alias de dominio: {','.join(destinos) or ast.unparse(nodo)}")
            continue

        if not isinstance(nodo, ast.Call):
            continue
        if isinstance(nodo.func, ast.Name):
            nombre = nodo.func.id
            if nombre in bindings or _es_dominio_por_nombre(nombre):
                ofensores.add(nombre)
            continue
        if not isinstance(nodo.func, ast.Attribute):
            continue

        receptor = nodo.func.value
        if isinstance(receptor, ast.Name) and receptor.id in bindings:
            ofensores.add(f"{receptor.id}.{nodo.func.attr}")
            continue
        if _es_self_facade_de_dominio(receptor):
            ofensores.add(f"{ast.unparse(receptor)}.{nodo.func.attr}")
            continue
        if _es_dominio_por_nombre(nodo.func.attr):
            ofensores.add(nodo.func.attr)
    return ofensores


def _accede_estado_de_self(nodo: ast.AST) -> bool:
    """Detecta estado/accesores internos que exponen el Supervisor por nombre."""
    return any(
        isinstance(desc, ast.Attribute)
        and isinstance(desc.value, ast.Name)
        and desc.value.id == "self"
        and desc.attr in _ACCESORES_INTERNOS_DE_SELF
        for desc in ast.walk(nodo)
    )


def _captura_self_anidado(nodo: ast.AST) -> bool:
    """Detecta ``self`` como valor, incluso dentro de contenedores o closures."""
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
    """Dice si una expresión entrega el Supervisor entero o estado equivalente."""
    real = nodo.value if isinstance(nodo, ast.Starred) else nodo
    return _accede_estado_de_self(real) or _captura_self_anidado(real)


def _ofensores_service_locator(arbol: ast.AST) -> set[str]:
    """Bloquea handoffs de ``self`` y aliases locales del Supervisor."""
    ofensores: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            argumentos: list[ast.expr] = [*nodo.args, *(kw.value for kw in nodo.keywords)]
            if any(_expone_self(arg) for arg in argumentos):
                ofensores.add(ast.unparse(nodo.func))
            continue

        if isinstance(nodo, (ast.Assign, ast.AnnAssign)) and nodo.value is not None:
            if not _expone_self(nodo.value):
                continue
            objetivos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
            for objetivo in objetivos:
                ofensores.add(f"handoff self -> {ast.unparse(objetivo)}")
    return ofensores


def _es_self_tool_dispatcher(nodo: ast.AST) -> bool:
    """Dice si el nodo es exactamente ``self._tool_dispatcher``."""
    return (
        isinstance(nodo, ast.Attribute)
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "self"
        and nodo.attr == "_tool_dispatcher"
    )


def _cuerpo_sin_docstring(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> list[ast.stmt]:
    """Devuelve el cuerpo ejecutable ignorando sólo el docstring."""
    cuerpo = list(fn.body)
    if (
        cuerpo
        and isinstance(cuerpo[0], ast.Expr)
        and isinstance(cuerpo[0].value, ast.Constant)
        and isinstance(cuerpo[0].value.value, str)
    ):
        return cuerpo[1:]
    return cuerpo


def _llamada_dispatch_valida(
    expr: ast.AST,
    *,
    receptor_alias: str | None,
) -> bool:
    """Valida la única llamada permitida por la fachada async ``dispatch_tool``."""
    if not isinstance(expr, ast.Await):
        return False
    llamada = expr.value
    if not (
        isinstance(llamada, ast.Call) and isinstance(llamada.func, ast.Attribute) and llamada.func.attr == "dispatch"
    ):
        return False
    receptor = llamada.func.value
    receptor_ok = _es_self_tool_dispatcher(receptor) or (
        receptor_alias is not None and isinstance(receptor, ast.Name) and receptor.id == receptor_alias
    )
    if not receptor_ok or llamada.keywords or len(llamada.args) != 2:
        return False
    route, payload = llamada.args
    return (
        isinstance(route, ast.Name)
        and route.id == "tool_name"
        and isinstance(payload, ast.Name)
        and payload.id == "payload_dict"
    )


def _errores_dispatch(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    """Congela ``dispatch_tool`` como fachada async estrecha y sin decoradores."""
    errores: list[str] = []
    if not isinstance(fn, ast.AsyncFunctionDef):
        errores.append("dispatch_tool debe seguir siendo async")
        return errores
    if fn.decorator_list:
        errores.append("dispatch_tool no admite decoradores")
        return errores

    argumentos = [arg.arg for arg in fn.args.args]
    if (
        argumentos != ["self", "tool_name", "payload_dict"]
        or fn.args.posonlyargs
        or fn.args.vararg
        or fn.args.kwarg
        or fn.args.kwonlyargs
    ):
        errores.append(f"firma inesperada: {argumentos}")
        return errores

    cuerpo = _cuerpo_sin_docstring(fn)
    alias: str | None = None
    retorno: ast.Return | None = None
    if len(cuerpo) == 1 and isinstance(cuerpo[0], ast.Return):
        retorno = cuerpo[0]
    elif len(cuerpo) == 2 and isinstance(cuerpo[0], (ast.Assign, ast.AnnAssign)) and isinstance(cuerpo[1], ast.Return):
        asignacion = cuerpo[0]
        targets = _targets_nombre(asignacion)
        if len(targets) == 1 and asignacion.value is not None and _es_self_tool_dispatcher(asignacion.value):
            alias = targets[0]
            retorno = cuerpo[1]

    if retorno is None or retorno.value is None:
        errores.append("la fachada debe ser delegación directa (o alias local de un nivel)")
        return errores
    if not _llamada_dispatch_valida(retorno.value, receptor_alias=alias):
        errores.append("retorno no delega exactamente con await a _tool_dispatcher.dispatch(tool_name, payload_dict)")
    return errores


def _funcion_sintetica(fuente: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    """Parsea una única función sintética para mutation tests end-to-end."""
    arbol = ast.parse(fuente)
    fn = arbol.body[0]
    assert isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))
    return fn


def test_guardrail_ancla_sobre_la_clase_supervisor() -> None:
    """El AST corresponde al archivo donde vive ``SupervisorAgent`` hoy."""
    fuente = inspect.getsourcefile(SupervisorAgent)
    assert fuente is not None and Path(fuente) == _SUPERVISOR_SRC
    assert _clase_supervisor().name == "SupervisorAgent"


def test_no_importa_dominio_de_herramientas() -> None:
    """La capa de dominio sólo cruza mediante el allowlist declarado."""
    ofensores = _ofensores_imports(_SUPERVISOR_AST)
    assert not ofensores, f"supervisor.py importó dominio: {sorted(ofensores)}"


def test_allowlist_de_dtos_sin_entradas_muertas() -> None:
    """El allowlist no acumula excepciones que ya no se usan."""
    importados: set[str] = set()
    for nodo in ast.walk(_SUPERVISOR_AST):
        if isinstance(nodo, ast.ImportFrom) and _es_modulo_de_dominio(_resolver_modulo(nodo)):
            importados.update(alias.name for alias in nodo.names)
    muertas = _DTOS_PERMITIDOS_DE_DOMINIO - importados
    assert not muertas, f"DTOs permitidos pero no importados: {sorted(muertas)}"


def test_no_invoca_dominio_de_herramientas() -> None:
    """El Supervisor no construye, aliasa ni invoca dominio de herramientas."""
    ofensores = _ofensores_invocaciones(_SUPERVISOR_AST)
    assert not ofensores, f"supervisor.py invocó/construyó dominio: {sorted(ofensores)}"


def test_no_pasa_self_como_service_locator() -> None:
    """El Supervisor no entrega ni aliasa ``self`` como Service Locator."""
    ofensores = _ofensores_service_locator(_SUPERVISOR_AST)
    assert not ofensores, f"supervisor.py expone self a: {sorted(ofensores)}"


def test_dispatch_tool_sigue_siendo_fachada_estrecha() -> None:
    """``dispatch_tool`` conserva firma, await y delegación directa."""
    fn = _metodo_de_clase(_clase_supervisor(), "dispatch_tool")
    assert fn is not None, "desapareció SupervisorAgent.dispatch_tool"
    errores = _errores_dispatch(fn)
    assert not errores, f"dispatch_tool reabsorbió routing: {errores}"


def test_matrix_m1_m16_tiene_evidencia_ejecutable() -> None:
    """Cada identificador M1–M16 está anclado a su analizador completo."""
    assert _ofensores_imports(ast.parse("from sky_claw.local.xedit.runner import XEditRunner\n"))  # M1
    assert _ofensores_invocaciones(ast.parse("ConflictAnalyzer()\n"))  # M2
    assert _ofensores_invocaciones(ast.parse("parse_active_plugins([])\n"))  # M3

    def dispatch(fuente: str) -> list[str]:
        return _errores_dispatch(_funcion_sintetica(fuente))

    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    if tool_name == 'x':\n"
        "        return {}\n"
        "    return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
    )  # M4
    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    table.get(tool_name)\n"
        "    return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
    )  # M5
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(self)\n"))  # M6
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(self.__dict__.copy())\n"))  # M7
    assert not dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    d = self._tool_dispatcher\n"
        "    return await d.dispatch(tool_name, payload_dict)\n"
    )  # M8
    assert _ofensores_invocaciones(
        ast.parse("from sky_claw.local.assets import AssetConflictDetector as Detector\nDetector()\n")
    )  # M9
    assert _ofensores_imports(ast.parse("import importlib\nimportlib.import_module('sky_claw.local.plugins')\n"))  # M10
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(lambda: self)\n"))  # M11
    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    router = self._legacy_router\n"
        "    return await router.dispatch(tool_name, payload_dict)\n"
    )  # M12
    assert _ofensores_imports(
        ast.parse("from importlib import import_module\nimport_module('sky_claw.local.plugins')\n")
    )  # M13
    assert _ofensores_invocaciones(
        ast.parse(
            "from sky_claw.local.assets import AssetConflictDetector\nDetector = AssetConflictDetector\nDetector()\n"
        )
    )  # M14
    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    d = self._tool_dispatcher\n"
        "    d = self._legacy_router\n"
        "    return await d.dispatch(tool_name, payload_dict)\n"
    )  # M15
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(self.__getattribute__)\n"))  # M16


def test_regresiones_de_review_fresca() -> None:
    """Ancla los bypasses adicionales sin ampliar a data-flow general."""
    # Constructor usado antes de un rebinding posterior.
    assert _ofensores_invocaciones(
        ast.parse(
            "from sky_claw.local.assets import AssetConflictDetector\n"
            "Detector = AssetConflictDetector\nDetector()\nDetector = Harmless\n"
        )
    )
    # Handoff directo y alias local de self.
    assert _ofensores_service_locator(_funcion_sintetica("def f(self, service):\n    service.supervisor = self\n"))
    assert _ofensores_service_locator(
        _funcion_sintetica("def f(self, service):\n    owner = self\n    service.owner = owner\n")
    )
    # Renombrar el parámetro contractual no permite esconder routing.
    assert _errores_dispatch(
        _funcion_sintetica(
            "async def dispatch_tool(self, name, payload_dict):\n"
            "    return await self._tool_dispatcher.dispatch(name, payload_dict)\n"
        )
    )
    # Un tipo allowlisted cruza como tipo, no ejecuta comportamiento de dominio.
    assert _ofensores_invocaciones(
        ast.parse(
            "from sky_claw.local.assets import AssetConflictDetector\n"
            "AssetConflictDetector.detect_conflicts(detector)\n"
        )
    )
    # La fachada pública del detector tampoco puede aliasarse para ejecutar dominio.
    assert _ofensores_invocaciones(ast.parse("self.asset_detector.detect_conflicts()\n"))
    assert _ofensores_invocaciones(ast.parse("detector = self.asset_detector\ndetector.detect_conflicts()\n"))
    # La carga dinámica queda prohibida como mecanismo, también con keyword name=.
    assert _ofensores_imports(ast.parse("import importlib\nimportlib.import_module(name='sky_claw.local.plugins')\n"))
    # Delegación escondida, sin await o decorada no satisface la fachada.
    assert _errores_dispatch(
        _funcion_sintetica(
            "async def dispatch_tool(self, tool_name, payload_dict):\n"
            "    async def hidden():\n"
            "        return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
            "    return {}\n"
        )
    )
    assert _errores_dispatch(
        _funcion_sintetica(
            "async def dispatch_tool(self, tool_name, payload_dict):\n"
            "    return self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
        )
    )
    assert _errores_dispatch(
        _funcion_sintetica(
            "@wrapper\n"
            "async def dispatch_tool(self, tool_name, payload_dict):\n"
            "    return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
        )
    )
