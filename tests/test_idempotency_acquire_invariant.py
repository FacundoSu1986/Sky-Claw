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
_MAQUINA_DE_ESTADOS = "sky_claw/app/orchestrator/tool_state_machine.py"

#: Únicos módulos de producción que toman la key de idempotencia o crean tasks del
#: ``ToolStateMachine``. Congelado por igualdad literal. La máquina de estados toma la key
#: con la primitiva (``self._guard.acquire`` dentro de ``acquire_idempotency``) y DEVUELVE el
#: ``bool``: es la envoltura, no un consumidor que decida con él.
_TOMADORES_DE_KEY = frozenset({_MIDDLEWARE, _MAQUINA_DE_ESTADOS})
_CREADORES_DE_TASK = frozenset({_MIDDLEWARE})


_RECEPTORES_DE_GUARD = frozenset({"guard", "_guard"})


def _es_toma_de_key(nodo: ast.Call) -> bool:
    """``<x>.acquire_idempotency(...)`` o ``<guard>.acquire(...)``.

    ``<guard>`` es ``guard`` o ``_guard``, como atributo (``sm.guard``, ``self._guard``) o como
    nombre suelto. Un ``acquire`` a secas (locks, semáforos) no cuenta. Límite declarado: no se
    siguen alias (``g = sm.guard; g.acquire(...)``) ni accesos dinámicos (``getattr``).
    """
    func = nodo.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "acquire_idempotency":
        return True
    if func.attr != "acquire":
        return False
    receptor = func.value
    nombre = receptor.id if isinstance(receptor, ast.Name) else getattr(receptor, "attr", "")
    return nombre in _RECEPTORES_DE_GUARD


def _es_creacion_de_task(nodo: ast.Call) -> bool:
    """``<sm>.create_task(tool_name, payload, ...)``.

    ``asyncio.create_task`` / ``loop.create_task`` reciben UN solo argumento posicional
    (la corrutina); la máquina de estados recibe dos (``tool_name`` y ``payload``).
    """
    func = nodo.func
    return isinstance(func, ast.Attribute) and func.attr == "create_task" and len(nodo.args) >= 2


def _padres(arbol: ast.AST) -> dict[ast.AST, ast.AST]:
    return {hijo: padre for padre in ast.walk(arbol) for hijo in ast.iter_child_nodes(padre)}


def _ambito(nodo: ast.AST, padres: dict[ast.AST, ast.AST]) -> ast.AST:
    """La función (o el módulo) que contiene ``nodo``."""
    actual = padres.get(nodo)
    while actual is not None and not isinstance(actual, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
        actual = padres.get(actual)
    return actual if actual is not None else nodo


def _se_descarta(llamada: ast.Call, padres: dict[ast.AST, ast.AST]) -> bool:
    """El resultado de la toma se tira: sentencia suelta, ``_ = ...`` o un nombre que nadie lee después.

    Asignarlo a un atributo o a un contenedor cuenta como guardarlo (no se sigue más allá).
    """
    padre = padres.get(llamada)
    if isinstance(padre, ast.Expr):
        return True
    if isinstance(padre, ast.Assign) and len(padre.targets) == 1:
        destino = padre.targets[0]
    elif isinstance(padre, ast.AnnAssign):
        destino = padre.target
    else:
        return False
    if not isinstance(destino, ast.Name):
        return False
    if destino.id == "_":
        return True
    leido_despues = any(
        isinstance(n, ast.Name) and n.id == destino.id and isinstance(n.ctx, ast.Load) and n.lineno > padre.lineno
        for n in ast.walk(_ambito(padre, padres))
    )
    return not leido_despues


def _tomas_sin_consumir(fuente: str) -> list[int]:
    """Líneas donde el resultado de la toma de la key se descarta."""
    arbol = ast.parse(fuente)
    padres = _padres(arbol)
    return sorted(
        nodo.lineno
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and _es_toma_de_key(nodo) and _se_descarta(nodo, padres)
    )


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
        ("ok = self._sm.acquire_idempotency(t)\nif not ok:\n    pass\n", []),
        ("if not self._sm.acquire_idempotency(t):\n    pass\n", []),
        ("return self._guard.acquire(k, t)\n", []),
        ("self.lock.acquire()\n", []),
        # El resultado también se descarta si se asigna a ``_`` o a un nombre que nadie lee después:
        # no alcanza con que la llamada no sea una sentencia suelta.
        ("_ = self._sm.acquire_idempotency(t)\n", [1]),
        ("ok = self._sm.acquire_idempotency(t)\nx = 1\n", [1]),
        ("def f(sm):\n    ok = sm.acquire_idempotency(t)\n    return None\n", [2]),
        ("def f(sm):\n    ok: bool = sm.acquire_idempotency(t)\n", [2]),
        # Una lectura ANTERIOR a la asignación no cuenta como uso del resultado.
        ("def f(sm, ok):\n    print(ok)\n    ok = sm.acquire_idempotency(t)\n", [3]),
        # Asignar y comprobar después es un estilo legítimo: no es un falso positivo.
        ("def f(sm):\n    ok = sm.acquire_idempotency(t)\n    if not ok:\n        return None\n", []),
        ("def f(sm):\n    ok = sm.acquire_idempotency(t)\n    return ok\n", []),
        # La primitiva también cuenta con el receptor privado y con un nombre suelto.
        ("self._guard.acquire(k, t)\n", [1]),
        ("guard.acquire(k, t)\n", [1]),
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
