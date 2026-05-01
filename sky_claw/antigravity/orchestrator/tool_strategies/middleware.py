"""Cross-cutting middleware for OrchestrationToolDispatcher.

Today there are FIVE middlewares:

1. **ErrorWrappingMiddleware** — catches uncaught exceptions → error dict.
2. **DictResultGuardMiddleware** — verifies inner chain returned a dict.
3. **HitlGateMiddleware** (FASE 1.5.1) — requires human approval before
   executing destructive tools.
4. **IdempotencyMiddleware** (FASE 1.5.4) — rejects duplicate concurrent
   executions of the same tool+payload via IdempotencyGuard.
5. **ProgressMiddleware** (FASE 1.5.4) — publishes granular tool lifecycle
   events (started/completed/failed) to CoreEventBus.

Note on HITL: only ONE branch (execute_loot_sorting) currently uses HITL
internally. HitlGateMiddleware is a *generic* gate that can be applied to
ANY destructive tool strategy at the dispatcher level, providing a
consistent approval layer regardless of the strategy's internal logic.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sky_claw.antigravity.orchestrator.tool_strategies.base import NextCall, ToolStrategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FASE 1.5.1: Tools that require mandatory human approval before execution.
# ---------------------------------------------------------------------------
DESTRUCTIVE_TOOL_PATTERNS: frozenset[str] = frozenset(
    {
        "execute_loot_sorting",
        "generate_bashed_patch",
        "generate_lods",
        "resolve_conflict_patch",
    }
)


class ErrorWrappingMiddleware:
    """Catches uncaught Exception from the inner chain and returns the legacy
    {"status": "error", "reason": <reason_code>, "details": <str(exc)>} dict.

    Intentionally catches `Exception` only — not `BaseException`. This
    preserves the standard escape hatches (KeyboardInterrupt, SystemExit,
    asyncio.CancelledError) so cancellation and shutdown signals propagate.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code

    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]:
        try:
            return await next_call()
        except Exception as exc:
            logger.exception(
                "RCA: Falló %s; se convierte la excepción a error dict.",
                strategy.name,
            )
            return {
                "status": "error",
                "reason": self.reason_code,
                "details": str(exc),
            }


class DictResultGuardMiddleware:
    """Verifies the inner chain returned a `dict`. Otherwise returns the legacy
    {"status": "error", "reason": <reason_code>} dict.

    Mirrors the `isinstance(result, dict)` guard at supervisor.py:281-289 and
    310-318. Place this INSIDE ErrorWrappingMiddleware (so wrapping catches
    its own logic exceptions) or alongside it as a sibling — outermost in
    either case wins for the final result shape.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code

    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]:
        result = await next_call()
        if not isinstance(result, dict):
            logger.error(
                "RCA: %s devolvió un tipo inválido: %s",
                strategy.name,
                type(result).__name__,
            )
            return {"status": "error", "reason": self.reason_code}
        return result


# ---------------------------------------------------------------------------
# FASE 1.5.1: HITL Gate Middleware
# ---------------------------------------------------------------------------


class HitlGateMiddleware:
    """Requires human approval before executing destructive tools.

    This middleware intercepts tool execution and emits an approval request
    via the provided ``notify_fn`` (typically a WebSocket event to the
    frontend). Execution is blocked until the human approves, denies, or
    the timeout expires.

    Args:
        notify_fn: Async callable that sends the approval request to the
            operator. Receives a dict with ``tool_name``, ``reason``, and
            ``timeout`` keys.
        timeout: Seconds to wait for operator response before auto-denying.
        destructive_tools: Set of tool names that require approval. Defaults
            to ``DESTRUCTIVE_TOOL_PATTERNS``.
    """

    def __init__(
        self,
        notify_fn: Any | None = None,
        timeout: float = 120.0,
        destructive_tools: frozenset[str] | None = None,
    ) -> None:
        self._notify_fn = notify_fn
        self._timeout = timeout
        self._destructive_tools = destructive_tools or DESTRUCTIVE_TOOL_PATTERNS
        self._pending: dict[str, asyncio.Event] = {}
        self._decisions: dict[str, bool] = {}

    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]:
        """Check if tool requires approval; if so, wait for human decision."""
        if strategy.name not in self._destructive_tools:
            return await next_call()

        logger.info(
            "HitlGateMiddleware: tool '%s' requires human approval.",
            strategy.name,
        )

        # If no notify_fn is configured, log warning and proceed (fail-open
        # for backward compatibility during migration).
        if self._notify_fn is None:
            logger.warning(
                "HitlGateMiddleware: no notify_fn configured — "
                "proceeding without human approval for '%s'. "
                "Configure notify_fn for production use.",
                strategy.name,
            )
            return await next_call()

        # Send approval request — usar request_id único para evitar colisiones cuando
        # dos invocaciones concurrentes de la MISMA tool destructiva con payloads
        # distintos llegan al gate (FASE 1.5.4 hardening: HITL key collision fix).
        request_id = uuid.uuid4().hex
        request_event = asyncio.Event()
        self._pending[request_id] = request_event

        await self._notify_fn(
            {
                "request_id": request_id,
                "tool_name": strategy.name,
                "reason": f"Tool '{strategy.name}' requires human approval before execution.",
                "timeout": self._timeout,
            }
        )

        # Wait for decision or timeout
        try:
            await asyncio.wait_for(request_event.wait(), timeout=self._timeout)
        except TimeoutError:
            logger.warning(
                "HitlGateMiddleware: approval timed out for '%s' (request_id=%s) — auto-denying.",
                strategy.name,
                request_id,
            )
            return {
                "status": "error",
                "reason": "HITLApprovalTimeout",
                "details": f"Human approval timed out for '{strategy.name}' after {self._timeout}s.",
            }
        finally:
            self._pending.pop(request_id, None)

        # Check decision
        approved = self._decisions.pop(request_id, False)
        if not approved:
            logger.info(
                "HitlGateMiddleware: tool '%s' DENIED by human operator.",
                strategy.name,
            )
            return {
                "status": "error",
                "reason": "HITLApprovalDenied",
                "details": f"Human operator denied execution of '{strategy.name}'.",
            }

        logger.info(
            "HitlGateMiddleware: tool '%s' APPROVED by human operator.",
            strategy.name,
        )
        return await next_call()

    def resolve(self, request_id: str, approved: bool) -> None:
        """Called by the HITL handler when the human makes a decision.

        Args:
            request_id: Unique ID of the pending request (received in
                ``notify_fn`` payload as ``"request_id"``). Distinguishes
                concurrent invocations of the same destructive tool.
            approved: True if the human approved, False if denied.
        """
        self._decisions[request_id] = approved
        event = self._pending.get(request_id)
        if event:
            event.set()


# ---------------------------------------------------------------------------
# FASE 1.5.4: Idempotency + Progress Middlewares
# ---------------------------------------------------------------------------


class IdempotencyMiddleware:
    """Rejects duplicate concurrent executions of the same tool+payload.

    Uses ``ToolStateMachine`` and its ``IdempotencyGuard`` to ensure that
    two concurrent calls with identical tool_name + payload are rejected.

    The idempotency key is derived from ``sha256(tool_name + sorted_payload)``,
    so ``{"b": 1, "a": 2}`` and ``{"a": 2, "b": 1}`` produce the same key.

    Args:
        state_machine: Shared ``ToolStateMachine`` instance.
    """

    def __init__(self, state_machine: Any) -> None:
        self._sm = state_machine

    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]:
        # Pre-check idempotency BEFORE creating a task record.
        # This avoids creating a PENDING task that can't transition to FAILED.
        key = self._sm.guard.make_key(strategy.name, payload_dict)
        if self._sm.guard.is_active(key):
            return {
                "status": "error",
                "reason": "DuplicateExecution",
                "details": (
                    f"Tool '{strategy.name}' is already executing with the same "
                    f"arguments. Wait for the current execution to finish."
                ),
            }

        task = self._sm.create_task(strategy.name, payload_dict)
        self._sm.acquire_idempotency(task.task_id)
        self._sm.transition(task.task_id, "RUNNING")
        try:
            result = await next_call()
        except Exception as exc:
            self._sm.transition(
                task.task_id,
                "FAILED",
                error_message=str(exc),
            )
            raise

        self._sm.transition(task.task_id, "COMPLETED", result=result)
        return result


class ProgressMiddleware:
    """Publishes granular tool lifecycle events to CoreEventBus.

    Emits events with topics:
    - ``ops.tool.started``    — before strategy execution
    - ``ops.tool.completed``  — after successful execution
    - ``ops.tool.failed``     — after failed execution

    Args:
        event_bus: ``CoreEventBus`` instance for publishing events.
            If None, the middleware is a no-op pass-through (safe default
            for environments without an event bus).
    """

    def __init__(self, event_bus: Any | None = None) -> None:
        self._bus = event_bus

    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]:
        if self._bus is None:
            return await next_call()

        # Emit started event
        await self._publish(
            "ops.tool.started",
            {
                "tool": strategy.name,
                "payload_keys": list(payload_dict.keys()),
            },
        )

        try:
            result = await next_call()
        except Exception as exc:
            # Emit failed event
            await self._publish(
                "ops.tool.failed",
                {
                    "tool": strategy.name,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise

        # Emit completed event
        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
        await self._publish(
            "ops.tool.completed",
            {
                "tool": strategy.name,
                "status": status,
            },
        )

        return result

    async def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Safely publish an event, swallowing errors to avoid disrupting tool execution."""
        try:
            from sky_claw.antigravity.core.event_bus import Event

            await self._bus.publish(Event(topic=topic, payload=payload, source="ProgressMiddleware"))
        except Exception:
            logger.debug(
                "ProgressMiddleware: failed to publish %s (bus may not be started)",
                topic,
                exc_info=True,
            )


__all__ = [
    "DESTRUCTIVE_TOOL_PATTERNS",
    "DictResultGuardMiddleware",
    "ErrorWrappingMiddleware",
    "HitlGateMiddleware",
    "IdempotencyMiddleware",
    "ProgressMiddleware",
]
