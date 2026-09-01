"""Guardrails AST del wiring del PluginLimitGuard (PR5).

Paralelos al oracle T9 del asset scanner: comprueban SEMÁNTICA, no estilo.
Toleran refactors equivalentes (variable local o ``self._plugin_limit_guard``)
y rechazan wiring distinto (otro objeto, dependencia incorrecta, ``**kwargs``,
reasignación del binding antes del builder, segunda llamada al builder).
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _arbol_supervisor() -> ast.Module:
    return ast.parse((REPO_ROOT / "sky_claw/app/orchestrator/supervisor.py").read_text(encoding="utf-8"))


def _init_del_supervisor(arbol: ast.Module) -> ast.FunctionDef:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            for miembro in nodo.body:
                if isinstance(miembro, ast.FunctionDef) and miembro.name == "__init__":
                    return miembro
    raise AssertionError("No se encontró SupervisorAgent.__init__")


def _unica_llamada_al_builder(miembro: ast.FunctionDef) -> ast.Call:
    llamadas = [
        sub
        for sub in ast.walk(miembro)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == "build_orchestration_composition"
    ]
    assert len(llamadas) == 1, (
        f"__init__ debe tener EXACTAMENTE UNA llamada a build_orchestration_composition "
        f"(encontradas {len(llamadas)}) — múltiples llamadas podrían mezclar contratos"
    )
    return llamadas[0]


def _referencia_logica(nodo: ast.expr) -> tuple[str, str] | None:
    """``name`` → ("name", name); ``self.attr`` → ("self_attr", attr); otro → None."""
    if isinstance(nodo, ast.Name):
        return ("name", nodo.id)
    if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "self":
        return ("self_attr", nodo.attr)
    return None


def _targets_de(nodo: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    return nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]


def _formato_kwarg(value: ast.expr) -> str:
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
        return f"self.{value.attr}"
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Constant):
        return repr(value.value)
    return type(value).__name__


def test_exactamente_un_guard_con_dependencia_correcta() -> None:
    """A/C/D: exactamente UNA construcción de PluginLimitGuard, sin posicionales
    ni ``**kwargs``, con exactamente ``path_resolver=self._path_resolver``."""
    init = _init_del_supervisor(_arbol_supervisor())

    construcciones = [
        sub
        for sub in ast.walk(init)
        if isinstance(sub, (ast.Assign, ast.AnnAssign))
        and sub.value is not None
        and isinstance(sub.value, ast.Call)
        and isinstance(sub.value.func, ast.Name)
        and sub.value.func.id == "PluginLimitGuard"
    ]
    assert len(construcciones) == 1, (
        f"__init__ debe construir EXACTAMENTE UN PluginLimitGuard (encontrados {len(construcciones)})"
    )

    call = construcciones[0].value
    assert isinstance(call, ast.Call)
    assert call.args == [], f"PluginLimitGuard no debe recibir posicionales (recibió {len(call.args)})"
    kw_named: dict[str, ast.expr] = {}
    for kw in call.keywords:
        assert kw.arg is not None, (
            "PluginLimitGuard no puede recibir `**kwargs` — el contrato es path_resolver explícito"
        )
        kw_named[kw.arg] = kw.value
    assert set(kw_named) == {"path_resolver"}, (
        f"PluginLimitGuard se construye con kwargs {set(kw_named)} — esperado exactamente {{path_resolver}}"
    )
    assert _formato_kwarg(kw_named["path_resolver"]) == "self._path_resolver", (
        "path_resolver debe ser self._path_resolver (NO _path_validator ni objeto fabricado)"
    )


def test_el_builder_recibe_el_run_del_mismo_guard() -> None:
    """B/E: el builder recibe ``plugin_limit_guard=<ref>.run`` donde <ref> es un
    binding vigente del guard construido en __init__ (soporta alias simples,
    self.attr y reasignación detector)."""
    init = _init_del_supervisor(_arbol_supervisor())
    builder_call = _unica_llamada_al_builder(init)

    for kw in builder_call.keywords:
        assert kw.arg is not None, (
            "build_orchestration_composition no puede usar `**kwargs` "
            "porque impediría inventariar exhaustivamente los seams"
        )

    asignaciones = sorted(
        (n for n in ast.walk(init) if isinstance(n, (ast.Assign, ast.AnnAssign))),
        key=lambda n: n.lineno,
    )
    construcciones = [
        sub
        for sub in asignaciones
        if sub.value is not None
        and isinstance(sub.value, ast.Call)
        and isinstance(sub.value.func, ast.Name)
        and sub.value.func.id == "PluginLimitGuard"
    ]
    assert len(construcciones) == 1
    construccion = construcciones[0]

    guard_id = "plugin_limit_guard#1"
    bindings: dict[tuple[str, str], str] = {}
    for target in _targets_de(construccion):
        ref = _referencia_logica(target)
        if ref is not None:
            bindings[ref] = guard_id
    assert any(v == guard_id for v in bindings.values()), (
        "La construcción del guard debe asignarse a un nombre local o a un atributo de self"
    )

    for sub in asignaciones:
        if sub.lineno <= construccion.lineno or sub.lineno >= builder_call.lineno:
            continue
        val_ref = _referencia_logica(sub.value) if sub.value is not None else None
        val_id = bindings.get(val_ref) if val_ref is not None else None
        for target in _targets_de(sub):
            target_ref = _referencia_logica(target)
            if target_ref is not None:
                bindings[target_ref] = guard_id if val_id == guard_id else "other"

    kwargs_al_builder = {kw.arg: kw.value for kw in builder_call.keywords}
    valor = kwargs_al_builder.get("plugin_limit_guard")
    assert valor is not None, "plugin_limit_guard no se entregó al builder"
    assert isinstance(valor, ast.Attribute) and valor.attr == "run", (
        "plugin_limit_guard debe ser <guard>.run — no otro callable ni un alias a otro guard"
    )
    ref_base = _referencia_logica(valor.value)
    assert ref_base is not None and bindings.get(ref_base) == guard_id, (
        f"plugin_limit_guard usa una referencia que no resuelve al guard construido "
        f"(ref: {ref_base}, bindings: {bindings}) — identidad lógica rota"
    )


def test_el_guard_no_depende_del_supervisor() -> None:
    """El seam extraído no puede importar ni instanciar SupervisorAgent."""
    fuente = (REPO_ROOT / "sky_claw/app/orchestrator/plugin_limit_guard.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and any(
            (alias.name == "SupervisorAgent" or (alias.asname == "SupervisorAgent")) for alias in nodo.names
        ):
            raise AssertionError("plugin_limit_guard.py importa SupervisorAgent — debe recibir path_resolver explícito")
        if isinstance(nodo, ast.Name) and nodo.id == "SupervisorAgent":
            raise AssertionError("plugin_limit_guard.py referencia SupervisorAgent por nombre")
        if isinstance(nodo, ast.Attribute) and nodo.attr == "SupervisorAgent":
            raise AssertionError("plugin_limit_guard.py referencia el atributo SupervisorAgent")
