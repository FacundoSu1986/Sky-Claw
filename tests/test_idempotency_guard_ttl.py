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

    def test_active_count_purges_expired_to_bound_dict(self) -> None:
        """Copilot review on PR #139: active_count must purge expired entries.

        Without purging, a workload that churns through millions of unique
        keys (e.g. per-request idempotency in a high-traffic service) leaks
        memory even when ``active_count`` returns 0.
        """
        guard = IdempotencyGuard(key_ttl_seconds=0.05)
        for i in range(100):
            guard.acquire(f"k{i}", task_id=f"t{i}")
        assert len(guard._active) == 100  # internal sanity check

        future = time.monotonic() + 1.0
        with patch(
            "sky_claw.antigravity.orchestrator.tool_state_machine.time.monotonic",
            return_value=future,
        ):
            assert guard.active_count == 0
            # Side effect: dict pruned, not just filtered.
            assert len(guard._active) == 0, "active_count must purge expired entries so _active stays bounded"

    def test_release_with_task_id_only_clears_matching_owner(self) -> None:
        """Codex P2 on PR #139: stale release must not clear a newer task's lock.

        Scenario:
          1. Task A acquires "k" at t=0.
          2. TTL elapses; task B reclaims "k".
          3. Task A finishes and calls release("k", task_id="A").
          4. Task B's lock MUST survive — otherwise a third concurrent
             execution can start while B is still running.
        """
        guard = IdempotencyGuard(key_ttl_seconds=0.05)
        guard.acquire("k", task_id="A")

        future = time.monotonic() + 1.0
        with patch(
            "sky_claw.antigravity.orchestrator.tool_state_machine.time.monotonic",
            return_value=future,
        ):
            # B reclaims the stale key.
            assert guard.acquire("k", task_id="B") is True

            # A finishes — must NOT clear B's lock.
            guard.release("k", task_id="A")
            assert guard.is_active("k") is True, "release() called by stale task A must not clear newer owner B's lock"

            # B's own release must work.
            guard.release("k", task_id="B")
            assert guard.is_active("k") is False

    def test_release_without_task_id_is_unconditional_for_legacy_callers(self) -> None:
        """Backward compat: callers that don't pass task_id pop unconditionally."""
        guard = IdempotencyGuard(key_ttl_seconds=10.0)
        guard.acquire("k", task_id="A")
        guard.release("k")  # legacy signature
        assert guard.is_active("k") is False
