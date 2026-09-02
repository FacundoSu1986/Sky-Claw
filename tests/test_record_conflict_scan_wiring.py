"""Guardrails arquitectónicos del wiring del RecordConflictScanner (PR6).

Comprueban que el seam extraído sea independiente de SupervisorAgent, que el
supervisor construya un único scanner con dependencias explícitas (sin self como
service locator) y que scan_record_conflicts sea una facade delegante delgada.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

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


def _arbol(ruta_rel: str) -> ast.Module:
    return ast.parse((REPO_ROOT / ruta_rel).read_text(encoding="utf-8"))


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


def _nombres_del_arbol(arbol: ast.AST) -> set[str]:
    """Extrae identificadores, módulos importados y atributos del AST."""
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name):
            nombres.add(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            nombres.add(nodo.attr)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres.add(nodo.module)
            nombres.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Import):
            nombres.update(alias.name for alias in nodo.names)
    return nombres


def _llamadas_scanner(clase: ast.ClassDef) -> list[ast.Call]:
    return [
        nodo
        for nodo in ast.walk(clase)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "RecordConflictScanner"
    ]


# ── A1: el seam extraído no conoce al supervisor ─────────────────────────────
def test_el_scanner_no_depende_del_supervisor() -> None:
    """record_conflict_scan.py no importa ni referencia SupervisorAgent."""
    nombres = _nombres_del_arbol(_arbol("sky_claw/app/orchestrator/record_conflict_scan.py"))
    assert "SupervisorAgent" not in nombres, "record_conflict_scan no debe referenciar SupervisorAgent"
    assert "sky_claw.app.orchestrator.supervisor" not in nombres, "record_conflict_scan no debe importar supervisor"


# ── A2, A3, A4: construcción única con dependencias explícitas ────────────────
def test_supervisor_construye_un_scanner_con_dependencias_explicitas() -> None:
    """SupervisorAgent construye exactamente un RecordConflictScanner con dependencias explícitas."""
    clase = _clase_supervisor(_arbol("sky_claw/app/orchestrator/supervisor.py"))
    llamadas = _llamadas_scanner(clase)
    assert len(llamadas) == 1, f"Debe haber exactamente UNA llamada a RecordConflictScanner (halladas {len(llamadas)})"

    call = llamadas[0]
    assert call.args == [], f"RecordConflictScanner no recibe args posicionales (recibió {len(call.args)})"

    kw_dict: dict[str, ast.expr] = {}
    for kw in call.keywords:
        assert kw.arg is not None, "RecordConflictScanner no admite **kwargs"
        kw_dict[kw.arg] = kw.value

    assert set(kw_dict) == {"path_resolver", "output_dir"}, (
        f"Kwargs esperados {{path_resolver, output_dir}}, hallados {set(kw_dict)}"
    )

    # path_resolver debe ser self._path_resolver (A3)
    pr = kw_dict["path_resolver"]
    assert (
        isinstance(pr, ast.Attribute)
        and pr.attr == "_path_resolver"
        and isinstance(pr.value, ast.Name)
        and pr.value.id == "self"
    ), "path_resolver debe ser self._path_resolver (no _path_validator ni service locator)"

    # No service locator: self no se pasa como argumento directo (A4)
    for valor in kw_dict.values():
        assert not (isinstance(valor, ast.Name) and valor.id == "self"), "RecordConflictScanner no debe recibir self"


# ── A5, A6: facade delgada en SupervisorAgent ─────────────────────────────────
def test_la_facade_de_record_conflicts_es_fina() -> None:
    """scan_record_conflicts no contiene lógica de dominio reintroducida."""
    clase = _clase_supervisor(_arbol("sky_claw/app/orchestrator/supervisor.py"))
    facade = _metodo(clase, "scan_record_conflicts")

    nombres = _nombres_del_arbol(ast.Module(body=facade.body, type_ignores=[]))
    reincidentes = sorted(_PROHIBIDOS_EN_LA_FACADE & nombres)
    assert not reincidentes, f"scan_record_conflicts reintrodujo lógica de dominio: {reincidentes}"


def test_la_facade_delega_en_el_scanner() -> None:
    """scan_record_conflicts delega con await self._record_conflict_scanner.scan(profile=profile, plugins=plugins)."""
    clase = _clase_supervisor(_arbol("sky_claw/app/orchestrator/supervisor.py"))
    facade = _metodo(clase, "scan_record_conflicts")

    stmts = [s for s in facade.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    assert len(stmts) == 1 and isinstance(stmts[0], ast.Return), "La facade debe ser un único return de delegación"

    ret_val = stmts[0].value
    assert isinstance(ret_val, ast.Await), "La facade debe await-ear el scan"
    call = ret_val.value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    assert call.func.attr == "scan", f"Debe llamar a .scan(), hallado .{call.func.attr}()"

    receptor = call.func.value
    assert (
        isinstance(receptor, ast.Attribute)
        and receptor.attr == "_record_conflict_scanner"
        and isinstance(receptor.value, ast.Name)
        and receptor.value.id == "self"
    ), "La llamada debe ser self._record_conflict_scanner.scan"

    kwargs = {kw.arg: kw.value.id for kw in call.keywords if kw.arg and isinstance(kw.value, ast.Name)}
    assert kwargs == {"profile": "profile", "plugins": "plugins"}, f"Kwargs delegados incorrectos: {kwargs}"
