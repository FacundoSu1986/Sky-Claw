"""Protocols and exceptions shared by all tool strategies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolStrategy(Protocol):
    """A single dispatchable tool.

    Implementations OWN their Pydantic validation: each strategy knows its
    own input schema and constructs/validates models inside `execute`.

    Strategies MAY raise. ErrorWrappingMiddleware (when registered on a
    strategy) converts uncaught exceptions to {"status": "error", ...} dicts
    so the dispatcher's caller (LangGraph) always sees a dict.
    """

    name: str  # registry key; must be unique within a dispatcher

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]: ...


NextCall = Callable[[], Awaitable[dict[str, Any]]]


@runtime_checkable
class ToolMiddleware(Protocol):
    """Cross-cutting concern wrapped around a strategy invocation.

    Middleware receives the strategy (for introspection / name access),
    the original payload_dict, and `next_call` — a thunk that invokes the
    next layer (another middleware or the strategy itself). Calling
    `await next_call()` advances the chain; skipping it short-circuits.
    """

    async def __call__(
        self,
        strategy: ToolStrategy,
        payload_dict: dict[str, Any],
        next_call: NextCall,
    ) -> dict[str, Any]: ...


class ToolNotFoundError(KeyError):
    """Exception type for unknown tool names in exception-based flows.

    Note: `OrchestrationToolDispatcher.dispatch()` currently preserves the
    legacy contract for missing tools and returns
    ``{"status": "error", "reason": "ToolNotFound"}`` instead of raising this
    exception. This class exists for potential future exception-based APIs.
    """


class DuplicateToolError(ValueError):
    """Raised when a strategy is registered with a name that already exists."""
