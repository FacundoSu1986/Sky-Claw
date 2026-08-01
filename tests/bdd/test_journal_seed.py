"""Tests unitarios del helper de seed del journal REAL (T3).

Contra un journal real (SQLite tmp), no un mock: el guard Stage 5→8 lee
``get_last_operation`` + ``is_flight_report_committed``, y esta capa afirma
que la receta de seed produce exactamente lo que el guard acepta — y que un
journal vacío lo deja fail-closed.
"""

from __future__ import annotations

import pathlib

from tests.bdd.support.grass_cache_harness import construir_servicio_grass, sembrar_loot_completado

from sky_claw.app.db.journal import OperationJournal, OperationStatus
from sky_claw.app.db.journal_contracts import is_flight_report_committed
from sky_claw.local.tools.grass_cache_service import GrassCacheService


async def test_sembrar_loot_deja_flight_report_commiteado_legible(tmp_path: pathlib.Path) -> None:
    journal = OperationJournal(tmp_path / "seed.db")
    await journal.open()
    try:
        await sembrar_loot_completado(journal)

        entrada = await journal.get_last_operation(
            GrassCacheService.LOOT_AGENT_ID, statuses=[OperationStatus.COMPLETED]
        )
        assert entrada is not None, "el seed debe dejar una operación de LOOT COMPLETED"
        assert is_flight_report_committed(entrada.metadata)
    finally:
        await journal.close()


async def test_journal_vacio_deja_al_guard_fail_closed(tmp_path: pathlib.Path) -> None:
    journal = OperationJournal(tmp_path / "seed.db")
    await journal.open()
    try:
        service, _ = construir_servicio_grass(tmp_path, journal=journal)

        mensaje_guard = await service._stage_guard(force=False)

        assert mensaje_guard is not None, "sin LOOT el guard debe rechazar (fail-closed)"
        assert "execute_loot_sorting" in mensaje_guard
    finally:
        await journal.close()


async def test_journal_sembrado_deja_al_guard_abierto(tmp_path: pathlib.Path) -> None:
    journal = OperationJournal(tmp_path / "seed.db")
    await journal.open()
    try:
        await sembrar_loot_completado(journal)
        service, _ = construir_servicio_grass(tmp_path, journal=journal)

        assert await service._stage_guard(force=False) is None
    finally:
        await journal.close()
