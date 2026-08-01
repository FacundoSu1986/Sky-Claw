"""Pruebas del lifecycle determinista del harness asíncrono BDD."""

from __future__ import annotations

import asyncio


async def _duplica(valor: int) -> int:
    await asyncio.sleep(0)
    return valor * 2


async def _tarea_larga() -> None:
    await asyncio.sleep(30)


def test_scenario_loop_cierra_y_cancela_pendientes_sin_orden_de_fixtures() -> None:
    from tests.bdd.support.async_harness import ScenarioLoop

    with ScenarioLoop() as harness:
        loop = harness.loop
        pendiente = loop.create_task(_tarea_larga())

        assert loop.run_until_complete(_duplica(21)) == 42
        assert not loop.is_closed()

    assert pendiente.cancelled()
    assert loop.is_closed()
    assert not asyncio.all_tasks(loop)


def test_scenario_loop_restaura_el_loop_exterior() -> None:
    from tests.bdd.support.async_harness import ScenarioLoop

    exterior = asyncio.new_event_loop()
    asyncio.set_event_loop(exterior)
    try:
        with ScenarioLoop() as harness:
            assert asyncio.get_event_loop() is harness.loop

        assert asyncio.get_event_loop() is exterior
        assert not exterior.is_closed()
    finally:
        asyncio.set_event_loop(None)
        exterior.close()
