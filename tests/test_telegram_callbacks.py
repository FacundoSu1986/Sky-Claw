"""Focused tests for the common Telegram callback dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.telegram import TelegramWebhook


def _make_webhook() -> tuple[TelegramWebhook, AsyncMock, MagicMock]:
    hitl = MagicMock()
    hitl.respond = AsyncMock(return_value=True)
    sender = MagicMock()
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock()
    sender.send = AsyncMock()
    webhook = TelegramWebhook(
        router=MagicMock(),
        sender=sender,
        session=MagicMock(spec=aiohttp.ClientSession),
        hitl=hitl,
        authorized_user_id=123,
    )
    return webhook, hitl.respond, sender


def _update(data: str, *, user_id: int = 123, chat_id: int = 123) -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": 9},
            "data": data,
        },
    }


@pytest.mark.asyncio
async def test_unknown_callback_action_does_not_resolve_request() -> None:
    webhook, respond, sender = _make_webhook()

    await webhook.process_update(_update("hitl:escalate:req-1"))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid HITL action")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_callback_does_not_resolve_request() -> None:
    webhook, respond, sender = _make_webhook()

    await webhook.process_update(_update("hitl:approve"))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid callback")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_callback_does_not_resolve_request() -> None:
    webhook, respond, sender = _make_webhook()

    await webhook.process_update(_update("hitl:approve:req-1", user_id=999, chat_id=999))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Unauthorized")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "approved"),
    [("hitl:approve:req-approve", True), ("hitl:deny:req-deny", False)],
)
async def test_valid_callback_maps_only_allowlisted_actions_to_hitl(
    data: str,
    approved: bool,
) -> None:
    webhook, respond, sender = _make_webhook()

    await webhook.process_update(_update(data))

    respond.assert_awaited_once_with(data.rsplit(":", 1)[1], approved)
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text=None)
    sender.edit_message.assert_awaited_once()
