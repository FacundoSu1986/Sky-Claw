"""Ancla de familia (#575 ítem 1): quien adquiere la guard de idempotencia tiene que COMPROBAR el resultado.

``IdempotencyMiddleware`` llamaba ``acquire_idempotency()`` y descartaba el ``bool``. La propiedad es del
mecanismo, no del caso: *una guard de idempotencia solo excluye si TODO consumidor que la adquiere actúa
sobre el rechazo*. Un caso escrito a mano para el middleware no ataja al próximo consumidor, así que este
módulo ENUMERA por AST los sitios de producción que adquieren la guard y congela la familia:

1. ningún sitio descarta el resultado (sentencia suelta o asignación a ``_``);
2. el censo de consumidores es exactamente el conocido: uno nuevo rompe el ancla hasta que alguien decide
   cómo reacciona al rechazo y lo suma acá;
3. fuera de la envoltura de la state machine (que devuelve el ``bool`` tal cual), el resultado se usa como
   condición de un ``if``: asignarlo a una variable y no mirarla tampoco cuenta como comprobarlo.

Los casos sintéticos del primer test demuestran que el detector distingue comprobar de descartar; sin ellos
el ancla podría estar verde por no encontrar nada.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
DIRECTORIOS_DE_PRODUCCION = ("sky_claw", "scripts")

_RECEPTORES_DE_GUARD = frozenset({"guard", "_guard"})

#: La state machine reexpone el resultado de la guard: DEVUELVE el ``bool``, no decide nada con él.
ENVOLTURA = (
    "sky_claw/app/orchestrator/tool_state_machine.py",
    "ToolStateMachine.acquire_idempotency",
)
CONSUMIDORES_CONOCIDOS = frozenset(
    {
        ENVOLTURA,
        (
            "sky_claw/app/orchestrator/tool_strategies/middleware.py",
            "IdempotencyMiddleware.__call__",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class Sitio:
    """Una llamada que adquiere la guard de idempotencia."""

    ruta: str
    funcion: str
    descartado: bool
    devuelto: bool
    en_if: bool


def _es_adquisicion(nodo: ast.AST) -> bool:
    """``<algo>.acquire_idempotency(...)`` o ``<guard|_guard>.acquire(...)``.

    ``acquire`` a secas es de locks y semáforos ajenos: solo cuenta con un receptor llamado guard.
    """
    if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
        return False
    if nodo.func.attr == "acquire_idempotency":
        return True
    if nodo.func.attr != "acquire":
        return False
    receptor = nodo.func.value
    nombre = receptor.id if isinstance(receptor, ast.Name) else getattr(receptor, "attr", "")
    return nombre in _RECEPTORES_DE_GUARD


def _padres(arbol: ast.AST) -> dict[ast.AST, ast.AST]:
    return {hijo: padre for padre in ast.walk(arbol) for hijo in ast.iter_child_nodes(padre)}


def _nombre_calificado(nodo: ast.AST, padres: dict[ast.AST, ast.AST]) -> str:
    partes: list[str] = []
    actual = padres.get(nodo)
    while actual is not None:
        if isinstance(actual, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            partes.append(actual.name)
        actual = padres.get(actual)
    return ".".join(reversed(partes)) or "<modulo>"


def _se_descarta(nodo: ast.AST, padres: dict[ast.AST, ast.AST]) -> bool:
    padre = padres.get(nodo)
    if isinstance(padre, ast.Expr):
        return True
    return isinstance(padre, ast.Assign) and all(isinstance(t, ast.Name) and t.id == "_" for t in padre.targets)


def _esta_en_el_test_de_un_if(nodo: ast.AST, padres: dict[ast.AST, ast.AST]) -> bool:
    hijo, padre = nodo, padres.get(nodo)
    while padre is not None:
        if isinstance(padre, ast.If | ast.IfExp) and hijo is padre.test:
            return True
        hijo, padre = padre, padres.get(padre)
    return False


def analizar(fuente: str, ruta: str) -> list[Sitio]:
    """Los sitios que adquieren la guard en ``fuente``, con lo que se hace con su resultado."""
    arbol = ast.parse(fuente)
    padres = _padres(arbol)
    return [
        Sitio(
            ruta=ruta,
            funcion=_nombre_calificado(nodo, padres),
            descartado=_se_descarta(nodo, padres),
            devuelto=isinstance(padres.get(nodo), ast.Return),
            en_if=_esta_en_el_test_de_un_if(nodo, padres),
        )
        for nodo in ast.walk(arbol)
        if _es_adquisicion(nodo)
    ]


def _sitios_de_produccion() -> list[Sitio]:
    sitios: list[Sitio] = []
    for directorio in DIRECTORIOS_DE_PRODUCCION:
        for archivo in sorted((RAIZ / directorio).rglob("*.py")):
            ruta = archivo.relative_to(RAIZ).as_posix()
            sitios.extend(analizar(archivo.read_text(encoding="utf-8"), ruta))
    return sitios


# ---------------------------------------------------------------------------
# El detector distingue comprobar de descartar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fuente", "descartado", "en_if"),
    [
        ("def f(sm):\n    sm.acquire_idempotency('t')\n", True, False),
        ("def f(sm):\n    _ = sm.acquire_idempotency('t')\n", True, False),
        ("def f(sm):\n    ok = sm.acquire_idempotency('t')\n    return ok\n", False, False),
        ("def f(sm):\n    if not sm.acquire_idempotency('t'):\n        return None\n", False, True),
        ("def f(guard):\n    guard.acquire('k', 't')\n", True, False),
        ("def f(self):\n    if not self._guard.acquire('k', 't'):\n        return None\n", False, True),
        ("def f(sm):\n    if sm.ok():\n        sm.acquire_idempotency('t')\n", True, False),
    ],
    ids=[
        "sentencia-suelta",
        "asignacion-a-guion-bajo",
        "asignada-y-devuelta",
        "comprobada-en-if",
        "guard-directa-descartada",
        "guard-privada-comprobada",
        "dentro-del-cuerpo-de-un-if",
    ],
)
def test_el_detector_distingue_comprobar_de_descartar(fuente: str, descartado: bool, en_if: bool) -> None:
    (sitio,) = analizar(fuente, "sintetico.py")

    assert sitio.descartado is descartado
    assert sitio.en_if is en_if


def test_el_detector_ignora_locks_ajenos() -> None:
    assert analizar("def f(lock):\n    lock.acquire()\n", "sintetico.py") == []


def test_el_detector_califica_la_funcion_envolvente() -> None:
    fuente = "class C:\n    async def m(self, sm):\n        sm.acquire_idempotency('t')\n"

    (sitio,) = analizar(fuente, "sintetico.py")

    assert sitio.funcion == "C.m"


# ---------------------------------------------------------------------------
# La familia de producción
# ---------------------------------------------------------------------------


def test_ningun_consumidor_descarta_el_resultado_de_acquire() -> None:
    descartados = [(s.ruta, s.funcion) for s in _sitios_de_produccion() if s.descartado]

    assert descartados == [], f"estos sitios adquieren la guard e ignoran si fue rechazada: {descartados}"


def test_el_censo_de_consumidores_de_acquire_esta_congelado() -> None:
    consumidores = {(s.ruta, s.funcion) for s in _sitios_de_produccion()}

    assert consumidores == CONSUMIDORES_CONOCIDOS, (
        "cambió la familia de consumidores de la guard de idempotencia: un consumidor nuevo tiene que "
        "decidir cómo reacciona al rechazo y sumarse a CONSUMIDORES_CONOCIDOS"
    )


def test_los_consumidores_fuera_de_la_envoltura_comprueban_el_resultado_en_un_if() -> None:
    sin_comprobar = [
        (s.ruta, s.funcion) for s in _sitios_de_produccion() if (s.ruta, s.funcion) != ENVOLTURA and not s.en_if
    ]

    assert sin_comprobar == [], f"estos sitios no usan el resultado como condición de un if: {sin_comprobar}"


def test_la_envoltura_de_la_state_machine_devuelve_el_resultado_de_la_guard() -> None:
    envoltura = [s for s in _sitios_de_produccion() if (s.ruta, s.funcion) == ENVOLTURA]

    assert len(envoltura) == 1
    assert envoltura[0].devuelto, "la envoltura tiene que devolver el bool de la guard, no tragárselo"
