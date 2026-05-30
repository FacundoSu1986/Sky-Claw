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


def test_redacts_google_api_key() -> None:
    """Google API keys (AIza + 35 chars) must be redacted wherever they appear."""
    redact_filter = SecurityRedactionFilter()
    # Split to avoid CI secret-scanner flagging this test file itself.
    key = _token("AI", "zaSyDaBcDeFgHiJkLmNoPqRsTuVwXyZ012345")
    text = f"google_key={key} other=safe"

    redacted = redact_filter._redact(text)

    assert "AIza" not in redacted
    assert "[REDACTED]" in redacted
    assert "safe" in redacted  # surrounding context preserved


def test_redacts_google_api_key_ending_in_dash() -> None:
    """Google API keys whose last character is '-' must still be redacted.

    The final \\b anchor fails for non-word trailing chars; the pattern must
    use a negative lookahead instead.
    """
    redact_filter = SecurityRedactionFilter()
    key_dash = _token("AI", "zaSyDaBcDeFgHiJkLmNoPqRsTuVwXyZ01234-")
    text = f"google_key={key_dash} other=safe"

    redacted = redact_filter._redact(text)

    assert "AIza" not in redacted
    assert "[REDACTED]" in redacted


def test_redacts_stripe_api_keys() -> None:
    """Stripe live/test secret and publishable keys must be redacted (all 4 variants)."""
    redact_filter = SecurityRedactionFilter()
    sk_live = _token("sk", "_live_", "abcdefghijklmnopqrstuvwxyz012345")
    pk_live = _token("pk", "_live_", "abcdefghijklmnopqrstuvwxyz012345")
    sk_test = _token("sk", "_test_", "abcdefghijklmnopqrstuvwxyz012345")
    pk_test = _token("pk", "_test_", "abcdefghijklmnopqrstuvwxyz012345")
    text = f"stripe_sk={sk_live} stripe_pk={pk_live} dev_sk={sk_test} dev_pk={pk_test}"

    redacted = redact_filter._redact(text)

    assert "sk_live_" not in redacted
    assert "pk_live_" not in redacted
    assert "sk_test_" not in redacted
    assert "pk_test_" not in redacted
    assert redacted.count("[REDACTED]") == 4


def test_redacts_aws_secret_access_key() -> None:
    """AWS Secret Access Key value must be redacted in key=value strings."""
    redact_filter = SecurityRedactionFilter()
    # Standard 40-char AWS Secret Access Key (example from AWS docs, split for CI scanner).
    secret = _token("wJalrXUtn", "FEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    text = f"aws_secret_access_key={secret} region=us-east-1"

    redacted = redact_filter._redact(text)

    assert "wJalrXUtn" not in redacted
    assert "[REDACTED]" in redacted
    assert "region=us-east-1" in redacted  # unrelated value preserved
    # Original key name casing must be preserved
    assert "aws_secret_access_key" in redacted


def test_redacts_aws_secret_access_key_uppercase_preserved() -> None:
    """AWS key name casing (e.g. AWS_SECRET_ACCESS_KEY) must survive redaction unchanged."""
    redact_filter = SecurityRedactionFilter()
    secret = _token("wJalrXUtn", "FEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    text = f"AWS_SECRET_ACCESS_KEY={secret}"

    redacted = redact_filter._redact(text)

    assert "wJalrXUtn" not in redacted
    assert "AWS_SECRET_ACCESS_KEY" in redacted  # casing preserved, not lowercased


def test_redacts_aws_secret_in_structured_extra() -> None:
    """AWS Secret Access Key logged as a Mapping extra must redact the value directly."""
    redact_filter = SecurityRedactionFilter()
    secret = _token("wJalrXUtn", "FEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="deploying",
        args=(),
        exc_info=None,
    )
    record.context = {"aws_secret_access_key": secret, "region": "us-east-1"}

    assert redact_filter.filter(record)

    assert "wJalrXUtn" not in str(record.context)
    assert record.context["region"] == "us-east-1"  # unrelated key untouched
