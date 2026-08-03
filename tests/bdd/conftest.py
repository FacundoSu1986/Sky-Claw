"""Fixtures y steps compartidos de la suite BDD de Sky-Claw.

pytest-bdd 8.x genera funciones de test SÍNCRONAS y no awaitea steps
``async def`` nativos (verificado por ejecución: ``RuntimeWarning: coroutine
... never awaited``). Por eso esta capa POSEE su propio event loop
(``bdd_loop``), creado y destruido de forma explícita: todos los steps de un
escenario corren en ese loop vía ``run()``, lo que da continuidad de estado
async entre Given/When/Then (journal real, locks, servicio) y un punto de
teardown limpio que no fuga hilos non-daemon (invariante del repo:
``pytest_unconfigure`` → ``os._exit(3)`` en ``tests/conftest.py``).

Los steps COMPARTIDOS (el Background de los features) viven acá: pytest-bdd
sólo resuelve steps entre módulos hermanos vía ``conftest.py`` (verificado:
``StepDefinitionNotFoundError`` cuando el step vive en otro módulo de
``step_defs/``). Ningún step debe inyectar fixtures async globales (ej.
``async_registry``): viven en el loop de pytest-asyncio, distinto de
``bdd_loop`` → riesgo de "attached to a different loop".
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_bdd import given, parsers
from tests.bdd.support.async_harness import ScenarioLoop, run
from tests.bdd.support.grass_cache_harness import (
    abrir_journal_real,
    construir_dispatcher_grass,
    construir_servicio_grass,
)

from sky_claw.app.db.journal import OperationJournal
from sky_claw.app.security.hitl import Decision

# ---------------------------------------------------------------------------
# Harness asíncrono: loop propio por escenario (3.11/3.12-safe)
# ---------------------------------------------------------------------------


@pytest.fixture()
def bdd_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Loop propio por escenario, administrado por una primitiva testeable."""
    with ScenarioLoop() as harness:
        yield harness.loop


@pytest.fixture()
def ctx(bdd_loop: asyncio.AbstractEventLoop) -> Iterator[dict[str, Any]]:
    """Estado del escenario + recursos async.

    Depende de ``bdd_loop``: pytest desarma fixtures en orden inverso al de
    creación, así que ``ctx`` se desarma ANTES que ``bdd_loop`` — el journal
    real (aiosqlite, hilo non-daemon) se cierra con el loop todavía vivo y
    jamás dispara ``os._exit(3)``.
    """
    state: dict[str, Any] = {}
    yield state
    journal: OperationJournal | None = state.get("journal")
    if journal is not None:
        bdd_loop.run_until_complete(journal.close())


# ---------------------------------------------------------------------------
# Steps compartidos (Background de los features)
# ---------------------------------------------------------------------------


@given("que el entorno de Mod Organizer 2 usa rutas de prueba aisladas")
def entorno_mo2_resuelto(bdd_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any], tmp_path: pathlib.Path) -> None:
    """Journal REAL abierto en el loop del escenario, bajo el tmp_path aislado.

    El árbol MO2 lo crea ``construir_servicio_grass`` (dueño único): duplicarlo
    acá dejaba ambiguo quién lo posee.
    """
    ctx["journal"] = run(bdd_loop, abrir_journal_real(tmp_path / "bdd_journal.db"))


@given("que el journal real y el servicio de grass cache están disponibles")
def servicio_grass_cableado(ctx: dict[str, Any], tmp_path: pathlib.Path) -> None:
    """Servicio con journal real, mocks en el boundary y lock factory que registra."""
    service, colaboradores = construir_servicio_grass(tmp_path, journal=ctx["journal"])
    dispatcher, hitl_guard = construir_dispatcher_grass(service)
    ctx["service"] = service
    ctx["dispatcher"] = dispatcher
    colaboradores["hitl_guard"] = hitl_guard
    ctx["colaboradores"] = colaboradores


@given("que el dispatcher se cablea sin ningún HITLGuard")
def dispatcher_sin_hitl_guard(ctx: dict[str, Any]) -> None:
    """Recablea el dispatcher sin guard: ``build_orchestration_dispatcher`` arma
    su ``HitlGateMiddleware()`` por defecto, que es fail-closed. NUNCA se usa
    ``allow_unattended=True`` — sería desactivar justo lo que se quiere anclar.
    """
    dispatcher, hitl_guard = construir_dispatcher_grass(ctx["service"], con_guard=False)
    ctx["dispatcher"] = dispatcher
    ctx["colaboradores"]["hitl_guard"] = hitl_guard


@given(parsers.parse('que el operador aprueba la ejecución destructiva de "{tool_name}"'))
def operador_aprueba_tool(ctx: dict[str, Any], tool_name: str) -> None:
    """Configura una aprobación explícita; nunca habilita ejecución unattended."""
    assert tool_name == "generate_grass_cache"
    ctx["colaboradores"]["hitl_guard"].request_approval.return_value = Decision.APPROVED


@given(parsers.parse('que el operador deniega la ejecución destructiva de "{tool_name}"'))
def operador_deniega_tool(ctx: dict[str, Any], tool_name: str) -> None:
    """Deniega explícitamente: el gate HITL debe cortar antes de cualquier mutación."""
    assert tool_name == "generate_grass_cache"
    ctx["colaboradores"]["hitl_guard"].request_approval.return_value = Decision.DENIED
