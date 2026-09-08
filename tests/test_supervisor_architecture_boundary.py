"""Guardrail arquitectónico del cierre del Strangler Fig de ``SupervisorAgent``.

PR1–PR6 extrajeron el dominio de herramientas. PR7 no crea otro seam: congela
la frontera para que ``SupervisorAgent`` conserve únicamente lifecycle, wiring,
bridges de eventos/interfaz y fachadas de compatibilidad.

El guardrail protege cuatro vías de reingreso: imports de dominio, construcción
o invocación de dominio, ``self`` como Service Locator y routing inline en
``dispatch_tool``. Las mutaciones sintéticas ejercitan los analizadores
completos, sin intentar implementar un motor general de data-flow.
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
    """Resuelve el fuente desde la clase, no desde un nombre de archivo congelado."""
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
    """Deriva los tipos producidos por ``OrchestrationComposition`` por introspección."""
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


def _es_modulo_de_dominio(ruta: str) -> bool:
    """Identifica la raíz de dominio, sus submódulos y extras cerrados."""
    return ruta == _RAIZ_DOMINIO or ruta.startswith(_RAIZ_DOMINIO + ".") or ruta in _MODULOS_DE_DOMINIO_EXTRA


def _resolver_modulo(nodo: ast.ImportFrom) -> str:
    """Resuelve un ``ImportFrom`` absoluto o relativo respecto del paquete Supervisor."""
    if not nodo.level:
        return nodo.module or ""
    partes = _PAQUETE_SUPERVISOR.split(".")
    raiz = partes[: len(partes) - (nodo.level - 1)]
    return ".".join([*raiz, *([nodo.module] if nodo.module else [])])


def _targets_nombre(nodo: ast.Assign | ast.AnnAssign) -> list[str]:
    """Nombres simples escritos por una asignación de un nivel."""
    objetivos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
    return [objetivo.id for objetivo in objetivos if isinstance(objetivo, ast.Name)]


def _bindings_import_module(arbol: ast.AST) -> frozenset[str]:
    """Bindings que alguna vez fueron alias directos de ``import_module``."""
    bindings: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module == "importlib":
            for alias in nodo.names:
                if alias.name == "import_module":
                    bindings.add(alias.asname or alias.name)

    # Propagación monotónica de un nivel/cadenas simples. Deliberadamente no se
    # "deshace" ante un rebinding posterior: el guardrail prefiere fallar
    # conservadoramente a perder una llamada que ocurrió antes del rebinding.
    cambio = True
    while cambio:
        cambio = False
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.Assign, ast.AnnAssign)):
                continue
            valor = nodo.value
            if not isinstance(valor, ast.Name) or valor.id not in bindings:
                continue
            for target in _targets_nombre(nodo):
                if target not in bindings:
                    bindings.add(target)
                    cambio = True
    return frozenset(bindings)


def _modulo_de_import_dinamico(
    nodo: ast.AST,
    bindings_import_module: frozenset[str],
) -> str | None:
    """Extrae la ruta constante de ``import_module``/``__import__`` si aplica."""
    if not isinstance(nodo, ast.Call):
        return None
    callee = nodo.func
    es_import_module_attr = isinstance(callee, ast.Attribute) and callee.attr == "import_module"
    es_import_module_directo = isinstance(callee, ast.Name) and callee.id in bindings_import_module
    es_dunder_import = isinstance(callee, ast.Name) and callee.id == "__import__"
    if not (es_import_module_attr or es_import_module_directo or es_dunder_import):
        return None
    if nodo.args and isinstance(nodo.args[0], ast.Constant) and isinstance(nodo.args[0].value, str):
        return nodo.args[0].value
    return None


def _ofensores_imports(arbol: ast.Module) -> set[str]:
    """Analizador completo de imports que cruzan la frontera de dominio."""
    ofensores: set[str] = set()
    bindings_import_module = _bindings_import_module(arbol)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            ofensores.update(alias.name for alias in nodo.names if _es_modulo_de_dominio(alias.name))
            continue
        if isinstance(nodo, ast.ImportFrom):
            modulo = _resolver_modulo(nodo)
            if _es_modulo_de_dominio(modulo):
                for alias in nodo.names:
                    if alias.name == "*":
                        ofensores.add(f"{modulo}.*")
                    elif alias.name not in _DTOS_PERMITIDOS_DE_DOMINIO:
                        ofensores.add(f"{modulo}.{alias.name}")
            else:
                for alias in nodo.names:
                    if alias.name == "*":
                        continue
                    subpaquete = f"{modulo}.{alias.name}" if modulo else alias.name
                    if _es_modulo_de_dominio(subpaquete):
                        ofensores.add(subpaquete)
                    elif _es_dominio_por_nombre(alias.name):
                        ofensores.add(alias.name)
            continue
        ruta = _modulo_de_import_dinamico(nodo, bindings_import_module)
        if ruta is not None and _es_modulo_de_dominio(ruta):
            ofensores.add(f"{ruta} (import dinámico)")
    return ofensores


def _alias_de_simbolos_de_dominio(arbol: ast.Module) -> dict[str, str]:
    """Normaliza aliases que alguna vez apuntaron a un símbolo construible de dominio."""
    mapa: dict[str, str] = {}
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.ImportFrom) or not _es_modulo_de_dominio(_resolver_modulo(nodo)):
            continue
        for alias in nodo.names:
            if _es_dominio_por_nombre(alias.name):
                mapa[alias.asname or alias.name] = alias.name

    # Igual que los aliases de import_module, la propagación es monotónica. Si un
    # nombre fue alias de un constructor de dominio, una llamada a ese nombre se
    # considera sospechosa aunque más tarde sea reasignado: así no se pierde una
    # invocación anterior por mirar sólo el estado final del mapa.
    cambio = True
    while cambio:
        cambio = False
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.Assign, ast.AnnAssign)):
                continue
            valor = nodo.value
            if not isinstance(valor, ast.Name):
                continue
            origen = mapa.get(valor.id)
            if origen is None and _es_dominio_por_nombre(valor.id):
                origen = valor.id
            if origen is None:
                continue
            for target in _targets_nombre(nodo):
                if target not in mapa:
                    mapa[target] = origen
                    cambio = True
    return mapa


def _ofensores_invocaciones(arbol: ast.Module) -> set[str]:
    """Analizador completo de construcción/invocación de dominio."""
    alias = _alias_de_simbolos_de_dominio(arbol)
    ofensores: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        if isinstance(nodo.func, ast.Name):
            nombre = nodo.func.id
            normalizado = alias.get(nombre, nombre)
            if _es_dominio_por_nombre(normalizado):
                ofensores.add(normalizado if normalizado == nombre else f"{nombre} (alias de {normalizado})")
            continue
        if not isinstance(nodo.func, ast.Attribute):
            continue

        # Un tipo de dominio allowlisted puede cruzar como símbolo/tipo, pero no
        # ejecutar comportamiento de dominio dentro del Supervisor.
        receptor = nodo.func.value
        if isinstance(receptor, ast.Name):
            propietario = alias.get(receptor.id, receptor.id)
            if _es_dominio_por_nombre(propietario):
                ofensores.add(f"{propietario}.{nodo.func.attr}")
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
    """Dice si una expresión entrega el Supervisor entero o acceso interno equivalente."""
    real = nodo.value if isinstance(nodo, ast.Starred) else nodo
    return _accede_estado_de_self(real) or _captura_self_anidado(real)


def _ofensores_service_locator(arbol: ast.AST) -> set[str]:
    """Detecta handoffs explícitos que convierten ``self`` en Service Locator."""
    ofensores: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            argumentos: list[ast.expr] = [*nodo.args, *(kw.value for kw in nodo.keywords)]
            if any(_expone_self(arg) for arg in argumentos):
                ofensores.add(ast.unparse(nodo.func))
            continue

        if isinstance(nodo, (ast.Assign, ast.AnnAssign)) and nodo.value is not None and _expone_self(nodo.value):
            objetivos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
            # Un alias local ``svc = self`` requeriría data-flow para saber si se
            # escapa después y queda deliberadamente fuera. En cambio, escribir
            # directamente sobre estado de un colaborador ya es el handoff.
            for objetivo in objetivos:
                if isinstance(objetivo, (ast.Attribute, ast.Subscript)):
                    ofensores.add(ast.unparse(objetivo))
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
    """Devuelve el cuerpo ejecutable del método, ignorando sólo su docstring."""
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
    route_name: str,
    payload_name: str,
) -> bool:
    """Valida la única llamada permitida por la fachada ``dispatch_tool``."""
    if isinstance(expr, ast.Await):
        expr = expr.value
    if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "dispatch"):
        return False
    receptor = expr.func.value
    receptor_ok = _es_self_tool_dispatcher(receptor) or (
        receptor_alias is not None and isinstance(receptor, ast.Name) and receptor.id == receptor_alias
    )
    if not receptor_ok or expr.keywords or len(expr.args) != 2:
        return False
    route, payload = expr.args
    return (
        isinstance(route, ast.Name)
        and route.id == route_name
        and isinstance(payload, ast.Name)
        and payload.id == payload_name
    )


def _errores_dispatch(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    """Congela ``dispatch_tool`` como fachada estrecha, sin reconstruir flujo arbitrario."""
    errores: list[str] = []
    argumentos = [arg.arg for arg in fn.args.args]
    if argumentos != ["self", "tool_name", "payload_dict"] or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
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
        errores.append("la fachada debe ser una delegación directa (o alias local de un nivel)")
        return errores
    if not _llamada_dispatch_valida(
        retorno.value,
        receptor_alias=alias,
        route_name="tool_name",
        payload_name="payload_dict",
    ):
        errores.append("retorno no delega exactamente a _tool_dispatcher.dispatch(tool_name, payload_dict)")
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
    """El Supervisor no construye ni invoca dominio de herramientas."""
    ofensores = _ofensores_invocaciones(_SUPERVISOR_AST)
    assert not ofensores, f"supervisor.py invocó/construyó dominio: {sorted(ofensores)}"


def test_no_pasa_self_como_service_locator() -> None:
    """El Supervisor no entrega ``self`` ni accesores internos a colaboradores."""
    ofensores = _ofensores_service_locator(_SUPERVISOR_AST)
    assert not ofensores, f"supervisor.py expone self a: {sorted(ofensores)}"


def test_dispatch_tool_sigue_siendo_fachada_estrecha() -> None:
    """``dispatch_tool`` conserva la firma y sólo delega en el dispatcher extraído."""
    fn = _metodo_de_clase(_clase_supervisor(), "dispatch_tool")
    assert fn is not None, "desapareció SupervisorAgent.dispatch_tool"
    errores = _errores_dispatch(fn)
    assert not errores, f"dispatch_tool reabsorbió routing: {errores}"


def test_matrix_m1_m16_tiene_evidencia_ejecutable() -> None:
    """Cada identificador M1–M16 está anclado a su analizador completo."""
    # M1 — import de runner.
    assert _ofensores_imports(ast.parse("from sky_claw.local.xedit.runner import XEditRunner\n"))
    # M2 — analyzer inline.
    assert _ofensores_invocaciones(ast.parse("ConflictAnalyzer()\n"))
    # M3 — parser inline.
    assert _ofensores_invocaciones(ast.parse("parse_active_plugins([])\n"))

    def dispatch(fuente: str) -> list[str]:
        return _errores_dispatch(_funcion_sintetica(fuente))

    # M4 — routing con if.
    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    if tool_name == 'x':\n"
        "        return {}\n"
        "    return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
    )
    # M5 — lookup de tabla.
    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    table.get(tool_name)\n"
        "    return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
    )
    # M6 — self pelado.
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(self)\n"))
    # M7 — estado derivado de self.
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(self.__dict__.copy())\n"))
    # M8 — control positivo: alias local de un nivel equivalente.
    assert not dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    d = self._tool_dispatcher\n"
        "    return await d.dispatch(tool_name, payload_dict)\n"
    )
    # M9 — constructor importado con alias.
    assert _ofensores_invocaciones(
        ast.parse("from sky_claw.local.assets import AssetConflictDetector as Detector\nDetector()\n")
    )
    # M10 — import dinámico por módulo.
    assert _ofensores_imports(ast.parse("import importlib\nimportlib.import_module('sky_claw.local.plugins')\n"))
    # M11 — self capturado en closure.
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(lambda: self)\n"))
    # M12 — receptor de dispatch equivocado.
    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    router = self._legacy_router\n"
        "    return await router.dispatch(tool_name, payload_dict)\n"
    )
    # M13 — import_module importado directamente.
    assert _ofensores_imports(
        ast.parse("from importlib import import_module\nimport_module('sky_claw.local.plugins')\n")
    )
    # M14 — constructor reasignado a nombre neutro.
    assert _ofensores_invocaciones(
        ast.parse("from sky_claw.local.assets import AssetConflictDetector\nDetector = AssetConflictDetector\nDetector()\n")
    )
    # M15 — alias del dispatcher rebotado.
    assert dispatch(
        "async def dispatch_tool(self, tool_name, payload_dict):\n"
        "    d = self._tool_dispatcher\n"
        "    d = self._legacy_router\n"
        "    return await d.dispatch(tool_name, payload_dict)\n"
    )
    # M16 — accessor interno ligado.
    assert _ofensores_service_locator(_funcion_sintetica("def f(self):\n    consume(self.__getattribute__)\n"))


def test_regresiones_de_review_fresca() -> None:
    """Ancla los bypasses adicionales encontrados sobre el HEAD final anterior."""
    # Constructor usado antes de un rebinding posterior: no se borra evidencia.
    assert _ofensores_invocaciones(
        ast.parse(
            "from sky_claw.local.assets import AssetConflictDetector\n"
            "Detector = AssetConflictDetector\nDetector()\nDetector = Harmless\n"
        )
    )
    # Handoff directo de self al estado de un colaborador.
    assert _ofensores_service_locator(_funcion_sintetica("def f(self, service):\n    service.supervisor = self\n"))
    # Renombrar el parámetro contractual no permite esconder routing.
    assert _errores_dispatch(
        _funcion_sintetica(
            "async def dispatch_tool(self, name, payload_dict):\n"
            "    if name == 'x':\n"
            "        return {}\n"
            "    return await self._tool_dispatcher.dispatch(name, payload_dict)\n"
        )
    )
    # Un tipo allowlisted puede cruzar como tipo, no ejecutar dominio.
    assert _ofensores_invocaciones(
        ast.parse(
            "from sky_claw.local.assets import AssetConflictDetector\n"
            "AssetConflictDetector.detect_conflicts(detector)\n"
        )
    )
    # Alias de un nivel de import_module.
    assert _ofensores_imports(
        ast.parse("from importlib import import_module\nload = import_module\nload('sky_claw.local.plugins')\n")
    )
    # Una delegación escondida en una coroutine anidada no satisface la fachada.
    assert _errores_dispatch(
        _funcion_sintetica(
            "async def dispatch_tool(self, tool_name, payload_dict):\n"
            "    async def hidden():\n"
            "        return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
            "    return {}\n"
        )
    )
