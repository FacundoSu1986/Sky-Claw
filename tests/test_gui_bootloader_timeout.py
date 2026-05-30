"""P1 §3.2 — GUI bootloader's router.chat call must be bounded by asyncio.wait_for.

Without a timeout, a hung LLM provider keeps the GUI's logic loop stuck on
a single message — the UI sees neither response nor error, indefinitely.

The chat-handling block was extracted into ``_dispatch_chat_to_router`` so
the timeout behavior is testable in isolation without spinning the full
``_gui_logic_loop`` (which is ``while True`` and pulls from a real queue).

Contracts:
- On hang, a timeout error event is published (no hang).
- On fast response, the LLM_RESPONSE event carries the actual reply.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_dispatch_chat_publishes_timeout_on_hang() -> None:
    """If router.chat hangs past _GUI_CHAT_TIMEOUT_SECONDS, an error event is emitted."""
    from sky_claw.antigravity.gui import _bootloader

    ctx = MagicMock()
    ctx.router = MagicMock()
    ctx.session = MagicMock()

    async def _hang(*_args: object, **_kwargs: object) -> str:
        await asyncio.Event().wait()
        return "never"  # pragma: no cover

    ctx.router.chat = AsyncMock(side_effect=_hang)

    published: list[object] = []
    with (
        patch.object(_bootloader, "_GUI_CHAT_TIMEOUT_SECONDS", 0.05),
        patch.object(_bootloader.gui_event_bus, "publish", side_effect=published.append),
    ):
        await asyncio.wait_for(_bootloader._dispatch_chat_to_router(ctx, "hola"), timeout=5.0)

    assert published, "Expected at least one event after the timeout"
    last = published[-1]
    payload = last.data["response"] if hasattr(last, "data") else last["data"]["response"]  # type: ignore[index]
    assert "timeout" in str(payload).lower() or "timed out" in str(payload).lower(), (
        f"Expected the published event to mention 'timeout'; got: {payload!r}"
    )


@pytest.mark.asyncio
async def test_dispatch_chat_publishes_response_on_success() -> None:
    """Happy path: a fast chat response flows into an LLM_RESPONSE event."""
    from sky_claw.antigravity.gui import _bootloader

    ctx = MagicMock()
    ctx.router = MagicMock()
    ctx.session = MagicMock()
    ctx.router.chat = AsyncMock(return_value="hello back")

    published: list[object] = []
    with (
        patch.object(_bootloader, "_GUI_CHAT_TIMEOUT_SECONDS", 5.0),
        patch.object(_bootloader.gui_event_bus, "publish", side_effect=published.append),
    ):
        await _bootloader._dispatch_chat_to_router(ctx, "hello")

    assert published, "Expected the LLM_RESPONSE event to be published"
    payload = published[-1].data["response"] if hasattr(published[-1], "data") else published[-1]["data"]["response"]  # type: ignore[index]
    assert payload == "hello back"
