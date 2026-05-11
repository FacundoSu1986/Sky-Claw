"""Global fixtures shared across the sky_claw test suite.

Add fixtures here when the same setup appears in 3+ test files.
Do NOT add single-use or highly specific fixtures — keep those inline.

Naming convention: fixtures are snake_case and describe WHAT they provide,
not how. Example: `async_registry` (not `make_registry_with_lifecycle`).

Coverage policy: target +5pp per sprint until 80% minimum.
Current gate: 55% (raised from 49% on 2026-05-11).
"""

from __future__ import annotations

import pathlib
import uuid
from collections.abc import AsyncGenerator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.antigravity.db.async_registry import AsyncModRegistry
from sky_claw.antigravity.security.network_gateway import NetworkGateway
from sky_claw.logging_config import correlation_id_var


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
        await registry.close()
        await lifecycle.shutdown_all()


@pytest.fixture()
def mock_network_gateway() -> MagicMock:
    """NetworkGateway stub for tests that should NOT hit the real network.

    Matches the real API: ``resp = await gateway.request(method, url, session, ...)``.
    The stub returns a 200-OK mock response with async ``text()``, ``json()``,
    and ``release()`` methods. Override ``mock_network_gateway.request.return_value``
    in your test to simulate specific status codes or response bodies.
    """
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = AsyncMock(return_value="")
    mock_resp.json = AsyncMock(return_value={})
    mock_resp.release = AsyncMock()

    gateway = MagicMock(spec=NetworkGateway)
    gateway.request = AsyncMock(return_value=mock_resp)
    return gateway


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
