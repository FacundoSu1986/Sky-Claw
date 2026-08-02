"""Ancla: la sesión arranca con un loop actual propio, y nadie fabrica huérfanos.

Cuando pytest empieza sin un event loop actual, pytest-asyncio **fabrica** uno y
lo deja suelto: ``_temporary_event_loop_policy`` abre con
``old_loop = _get_event_loop_no_warn()`` (``plugin.py:618``), que en Python 3.11
llama a ``asyncio.get_event_loop()`` con el ``DeprecationWarning`` silenciado — y
esa función **crea** un loop si no hay ninguno. Ese loop nunca se corre, nunca se
cierra, y queda de "actual" hasta que alguien ponga el loop actual en ``None``.
Ahí pierde su última referencia, el GC lo finaliza y protesta con un
``unclosed event loop`` más los dos sockets ``AF_UNIX`` de su self-pipe. Con
``error::ResourceWarning`` (#409), eso es la suite en rojo.

Es la fuga que #414 describió —«dos sockets y un ``ProactorEventLoop`` no
cerrados durante GC», reproducida sobre ``origin/main`` limpio— y declaró fuera
de su alcance. En Windows el fabricado es un ``ProactorEventLoop``; en POSIX un
``_UnixSelectorEventLoop``. Mismo mecanismo.

**Lo que se ancla es la precondición, no el síntoma.** El test que en la suite
completa hacía saltar la falla (``test_scenario_loop_restaura_el_loop_exterior``,
que pone el loop actual en ``None`` en su ``finally``) no es el culpable: sólo
suelta lo que otro dejó. Anclar «que nadie ponga el loop actual en ``None``»
habría escondido el objeto sin cerrarlo, y habría prohibido una higiene correcta.
Lo que se fija es que al llegar el primer test ya haya un loop actual con dueño.

El escenario se ejerce en un pytest hijo porque la fabricación ocurre **una vez
por sesión**, en el setup del primer test: no hay forma de reproducirla dentro de
una sesión que ya arrancó.
"""

from __future__ import annotations

import asyncio
import pathlib

from tests.conftest import GC_POR_TEST
from tests.test_atribucion_de_warnings import correr_pytest_hijo, ini_del_hijo

#: Un test async —que hace entrar a pytest-asyncio— seguido de uno que suelta el
#: loop actual. Ese par es el mínimo que destapa el loop fabricado: el primero lo
#: provoca, el segundo lo deja sin referencias para que el GC lo finalice.
_MODULO_DE_PRUEBA = """
import asyncio


async def test_async_hace_entrar_a_pytest_asyncio():
    await asyncio.sleep(0)


def test_suelta_el_loop_actual():
    asyncio.set_event_loop(None)
"""

#: Sin el hook de sesión: sólo el GC determinista (``--gc-por-test``), para que
#: la fuga se detecte YA y no en un test al azar más adelante.
_CONFTEST_SIN_HOOK = "from tests.conftest import _gc_al_cerrar_cada_test, pytest_addoption  # noqa: F401\n"

#: Con los hooks REALES de ``tests/conftest.py``. Se importan en vez de
#: replicarse: una copia local anclaría un clon y dejaría pasar que alguien borre
#: el original.
_CONFTEST_CON_HOOK = (
    "from tests.conftest import (  # noqa: F401\n"
    "    _gc_al_cerrar_cada_test,\n"
    "    pytest_addoption,\n"
    "    pytest_sessionfinish,\n"
    "    pytest_sessionstart,\n"
    ")\n"
)


def _preparar(carpeta: pathlib.Path, *, con_hook: bool) -> pathlib.Path:
    escenario = carpeta / ("con_hook" if con_hook else "sin_hook")
    escenario.mkdir()
    (escenario / "test_fuga_de_loop.py").write_text(_MODULO_DE_PRUEBA, encoding="utf-8")
    # ``asyncio_mode = auto`` es lo que hace que el test async del escenario
    # entre por pytest-asyncio sin decorador, igual que en esta suite.
    (escenario / "pytest.ini").write_text(ini_del_hijo() + "asyncio_mode = auto\n", encoding="utf-8")
    (escenario / "conftest.py").write_text(_CONFTEST_CON_HOOK if con_hook else _CONFTEST_SIN_HOOK, encoding="utf-8")
    return escenario


def test_sin_el_hook_de_sesion_queda_un_event_loop_sin_cerrar(tmp_path: pathlib.Path) -> None:
    """Control del experimento: la fuga es real y el gate de #409 la ve.

    Si pytest-asyncio dejara de fabricar el loop, ESTE test se pone rojo primero
    — y ahí el hook pasa a ser código muerto que se puede retirar con evidencia,
    en vez de quedarse para siempre "por las dudas".
    """
    reporte = correr_pytest_hijo(_preparar(tmp_path, con_hook=False), GC_POR_TEST)

    assert "unclosed event loop" in reporte, reporte


def test_con_el_hook_de_sesion_no_queda_ningun_loop_sin_cerrar(tmp_path: pathlib.Path) -> None:
    """La propiedad: con un loop actual instalado de entrada, no hay huérfano."""
    reporte = correr_pytest_hijo(_preparar(tmp_path, con_hook=True), GC_POR_TEST)

    assert "unclosed event loop" not in reporte, reporte
    assert "2 passed" in reporte, reporte


def test_la_sesion_es_duena_de_un_loop_abierto() -> None:
    """El dueño existe y está vivo mientras corre la suite.

    Complementa a los dos de arriba desde adentro: aquellos prueban el efecto en
    un hijo, éste comprueba que ESTA sesión —la que corre ahora— tiene el loop
    instalado y sin cerrar, que es lo que impide la fabricación.
    """
    from tests import conftest

    assert conftest._LOOP_DE_SESION is not None
    assert not conftest._LOOP_DE_SESION.is_closed()
    assert isinstance(conftest._LOOP_DE_SESION, asyncio.AbstractEventLoop)
