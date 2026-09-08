"""Anclas acotadas para findings tardíos del cierre F9 de ``SupervisorAgent``."""

from __future__ import annotations

import ast

from tests import test_supervisor_architecture_boundary as boundary

_CONSTRUCTOR_DOMINIO = "AssetConflictDetector"


def _bindings_constructor_dominio(arbol: ast.Module) -> frozenset[str]:
    """Resuelve el constructor permitido tanto canónico como re-exportado."""
    bindings: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.ImportFrom):
            continue
        modulo = boundary._resolver_modulo(nodo)
        if modulo != "sky_claw" and not boundary._es_modulo_de_dominio(modulo):
            continue
        for alias in nodo.names:
            if alias.name == _CONSTRUCTOR_DOMINIO:
                bindings.add(alias.asname or alias.name)
    return frozenset(bindings)


def _bindings_paquete_raiz(arbol: ast.Module) -> frozenset[str]:
    """Bindings directos creados por ``import sky_claw [as ...]``."""
    bindings: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Import):
            continue
        for alias in nodo.names:
            if alias.name == "sky_claw":
                bindings.add(alias.asname or alias.name)
    return frozenset(bindings)


def _defaults_de_constructor_dominio(arbol: ast.Module) -> set[str]:
    """Detecta defaults que capturan un constructor de dominio ejecutable."""
    bindings = _bindings_constructor_dominio(arbol)
    ofensores: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        defaults: list[ast.expr] = [*nodo.args.defaults, *(d for d in nodo.args.kw_defaults if d is not None)]
        for default in defaults:
            if any(isinstance(sub, ast.Name) and sub.id in bindings for sub in ast.walk(default)):
                ofensores.add(f"{nodo.name}: {ast.unparse(default)}")
    return ofensores


def _reexports_constructor_dominio(arbol: ast.Module) -> set[str]:
    """Fuerza que el constructor cruce desde su módulo canónico, no desde la raíz."""
    ofensores: set[str] = set()
    bindings_raiz = _bindings_paquete_raiz(arbol)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and boundary._resolver_modulo(nodo) == "sky_claw":
            for alias in nodo.names:
                if alias.name == _CONSTRUCTOR_DOMINIO:
                    ofensores.add(alias.asname or alias.name)
            continue
        if (
            isinstance(nodo, ast.Attribute)
            and nodo.attr == _CONSTRUCTOR_DOMINIO
            and isinstance(nodo.value, ast.Name)
            and nodo.value.id in bindings_raiz
        ):
            ofensores.add(f"{nodo.value.id}.{nodo.attr}")
    return ofensores


def _calls_import_builtin(arbol: ast.Module) -> set[str]:
    """Ancla independientemente la forma directa ``__import__(...)``."""
    return {
        ast.unparse(nodo)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "__import__"
    }


def _errores_dispatch_de_clase(clase: ast.ClassDef) -> list[str]:
    """Congela unicidad y ausencia de defaults en la firma pública."""
    metodos = [
        nodo
        for nodo in clase.body
        if isinstance(nodo, (ast.AsyncFunctionDef, ast.FunctionDef)) and nodo.name == "dispatch_tool"
    ]
    if len(metodos) != 1:
        return [f"dispatch_tool debe tener una única definición directa; encontradas={len(metodos)}"]
    fn = metodos[0]
    if fn.args.defaults or any(default is not None for default in fn.args.kw_defaults):
        return ["dispatch_tool no admite defaults"]
    return []


def test_supervisor_no_captura_constructor_dominio_en_defaults() -> None:
    assert not _defaults_de_constructor_dominio(boundary._SUPERVISOR_AST)

    mutante = ast.parse(
        "from sky_claw.local.assets import AssetConflictDetector\n"
        "def build(self, factory=AssetConflictDetector):\n"
        "    return factory()\n"
    )
    assert _defaults_de_constructor_dominio(mutante)


def test_supervisor_no_importa_constructor_desde_reexport_raiz() -> None:
    assert not _reexports_constructor_dominio(boundary._SUPERVISOR_AST)

    desde_raiz = ast.parse("from sky_claw import AssetConflictDetector as Detector\nDetector()\n")
    assert _reexports_constructor_dominio(desde_raiz) == {"Detector"}

    cualificado = ast.parse("import sky_claw\nsky_claw.AssetConflictDetector()\n")
    assert _reexports_constructor_dominio(cualificado) == {"sky_claw.AssetConflictDetector"}

    cualificado_alias = ast.parse("import sky_claw as sc\nsc.AssetConflictDetector()\n")
    assert _reexports_constructor_dominio(cualificado_alias) == {"sc.AssetConflictDetector"}


def test_import_builtin_directo_tiene_ancla_independiente() -> None:
    assert not _calls_import_builtin(boundary._SUPERVISOR_AST)

    mutante = ast.parse("__import__('sky_claw.local.plugins')\n")
    assert _calls_import_builtin(mutante)
    assert boundary._ofensores_imports(mutante)


def test_dispatch_tool_tiene_una_definicion_y_cero_defaults() -> None:
    assert not _errores_dispatch_de_clase(boundary._clase_supervisor())

    con_defaults = ast.parse(
        "class SupervisorAgent:\n"
        "    async def dispatch_tool(self, tool_name='loot', payload_dict={}):\n"
        "        return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
    ).body[0]
    assert isinstance(con_defaults, ast.ClassDef)
    assert _errores_dispatch_de_clase(con_defaults)

    duplicada = ast.parse(
        "class SupervisorAgent:\n"
        "    async def dispatch_tool(self, tool_name, payload_dict):\n"
        "        return await self._tool_dispatcher.dispatch(tool_name, payload_dict)\n"
        "    async def dispatch_tool(self, tool_name, payload_dict):\n"
        "        return await self._legacy_router.dispatch(tool_name, payload_dict)\n"
    ).body[0]
    assert isinstance(duplicada, ast.ClassDef)
    assert _errores_dispatch_de_clase(duplicada)
