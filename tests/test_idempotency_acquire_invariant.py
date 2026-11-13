"""Ancla de #575 §1: el resultado de ``acquire_idempotency()`` no se ignora.

La propiedad del mecanismo: *la exclusión por idempotencia solo protege si TODO
quien toma la key mira si la obtuvo*. ``IdempotencyMiddleware`` llamaba
``acquire_idempotency(task_id)`` y descartaba el ``bool``: si la key ya tenía otro
dueño, igual pasaba a ``RUNNING`` y ejecutaba el tool (doble ejecución) y dejaba una
task huérfana.

Este archivo **enumera** por AST la familia de llamadores de producción en vez de
muestrear un caso: un mutador nuevo que tome la key rompe el ancla hasta que alguien
decide, a propósito, si participa de la regla.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
_ARBOLES_DE_PRODUCCION = ("sky_claw", "scripts")

_MIDDLEWARE = "sky_claw/app/orchestrator/tool_strategies/middleware.py"

#: Únicos módulos de producción que toman la key de idempotencia o crean tasks del
#: ``ToolStateMachine``. Congelado por igualdad literal.
_TOMADORES_DE_KEY = frozenset({_MIDDLEWARE})
_CREADORES_DE_TASK = frozenset({_MIDDLEWARE})


def _es_toma_de_key(nodo: ast.Call) -> bool:
    """``<x>.acquire_idempotency(...)`` o ``<x>.guard.acquire(...)``."""
    func = nodo.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "acquire_idempotency":
        return True
    return func.attr == "acquire" and isinstance(func.value, ast.Attribute) and func.value.attr == "guard"


def _es_creacion_de_task(nodo: ast.Call) -> bool:
    """``<sm>.create_task(tool_name, payload, ...)``.

    ``asyncio.create_task`` / ``loop.create_task`` reciben UN solo argumento posicional
    (la corrutina); la máquina de estados recibe dos (``tool_name`` y ``payload``).
    """
    func = nodo.func
    return isinstance(func, ast.Attribute) and func.attr == "create_task" and len(nodo.args) >= 2


def _tomas_sin_consumir(fuente: str) -> list[int]:
    """Líneas donde la toma de la key es una sentencia suelta (resultado descartado)."""
    arbol = ast.parse(fuente)
    descartadas: list[int] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Expr) and isinstance(nodo.value, ast.Call) and _es_toma_de_key(nodo.value):
            descartadas.append(nodo.lineno)
    return sorted(descartadas)


def _archivos_de_produccion() -> list[Path]:
    archivos: list[Path] = []
    for arbol in _ARBOLES_DE_PRODUCCION:
        archivos.extend(sorted((_RAIZ / arbol).rglob("*.py")))
    return archivos


def _modulos_que(predicado: object) -> frozenset[str]:
    encontrados: set[str] = set()
    for ruta in _archivos_de_produccion():
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        if any(isinstance(n, ast.Call) and predicado(n) for n in ast.walk(arbol)):  # type: ignore[operator]
            encontrados.add(ruta.relative_to(_RAIZ).as_posix())
    return frozenset(encontrados)


# ---------------------------------------------------------------------------
# Casos sintéticos: el detector distingue lo que debe distinguir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fuente", "esperadas"),
    [
        ("self._sm.acquire_idempotency(t)\n", [1]),
        ("self._sm.guard.acquire(k, t)\n", [1]),
        ("x = 1\nsm.acquire_idempotency(t)\n", [2]),
        ("ok = self._sm.acquire_idempotency(t)\n", []),
        ("if not self._sm.acquire_idempotency(t):\n    pass\n", []),
        ("return self._guard.acquire(k, t)\n", []),
        ("self.lock.acquire()\n", []),
    ],
)
def test_detector_de_tomas_descartadas_sintetico(fuente: str, esperadas: list[int]) -> None:
    assert _tomas_sin_consumir(fuente) == esperadas


def test_detector_de_creacion_de_task_ignora_asyncio() -> None:
    llamada_asyncio = ast.parse("asyncio.create_task(coro())").body[0]
    llamada_sm = ast.parse("sm.create_task('tool', {})").body[0]
    assert isinstance(llamada_asyncio, ast.Expr) and isinstance(llamada_asyncio.value, ast.Call)
    assert isinstance(llamada_sm, ast.Expr) and isinstance(llamada_sm.value, ast.Call)
    assert not _es_creacion_de_task(llamada_asyncio.value)
    assert _es_creacion_de_task(llamada_sm.value)


# ---------------------------------------------------------------------------
# Anclas sobre el árbol real
# ---------------------------------------------------------------------------


def test_los_tomadores_de_la_key_estan_congelados() -> None:
    assert _modulos_que(_es_toma_de_key) == _TOMADORES_DE_KEY


def test_los_creadores_de_task_estan_congelados() -> None:
    assert _modulos_que(_es_creacion_de_task) == _CREADORES_DE_TASK


def test_ningun_tomador_de_produccion_descarta_el_resultado_del_acquire() -> None:
    descartadas: dict[str, list[int]] = {}
    for ruta in _archivos_de_produccion():
        lineas = _tomas_sin_consumir(ruta.read_text(encoding="utf-8"))
        if lineas:
            descartadas[ruta.relative_to(_RAIZ).as_posix()] = lineas

    assert descartadas == {}
