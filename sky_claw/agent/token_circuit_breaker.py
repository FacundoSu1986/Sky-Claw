"""TokenCircuitBreaker — protects against token consumption spikes.

Implements the Circuit Breaker pattern with three states:
- CLOSED: Normal operation. Requests are allowed.
- OPEN: Spike detected. All requests are rejected (fail-fast).
- HALF_OPEN: Recovery period elapsed. One probe request allowed.

This prevents runaway token consumption from:
- Infinite tool loops that accumulate context
- Malicious or broken prompts that generate massive outputs
- Recursive summarization failures

Design invariants:
- Thread-safe for asyncio (single event loop).
- State transitions are atomic.
- Recovery timeout prevents permanent OPEN state.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("SkyClaw.TokenCircuitBreaker")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TokenCircuitBreakerConfig(BaseModel):
    """Immutable configuration for TokenCircuitBreaker."""

    model_config = ConfigDict(strict=True, frozen=True)

    spike_threshold_tokens: int = 50_000  # Single request > 50K → spike
    window_budget_tokens: int = 200_000  # Budget in 5-min window
    window_duration_seconds: int = 300
    recovery_timeout_seconds: int = 60


# ---------------------------------------------------------------------------
# TokenCircuitBreaker
# ---------------------------------------------------------------------------


class TokenCircuitBreaker:
    """Circuit breaker for token consumption protection.

    Usage::

        cb = TokenCircuitBreaker()
        if cb.check_request(estimated_tokens=5000):
            response = await llm_call(...)
            cb.record_response(tokens_used=response_tokens)
        else:
            raise TokenBudgetExceeded("Circuit breaker is OPEN")
    """

    def __init__(self, config: TokenCircuitBreakerConfig | None = None) -> None:
        self._config = config or TokenCircuitBreakerConfig()
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._opened_at: float = 0.0
        self._window_start: float = time.monotonic()
        self._window_consumed: int = 0
        self._half_open_used: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current state of the circuit breaker.

        Automatically transitions OPEN → HALF_OPEN after recovery timeout.
        """
        if self._state == "open":
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._config.recovery_timeout_seconds:
                self._state = "half_open"
                self._half_open_used = False
                logger.info(
                    "TokenCircuitBreaker: OPEN → HALF_OPEN (recovery timeout %ds elapsed)",
                    self._config.recovery_timeout_seconds,
                )
        return self._state

    def check_request(self, estimated_tokens: int) -> bool:
        """Check if a request should be allowed.

        Args:
            estimated_tokens: Estimated token count for the upcoming request.

        Returns:
            True if the request is allowed, False if it should be rejected.
        """
        current_state = self.state  # Triggers OPEN→HALF_OPEN transition

        if current_state == "closed":
            # Check for single-request spike
            if estimated_tokens > self._config.spike_threshold_tokens:
                self._trip(
                    f"Spike detected: {estimated_tokens} tokens > {self._config.spike_threshold_tokens} threshold"
                )
                return False

            # Check window budget
            self._maybe_reset_window()
            if self._window_consumed + estimated_tokens > self._config.window_budget_tokens:
                self._trip(
                    f"Window budget exceeded: {self._window_consumed + estimated_tokens} > {self._config.window_budget_tokens}"
                )
                return False

            return True

        if current_state == "half_open":
            # Allow exactly ONE probe request
            if not self._half_open_used:
                self._half_open_used = True
                return True
            return False

        # OPEN state — reject everything
        return False

    def record_response(self, tokens_used: int) -> None:
        """Record actual token consumption from a completed request.

        If in HALF_OPEN and the response is within budget, transition to CLOSED.
        If the response exceeds spike threshold, trip back to OPEN.
        """
        self._window_consumed += tokens_used

        if self._state == "half_open":
            if tokens_used <= self._config.spike_threshold_tokens:
                self._state = "closed"
                self._half_open_used = False
                logger.info(
                    "TokenCircuitBreaker: HALF_OPEN → CLOSED (probe request successful, %d tokens used)",
                    tokens_used,
                )
            else:
                self._trip(f"Probe request spike in HALF_OPEN: {tokens_used} tokens")

    def reset(self) -> None:
        """Force transition to CLOSED state (manual reset after HITL)."""
        self._state = "closed"
        self._half_open_used = False
        self._window_consumed = 0
        self._window_start = time.monotonic()
        logger.info("TokenCircuitBreaker: manually reset to CLOSED")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _trip(self, reason: str) -> None:
        """Transition to OPEN state."""
        self._state = "open"
        self._opened_at = time.monotonic()
        logger.warning(
            "TokenCircuitBreaker: → OPEN (%s). All requests will be rejected for %ds.",
            reason,
            self._config.recovery_timeout_seconds,
        )

    def _maybe_reset_window(self) -> None:
        """Reset the consumption window if it has expired."""
        elapsed = time.monotonic() - self._window_start
        if elapsed >= self._config.window_duration_seconds:
            self._window_consumed = 0
            self._window_start = time.monotonic()
