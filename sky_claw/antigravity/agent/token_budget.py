"""TokenBudgetManager — sliding window context management for LLM calls.

Prevents Context Rot and Economic Denial of Service by:
- Estimating token usage of the conversation history
- Applying a sliding window with automatic summarization at 75% capacity
- Truncating aggressively at 90% capacity
- Rejecting outright at 100% capacity
- Tracking session-level token consumption and costs

Design invariants:
- System prompt is NEVER summarized or truncated.
- The last N messages (configurable) are always preserved intact.
- Summarization is non-recursive: summaries are plain text, never re-summarized.
- All thresholds are configurable via immutable Pydantic config.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("SkyClaw.TokenBudget")

# ---------------------------------------------------------------------------
# Default heuristic: ~4 chars per token for English/Spanish mixed content.
_CHARS_PER_TOKEN = 4.0


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TokenBudgetConfig(BaseModel):
    """Immutable configuration for TokenBudgetManager."""

    model_config = ConfigDict(strict=True, frozen=True)

    max_context_tokens: int = 32_000
    warning_threshold_pct: float = 0.75  # Activate summarization
    critical_threshold_pct: float = 0.90  # Aggressive truncation
    messages_to_preserve: int = 6  # Last N messages always intact
    max_tool_rounds: int = 10
    tool_round_timeout_seconds: float = 120.0
    max_retries_per_tool: int = 3
    enable_auto_summarization: bool = True


class BudgetVerdict(BaseModel):
    """Result of a budget check."""

    model_config = ConfigDict(strict=True, frozen=True)

    action: str  # "allow" | "summarize" | "truncate" | "reject"
    current_tokens: int
    max_tokens: int
    utilization_pct: float
    messages_affected: int = 0


class TokenSessionReport(BaseModel):
    """End-of-session token consumption report."""

    model_config = ConfigDict(strict=True, frozen=True)

    total_tokens_consumed: int
    estimated_cost_usd: float
    peak_context_tokens: int
    summarization_count: int
    session_duration_seconds: float


# ---------------------------------------------------------------------------
# TokenBudgetManager
# ---------------------------------------------------------------------------


class TokenBudgetManager:
    """Manages token budget for LLM conversation context.

    Usage::

        manager = TokenBudgetManager()
        verdict = manager.check_budget(messages)
        if verdict.action == "summarize":
            messages = manager.summarize_older_messages(messages)
        manager.record_usage(tokens_used=1500)
    """

    def __init__(self, config: TokenBudgetConfig | None = None) -> None:
        self._config = config or TokenBudgetConfig()
        self._session_start = time.monotonic()
        self._total_tokens_consumed: int = 0
        self._peak_context_tokens: int = 0
        self._summarization_count: int = 0
        # Cost estimation: $0.15 per 1M input tokens (conservative average)
        self._cost_per_token: float = 0.15 / 1_000_000

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for a string.

        Uses a conservative heuristic of 4 characters per token.
        Can be replaced with tiktoken if available.
        """
        if not text:
            return 0
        return max(1, int(len(text) / _CHARS_PER_TOKEN))

    def _estimate_messages_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total tokens across all messages."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.estimate_tokens(content)
            elif isinstance(content, list):
                # Multi-part content (e.g. tool results)
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text", "")
                        if isinstance(text, str):
                            total += self.estimate_tokens(text)
        return total

    # ------------------------------------------------------------------
    # Budget check
    # ------------------------------------------------------------------

    def check_budget(self, messages: list[dict[str, Any]]) -> BudgetVerdict:
        """Check if the message history fits within the token budget.

        Returns a BudgetVerdict indicating the recommended action:
        - "allow": Under 75% — proceed normally.
        - "summarize": 75-90% — summarize older messages.
        - "truncate": 90-100% — aggressively truncate.
        - "reject": Over 100% — reject the request.
        """
        current = self._estimate_messages_tokens(messages)
        max_tokens = self._config.max_context_tokens
        utilization = current / max_tokens if max_tokens > 0 else 1.0

        # Track peak
        if current > self._peak_context_tokens:
            self._peak_context_tokens = current

        if utilization >= 1.0:
            return BudgetVerdict(
                action="reject",
                current_tokens=current,
                max_tokens=max_tokens,
                utilization_pct=round(utilization * 100, 1),
                messages_affected=len(messages),
            )

        if utilization >= self._config.critical_threshold_pct:
            # Count messages that would be affected (all except preserved)
            affected = max(0, len(messages) - self._config.messages_to_preserve - 1)
            return BudgetVerdict(
                action="truncate",
                current_tokens=current,
                max_tokens=max_tokens,
                utilization_pct=round(utilization * 100, 1),
                messages_affected=affected,
            )

        if utilization >= self._config.warning_threshold_pct:
            affected = max(0, len(messages) - self._config.messages_to_preserve - 1)
            return BudgetVerdict(
                action="summarize",
                current_tokens=current,
                max_tokens=max_tokens,
                utilization_pct=round(utilization * 100, 1),
                messages_affected=affected,
            )

        return BudgetVerdict(
            action="allow",
            current_tokens=current,
            max_tokens=max_tokens,
            utilization_pct=round(utilization * 100, 1),
        )

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    def summarize_older_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply sliding window summarization to older messages.

        Algorithm:
        1. Preserve system messages (role="system") — always first.
        2. Preserve the last N user/assistant messages intact.
        3. Replace all messages between system and preserved window with
           a single summary message.

        The summary is a simple concatenation of message previews, NOT
        a recursive LLM call — this prevents summarization loops and
        additional token costs.

        Args:
            messages: Current conversation history.

        Returns:
            New list with older messages replaced by a summary.
        """
        if not self._config.enable_auto_summarization:
            return messages

        if len(messages) <= self._config.messages_to_preserve + 1:
            # Not enough messages to summarize
            return messages

        # Separate system messages from conversation
        system_msgs: list[dict[str, Any]] = []
        conv_msgs: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                conv_msgs.append(msg)

        preserve_count = self._config.messages_to_preserve
        if len(conv_msgs) <= preserve_count:
            return messages

        # Split: older messages to summarize vs recent to preserve
        older = conv_msgs[:-preserve_count]
        recent = conv_msgs[-preserve_count:]

        # Generate summary from older messages (non-recursive)
        summary_parts: list[str] = []
        for msg in older:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:200] + "..." if len(content) > 200 else content
            else:
                preview = "[multi-part content]"
            summary_parts.append(f"[{role}]: {preview}")

        summary_text = "CONTEXT SUMMARY (auto-generated):\n" + "\n".join(summary_parts)

        # Cap summary to prevent it from being too large
        max_summary_chars = int(self._config.max_context_tokens * _CHARS_PER_TOKEN * 0.15)
        if len(summary_text) > max_summary_chars:
            summary_text = summary_text[:max_summary_chars] + "\n... [summary truncated]"

        summary_msg: dict[str, Any] = {
            "role": "system",
            "content": summary_text,
        }

        self._summarization_count += 1
        logger.info(
            "TokenBudget: summarized %d older messages into 1 summary block (%d chars). Preserved %d recent messages.",
            len(older),
            len(summary_text),
            len(recent),
        )

        return system_msgs + [summary_msg] + recent

    # ------------------------------------------------------------------
    # Truncation (aggressive)
    # ------------------------------------------------------------------

    def truncate_older_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggressively truncate older messages to fit budget.

        Unlike summarization, this simply drops older messages entirely,
        keeping only system messages and the last N messages.
        """
        system_msgs: list[dict[str, Any]] = []
        conv_msgs: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                conv_msgs.append(msg)

        preserve_count = self._config.messages_to_preserve
        dropped = max(0, len(conv_msgs) - preserve_count)
        recent = conv_msgs[-preserve_count:] if len(conv_msgs) > preserve_count else conv_msgs

        if dropped > 0:
            logger.warning(
                "TokenBudget: DROPPED %d older messages to fit budget. Preserved %d recent messages.",
                dropped,
                len(recent),
            )

        return system_msgs + recent

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    def record_usage(self, tokens_used: int) -> None:
        """Record token consumption for the session."""
        self._total_tokens_consumed += tokens_used

    def get_session_report(self) -> TokenSessionReport:
        """Generate a report of token consumption for this session."""
        duration = time.monotonic() - self._session_start
        return TokenSessionReport(
            total_tokens_consumed=self._total_tokens_consumed,
            estimated_cost_usd=round(self._total_tokens_consumed * self._cost_per_token, 6),
            peak_context_tokens=self._peak_context_tokens,
            summarization_count=self._summarization_count,
            session_duration_seconds=round(duration, 2),
        )
