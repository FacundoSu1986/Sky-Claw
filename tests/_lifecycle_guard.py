"""Fail-fast helpers for test-session lifecycle bugs.

Targets the documented CI failure mode: aiosqlite worker threads are
non-daemon, so any connection left open after the session blocks interpreter
exit and burns the full 20-minute CI job timeout. These helpers make that
failure immediate and attributable instead of a silent hang.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sky_claw.antigravity.core.db_lifecycle import DatabaseLifecycleManager


class _SupportsAsyncClose(Protocol):
    async def close(self) -> None: ...


def find_leaked_threads(grace_seconds: float = 0.0) -> list[threading.Thread]:
    """Return alive non-daemon threads other than the main thread.

    Threads that are still draining get up to ``grace_seconds`` to finish
    before being flagged, so a shutdown in progress is not a false positive.
    """
    deadline = time.monotonic() + grace_seconds
    while True:
        leaked = [
            t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon and t.is_alive()
        ]
        if not leaked or time.monotonic() >= deadline:
            return leaked
        time.sleep(0.1)


async def close_registry_then_lifecycle(
    registry: _SupportsAsyncClose,
    lifecycle: DatabaseLifecycleManager,
) -> None:
    """Teardown that guarantees ``lifecycle.shutdown_all()`` runs.

    A sequential ``await registry.close(); await lifecycle.shutdown_all()``
    skips the lifecycle shutdown when ``close()`` raises, leaving non-daemon
    aiosqlite worker threads alive. The nested ``finally`` closes them no
    matter what; the original exception still propagates.
    """
    try:
        await registry.close()
    finally:
        await lifecycle.shutdown_all()
