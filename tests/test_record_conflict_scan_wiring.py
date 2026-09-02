"""Guardrails AST del wiring del RecordConflictScanner (PR6).

Paralelos al oracle de PR5 (``tests/test_plugin_limit_guard_wiring.py``):
comprueban SEMÁNTICA, no estilo. Toleran refactors equivalentes y rechazan
wiring distinto (dependencia incorrecta, ``**kwargs``, otro ``output_dir``,
reasignación del scanner, o lógica de dominio reintroducida en la facade).

A diferencia de PR5, el supervisor SÍ retiene el objeto (``self._record_conflict_scanner``)
porque la facade lo necesita en runtime — así que acá se ancla además que la
facade delegue sobre ESE mismo atributo y que nadie lo reasigne.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Atributo donde el supervisor guarda el scanner (contrato del wiring).
_ATRIBUTO = "_record_conflict_scanner"

#: Nombres de dominio que NO pueden reaparecer en el cuerpo de la facade. Si
#: alguno vuelve, la extracción se deshizo: la facade volvió a leer archivos,
#: resolver rutas, construir el runner o decidir precedencia.
_PROHIBIDOS_EN_LA_FACADE = frozenset(
    {
        "XEditRunner",
        "ConflictAnalyzer",
        "parse_active_plugins",
        "read_text",
        "exists",
        "resolve_modlist_path",
        "get_skyrim_path",
        "get_xedit_path",
        "get_active_profile",
        "BACKUP_STAGING_DIR",
        "DEEP_SCAN_TIMEOUT_SECONDS",
    }
)


def _arbol_supervisor() -> ast.Module:
    return ast.parse((REPO_ROOT / "sky_claw/app/orchestrator/supervisor.py").read_text(encoding="utf-8"))


def _clase_supervisor(arbol: ast.Module) -> ast.ClassDef:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "SupervisorAgent":
            return nodo
    raise AssertionError("No se encontró la clase SupervisorAgent en supervisor.py")


def _metodo(clase: ast.ClassDef, nombre: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for miembro in clase.body:
        if isinstance(miembro, (ast.FunctionDef, ast.AsyncFunctionDef)) and miembro.name == nombre:
            return miembro
    raise AssertionError(f"No se encontró SupervisorAgent.{nombre}")


def _inventario_nombres_e_imports(arbol: ast.AST) -> set[str]:
    """Inventario amplio de identificadores: imports (módulo y nombres),
    nombres y atributos (encadena los dotted a sus partes)."""
    inventario: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            if nodo.module:
                inventario.add(nodo.module)
            inventario.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Import):
            inventario.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Name):
            inventario.add(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            cursor: ast.expr = nodo
            while isinstance(cursor, ast.Attribute):
                inventario.add(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name):
                inventario.add(cursor.id)
    return inventario


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


def _construcciones_del_scanner(nodo: ast.AST) -> list[ast.Assign | ast.AnnAssign]:
    return [
        sub
        for sub in ast.walk(nodo)
        if isinstance(sub, (ast.Assign, ast.AnnAssign))
        and sub.value is not None
        and isinstance(sub.value, ast.Call)
        and isinstance(sub.value.func, ast.Name)
        and sub.value.func.id == "RecordConflictScanner"
    ]


# ── A: el seam extraído no conoce al supervisor ────────────────────────────────
def test_el_scanner_no_depende_del_supervisor() -> None:
    """El módulo nuevo no importa ni referencia SupervisorAgent.

    Detecta referencias en nombres, atributos e imports, incluidos imports
    perezosos o locales al cuerpo de funciones.
    """
    arbol = ast.parse((REPO_ROOT / "sky_claw/app/orchestrator/record_conflict_scan.py").read_text(encoding="utf-8"))
    inventario = _inventario_nombres_e_imports(arbol)

    assert "SupervisorAgent" not in inventario, (
        "record_conflict_scan referencia 'SupervisorAgent' — el flujo es scanner ← supervisor, nunca al revés"
    )
    assert "sky_claw.app.orchestrator.supervisor" not in inventario, (
        "record_conflict_scan importa el módulo del supervisor — crearía circularidad"
    )


# ── B/C: una sola construcción, con la dependencia correcta ─────────────────────
def test_exactamente_un_scanner_con_dependencias_correctas() -> None:
    """UNA construcción de RecordConflictScanner, sin posicionales ni ``**kwargs``,
    con ``path_resolver=self._path_resolver`` y el ``output_dir`` esperado."""
    clase = _clase_supervisor(_arbol_supervisor())
    construcciones = _construcciones_del_scanner(clase)
    assert len(construcciones) == 1, (
        f"SupervisorAgent debe construir EXACTAMENTE UN RecordConflictScanner (encontrados {len(construcciones)})"
    )

    call = construcciones[0].value
    assert isinstance(call, ast.Call)
    assert call.args == [], f"RecordConflictScanner no debe recibir posicionales (recibió {len(call.args)})"

    kw_named: dict[str, ast.expr] = {}
    for kw in call.keywords:
        assert kw.arg is not None, (
            "RecordConflictScanner no puede recibir `**kwargs` — el contrato es de dependencias explícitas"
        )
        kw_named[kw.arg] = kw.value

    assert set(kw_named) == {"path_resolver", "output_dir"}, (
        f"RecordConflictScanner se construye con kwargs {set(kw_named)} — esperado {{path_resolver, output_dir}}"
    )
    assert _formato_kwarg(kw_named["path_resolver"]) == "self._path_resolver", (
        "path_resolver debe ser self._path_resolver (NO _path_validator ni objeto fabricado)"
    )

    # output_dir: pathlib.Path(BACKUP_STAGING_DIR) / "patches" — se compara la
    # ESTRUCTURA (no el texto) y se prohíbe explícitamente normalizarlo a
    # absoluto: XEditRunner lo resuelve contra el cwd del scan.
    output_dir = kw_named["output_dir"]
    assert isinstance(output_dir, ast.BinOp) and isinstance(output_dir.op, ast.Div), (
        "output_dir debe ser '<staging> / \"patches\"' — el subdirectorio de patches del staging de backups"
    )
    assert isinstance(output_dir.right, ast.Constant) and output_dir.right.value == "patches", (
        f"el subdirectorio de output debe ser 'patches' (encontrado {_formato_kwarg(output_dir.right)})"
    )
    izquierda = output_dir.left
    assert isinstance(izquierda, ast.Call), "el staging de output_dir debe construirse con pathlib.Path(...)"
    assert _inventario_nombres_e_imports(izquierda) >= {"Path", "BACKUP_STAGING_DIR"}, (
        "output_dir debe derivar de pathlib.Path(BACKUP_STAGING_DIR) — no de otra raíz"
    )
    assert {"resolve", "absolute"}.isdisjoint(_inventario_nombres_e_imports(output_dir)), (
        "output_dir NO debe normalizarse a absoluto: XEditRunner lo resuelve contra el cwd del scan, "
        "y anclarlo al cwd de arranque movería dónde caen los patches"
    )


def test_el_scanner_se_guarda_en_el_atributo_del_contrato() -> None:
    """La construcción se asigna a ``self._record_conflict_scanner`` (el que usa
    la facade) y NADIE más lo reasigna en la clase."""
    clase = _clase_supervisor(_arbol_supervisor())
    construccion = _construcciones_del_scanner(clase)[0]

    targets = [
        t.attr
        for t in _targets_de(construccion)
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"
    ]
    assert targets == [_ATRIBUTO], (
        f"la construcción debe asignarse a self.{_ATRIBUTO} (encontrado {targets}) — es el binding que usa la facade"
    )

    # E: ninguna otra asignación al mismo atributo (detecta la reasignación
    # `self._record_conflict_scanner = otro` sin escribir un motor de dataflow).
    asignaciones = [
        nodo
        for nodo in ast.walk(clase)
        if isinstance(nodo, (ast.Assign, ast.AnnAssign))
        for target in _targets_de(nodo)
        if isinstance(target, ast.Attribute)
        and target.attr == _ATRIBUTO
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ]
    assert len(asignaciones) == 1, (
        f"self.{_ATRIBUTO} se asigna {len(asignaciones)} veces — debe haber UNA sola "
        "(una reasignación posterior haría que la facade delegue en otro objeto)"
    )


def test_el_scanner_no_recibe_al_supervisor() -> None:
    """Ningún argumento de la construcción es ``self`` (nada de service locator)."""
    clase = _clase_supervisor(_arbol_supervisor())
    call = _construcciones_del_scanner(clase)[0].value
    assert isinstance(call, ast.Call)

    for nodo in [*call.args, *(kw.value for kw in call.keywords)]:
        assert not (isinstance(nodo, ast.Name) and nodo.id == "self"), (
            "RecordConflictScanner no puede recibir el supervisor completo — solo dependencias explícitas"
        )


# ── D: la facade es fina ───────────────────────────────────────────────────────
def test_la_facade_de_record_conflicts_es_fina() -> None:
    """``scan_record_conflicts`` solo delega: nada de dominio en su cuerpo.

    Por AST (sin fijar whitespace ni números de línea). Si alguien vuelve a meter
    ahí la construcción del runner, la lectura del load order o la resolución de
    rutas, este ancla rompe.
    """
    clase = _clase_supervisor(_arbol_supervisor())
    facade = _metodo(clase, "scan_record_conflicts")

    inventario = _inventario_nombres_e_imports(ast.Module(body=facade.body, type_ignores=[]))
    reincidentes = sorted(_PROHIBIDOS_EN_LA_FACADE & inventario)
    assert not reincidentes, (
        f"scan_record_conflicts volvió a referenciar lógica de dominio: {reincidentes}. "
        "Vive en RecordConflictScanner (sky_claw/app/orchestrator/record_conflict_scan.py)."
    )

    assert _ATRIBUTO in inventario, f"la facade debe delegar en self.{_ATRIBUTO}"


def test_la_facade_delega_en_el_scanner_construido() -> None:
    """El cuerpo de la facade es exactamente ``await self.<atributo>.scan(...)``."""
    clase = _clase_supervisor(_arbol_supervisor())
    facade = _metodo(clase, "scan_record_conflicts")

    cuerpo = [
        nodo for nodo in facade.body if not isinstance(nodo, ast.Expr) or not isinstance(nodo.value, ast.Constant)
    ]
    assert len(cuerpo) == 1 and isinstance(cuerpo[0], ast.Return), (
        "la facade debe ser un único return de delegación (más su docstring)"
    )

    valor = cuerpo[0].value
    assert isinstance(valor, ast.Await), "la facade debe await-ear la corrutina del scanner"
    llamada = valor.value
    assert isinstance(llamada, ast.Call) and isinstance(llamada.func, ast.Attribute), (
        "la facade debe llamar a un método del scanner"
    )
    assert llamada.func.attr == "scan", f"la facade debe delegar en .scan() (encontrado .{llamada.func.attr}())"

    receptor = llamada.func.value
    assert (
        isinstance(receptor, ast.Attribute)
        and receptor.attr == _ATRIBUTO
        and isinstance(receptor.value, ast.Name)
        and receptor.value.id == "self"
    ), f"la facade debe delegar en self.{_ATRIBUTO} — no en otro objeto ni en uno fabricado al vuelo"

    # Los dos parámetros públicos se propagan por nombre, sin reinterpretarlos.
    assert llamada.args == [], "los argumentos deben pasarse por keyword para no depender del orden"
    kwargs = {kw.arg: _formato_kwarg(kw.value) for kw in llamada.keywords}
    assert kwargs == {"profile": "profile", "plugins": "plugins"}, (
        f"la facade debe propagar profile/plugins tal cual (encontrado {kwargs})"
    )


# ── El seam movido no quedó duplicado en el supervisor ─────────────────────────
def test_el_supervisor_ya_no_define_el_timeout_del_deep_scan() -> None:
    """``DEEP_SCAN_TIMEOUT_SECONDS`` se mudó al módulo del scanner (PR6, §6).

    Si reaparece en supervisor.py habría dos fuentes de verdad para el mismo
    timeout y podrían driftar.
    """
    arbol = _arbol_supervisor()
    definiciones = [
        target.id
        for nodo in arbol.body
        if isinstance(nodo, (ast.Assign, ast.AnnAssign))
        for target in _targets_de(nodo)
        if isinstance(target, ast.Name) and target.id == "DEEP_SCAN_TIMEOUT_SECONDS"
    ]
    assert not definiciones, (
        "supervisor.py volvió a definir DEEP_SCAN_TIMEOUT_SECONDS — vive en "
        "sky_claw/app/orchestrator/record_conflict_scan.py"
    )
