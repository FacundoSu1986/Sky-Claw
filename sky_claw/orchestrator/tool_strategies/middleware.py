"""Cross-cutting middleware for OrchestrationToolDispatcher.

Today there are TWO middlewares because two patterns are duplicated across
the legacy match/case branches (supervisor.py:270-320, identical try/except
+ isinstance guards in synthesis and xedit). Centralizing them kills ~50
lines of copy-paste.

Note on HITL: only ONE branch (execute_loot_sorting) currently uses HITL,
and it requires Pydantic validation BEFORE the prompt. Wrapping that into
a middleware would either invert the order (HITL before validation) or
require the strategy to expose its validation method to the middleware —
both worse than leaving HITL inside the LOOT strategy. We'll add a
HitlGateMiddleware when a SECOND use site appears (YAGNI).
"""

from __future__ import annotations

import logging
from typing import Any

from sky_claw.orchestrator.tool_strategies.base import NextCall, ToolStrategy

logger = logging.getLogger(__name__)


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


__all__ = [
    "DictResultGuardMiddleware",
    "ErrorWrappingMiddleware",
]
