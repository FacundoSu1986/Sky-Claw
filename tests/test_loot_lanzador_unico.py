"""M3 — LOOT se lanza desde UN solo lugar: ``LOOTRunner`` (``local/loot/cli.py``).

Propiedad: *todo lanzamiento de LOOT pasa por el runner que arma el data root
aislado, los settings headless y el testigo de ejecución*. Un segundo lanzador
—como el ``ModdingToolsAgent.run_loot`` legacy, con rutas hardcodeadas y sin
lock, journal ni testigo— es exactamente el hermano que el contrato de PR-2
deja afuera.

La propiedad se ancla sobre el switch que dispara el sort: ``--auto-sort``
(``src/gui/qt/main.cpp``). El censo recorre TODO el paquete por AST.
"""

from __future__ import annotations

import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Módulos de producción que contienen el literal ``--auto-sort``.
_LANZADORES_DE_LOOT = {"local/loot/cli.py"}


def _modulos_con_auto_sort() -> set[str]:
    encontrados: set[str] = set()
    for ruta in sorted(PAQUETE.rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        docstrings = {
            id(nodo.body[0].value)
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and nodo.body
            and isinstance(nodo.body[0], ast.Expr)
            and isinstance(nodo.body[0].value, ast.Constant)
        }
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Constant)
                and isinstance(nodo.value, str)
                and nodo.value == "--auto-sort"
                and id(nodo) not in docstrings
            ):
                encontrados.add(ruta.relative_to(PAQUETE).as_posix())
    return encontrados


def test_un_solo_modulo_lanza_loot() -> None:
    assert _modulos_con_auto_sort() == _LANZADORES_DE_LOOT


def test_el_lanzador_legacy_de_windows_interop_no_existe() -> None:
    from sky_claw.app.core.windows_interop import ModdingToolsAgent

    assert not hasattr(ModdingToolsAgent, "run_loot")


def test_el_detector_ve_el_literal() -> None:
    """Contracara: el censo no puede pasar por no ver nada."""
    arbol = ast.parse('args = ["LOOT.exe", "--auto-sort"]')
    assert any(isinstance(n, ast.Constant) and n.value == "--auto-sort" for n in ast.walk(arbol))
