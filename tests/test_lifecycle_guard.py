"""Tests for tests/_lifecycle_guard.py — fail-fast helpers for CI lifecycle bugs.

Covers the documented failure mode: non-daemon aiosqlite worker threads that
outlive the test session when ``close()`` raises during teardown, hanging the
pytest process until the CI job timeout (20 min).
"""

from __future__ import annotations

import pathlib
import threading
import time

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from tests._lifecycle_guard import close_registry_then_lifecycle, find_leaked_threads


def test_find_leaked_threads_detects_non_daemon_thread() -> None:
    """An alive non-daemon thread must be reported as leaked."""
    stop = threading.Event()
    probe = threading.Thread(target=stop.wait, name="leak-probe", daemon=False)
    probe.start()
    try:
        leaked = find_leaked_threads()
        assert any(t.name == "leak-probe" for t in leaked)
    finally:
        stop.set()
        probe.join(timeout=5)


def test_find_leaked_threads_ignores_daemon_and_main_threads() -> None:
    """Daemon threads and the main thread are not leaks — they never block exit."""
    stop = threading.Event()
    probe = threading.Thread(target=stop.wait, name="daemon-probe", daemon=True)
    probe.start()
    try:
        leaked = find_leaked_threads()
        assert all(t.name != "daemon-probe" for t in leaked)
        assert threading.main_thread() not in leaked
    finally:
        stop.set()
        probe.join(timeout=5)


def test_find_leaked_threads_grace_period_allows_threads_to_finish() -> None:
    """Threads still draining during shutdown get a grace window before being flagged."""
    probe = threading.Thread(target=lambda: time.sleep(0.3), name="slow-finisher", daemon=False)
    probe.start()
    try:
        leaked = find_leaked_threads(grace_seconds=2.0)
        assert all(t.name != "slow-finisher" for t in leaked)
    finally:
        probe.join(timeout=5)


class _ExplodingRegistry:
    """Registry stub whose close() fails, simulating a teardown error."""

    async def close(self) -> None:
        raise RuntimeError("close failed")


async def test_lifecycle_shutdown_runs_even_if_registry_close_raises(
    tmp_path: pathlib.Path,
) -> None:
    """shutdown_all() must run even when registry.close() raises.

    This is the exact bug behind the CI 20-minute hangs: a sequential
    ``await registry.close(); await lifecycle.shutdown_all()`` teardown skips
    the lifecycle shutdown when close() raises, leaving non-daemon aiosqlite
    worker threads alive.
    """
    lifecycle = DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    await lifecycle.get_connection(tmp_path / "guard.db")
    assert lifecycle._connections, "precondition: lifecycle holds an open connection"

    with pytest.raises(RuntimeError, match="close failed"):
        await close_registry_then_lifecycle(_ExplodingRegistry(), lifecycle)

    assert lifecycle._connections == {}, "shutdown_all() must have closed all connections"


class _ExplodingLifecycle:
    """Lifecycle stub whose shutdown_all() fails, simulating a double teardown error."""

    async def shutdown_all(self) -> None:
        raise OSError("shutdown failed")


class _OkRegistry:
    """Registry stub whose close() succeeds."""

    async def close(self) -> None:
        pass


async def test_close_error_propagates_even_if_shutdown_also_fails() -> None:
    """A shutdown_all() failure must not mask the original close() error.

    The close() exception is the root cause of the teardown failure; the
    shutdown error is secondary and gets logged instead of propagated.
    """
    with pytest.raises(RuntimeError, match="close failed"):
        await close_registry_then_lifecycle(_ExplodingRegistry(), _ExplodingLifecycle())


async def test_shutdown_error_propagates_when_close_succeeds() -> None:
    """With a clean close(), a shutdown_all() failure is the primary error."""
    with pytest.raises(OSError, match="shutdown failed"):
        await close_registry_then_lifecycle(_OkRegistry(), _ExplodingLifecycle())
