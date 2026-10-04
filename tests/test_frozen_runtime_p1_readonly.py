"""Ancla de P1: el paquete Frozen Runtime es READ-ONLY sobre la Managed Source.

El discovery/observación/estabilización de P1 no puede escribir, renombrar,
borrar, linkear, cambiar permisos ni lanzar procesos. Este ancla congela por
AST el vocabulario de mutación del paquete: cualquier símbolo mutador nuevo
rompe el test a propósito (misma técnica que tests/test_db_connection_invariant.py).
"""

from __future__ import annotations

import ast
import pathlib

PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime"

MUTADORES_FILESYSTEM: frozenset[str] = frozenset(
    {
        "write_text",
        "write_bytes",
        "remove",
        "unlink",
        "rmdir",
        "replace",
        "rename",
        "chmod",
        "mkdir",
        "makedirs",
        "symlink_to",
        "touch",
        "rmtree",
        "move",
        "copy2",
        "copyfile",
        "copytree",
    }
)

LANZADORES_PROCESO: frozenset[str] = frozenset(
    {"run", "call", "check_call", "check_output", "Popen", "system", "CreateProcess"}
)

MODOS_ESCRITURA: frozenset[str] = frozenset({"w", "a", "x", "+", "wb", "ab", "xb", "r+", "rb+"})


def _modulos_del_paquete() -> tuple[pathlib.Path, ...]:
    return tuple(sorted(PAQUETE.glob("*.py")))


def test_paquete_sin_simbolos_de_mutacion() -> None:
    """Congela el vocabulario: ningún símbolo mutador puede aparecer en el paquete."""
    violaciones: list[str] = []
    for modulo in _modulos_del_paquete():
        arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Attribute) and nodo.attr in MUTADORES_FILESYSTEM:
                violaciones.append(f"{modulo.name}:{nodo.lineno}: {nodo.attr}")
            if isinstance(nodo, ast.Attribute) and nodo.attr in LANZADORES_PROCESO:
                violaciones.append(f"{modulo.name}:{nodo.lineno}: {nodo.attr}")
    assert not violaciones, f"símbolos de mutación en Frozen Runtime P1 (read-only roto): {violaciones}"


def test_paquete_sin_open_en_modo_escritura() -> None:
    """``open()`` sólo puede usarse en modo lectura si aparece."""
    violaciones: list[str] = []
    for modulo in _modulos_del_paquete():
        arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "open" and nodo.args:
                modo = (
                    nodo.args[1]
                    if len(nodo.args) > 1
                    else nodo.keywords and next((kw.value for kw in nodo.keywords if kw.arg == "mode"), None)
                )
                if isinstance(modo, ast.Constant) and isinstance(modo.value, str) and modo.value in MODOS_ESCRITURA:
                    violaciones.append(f"{modulo.name}:{nodo.lineno}: open modo '{modo.value}'")
    assert not violaciones, f"open() en modo escritura dentro de Frozen Runtime P1: {violaciones}"
