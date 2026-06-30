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

Note on HITL: HitlGateMiddleware is the SINGLE approval point for
destructive tools (PR #173 review: the strategy-internal gateway HITL was
removed to avoid double-gating). Strategies may opt into pre-prompt payload
validation (``validate_for_approval``) and operator-facing summaries
(``describe_for_approval``) via the optional protocols in ``base.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sky_claw.antigravity.orchestrator.tool_strategies.base import (
    ApprovalPayloadDescriber,
    ApprovalPayloadValidator,
    NextCall,
    ToolStrategy,
)
from sky_claw.antigravity.security.hitl import Decision, HITLGuard

logger = logging.getLogger(__name__)

_MAX_APPROVAL_DETAIL_LENGTH = 800
_MAX_APPROVAL_VALUE_LENGTH = 120
_SENSITIVE_KEY_PARTS = frozenset({"api_key", "auth", "credential", "password", "secret", "token"})

# ---------------------------------------------------------------------------
# FASE 1.5.1: Tools that require mandatory human approval before execution.
# ---------------------------------------------------------------------------
DESTRUCTIVE_TOOL_PATTERNS: frozenset[str] = frozenset(
    {
        "execute_loot_sorting",
        "generate_bashed_patch",
        "generate_lods",
        # Follow-up A: Pandora reescribe los grafos de comportamiento del juego.
        "generate_animations",
        # Follow-up B: QuickAutoClean reescribe los plugins oficiales en disco.
        "quick_auto_clean",
        # Nombre real de la strategy (resolve_conflict_patch.py define
        # name="resolve_conflict_with_patch"); el alias viejo sin "_with"
        # nunca matcheó y dejaba la tool de xEdit SIN gate HITL.
        "resolve_conflict_with_patch",
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

    Backed by :class:`~sky_claw.antigravity.security.hitl.HITLGuard` — the
    project's single approval backbone. The guard delivers the prompt to
    the operator (Telegram inline buttons / ``/approve <id>`` commands)
    and this middleware blocks until the human approves, denies, or the
    guard's timeout expires (fail-secure auto-deny).

    Fail-closed: without a guard, destructive tools are DENIED with
    ``HITLGateUnavailable``. The only bypass is ``allow_unattended=True``
    (explicit opt-in for tests / headless automation; CRITICAL-logged).

    Before prompting, the gate runs the strategy's ``validate_for_approval``
    (if implemented) so malformed payloads fail without bothering the
    operator, and builds the prompt detail from ``describe_for_approval``
    or a redacted/truncated key=value dump of the payload.

    Args:
        hitl_guard: Shared ``HITLGuard`` used to request operator approval.
            Requests are tagged ``category="tool_execution"`` so notify
            closures can distinguish them from download/scope approvals.
        destructive_tools: Set of tool names that require approval. Defaults
            to ``DESTRUCTIVE_TOOL_PATTERNS``.
        allow_unattended: When True and no guard is configured, destructive
            tools proceed without approval (CRITICAL-logged). Never enable
            in production.
    """

    def __init__(
        self,
        hitl_guard: HITLGuard | None = None,
        *,
        destructive_tools: frozenset[str] | None = None,
        allow_unattended: bool = False,
    ) -> None:
        self._guard = hitl_guard
        self._destructive_tools = DESTRUCTIVE_TOOL_PATTERNS if destructive_tools is None else destructive_tools
        self._allow_unattended = allow_unattended

    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]:
        """Check if tool requires approval; if so, wait for the human decision."""
        if strategy.name not in self._destructive_tools:
            return await next_call()

        if self._guard is None:
            if self._allow_unattended:
                logger.critical(
                    "HitlGateMiddleware: allow_unattended=True — executing "
                    "destructive tool '%s' WITHOUT human approval.",
                    strategy.name,
                )
                return await next_call()
            logger.critical(
                "HitlGateMiddleware: no HITLGuard configured — DENYING "
                "destructive tool '%s' (fail-closed). Inject AppContext.hitl "
                "to enable operator approval.",
                strategy.name,
            )
            return {
                "status": "error",
                "reason": "HITLGateUnavailable",
                "details": (
                    f"No HITL guard configured; destructive tool '{strategy.name}' denied by fail-closed policy."
                ),
            }

        # request_id único para evitar colisiones cuando dos invocaciones
        # concurrentes de la MISMA tool destructiva con payloads distintos
        # llegan al gate (FASE 1.5.4 hardening: HITL key collision fix).
        _validate_for_approval(strategy, payload_dict)
        detail = _describe_for_approval(strategy, payload_dict)

        request_id = f"tool-{strategy.name}-{uuid.uuid4().hex[:12]}"
        logger.info(
            "HitlGateMiddleware: tool '%s' requires human approval (request_id=%s).",
            strategy.name,
            request_id,
        )

        decision = await self._guard.request_approval(
            request_id=request_id,
            reason=f"Tool '{strategy.name}' requires human approval before execution.",
            detail=detail,
            category="tool_execution",
        )

        if decision is not Decision.APPROVED:
            logger.warning(
                "HitlGateMiddleware: tool '%s' NOT approved (decision=%s, request_id=%s).",
                strategy.name,
                decision.value,
                request_id,
            )
            return {
                "status": "error",
                "reason": "HITLApprovalDenied",
                "details": (
                    f"Human approval for '{strategy.name}' resolved as '{decision.value}' (request_id={request_id})."
                ),
            }

        logger.info(
            "HitlGateMiddleware: tool '%s' APPROVED by human operator.",
            strategy.name,
        )
        return await next_call()


def _validate_for_approval(strategy: ToolStrategy, payload_dict: dict[str, Any]) -> None:
    if isinstance(strategy, ApprovalPayloadValidator):
        strategy.validate_for_approval(payload_dict)


def _describe_for_approval(strategy: ToolStrategy, payload_dict: dict[str, Any]) -> str:
    if isinstance(strategy, ApprovalPayloadDescriber):
        return _truncate_approval_detail(strategy.describe_for_approval(payload_dict))
    return _format_payload_values(payload_dict)


def _format_payload_values(payload_dict: dict[str, Any]) -> str:
    if not payload_dict:
        return "payload: <empty>"
    parts = [f"{key}={_format_payload_value(key, value)}" for key, value in sorted(payload_dict.items())]
    return _truncate_approval_detail("payload: " + ", ".join(parts))


def _format_payload_value(key: str, value: Any) -> str:
    if _is_sensitive_key(key):
        return "<redacted>"
    if isinstance(value, str):
        return repr(_truncate_value(value))
    if isinstance(value, bool | int | float | type(None)):
        return repr(value)
    if isinstance(value, dict):
        visible_keys = ", ".join(sorted(str(item) for item in value)[:5])
        suffix = ", ..." if len(value) > 5 else ""
        return f"{{{len(value)} keys: {visible_keys}{suffix}}}"
    if isinstance(value, list | tuple | set | frozenset):
        return f"{type(value).__name__}({len(value)} items)"
    return repr(_truncate_value(str(value)))


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _truncate_value(value: str) -> str:
    if len(value) <= _MAX_APPROVAL_VALUE_LENGTH:
        return value
    return value[: _MAX_APPROVAL_VALUE_LENGTH - 3] + "..."


def _truncate_approval_detail(detail: str) -> str:
    if len(detail) <= _MAX_APPROVAL_DETAIL_LENGTH:
        return detail
    return detail[: _MAX_APPROVAL_DETAIL_LENGTH - 3] + "..."


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
