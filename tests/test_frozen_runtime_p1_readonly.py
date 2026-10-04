"""Ancla de P1/P2: boundary de escritura del paquete Frozen Runtime.

P1 (discovery/observation/provider_signals/models/_vdf/stabilization) es
READ-ONLY sobre disco: sin símbolos de mutación (congelado por AST, misma
técnica que tests/test_db_connection_invariant.py).

P2 introduce el ÚNICO módulo con escritura permitida (``state.py``: estado
persistente + metadata de generations, SIEMPRE dentro del FrozenRuntimeRoot
propio). Su vocabulario de mutación está congelado por igualdad literal: un
mutador nuevo (rmtree, chmod, rename no-atómico, copy*, symlink_to...) rompe
el test a propósito. Los lanzadores de procesos están prohibidos en todo el
paquete. MANAGED_SOURCE_WRITES=NO se mantiene por construcción: ninguna API
de escritura acepta una Managed Source.
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
        "write",
        "flush",
        "fsync",
        "mkstemp",
        "fdopen",
    }
)

LANZADORES_PROCESO: frozenset[str] = frozenset(
    {"run", "call", "check_call", "check_output", "Popen", "system", "CreateProcess"}
)

# Vocabulario de mutación congelado por módulo (P2): ÚNICOS módulos con
# escritura permitida, SIEMPRE dentro del FrozenRuntimeRoot propio.
# state.py: temporal en el mismo directorio + os.replace + fsync + cleanup.
# storage.py: creación idempotente del layout (mkdir).
MODULOS_CON_ESCRITURA_PERMITIDA: dict[str, frozenset[str]] = {
    "state.py": frozenset({"fdopen", "flush", "fsync", "mkstemp", "replace", "unlink"}),
    "storage.py": frozenset({"mkdir"}),
}

MODOS_ESCRITURA: frozenset[str] = frozenset({"w", "a", "x", "+", "wb", "ab", "xb", "r+", "rb+"})


def _modulos_del_paquete() -> tuple[pathlib.Path, ...]:
    return tuple(sorted(PAQUETE.glob("*.py")))


def _es_mutador(nodo: ast.Attribute) -> bool:
    if nodo.attr not in MUTADORES_FILESYSTEM:
        return False
    if nodo.attr == "replace":
        # str.replace no es mutación de filesystem; sólo os.replace lo es.
        return isinstance(nodo.value, ast.Name) and nodo.value.id == "os"
    return True


def _mutadores_usados(modulo: pathlib.Path) -> set[str]:
    arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
    return {nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute) and _es_mutador(nodo)}


def test_escritura_solo_en_modulos_permitidos() -> None:
    """Sólo state.py/storage.py mutan, y sólo con su vocabulario congelado."""
    violaciones: list[str] = []
    for modulo in _modulos_del_paquete():
        usados = _mutadores_usados(modulo)
        permitidos = MODULOS_CON_ESCRITURA_PERMITIDA.get(modulo.name, frozenset())
        if usados != permitidos:
            violaciones.append(
                f"{modulo.name}: usados={sorted(usados)} permitidos={sorted(permitidos)} — "
                "decidí si el símbolo pertenece al boundary (escritura dentro del FrozenRuntimeRoot propio) "
                "y congélalo en MODULOS_CON_ESCRITURA_PERMITIDA"
            )
    assert not violaciones, f"boundary de escritura violado: {violaciones}"


def test_sin_lanzadores_de_proceso() -> None:
    """Ningún módulo del paquete puede lanzar procesos (ni Steam ni nada)."""
    violaciones: list[str] = []
    for modulo in _modulos_del_paquete():
        arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Attribute) and nodo.attr in LANZADORES_PROCESO:
                violaciones.append(f"{modulo.name}:{nodo.lineno}: {nodo.attr}")
    assert not violaciones, f"lanzadores de proceso en Frozen Runtime: {violaciones}"


def test_p1_sin_open_en_modo_escritura() -> None:
    """``open()`` sólo puede usarse en modo lectura (la escritura va por fdopen atómico)."""
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
