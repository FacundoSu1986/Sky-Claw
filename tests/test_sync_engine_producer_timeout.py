"""P0.7 R-01 — POISON delivery timeout in _produce_then_poison.

Without asyncio.wait_for, the producer's finally block hangs forever when
all workers die before draining the queue:

  1. Producer fills the queue (maxsize=N).
  2. All N workers raise immediately — queue never drains.
  3. Producer's finally does ``await queue.put(_POISON)`` for each worker.
  4. Queue is full, no consumer → put blocks indefinitely → deadlock.

The fix: wrap each put in asyncio.wait_for(_POISON_DELIVERY_TIMEOUT).
On timeout log CRITICAL and raise — TaskGroup cancels all remaining workers.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from sky_claw.antigravity.orchestrator.sync_engine import SyncConfig, SyncEngine


def _make_engine(worker_count: int = 2, queue_maxsize: int = 2) -> SyncEngine:
    cfg = SyncConfig(worker_count=worker_count, queue_maxsize=queue_maxsize)
    return SyncEngine(
        mo2=MagicMock(),
        masterlist=MagicMock(),
        registry=MagicMock(),
        config=cfg,
        fetch_retry_wait=None,
    )


@pytest.mark.asyncio
async def test_poison_delivery_doesnt_deadlock_when_workers_die() -> None:
    """run() must complete even when all workers die without draining the queue.

    Without asyncio.wait_for the finally block would hang forever; this test
    would time out at ~30s without the fix. With the fix it completes in < 1s
    (poison timeout patched to 0.05s) because TimeoutError propagates and
    TaskGroup cancels all remaining workers.
    """
    engine = _make_engine(worker_count=2, queue_maxsize=2)
    session = MagicMock()

    async def _filling_produce(queue: asyncio.Queue, profile: str) -> None:
        """Fill the queue to capacity, then fail — no consumer will drain it."""
        await queue.put([("mod-a", True)])
        await queue.put([("mod-b", True)])  # queue now full (maxsize=2)
        raise RuntimeError("producer failed")

    async def _dying_consume(
        queue,
        session,
        semaphore,
        result,
        *,
        enrich_remote: bool = True,
    ) -> None:  # noqa: ANN001
        """Worker que falla sin drenar la cola."""
        assert enrich_remote is True
        raise RuntimeError("worker died")

    engine._produce = _filling_produce  # type: ignore[method-assign]
    engine._consume = _dying_consume  # type: ignore[method-assign]

    # Patch timeout to 50ms so the test runs fast.
    with patch("sky_claw.antigravity.orchestrator.sync_engine._POISON_DELIVERY_TIMEOUT", 0.05):
        try:
            await asyncio.wait_for(engine.run(session, profile="Default"), timeout=3.0)
        except TimeoutError:
            pytest.fail(
                "engine.run() hung — POISON delivery is blocking indefinitely "
                "(missing asyncio.wait_for in _produce_then_poison)"
            )
        except BaseException:
            pass  # expected: ExceptionGroup from workers + producer failing


@pytest.mark.asyncio
async def test_poison_delivery_succeeds_when_queue_has_space() -> None:
    """When the queue has space, POISON pills are delivered normally."""
    engine = _make_engine(worker_count=1, queue_maxsize=10)
    session = MagicMock()

    poison_received: list[object] = []

    async def _empty_produce(queue: asyncio.Queue, profile: str) -> None:
        pass  # produce nothing

    async def _draining_consume(
        queue,
        session,
        semaphore,
        result,
        *,
        enrich_remote: bool = True,
    ) -> None:  # noqa: ANN001
        assert enrich_remote is True
        item = await queue.get()
        poison_received.append(item)  # should be None (_POISON)

    engine._produce = _empty_produce  # type: ignore[method-assign]
    engine._consume = _draining_consume  # type: ignore[method-assign]

    await asyncio.wait_for(engine.run(session, profile="Default"), timeout=3.0)
    assert poison_received == [None], "Worker must receive exactly one POISON pill"
