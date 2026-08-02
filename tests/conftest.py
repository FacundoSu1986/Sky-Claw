"""Global fixtures shared across the sky_claw test suite.

Add fixtures here when the same setup appears in 3+ test files.
Do NOT add single-use or highly specific fixtures — keep those inline.

Naming convention: fixtures are snake_case and describe WHAT they provide,
not how. Example: `async_registry` (not `make_registry_with_lifecycle`).

Coverage policy: target +5pp per sprint until 80% minimum.
Current gate: 60% (raised from 55% on 2026-05-28, P0.4). Actual: ~65%.
"""

from __future__ import annotations

import gc
import os
import pathlib
import shutil
import stat
import sys
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.app.db.async_registry import AsyncModRegistry
from sky_claw.app.security.network_gateway import NetworkGateway
from sky_claw.logging_config import correlation_id_var
from tests._lifecycle_guard import close_registry_then_lifecycle, find_leaked_threads

#: Flag que enciende el GC determinista por test. Ver ``_gc_al_cerrar_cada_test``.
GC_POR_TEST = "--gc-por-test"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Registra ``--gc-por-test``, el modo de diagnóstico de fugas de lifecycle."""
    parser.addoption(
        GC_POR_TEST,
        action="store_true",
        default=False,
        help=(
            "Corre gc.collect() al cerrar cada test para que un "
            "PytestUnraisableExceptionWarning acuse al test que fugó el recurso "
            "y no a uno posterior al azar. Cuadruplica la duración de la suite: "
            "encendelo para diagnosticar, no en cada corrida."
        ),
    )


@pytest.fixture(autouse=True)
def _gc_al_cerrar_cada_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Corre el GC cíclico en el teardown, para que el warning acuse al culpable.

    ``pyproject.toml`` convierte ``PytestUnraisableExceptionWarning`` en error
    (#409). Ese warning se emite cuando el intérprete finaliza un objeto cuyo
    ``__del__`` protesta —un socket o un event loop sin cerrar—, y **eso ocurre
    cuando corre el GC, no cuando el test fugó el recurso**. Si el recurso quedó
    atrapado en un ciclo de referencias, el refcount nunca llega a cero y sólo lo
    libera el GC generacional, que se dispara por presión de asignación en un
    test *posterior* y arbitrario. pytest le carga la falla a ese test.

    El resultado medido, antes de este fixture: #413 acusó a
    ``test_ningun_modulo_borra_arboles_con_shutil_rmtree`` —que sólo hace
    ``ast.parse`` en memoria y no abre un solo descriptor— y en otra corrida a
    ``test_wrong_token_returns_false``; #414 reprodujo la misma firma sobre
    ``origin/main`` limpio, en un tercer test distinto. Tres víctimas inocentes,
    todas verdes al reejecutarse enfocadas: la marca de una acusación falsa, no
    de un test inestable.

    **La propiedad que se restaura:** *un warning emitido durante el GC
    pertenece al test que dejó el objeto inalcanzable, y eso sólo es cierto si el
    GC corre dentro del teardown de ese test.* Con el ciclo recolectado acá, la
    falla es reproducible y nombra a quien la causó.

    Es autouse y se declara primero a propósito: pytest desarma en orden inverso
    al de creación, así que este teardown corre **último** — después de que el
    resto de los fixtures soltó sus referencias, que es cuando el recurso fugado
    recién queda inalcanzable.

    **Va apagado por defecto, y eso se decidió con números.** Medido sobre esta
    suite (3863 tests, misma máquina, misma corrida completa):

    ===============================================  ==========
    sin ``gc.collect()``                              129 s
    con ``gc.collect()`` por test                     536 s
    lo mismo + ``gc.freeze()`` al arrancar la sesión   583 s
    ===============================================  ==========

    Son ~105 ms por test: un recorrido completo del heap, que ``gc.freeze()`` no
    abarata porque el costo lo ponen los objetos que asignan los propios tests,
    no los del import. Cuadruplicar cada corrida de CI para adelantarse a una
    mala atribución hipotética es mal negocio; el diagnóstico se paga sólo
    cuando hace falta.

    **Cuando haga falta**, que es al ver un ``PytestUnraisableExceptionWarning``
    en un test que no abre recursos::

        pytest --gc-por-test

    Eso convierte la acusación falsa en el nombre del culpable. Es el camino que
    a #413 y #414 les faltó: entre los dos gastaron dos PRs sin poder reproducir
    la falla, y la fuga real —el event loop huérfano que fabricaba pytest-asyncio
    hasta 1.3.0, cerrado subiendo el piso de la dependencia— apareció en la
    primera corrida con este flag.

    No arregla ninguna fuga: la vuelve atribuible. El ancla de esta propiedad
    vive en ``tests/test_atribucion_de_warnings.py``, y corre en CI con el flag
    encendido en un pytest hijo — así que el mecanismo está verificado aunque la
    suite grande no lo use.
    """
    yield
    if request.config.getoption(GC_POR_TEST):
        gc.collect()


@pytest.fixture()
async def async_registry(tmp_path: pathlib.Path) -> AsyncGenerator[AsyncModRegistry, None]:
    """AsyncModRegistry backed by a per-test tmp_path SQLite database.

    M-01 compliant: uses an explicit DatabaseLifecycleManager so the registry
    participates in the process-wide connection-pool lifecycle. Closes cleanly
    on teardown — no leaked aiosqlite connections.
    """
    lifecycle = DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    registry = AsyncModRegistry(db_path=tmp_path / "test.db", lifecycle=lifecycle)
    await registry.open()
    try:
        yield registry
    finally:
        # Nested finally: shutdown_all() must run even if close() raises,
        # otherwise non-daemon aiosqlite threads hang the session (CI 20-min timeout).
        await close_registry_then_lifecycle(registry, lifecycle)


@pytest.fixture()
def mock_network_gateway() -> MagicMock:
    """NetworkGateway stub for tests that should NOT hit the real network.

    Matches the real API: ``resp = await gateway.request(method, url, session, ...)``.
    The stub returns a 200-OK mock response with async ``text()``/``json()``
    and synchronous ``release()``, matching ``aiohttp.ClientResponse``.
    Override ``mock_network_gateway.request.return_value`` in a test to simulate
    specific status codes or response bodies.
    """
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = AsyncMock(return_value="")
    mock_resp.json = AsyncMock(return_value={})
    mock_resp.release = MagicMock()

    gateway = MagicMock(spec=NetworkGateway)
    gateway.request = AsyncMock(return_value=mock_resp)
    return gateway


@pytest.fixture(autouse=True)
def _reset_governance_singleton() -> Iterator[None]:
    """Reset the GovernanceManager singleton around every test.

    Tests that call ``GovernanceManager.get_instance()`` would otherwise leak
    a base_path-bound instance into unrelated tests (order-dependent failures).
    Mutates under the same class lock ``get_instance()`` uses, so a background
    thread mid-``get_instance()`` never observes a torn singleton.
    """
    from sky_claw.app.security.governance import GovernanceManager

    with GovernanceManager._lock:
        GovernanceManager._instance = None
    yield
    with GovernanceManager._lock:
        GovernanceManager._instance = None


@pytest.fixture(autouse=True)
def _localappdata_aislado(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirige LOCALAPPDATA a un directorio vacío por test (aislamiento del host).

    ``LoadOrderFileResolver`` (y todo lo que lo construye por defecto, como
    ``LootSortingService._ensure_load_order_resolver``) cae al env
    ``LOCALAPPDATA`` cuando no se inyecta ``local_app_data``. En una máquina
    con Skyrim SE instalado eso hacía que los tests vieran el
    ``plugins.txt``/``loadorder.txt`` REALES del host y fallaran de forma
    dependiente del entorno. Los tests que necesitan candidatos en
    LOCALAPPDATA inyectan su propio directorio; los que necesitan la variable
    ausente la borran con ``monkeypatch.delenv``.
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("localappdata_aislado")))


@pytest.fixture()
def correlation_id() -> Iterator[str]:
    """Set a UUID4 correlation_id on the logging ContextVar for test duration.

    Yields the string ID so tests can assert against it in log records.
    Resets the ContextVar on teardown to avoid leaking into other tests.
    """
    cid = str(uuid.uuid4())
    token = correlation_id_var.set(cid)
    yield cid
    correlation_id_var.reset(token)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    """Remove .pytest-tmp after every session to prevent Windows ACL lock buildup.

    On Windows, temp dirs created under AppData can accumulate ACL entries that
    cause PermissionError on the next pytest run. Using a workspace-local basetemp
    (.pytest-tmp) and cleaning it here keeps CI and local runs reproducible.

    Only runs on the controller process — xdist workers set ``workerinput`` on
    their config, so we skip cleanup there to avoid deleting the shared basetemp
    root while sibling workers are still writing to it.
    """
    if hasattr(session.config, "workerinput"):
        return  # xdist worker — controller handles cleanup

    basetemp = pathlib.Path(".pytest-tmp")
    if not basetemp.exists():
        return

    def _force_remove(func: Callable[[str], None], path: str, _exc: object) -> None:
        """onerror handler: chmod read-only flag then retry the original operation."""
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass  # best-effort — leave orphan rather than crash session teardown

    shutil.rmtree(basetemp, onerror=_force_remove)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Fail fast when non-daemon threads survive the session.

    Leaked aiosqlite worker threads (non-daemon) block interpreter exit, so a
    single missed ``close()`` turns into a silent hang that burns the full CI
    job timeout (20 min). ``os._exit(3)`` converts that hang into an immediate,
    attributable failure. Runs after the terminal summary, controller only.
    """
    if hasattr(config, "workerinput"):
        return  # xdist worker — controller owns the verdict

    leaked = find_leaked_threads(grace_seconds=2.0)
    if leaked:
        names = ", ".join(f"{t.name!r} (ident={t.ident})" for t in leaked)
        print(
            f"\n[LIFECYCLE FAIL-FAST] {len(leaked)} hilo(s) non-daemon sin cerrar "
            f"al final de la sesion: {names}. Saliendo con codigo 3 para evitar "
            "el cuelgue del proceso (timeout CI de 20 min).",
            file=sys.stderr,
        )
        sys.stderr.flush()
        sys.stdout.flush()
        os._exit(3)
