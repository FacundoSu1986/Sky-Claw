"""Contrato del helper de seed del journal REAL.

Contra un journal real (SQLite tmp), no un mock: afirma que la receta de seed
produce exactamente lo que el guard Stage 5→8 lee (``get_last_operation`` +
``is_flight_report_committed``). Si el seed miente, los escenarios Gherkin
pasarían por la razón equivocada.

Alcance deliberado: acá NO se testea el guard. Su comportamiento ya está
cubierto por los unitarios de ``tests/test_grass_cache_service.py`` y, end-to-end
y a través de la API pública, por los escenarios de
``features/sop_modding/generacion_grass_cache.feature``. Los tests que llamaban
``service._stage_guard(...)`` acoplaban esta capa a un método privado sin
comprar cobertura.
"""

from __future__ import annotations

import pathlib

from tests.bdd.support.grass_cache_harness import sembrar_loot_completado

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


async def test_journal_vacio_no_deja_flight_report_legible(tmp_path: pathlib.Path) -> None:
    """La variante "sin LOOT" del seed es la ausencia total: journal vacío."""
    journal = OperationJournal(tmp_path / "seed.db")
    await journal.open()
    try:
        entrada = await journal.get_last_operation(
            GrassCacheService.LOOT_AGENT_ID, statuses=[OperationStatus.COMPLETED]
        )
        assert entrada is None, "sin seed no puede haber FlightReport de LOOT que el guard acepte"
    finally:
        await journal.close()
