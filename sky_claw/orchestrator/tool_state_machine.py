"""ToolStateMachine + IdempotencyGuard — FASE 1.5.4.

Manages per-tool execution lifecycle with strict state transitions and
idempotency protection to prevent duplicate concurrent executions.

State machine::

    PENDING ──► RUNNING ──► COMPLETED
      │              │
      │              ▼
      │           FAILED
      │
      ▼
    AWAITING_APPROVAL ──► RUNNING
                    │
                    ▼
                 FAILED

Design invariants:
- All transitions are validated; invalid transitions raise ``InvalidTransitionError``.
- ``IdempotencyGuard`` uses deterministic hashing with sorted JSON keys to
  ensure identical payloads produce the same idempotency key regardless of
  dict insertion order.
- ``TaskRecord`` is a frozen Pydantic model (strict=True).
- Thread-safe for asyncio (single event loop).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("SkyClaw.ToolStateMachine")

# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

TaskState = Literal[
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "AWAITING_APPROVAL",
]

# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"RUNNING", "AWAITING_APPROVAL"},
    "AWAITING_APPROVAL": {"RUNNING", "FAILED"},
    "RUNNING": {"COMPLETED", "FAILED"},
    "COMPLETED": set(),  # terminal
    "FAILED": set(),      # terminal
}

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TaskRecord(BaseModel):
    """Immutable snapshot of a tool execution task."""

    model_config = ConfigDict(strict=True, frozen=True)

    task_id: str
    tool_name: str
    state: str  # TaskState values
    idempotency_key: str
    created_at: float = Field(default_factory=time.monotonic)
    updated_at: float = Field(default_factory=time.monotonic)
    error_message: str | None = None
    result: dict[str, Any] | None = None


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed."""


# ---------------------------------------------------------------------------
# IdempotencyGuard
# ---------------------------------------------------------------------------


class IdempotencyGuard:
    """Prevents duplicate concurrent executions of the same tool+payload.

    The idempotency key is derived from ``sha256(tool_name + sorted_payload_json)``.
    Sorting keys alphabetically before hashing ensures that semantically
    identical payloads with different insertion orders produce the same key.

    Usage::

        guard = IdempotencyGuard()
        key = guard.make_key("list_mods", {"status": "active"})
        if not guard.acquire(key, task_id="t1"):
            raise DuplicateExecutionError(...)
        # ... execute ...
        guard.release(key)
    """

    def __init__(self) -> None:
        self._active: dict[str, str] = {}  # idempotency_key → task_id

    @staticmethod
    def make_key(tool_name: str, payload: dict[str, Any]) -> str:
        """Generate a deterministic idempotency key.

        CRITICAL: ``json.dumps(payload, sort_keys=True)`` ensures that
        ``{"b": 1, "a": 2}`` and ``{"a": 2, "b": 1}`` produce the same hash.
        """
        canonical = json.dumps(payload, sort_keys=True, default=str)
        raw = f"{tool_name}:{canonical}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def acquire(self, key: str, task_id: str) -> bool:
        """Try to acquire the idempotency lock.

        Returns:
            True if the lock was acquired (no active task with this key).
            False if a task with this key is already active.
        """
        if key in self._active:
            existing = self._active[key]
            logger.warning(
                "IdempotencyGuard: rejected duplicate execution "
                "(key=%s..., existing_task=%s, new_task=%s)",
                key[:12],
                existing,
                task_id,
            )
            return False
        self._active[key] = task_id
        return True

    def release(self, key: str) -> None:
        """Release the idempotency lock after task completion."""
        self._active.pop(key, None)

    @property
    def active_count(self) -> int:
        """Number of currently active (locked) idempotency keys."""
        return len(self._active)

    def is_active(self, key: str) -> bool:
        """Check if a key is currently locked."""
        return key in self._active


# ---------------------------------------------------------------------------
# ToolStateMachine
# ---------------------------------------------------------------------------


class ToolStateMachine:
    """Manages lifecycle of tool execution tasks with strict transitions.

    Usage::

        sm = ToolStateMachine()
        record = sm.create_task("list_mods", {"status": "active"})
        sm.transition(record.task_id, "RUNNING")
        sm.transition(record.task_id, "COMPLETED", result={"mods": [...]})
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._guard = IdempotencyGuard()
        self._task_counter: int = 0

    def create_task(
        self,
        tool_name: str,
        payload: dict[str, Any],
        *,
        initial_state: str = "PENDING",
    ) -> TaskRecord:
        """Create a new task record with idempotency protection.

        Args:
            tool_name: Name of the tool to execute.
            payload: Tool arguments (used for idempotency key generation).
            initial_state: Starting state (default: PENDING).

        Returns:
            Immutable TaskRecord for the new task.

        Raises:
            InvalidTransitionError: If initial_state is not a valid state.
        """
        _VALID_INITIAL_STATES = {"PENDING", "AWAITING_APPROVAL"}
        if initial_state not in _VALID_INITIAL_STATES:
            raise InvalidTransitionError(
                f"Invalid initial state: {initial_state}. "
                f"Allowed: {_VALID_INITIAL_STATES}"
            )

        self._task_counter += 1
        task_id = f"task_{self._task_counter}"
        idempotency_key = self._guard.make_key(tool_name, payload)

        now = time.monotonic()
        record = TaskRecord(
            task_id=task_id,
            tool_name=tool_name,
            state=initial_state,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task_id] = record
        return record

    def transition(
        self,
        task_id: str,
        new_state: str,
        *,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> TaskRecord:
        """Transition a task to a new state.

        Args:
            task_id: ID of the task to transition.
            new_state: Target state.
            result: Optional result dict (for COMPLETED).
            error_message: Optional error message (for FAILED).

        Returns:
            Updated TaskRecord.

        Raises:
            KeyError: If task_id is not found.
            InvalidTransitionError: If the transition is not allowed.
        """
        current = self._tasks.get(task_id)
        if current is None:
            raise KeyError(f"Task not found: {task_id}")

        allowed = _VALID_TRANSITIONS.get(current.state, set())
        if new_state not in allowed:
            raise InvalidTransitionError(
                f"Invalid transition: {current.state} → {new_state} "
                f"(task={task_id}, tool={current.tool_name})"
            )

        # Release idempotency lock on terminal states
        if new_state in ("COMPLETED", "FAILED"):
            self._guard.release(current.idempotency_key)

        updated = TaskRecord(
            task_id=task_id,
            tool_name=current.tool_name,
            state=new_state,
            idempotency_key=current.idempotency_key,
            created_at=current.created_at,
            updated_at=time.monotonic(),
            error_message=error_message,
            result=result,
        )
        self._tasks[task_id] = updated

        logger.info(
            "ToolStateMachine: %s %s → %s (tool=%s)",
            task_id,
            current.state,
            new_state,
            current.tool_name,
        )
        return updated

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Retrieve a task record by ID."""
        return self._tasks.get(task_id)

    @property
    def guard(self) -> IdempotencyGuard:
        """Access the underlying IdempotencyGuard."""
        return self._guard

    @property
    def active_task_count(self) -> int:
        """Number of non-terminal tasks."""
        return sum(
            1 for t in self._tasks.values()
            if t.state not in ("COMPLETED", "FAILED")
        )

    def acquire_idempotency(self, task_id: str) -> bool:
        """Acquire the idempotency lock for a task.

        Must be called before transitioning to RUNNING.
        Returns False if a duplicate execution is detected.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        return self._guard.acquire(task.idempotency_key, task_id)

    def cleanup_terminal(self, max_age_seconds: float = 3600.0) -> int:
        """Remove completed/failed tasks older than max_age_seconds.

        Returns:
            Number of tasks cleaned up.
        """
        now = time.monotonic()
        to_remove = [
            tid
            for tid, task in self._tasks.items()
            if task.state in ("COMPLETED", "FAILED")
            and (now - task.updated_at) > max_age_seconds
        ]
        for tid in to_remove:
            del self._tasks[tid]
        return len(to_remove)
