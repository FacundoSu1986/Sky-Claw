"""Tests for FASE 1.5.4 — ToolStateMachine + IdempotencyGuard.

Covers:
- Valid state transitions
- Invalid transition rejection
- Idempotency key determinism (sorted keys)
- Duplicate execution rejection
- Lock release on terminal states
- Task lifecycle end-to-end
- Cleanup of terminal tasks
"""

from __future__ import annotations

import pytest

from sky_claw.orchestrator.tool_state_machine import (
    IdempotencyGuard,
    InvalidTransitionError,
    TaskRecord,
    ToolStateMachine,
)


# ------------------------------------------------------------------
# TaskRecord schema
# ------------------------------------------------------------------


class TestTaskRecord:
    """TaskRecord is a frozen Pydantic model."""

    def test_frozen(self) -> None:
        rec = TaskRecord(
            task_id="t1",
            tool_name="list_mods",
            state="PENDING",
            idempotency_key="abc123",
        )
        with pytest.raises(Exception):
            rec.task_id = "t2"  # type: ignore[misc]

    def test_strict_type_validation(self) -> None:
        """Strict mode rejects type coercion (e.g., int where str expected)."""
        with pytest.raises(Exception):
            TaskRecord(
                task_id=123,  # type: ignore[arg-type]  # str expected, int provided
                tool_name="list_mods",
                state="PENDING",
                idempotency_key="abc123",
            )

    def test_optional_fields(self) -> None:
        rec = TaskRecord(
            task_id="t1",
            tool_name="list_mods",
            state="COMPLETED",
            idempotency_key="abc123",
            result={"mods": [1, 2, 3]},
            error_message=None,
        )
        assert rec.result == {"mods": [1, 2, 3]}
        assert rec.error_message is None


# ------------------------------------------------------------------
# IdempotencyGuard
# ------------------------------------------------------------------


class TestIdempotencyGuard:
    """IdempotencyGuard with deterministic key generation."""

    def test_make_key_deterministic(self) -> None:
        """Same tool_name + same payload → same key."""
        key1 = IdempotencyGuard.make_key("list_mods", {"status": "active"})
        key2 = IdempotencyGuard.make_key("list_mods", {"status": "active"})
        assert key1 == key2

    def test_make_key_sorted_dict_order(self) -> None:
        """CRITICAL: dicts with different insertion order produce the same key.

        This is the architecture gotcha mentioned in the requirements.
        """
        key1 = IdempotencyGuard.make_key("tool", {"b": 1, "a": 2})
        key2 = IdempotencyGuard.make_key("tool", {"a": 2, "b": 1})
        assert key1 == key2, (
            "Idempotency keys must be identical for dicts with different "
            "insertion order but same content. Use json.dumps(sort_keys=True)."
        )

    def test_make_key_different_tools(self) -> None:
        """Different tool names → different keys, even with same payload."""
        key1 = IdempotencyGuard.make_key("tool_a", {"x": 1})
        key2 = IdempotencyGuard.make_key("tool_b", {"x": 1})
        assert key1 != key2

    def test_make_key_different_payloads(self) -> None:
        """Different payloads → different keys, even with same tool."""
        key1 = IdempotencyGuard.make_key("tool", {"x": 1})
        key2 = IdempotencyGuard.make_key("tool", {"x": 2})
        assert key1 != key2

    def test_acquire_success(self) -> None:
        guard = IdempotencyGuard()
        key = guard.make_key("tool", {"a": 1})
        assert guard.acquire(key, "task_1") is True
        assert guard.is_active(key)

    def test_acquire_rejects_duplicate(self) -> None:
        guard = IdempotencyGuard()
        key = guard.make_key("tool", {"a": 1})
        assert guard.acquire(key, "task_1") is True
        assert guard.acquire(key, "task_2") is False

    def test_release_allows_reacquire(self) -> None:
        guard = IdempotencyGuard()
        key = guard.make_key("tool", {"a": 1})
        guard.acquire(key, "task_1")
        guard.release(key)
        assert not guard.is_active(key)
        assert guard.acquire(key, "task_2") is True

    def test_active_count(self) -> None:
        guard = IdempotencyGuard()
        assert guard.active_count == 0
        k1 = guard.make_key("tool_a", {"x": 1})
        k2 = guard.make_key("tool_b", {"x": 1})
        guard.acquire(k1, "t1")
        guard.acquire(k2, "t2")
        assert guard.active_count == 2
        guard.release(k1)
        assert guard.active_count == 1

    def test_release_nonexistent_is_noop(self) -> None:
        guard = IdempotencyGuard()
        guard.release("nonexistent_key")  # should not raise


# ------------------------------------------------------------------
# ToolStateMachine — Transitions
# ------------------------------------------------------------------


class TestValidTransitions:
    """Valid state transitions succeed."""

    def test_pending_to_running(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("list_mods", {})
        updated = sm.transition(task.task_id, "RUNNING")
        assert updated.state == "RUNNING"

    def test_pending_to_awaiting_approval(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("execute_loot_sorting", {})
        updated = sm.transition(task.task_id, "AWAITING_APPROVAL")
        assert updated.state == "AWAITING_APPROVAL"

    def test_awaiting_approval_to_running(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        sm.transition(task.task_id, "AWAITING_APPROVAL")
        updated = sm.transition(task.task_id, "RUNNING")
        assert updated.state == "RUNNING"

    def test_awaiting_approval_to_failed(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        sm.transition(task.task_id, "AWAITING_APPROVAL")
        updated = sm.transition(task.task_id, "FAILED", error_message="Denied")
        assert updated.state == "FAILED"
        assert updated.error_message == "Denied"

    def test_running_to_completed(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        sm.transition(task.task_id, "RUNNING")
        result = {"mods": [1, 2, 3]}
        updated = sm.transition(task.task_id, "COMPLETED", result=result)
        assert updated.state == "COMPLETED"
        assert updated.result == result

    def test_running_to_failed(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        sm.transition(task.task_id, "RUNNING")
        updated = sm.transition(
            task.task_id, "FAILED", error_message="Timeout"
        )
        assert updated.state == "FAILED"
        assert updated.error_message == "Timeout"

    def test_full_lifecycle_pending_running_completed(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("list_mods", {"status": "active"})
        sm.transition(task.task_id, "RUNNING")
        final = sm.transition(
            task.task_id, "COMPLETED", result={"status": "success"}
        )
        assert final.state == "COMPLETED"
        assert final.result == {"status": "success"}

    def test_full_lifecycle_with_approval(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("execute_loot_sorting", {"profile": "Default"})
        sm.transition(task.task_id, "AWAITING_APPROVAL")
        sm.transition(task.task_id, "RUNNING")
        final = sm.transition(task.task_id, "COMPLETED", result={"sorted": True})
        assert final.state == "COMPLETED"


class TestInvalidTransitions:
    """Invalid state transitions raise InvalidTransitionError."""

    def test_pending_to_completed(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        with pytest.raises(InvalidTransitionError, match="PENDING.*COMPLETED"):
            sm.transition(task.task_id, "COMPLETED")

    def test_pending_to_failed(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        with pytest.raises(InvalidTransitionError, match="PENDING.*FAILED"):
            sm.transition(task.task_id, "FAILED")

    def test_completed_to_running(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        sm.transition(task.task_id, "RUNNING")
        sm.transition(task.task_id, "COMPLETED")
        with pytest.raises(InvalidTransitionError, match="COMPLETED.*RUNNING"):
            sm.transition(task.task_id, "RUNNING")

    def test_failed_to_running(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        sm.transition(task.task_id, "RUNNING")
        sm.transition(task.task_id, "FAILED")
        with pytest.raises(InvalidTransitionError, match="FAILED.*RUNNING"):
            sm.transition(task.task_id, "RUNNING")

    def test_running_to_pending(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        sm.transition(task.task_id, "RUNNING")
        with pytest.raises(InvalidTransitionError, match="RUNNING.*PENDING"):
            sm.transition(task.task_id, "PENDING")

    def test_nonexistent_task(self) -> None:
        sm = ToolStateMachine()
        with pytest.raises(KeyError, match="Task not found"):
            sm.transition("ghost_task", "RUNNING")


# ------------------------------------------------------------------
# ToolStateMachine — Idempotency integration
# ------------------------------------------------------------------


class TestIdempotencyIntegration:
    """IdempotencyGuard is integrated into ToolStateMachine."""

    def test_acquire_idempotency_success(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {"x": 1})
        assert sm.acquire_idempotency(task.task_id) is True

    def test_acquire_idempotency_rejects_duplicate(self) -> None:
        sm = ToolStateMachine()
        task1 = sm.create_task("tool", {"x": 1})
        sm.acquire_idempotency(task1.task_id)
        sm.transition(task1.task_id, "RUNNING")

        task2 = sm.create_task("tool", {"x": 1})
        assert sm.acquire_idempotency(task2.task_id) is False

    def test_terminal_state_releases_lock(self) -> None:
        sm = ToolStateMachine()
        task1 = sm.create_task("tool", {"x": 1})
        sm.acquire_idempotency(task1.task_id)
        sm.transition(task1.task_id, "RUNNING")
        sm.transition(task1.task_id, "COMPLETED")

        # Lock should be released, new task with same payload should succeed
        task2 = sm.create_task("tool", {"x": 1})
        assert sm.acquire_idempotency(task2.task_id) is True

    def test_failed_state_releases_lock(self) -> None:
        sm = ToolStateMachine()
        task1 = sm.create_task("tool", {"x": 1})
        sm.acquire_idempotency(task1.task_id)
        sm.transition(task1.task_id, "RUNNING")
        sm.transition(task1.task_id, "FAILED")

        task2 = sm.create_task("tool", {"x": 1})
        assert sm.acquire_idempotency(task2.task_id) is True


# ------------------------------------------------------------------
# ToolStateMachine — Task management
# ------------------------------------------------------------------


class TestTaskManagement:
    """Task retrieval and cleanup."""

    def test_get_task(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        retrieved = sm.get_task(task.task_id)
        assert retrieved is not None
        assert retrieved.task_id == task.task_id

    def test_get_nonexistent_task(self) -> None:
        sm = ToolStateMachine()
        assert sm.get_task("ghost") is None

    def test_active_task_count(self) -> None:
        sm = ToolStateMachine()
        assert sm.active_task_count == 0
        t1 = sm.create_task("tool_a", {})
        sm.transition(t1.task_id, "RUNNING")
        assert sm.active_task_count == 1
        t2 = sm.create_task("tool_b", {})
        assert sm.active_task_count == 2
        sm.transition(t1.task_id, "COMPLETED")
        assert sm.active_task_count == 1

    def test_cleanup_terminal(self) -> None:
        sm = ToolStateMachine()
        t1 = sm.create_task("tool_a", {})
        sm.transition(t1.task_id, "RUNNING")
        sm.transition(t1.task_id, "COMPLETED")

        t2 = sm.create_task("tool_b", {})
        # t2 is still PENDING (not terminal)

        # Cleanup with max_age=0 should remove completed tasks immediately
        cleaned = sm.cleanup_terminal(max_age_seconds=0)
        assert cleaned == 1
        assert sm.get_task(t1.task_id) is None
        assert sm.get_task(t2.task_id) is not None

    def test_task_record_updated_at_changes(self) -> None:
        sm = ToolStateMachine()
        task = sm.create_task("tool", {})
        original_updated = task.updated_at
        import time
        time.sleep(0.01)
        updated = sm.transition(task.task_id, "RUNNING")
        assert updated.updated_at >= original_updated


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_payload(self) -> None:
        key = IdempotencyGuard.make_key("tool", {})
        assert isinstance(key, str)
        assert len(key) == 64  # SHA-256 hex digest

    def test_nested_dict_sorted(self) -> None:
        """Nested dicts are also deterministically serialized."""
        key1 = IdempotencyGuard.make_key("tool", {"outer": {"b": 1, "a": 2}})
        key2 = IdempotencyGuard.make_key("tool", {"outer": {"a": 2, "b": 1}})
        assert key1 == key2

    def test_payload_with_list(self) -> None:
        """Lists preserve order (not sorted)."""
        key1 = IdempotencyGuard.make_key("tool", {"items": [1, 2, 3]})
        key2 = IdempotencyGuard.make_key("tool", {"items": [3, 2, 1]})
        assert key1 != key2  # Lists are NOT sorted

    def test_multiple_tasks_same_tool_different_payload(self) -> None:
        """Same tool, different payloads → different tasks, both can run."""
        sm = ToolStateMachine()
        t1 = sm.create_task("list_mods", {"status": "active"})
        t2 = sm.create_task("list_mods", {"status": "inactive"})
        assert sm.acquire_idempotency(t1.task_id) is True
        assert sm.acquire_idempotency(t2.task_id) is True

    def test_create_task_with_invalid_initial_state(self) -> None:
        sm = ToolStateMachine()
        with pytest.raises(InvalidTransitionError, match="Invalid initial state"):
            sm.create_task("tool", {}, initial_state="COMPLETED")
