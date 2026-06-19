"""Regression tests for DLQ schema init, incl. under an owner-restricted parent.

These pin the exact stack that failed in the packaged exe:

    SupervisorAgent.start()
      → CoreEventBus.start()
        → DLQManager.start()
          → DLQManager._ensure_schema()
            → DatabaseLifecycleManager.get_connection()
              → aiosqlite.connect()  →  OperationalError: unable to open database file

The DLQ's own path/parent handling was never the bug (it ``mkdir(parents=True)``
the same dir it connects to — see ``test_schema_bootstraps_on_first_use``).  The
failure was *downstream* of ``restrict_to_owner(~/.sky_claw)``: a non-inheritable
owner grant on the shared salt dir left freshly-created children (``dlq/``) with
no writable ACE.  The Windows test below is the end-to-end guard for that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import aiosqlite
import pytest

from sky_claw.antigravity.core.db_lifecycle import DatabaseLifecycleManager
from sky_claw.antigravity.core.dlq_manager import DLQManager
from sky_claw.antigravity.core.event_bus import create_bus_with_dlq


@pytest.mark.asyncio
async def test_event_bus_start_initializes_dlq_schema_with_lifecycle(tmp_path: Path) -> None:
    """The full supervisor start path creates the DLQ schema with no OperationalError."""
    db_path = tmp_path / "dlq" / "dlq.db"
    lifecycle = DatabaseLifecycleManager()
    bus = create_bus_with_dlq(db_path=db_path, lifecycle=lifecycle)

    try:
        await bus.start()  # event_bus.start → dlq.start → _ensure_schema → connect
        assert db_path.exists(), "DLQ schema init must create the db file"
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dead_letter_events'") as cur,
        ):
            assert await cur.fetchone() is not None
    finally:
        await bus.stop()
        await lifecycle.shutdown_all()


@pytest.mark.skipif(sys.platform != "win32", reason="ACL inheritance is Windows-only")
@pytest.mark.asyncio
async def test_dlq_schema_init_under_owner_restricted_parent(tmp_path: Path) -> None:
    """DLQ db is creatable under a ``restrict_to_owner``-hardened parent dir.

    Mirrors ``~/.sky_claw`` (restricted) → ``~/.sky_claw/dlq/dlq.db``.  Before the
    inheritable-grant fix the ``dlq`` child inherited only a non-writable ACE and
    this raised ``OperationalError: unable to open database file``.
    """
    from sky_claw.antigravity.security.file_permissions import restrict_to_owner

    sky_claw_dir = tmp_path / ".sky_claw"
    sky_claw_dir.mkdir()
    restrict_to_owner(sky_claw_dir)  # the salt-dir hardening that broke children

    db_path = sky_claw_dir / "dlq" / "dlq.db"
    lifecycle = DatabaseLifecycleManager()
    dlq = DLQManager(db_path=db_path, handler_resolver={}.get, lifecycle=lifecycle)

    try:
        await dlq.start()  # _ensure_schema → connect; must not raise
        assert db_path.exists()
    finally:
        await dlq.stop()
        await lifecycle.shutdown_all()
