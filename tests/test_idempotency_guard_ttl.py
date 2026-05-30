"""P1 R-03 — IdempotencyGuard must auto-expire stale keys after a TTL.

Original code keeps ``_active[key] = task_id`` forever once acquired. If the
task crashes between ``acquire()`` and ``release()`` (subprocess kill, OS
SIGKILL, OOM), the key becomes a permanent zombie blocking every future
identical request until the process restarts.

Fix: ``acquire`` records ``(task_id, monotonic_ts)`` and treats any entry
older than ``key_ttl_seconds`` as released. Lazy cleanup on each call keeps
the dict bounded.

Contracts:
- Acquiring twice within the TTL still returns False (existing behavior).
- After TTL elapses, a stale key is treated as released — second acquire
  succeeds and the new task_id wins.
- ``is_active`` honors the TTL.
- A guard configured with ``key_ttl_seconds=0`` disables expiration (legacy).
"""

from __future__ import annotations

import time
from unittest.mock import patch

from sky_claw.antigravity.orchestrator.tool_state_machine import IdempotencyGuard


class TestIdempotencyGuardTtl:
    def test_acquire_blocks_within_ttl(self) -> None:
        """Within TTL, a second acquire of the same key is rejected (legacy contract)."""
        guard = IdempotencyGuard(key_ttl_seconds=10.0)
        assert guard.acquire("k1", task_id="t1") is True
        assert guard.acquire("k1", task_id="t2") is False
        assert guard.is_active("k1") is True

    def test_acquire_after_ttl_expiry_succeeds(self) -> None:
        """After the TTL elapses, a stale key is treated as released."""
        guard = IdempotencyGuard(key_ttl_seconds=0.05)

        assert guard.acquire("k1", task_id="t1") is True

        # Simulate time passing past TTL.
        future = time.monotonic() + 1.0
        with patch(
            "sky_claw.antigravity.orchestrator.tool_state_machine.time.monotonic",
            return_value=future,
        ):
            # is_active must now report False — TTL elapsed.
            assert guard.is_active("k1") is False, "is_active must honor TTL — stale entries are logically released"
            # And a new acquire wins.
            assert guard.acquire("k1", task_id="t2") is True

    def test_zero_ttl_disables_expiration(self) -> None:
        """key_ttl_seconds=0 preserves the original eternal-lock behavior."""
        guard = IdempotencyGuard(key_ttl_seconds=0)
        assert guard.acquire("k1", task_id="t1") is True

        future = time.monotonic() + 1_000_000
        with patch(
            "sky_claw.antigravity.orchestrator.tool_state_machine.time.monotonic",
            return_value=future,
        ):
            assert guard.is_active("k1") is True
            assert guard.acquire("k1", task_id="t2") is False

    def test_release_works_independently_of_ttl(self) -> None:
        """Explicit release before TTL expiry still frees the slot."""
        guard = IdempotencyGuard(key_ttl_seconds=10.0)
        guard.acquire("k1", task_id="t1")
        guard.release("k1")
        assert guard.is_active("k1") is False
        assert guard.acquire("k1", task_id="t2") is True

    def test_active_count_excludes_expired(self) -> None:
        """active_count must reflect logical occupancy, not raw dict size."""
        guard = IdempotencyGuard(key_ttl_seconds=0.05)
        guard.acquire("k1", task_id="t1")
        guard.acquire("k2", task_id="t2")
        assert guard.active_count == 2

        future = time.monotonic() + 1.0
        with patch(
            "sky_claw.antigravity.orchestrator.tool_state_machine.time.monotonic",
            return_value=future,
        ):
            assert guard.active_count == 0
