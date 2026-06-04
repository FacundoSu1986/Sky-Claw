"""P1: AsyncModRegistry shares ONE aiosqlite connection across all coroutines.

SyncEngine fans writes out across an ``asyncio.TaskGroup`` (up to 15 tasks),
each doing ``execute``/``commit`` (and batch paths doing ``rollback`` on error)
on that single connection. Because the logical transaction is not atomic across
``await`` points, one writer's ``commit``/``rollback`` can land in the middle of
another writer's transaction — committing partial state or discarding another
task's uncommitted rows (silent data loss). WAL prevents file corruption, not
this logical loss.

Writes must be serialized (an ``asyncio.Lock`` around execute+commit).
"""

from __future__ import annotations

import asyncio


async def test_concurrent_writes_are_serialized(async_registry, monkeypatch):
    reg = async_registry

    in_flight = 0
    max_in_flight = 0
    real_commit = reg._conn.commit

    async def tracking_commit():
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.01)  # widen the write critical-section window
            return await real_commit()
        finally:
            in_flight -= 1

    monkeypatch.setattr(reg._conn, "commit", tracking_commit)

    await asyncio.gather(*(reg.upsert_mod(nexus_id=i, name=f"mod{i}") for i in range(12)))

    ids = await reg.get_all_nexus_ids()
    assert ids == set(range(12))
    # The shared connection must never have two writers committing at once.
    assert max_in_flight == 1, f"writes overlapped on the shared connection (max={max_in_flight})"
