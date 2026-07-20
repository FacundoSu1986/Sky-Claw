"""Global fixtures shared across the sky_claw test suite.

Add fixtures here when the same setup appears in 3+ test files.
Do NOT add single-use or highly specific fixtures — keep those inline.

Naming convention: fixtures are snake_case and describe WHAT they provide,
not how. Example: `async_registry` (not `make_registry_with_lifecycle`).

Coverage policy: target +5pp per sprint until 80% minimum.
Current gate: 60% (raised from 55% on 2026-05-28, P0.4). Actual: ~65%.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import sys
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.antigravity.db.async_registry import AsyncModRegistry
from sky_claw.antigravity.security.network_gateway import NetworkGateway
from sky_claw.logging_config import correlation_id_var
from tests._lifecycle_guard import close_registry_then_lifecycle, find_leaked_threads


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
    from sky_claw.antigravity.security.governance import GovernanceManager

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
