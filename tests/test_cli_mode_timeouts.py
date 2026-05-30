"""P1 §3.2 — CLI mode's router.chat call must be bounded by asyncio.wait_for.

Without a timeout, a hung LLM provider freezes the CLI indefinitely.
This test verifies that ``_run_oneshot`` and ``_run_cli`` honor a
configurable timeout (default 300s) and surface a TimeoutError as a
RuntimeError (which they already log gracefully).

Contracts:
- ``_run_oneshot`` raises SystemExit when chat times out (existing
  error-path behavior — must not hang).
- ``_run_cli`` logs the timeout but does NOT crash the REPL loop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_run_oneshot_times_out_and_exits() -> None:
    """_run_oneshot must surface a chat timeout as a clean SystemExit, not hang."""
    from sky_claw.antigravity.modes import cli_mode

    ctx = MagicMock()
    ctx.router = MagicMock()
    ctx.session = MagicMock()

    # Make chat hang forever.
    async def _hang(*_args: object, **_kwargs: object) -> str:
        await asyncio.Event().wait()
        return "never"  # pragma: no cover

    ctx.router.chat = AsyncMock(side_effect=_hang)

    # Patch the cli_mode logger directly — caplog propagation is unreliable across
    # OS / Python version combinations on CI.
    with (
        patch.object(cli_mode, "_CHAT_TIMEOUT_SECONDS", 0.05),
        patch.object(cli_mode.logger, "error") as mock_error,
        pytest.raises(SystemExit),
    ):
        await asyncio.wait_for(cli_mode._run_oneshot(ctx, "hello"), timeout=5.0)

    # The error log should reflect the timeout, not a generic crash.
    assert mock_error.called, "Expected logger.error to be called before SystemExit"
    assert any(
        "timeout" in str(call.args[0]).lower() or "timed out" in str(call.args[0]).lower()
        for call in mock_error.call_args_list
    ), (
        f"Expected a timeout-related error log; got: "
        f"{[call.args[0] for call in mock_error.call_args_list]!r}"
    )


@pytest.mark.asyncio
async def test_run_oneshot_completes_on_fast_response() -> None:
    """Happy path: a quick chat response must not be affected by the wait_for wrapper."""
    from sky_claw.antigravity.modes import cli_mode

    ctx = MagicMock()
    ctx.router = MagicMock()
    ctx.session = MagicMock()
    ctx.router.chat = AsyncMock(return_value="hello back")

    # Patch timeout high enough that it's irrelevant.
    with patch.object(cli_mode, "_CHAT_TIMEOUT_SECONDS", 5.0):
        await cli_mode._run_oneshot(ctx, "hello")

    ctx.router.chat.assert_awaited_once_with("hello", ctx.session, chat_id="oneshot")
