"""Tests for TokenBudgetManager and TokenCircuitBreaker — FASE 1.5.3.

Validates:
- Token estimation
- Budget verdicts (allow/summarize/truncate/reject)
- Sliding window summarization
- Aggressive truncation
- Session report generation
- Circuit breaker state machine (CLOSED/OPEN/HALF_OPEN)
- Spike detection
- Window budget enforcement
- Manual reset
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sky_claw.agent.token_budget import (
    BudgetVerdict,
    TokenBudgetConfig,
    TokenBudgetManager,
)
from sky_claw.agent.token_circuit_breaker import (
    TokenCircuitBreaker,
    TokenCircuitBreakerConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages(*contents: str) -> list[dict]:
    """Create a list of user messages from strings."""
    return [{"role": "user", "content": c} for c in contents]


def _make_large_messages(count: int, chars_per_msg: int = 4000) -> list[dict]:
    """Create messages that simulate a large conversation."""
    msgs: list[dict] = [{"role": "system", "content": "System prompt"}]
    for _ in range(count):
        msgs.append({"role": "user", "content": "A" * chars_per_msg})
        msgs.append({"role": "assistant", "content": "B" * chars_per_msg})
    return msgs


# ---------------------------------------------------------------------------
# TokenBudgetManager — Token Estimation
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    def test_empty_string(self) -> None:
        mgr = TokenBudgetManager()
        assert mgr.estimate_tokens("") == 0

    def test_short_string(self) -> None:
        mgr = TokenBudgetManager()
        # ~4 chars per token → 20 chars ≈ 5 tokens
        assert mgr.estimate_tokens("A" * 20) == 5

    def test_long_string(self) -> None:
        mgr = TokenBudgetManager()
        # 4000 chars ≈ 1000 tokens
        assert mgr.estimate_tokens("A" * 4000) == 1000


# ---------------------------------------------------------------------------
# TokenBudgetManager — Budget Check
# ---------------------------------------------------------------------------


class TestBudgetCheck:
    def test_allow_under_threshold(self) -> None:
        mgr = TokenBudgetManager(TokenBudgetConfig(max_context_tokens=10_000))
        msgs = _make_messages("A" * 100)  # ~25 tokens
        verdict = mgr.check_budget(msgs)
        assert verdict.action == "allow"
        assert verdict.utilization_pct < 75.0

    def test_summarize_at_warning(self) -> None:
        # 75% of 1000 tokens = 750 tokens = 3000 chars
        mgr = TokenBudgetManager(
            TokenBudgetConfig(
                max_context_tokens=1000,
                messages_to_preserve=2,
            )
        )
        msgs = _make_messages("A" * 3000)
        verdict = mgr.check_budget(msgs)
        assert verdict.action == "summarize"
        assert verdict.utilization_pct >= 75.0

    def test_truncate_at_critical(self) -> None:
        # 90% of 1000 tokens = 900 tokens = 3600 chars
        mgr = TokenBudgetManager(
            TokenBudgetConfig(
                max_context_tokens=1000,
                messages_to_preserve=2,
            )
        )
        msgs = _make_messages("A" * 3600)
        verdict = mgr.check_budget(msgs)
        assert verdict.action == "truncate"
        assert verdict.utilization_pct >= 90.0

    def test_reject_at_max(self) -> None:
        mgr = TokenBudgetManager(TokenBudgetConfig(max_context_tokens=100))
        msgs = _make_messages("A" * 1000)  # ~250 tokens > 100
        verdict = mgr.check_budget(msgs)
        assert verdict.action == "reject"
        assert verdict.utilization_pct >= 100.0


# ---------------------------------------------------------------------------
# TokenBudgetManager — Summarization
# ---------------------------------------------------------------------------


class TestSummarization:
    def test_summarize_preserves_recent(self) -> None:
        mgr = TokenBudgetManager(
            TokenBudgetConfig(
                max_context_tokens=10_000,
                messages_to_preserve=4,
            )
        )
        msgs = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Old message 1"},
            {"role": "assistant", "content": "Old reply 1"},
            {"role": "user", "content": "Old message 2"},
            {"role": "assistant", "content": "Old reply 2"},
            {"role": "user", "content": "Recent 1"},
            {"role": "assistant", "content": "Recent reply 1"},
            {"role": "user", "content": "Recent 2"},
            {"role": "assistant", "content": "Recent reply 2"},
        ]
        result = mgr.summarize_older_messages(msgs)

        # System message should be preserved
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "System"

        # Recent messages should be preserved intact
        assert result[-4:] == msgs[-4:]

        # There should be a summary message
        summary_msgs = [m for m in result if "CONTEXT SUMMARY" in m.get("content", "")]
        assert len(summary_msgs) == 1

    def test_summarize_disabled(self) -> None:
        mgr = TokenBudgetManager(
            TokenBudgetConfig(
                enable_auto_summarization=False,
            )
        )
        msgs = _make_messages("A", "B", "C")
        result = mgr.summarize_older_messages(msgs)
        assert result == msgs  # Unchanged

    def test_summarize_too_few_messages(self) -> None:
        mgr = TokenBudgetManager(
            TokenBudgetConfig(
                messages_to_preserve=10,
            )
        )
        msgs = _make_messages("A", "B")
        result = mgr.summarize_older_messages(msgs)
        assert result == msgs  # Not enough to summarize

    def test_summarization_count_tracked(self) -> None:
        mgr = TokenBudgetManager(
            TokenBudgetConfig(
                messages_to_preserve=2,
            )
        )
        msgs = [
            {"role": "system", "content": "Sys"},
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "C"},
            {"role": "assistant", "content": "D"},
        ]
        mgr.summarize_older_messages(msgs)
        report = mgr.get_session_report()
        assert report.summarization_count == 1


# ---------------------------------------------------------------------------
# TokenBudgetManager — Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_truncate_drops_older_messages(self) -> None:
        mgr = TokenBudgetManager(
            TokenBudgetConfig(
                messages_to_preserve=2,
            )
        )
        msgs = [
            {"role": "system", "content": "Sys"},
            {"role": "user", "content": "Old 1"},
            {"role": "assistant", "content": "Old 2"},
            {"role": "user", "content": "Recent 1"},
            {"role": "assistant", "content": "Recent 2"},
        ]
        result = mgr.truncate_older_messages(msgs)

        # System preserved
        assert result[0]["role"] == "system"
        # Only last 2 conversation messages preserved
        assert len(result) == 3  # system + 2 recent
        assert result[1]["content"] == "Recent 1"
        assert result[2]["content"] == "Recent 2"


# ---------------------------------------------------------------------------
# TokenBudgetManager — Session Report
# ---------------------------------------------------------------------------


class TestSessionReport:
    def test_record_usage(self) -> None:
        mgr = TokenBudgetManager()
        mgr.record_usage(1000)
        mgr.record_usage(500)
        report = mgr.get_session_report()
        assert report.total_tokens_consumed == 1500
        assert report.estimated_cost_usd > 0
        assert report.session_duration_seconds >= 0

    def test_peak_tracking(self) -> None:
        mgr = TokenBudgetManager(TokenBudgetConfig(max_context_tokens=10_000))
        mgr.check_budget(_make_messages("A" * 100))
        mgr.check_budget(_make_messages("A" * 1000))
        report = mgr.get_session_report()
        assert report.peak_context_tokens > 0


# ---------------------------------------------------------------------------
# TokenCircuitBreaker — State Machine
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = TokenCircuitBreaker()
        assert cb.state == "closed"

    def test_allows_normal_request(self) -> None:
        cb = TokenCircuitBreaker()
        assert cb.check_request(1000) is True

    def test_trips_on_spike(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=1000,
            )
        )
        assert cb.check_request(2000) is False
        assert cb.state == "open"

    def test_open_rejects_all(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=100,
            )
        )
        cb.check_request(200)  # Trip to OPEN
        assert cb.state == "open"
        assert cb.check_request(10) is False
        assert cb.check_request(1) is False

    def test_half_open_after_recovery(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=100,
                recovery_timeout_seconds=0,  # Immediate recovery
            )
        )
        cb.check_request(200)  # Trip to OPEN
        # With 0-second recovery, next state access → HALF_OPEN
        assert cb.state == "half_open"

    def test_half_open_allows_one_probe(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=100,
                recovery_timeout_seconds=0,
            )
        )
        cb.check_request(200)  # Trip to OPEN
        assert cb.state == "half_open"
        assert cb.check_request(50) is True  # Probe allowed
        assert cb.check_request(50) is False  # Second rejected

    def test_half_open_to_closed_on_success(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=100,
                recovery_timeout_seconds=0,
            )
        )
        cb.check_request(200)  # Trip to OPEN
        assert cb.state == "half_open"
        cb.check_request(50)  # Probe
        cb.record_response(50)  # Success → CLOSED
        assert cb.state == "closed"

    def test_half_open_to_open_on_spike(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=100,
                recovery_timeout_seconds=60,  # Non-zero so OPEN stays OPEN
            )
        )
        cb.check_request(200)  # Trip to OPEN
        assert cb.state == "open"
        # Manually force to HALF_OPEN for testing
        cb._state = "half_open"
        cb._half_open_used = False
        cb.check_request(50)  # Probe
        cb.record_response(200)  # Spike in probe → back to OPEN
        assert cb.state == "open"

    def test_manual_reset(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=100,
            )
        )
        cb.check_request(200)  # Trip to OPEN
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"

    def test_window_budget_enforcement(self) -> None:
        cb = TokenCircuitBreaker(
            TokenCircuitBreakerConfig(
                spike_threshold_tokens=100_000,  # High spike threshold
                window_budget_tokens=1000,
                window_duration_seconds=300,
            )
        )
        assert cb.check_request(300) is True
        cb.record_response(300)
        assert cb.check_request(300) is True
        cb.record_response(300)
        # Window consumed: 600. Next request 300+600=900 < 1000 → still allowed
        assert cb.check_request(300) is True
        cb.record_response(300)
        # Window consumed: 900. Next request 300+900=1200 > 1000 → should trip
        assert cb.check_request(300) is False
        assert cb.state == "open"


# ---------------------------------------------------------------------------
# Config Immutability
# ---------------------------------------------------------------------------


class TestConfigImmutability:
    def test_budget_config_frozen(self) -> None:
        cfg = TokenBudgetConfig()
        with pytest.raises(ValidationError):
            cfg.max_context_tokens = 99999  # type: ignore[misc]

    def test_breaker_config_frozen(self) -> None:
        cfg = TokenCircuitBreakerConfig()
        with pytest.raises(ValidationError):
            cfg.spike_threshold_tokens = 99999  # type: ignore[misc]

    def test_verdict_frozen(self) -> None:
        v = BudgetVerdict(
            action="allow",
            current_tokens=100,
            max_tokens=1000,
            utilization_pct=10.0,
        )
        with pytest.raises(ValidationError):
            v.action = "reject"  # type: ignore[misc]
