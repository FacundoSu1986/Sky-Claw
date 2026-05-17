from __future__ import annotations

import logging
from unittest.mock import patch

from sky_claw import logging_config
from sky_claw.logging_config import SecurityRedactionFilter


def _token(*parts: str) -> str:
    return "".join(parts)


def test_redacts_modern_llm_and_bearer_tokens() -> None:
    redact_filter = SecurityRedactionFilter()
    text = (
        "openai=sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 "
        "anthropic=sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 "
        "auth=Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    )

    redacted = redact_filter._redact(text)

    assert "sk-proj-" not in redacted
    assert "sk-ant-" not in redacted
    assert "Bearer abc" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_redacts_long_telegram_bot_ids() -> None:
    redact_filter = SecurityRedactionFilter()

    redacted = redact_filter._redact("token=12345678901:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")

    assert "12345678901:" not in redacted
    assert redacted == "token=[REDACTED]"


def test_redacts_common_platform_tokens() -> None:
    redact_filter = SecurityRedactionFilter()
    github_classic = _token("gh", "p_", "1234567890abcdefABCDEF1234567890abcdef")
    github_fine_grained = _token(
        "github",
        "_pat_",
        "1234567890_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghi",
    )
    aws_key = _token("AK", "IA", "IOSFODNN7EXAMPLE")
    slack_token = _token("xo", "xb-", "123456789012-", "123456789012-", "abcdefghijklmnopqrstuvwxyz")
    gitlab_token = _token("gl", "pat-", "abcdefghijklmnopqrst")
    jwt_token = _token(
        "eyJhbGciOiJIUzI1NiJ9",
        ".",
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        ".",
        "signaturepart",
    )
    text = " ".join(
        (
            f"github={github_classic}",
            f"github_fine_grained={github_fine_grained}",
            f"aws={aws_key}",
            f"slack={slack_token}",
            f"gitlab={gitlab_token}",
            f"jwt={jwt_token}",
        )
    )

    redacted = redact_filter._redact(text)

    assert "ghp_" not in redacted
    assert "github_pat_" not in redacted
    assert "AKIA" not in redacted
    assert "xoxb-" not in redacted
    assert "glpat-" not in redacted
    assert "eyJ" not in redacted
    assert redacted.count("[REDACTED]") == 6


def test_filter_redacts_nested_extra_values() -> None:
    redact_filter = SecurityRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.context = {
        "headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123456789"},
        "keys": ["sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"],
    }

    assert redact_filter.filter(record)

    assert "Bearer abc" not in str(record.context)
    assert "sk-proj-" not in str(record.context)


def test_filter_breaks_cycles_in_nested_extra_values() -> None:
    redact_filter = SecurityRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    context: dict[str, object] = {"token": "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"}
    context["self"] = context
    record.context = context

    assert redact_filter.filter(record)

    assert record.context["self"] == "[REDACTED:CYCLE]"
    assert "sk-proj-" not in str(record.context)


def test_filter_caps_deeply_nested_values() -> None:
    # The depth cap only protects against RecursionError if it stays well
    # below Python's recursion limit. Pin a hard upper bound so that raising
    # _MAX_DEPTH past a safe value is itself caught as a regression.
    assert SecurityRedactionFilter._MAX_DEPTH <= 128

    redact_filter = SecurityRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    deep_secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    # Fixed nesting depth (not derived from _MAX_DEPTH): comfortably above the
    # asserted upper bound, so removing or disabling the cap leaves the deep
    # branch un-collapsed and fails the test.
    fixed_depth = 256
    nested: dict[str, object] = {"token": deep_secret}
    for _ in range(fixed_depth):
        nested = {"child": nested}
    record.context = {"shallow": deep_secret, "deep": nested}

    assert redact_filter.filter(record)

    assert "[REDACTED:DEPTH]" in str(record.context)
    assert "sk-proj-" not in str(record.context["shallow"])


def test_resolve_current_user_falls_back_for_legacy_getpass_errors() -> None:
    with patch("sky_claw.logging_config.getpass.getuser", side_effect=KeyError("missing passwd entry")):
        assert logging_config._resolve_current_user() == "User"
