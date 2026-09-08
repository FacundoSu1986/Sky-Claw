"""Guardrail arquitectónico del cierre del Strangler Fig de ``SupervisorAgent``.

PR1–PR6 extrajeron el dominio de herramientas. PR7 no crea otro seam: congela
la frontera para que ``SupervisorAgent`` conserve únicamente lifecycle, wiring,
bridges de eventos/interfaz y fachadas de compatibilidad.

El guardrail protege cuatro vías de reingreso: imports de dominio, construcción
o invocación de dominio, ``self`` como Service Locator y routing inline en
``dispatch_tool``. Las mutaciones sintéticas ejercitan los analizadores
COMPLETOS, no helpers aislados.
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
_ACCESORES_INTERNOS_DE_SELF = frozenset(
    {"__dict__", "__class__", "__getattribute__", "__getattr__"}
)


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


def _bindings_import_module(arbol: ast.AST) -> frozenset[str]:
    """Bindings locales equivalentes a ``importlib.import_module``."""
    bindings: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.ImportFrom) or nodo.module != "importlib":
            continue
        for alias in nodo.names:
            if alias.name == "import_module":
                bindings.add(alias.asname or alias.name)
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


def _targets_nombre(nodo: ast.Assign | ast.AnnAssign) -> list[str]:
    """Nombres simples escritos por una asignación de un nivel."""
    objetivos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
    return [objetivo.id for objetivo in objetivos if isinstance(objetivo, ast.Name)]


def _alias_de_simbolos_de_dominio(arbol: ast.Module) -> dict[str, str]:
    """Normaliza imports y asignaciones directas de constructores de dominio."""
    mapa: dict[str, str] = {}
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.ImportFrom) or not _es_modulo_de_dominio(_resolver_modulo(nodo)):
            continue
        for alias in nodo.names:
            if _es_dominio_por_nombre(alias.name):
                mapa[alias.asname or alias.name] = alias.name

    asignaciones = sorted(
        (nodo for nodo in ast.walk(arbol) if isinstance(nodo, (ast.Assign, ast.AnnAssign))),
        key=lambda nodo: getattr(nodo, "lineno", 0),
    )
    for nodo in asignaciones:
        valor = nodo.value
        if valor is None or not isinstance(valor, ast.Name):
            continue
        origen = mapa.get(valor.id)
        if origen is None and _es_dominio_por_nombre(valor.id):
            origen = valor.id
        for target in _targets_nombre(nodo):
            if origen is None:
                mapa.pop(target, None)
            else:
                mapa[target] = origen
    return mapa


def _nombres_invocados(arbol: ast.AST) -> set[str]:
    """Recolecta el nombre terminal de cada callee."""
    invocados: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        if isinstance(nodo.func, ast.Name):
            invocados.add(nodo.func.id)
        elif isinstance(nodo.func, ast.Attribute):
            invocados.add(nodo.func.attr)
    return invocados


def _ofensores_invocaciones(arbol: ast.Module) -> set[str]:
    """Analizador completo de construcción/invocación de dominio."""
    alias = _alias_de_simbolos_de_dominio(arbol)
    ofensores: set[str] = set()
    for nombre in _nombres_invocados(arbol):
        normalizado = alias.get(nombre, nombre)
        if _es_dominio_por_nombre(normalizado):
            ofensores.add(normalizado if normalizado == nombre else f"{nombre} (alias de {normalizado})")
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
    """Analizador completo de argumentos que convierten ``self`` en Service Locator."""
    ofensores: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        argumentos: list[ast.expr] = [*nodo.args, *(kw.value for kw in nodo.keywords)]
        if any(_expone_self(arg) for arg in argumentos):
            ofensores.add(ast.unparse(nodo.func))
    return ofensores


def _es_self_tool_dispatcher(nodo: ast.AST) -> bool:
    """Dice si el nodo es exactamente ``self._tool_dispatcher``."""
    return (
        isinstance(nodo, ast.Attribute)
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "self"
        and nodo.attr == "_tool_dispatcher"
    )


def _alias_locales_del_dispatcher(fn: ast.AST) -> set[str]:
    """Acepta sólo aliases directos del dispatcher que nunca sean reasignados."""
    desde_dispatcher: set[str] = set()
    desde_otro_valor: set[str] = set()
    asignaciones = sorted(
        (nodo for nodo in ast.walk(fn) if isinstance(nodo, (ast.Assign, ast.AnnAssign))),
        key=lambda nodo: getattr(nodo, "lineno", 0),
    )
    for nodo in asignaciones:
        valor = nodo.value
        for target in _targets_nombre(nodo):
            if valor is not None and _es_self_tool_dispatcher(valor):
                desde_dispatcher.add(target)
            else:
                desde_otro_valor.add(target)
    return desde_dispatcher - desde_otro_valor


def _delega_al_dispatcher(nodo: ast.AST, alias_locales: set[str]) -> bool:
    """Reconoce delegación al dispatcher real o a un alias local no rebotado."""
    if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "dispatch"):
        return False
    receptor = nodo.func.value
    return _es_self_tool_dispatcher(receptor) or (
        isinstance(receptor, ast.Name) and receptor.id in alias_locales
    )


def _errores_dispatch(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    """Analizador completo de la frontera de ``dispatch_tool``."""
    alias = _alias_locales_del_dispatcher(fn)
    llamadas = [nodo for nodo in ast.walk(fn) if _delega_al_dispatcher(nodo, alias)]
    errores: list[str] = []
    if not llamadas:
        errores.append("sin delegación a _tool_dispatcher.dispatch")

    permitidos: set[int] = set()
    for llamada in llamadas:
        for arg in llamada.args:
            real = arg.value if isinstance(arg, ast.Starred) else arg
            if isinstance(real, ast.Name) and real.id == "tool_name":
                permitidos.add(id(real))
        for kw in llamada.keywords:
            if isinstance(kw.value, ast.Name) and kw.value.id == "tool_name":
                permitidos.add(id(kw.value))

    fuera = [
        nodo
        for nodo in ast.walk(fn)
        if isinstance(nodo, ast.Name) and nodo.id == "tool_name" and id(nodo) not in permitidos
    ]
    if fuera:
        errores.append(f"tool_name fuera de delegación: {len(fuera)}")
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
    """La capa de dominio sólo cruza mediante el allowlist de DTOs declarado."""
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


def test_dispatch_tool_sigue_delegando() -> None:
    """``dispatch_tool`` delega y ``tool_name`` no participa en routing inline."""
    fn = _metodo_de_clase(_clase_supervisor(), "dispatch_tool")
    assert fn is not None, "desapareció SupervisorAgent.dispatch_tool"
    errores = _errores_dispatch(fn)
    assert not errores, f"dispatch_tool reabsorbió routing: {errores}"


def test_mutantes_de_import_ejercitan_el_analizador_completo() -> None:
    """M9/M10/M13: imports por alias y APIs dinámicas fallan end-to-end."""
    mutantes = (
        "from sky_claw.local.assets import AssetConflictDetector as Detector\nDetector()\n",
        "import importlib\nimportlib.import_module('sky_claw.local.plugins')\n",
        "from importlib import import_module\nimport_module('sky_claw.local.plugins')\n",
        "from importlib import import_module as load_module\nload_module('sky_claw.local.plugins')\n",
    )
    for fuente in mutantes:
        arbol = ast.parse(fuente)
        assert _ofensores_imports(arbol) or _ofensores_invocaciones(arbol), fuente


def test_alias_directo_de_constructor_ejercita_el_analizador_completo() -> None:
    """M14: un constructor de dominio reasignado a nombre neutro sigue prohibido."""
    arbol = ast.parse(
        "from sky_claw.local.assets import AssetConflictDetector\n"
        "Detector = AssetConflictDetector\n"
        "Detector()\n"
    )
    assert _ofensores_invocaciones(arbol)


def test_mutantes_service_locator_ejercitan_el_analizador_completo() -> None:
    """M11/M16: closures, contenedores y accessors ligados de self fallan end-to-end."""
    for expresion in (
        "consume(lambda: self)",
        "consume({'supervisor': self})",
        "consume(self.__dict__.copy())",
        "consume(self.__getattribute__)",
    ):
        fn = _funcion_sintetica(f"def f(self):\n    {expresion}\n")
        assert _ofensores_service_locator(fn), expresion
    permitido = _funcion_sintetica("def f(self):\n    consume(self._tool_dispatcher)\n")
    assert not _ofensores_service_locator(permitido)


def test_mutantes_dispatch_ejercitan_el_analizador_completo() -> None:
    """M4/M5/M12/M15 y M8: routing/rebinding falla; alias equivalente pasa."""
    mutantes = (
        "async def dispatch_tool(self, tool_name, payload):\n"
        "    if tool_name == 'x':\n"
        "        return {}\n"
        "    return await self._tool_dispatcher.dispatch(tool_name, payload)\n",
        "async def dispatch_tool(self, tool_name, payload):\n"
        "    table.get(tool_name)\n"
        "    return await self._tool_dispatcher.dispatch(tool_name, payload)\n",
        "async def dispatch_tool(self, tool_name, payload):\n"
        "    router = self._legacy_router\n"
        "    return await router.dispatch(tool_name, payload)\n",
        "async def dispatch_tool(self, tool_name, payload):\n"
        "    d = self._tool_dispatcher\n"
        "    d = self._legacy_router\n"
        "    return await d.dispatch(tool_name, payload)\n",
    )
    for fuente in mutantes:
        assert _errores_dispatch(_funcion_sintetica(fuente)), fuente

    equivalente = _funcion_sintetica(
        "async def dispatch_tool(self, tool_name, payload):\n"
        "    d = self._tool_dispatcher\n"
        "    return await d.dispatch(tool_name, payload)\n"
    )
    assert not _errores_dispatch(equivalente)
