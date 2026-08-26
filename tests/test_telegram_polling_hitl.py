"""Regression tests for Telegram polling transport and HITL lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.comms.telegram import TelegramWebhook
from sky_claw.app.comms.telegram_polling import TelegramPolling
from sky_claw.app.security.hitl import Decision, HITLGuard


async def _wait_for_notification(notification_sent: asyncio.Event) -> None:
    await asyncio.wait_for(notification_sent.wait(), timeout=1)


def _callback_update(
    update_id: int,
    action: str,
    request_id: str,
    *,
    user_id: int = 123,
    chat_id: int = 123,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": 77},
            "data": f"hitl:{action}:{request_id}",
        },
    }


def _make_transport(hitl: HITLGuard) -> tuple[TelegramPolling, MagicMock, MagicMock]:
    router = MagicMock()
    sender = MagicMock()
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock()
    sender.send = AsyncMock()
    session = MagicMock(spec=aiohttp.ClientSession)
    webhook = TelegramWebhook(
        router=router,
        sender=sender,
        session=session,
        hitl=hitl,
        authorized_user_id=123,
    )
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=webhook,
        gateway=MagicMock(),
        session=session,
        interval=0,
        authorized_chat_id=123,
    )
    return polling, sender, session


@pytest.mark.asyncio
async def test_polling_callback_approve_resolves_pending_hitl() -> None:
    notification_sent = asyncio.Event()

    async def notify(_request) -> None:
        notification_sent.set()

    hitl = HITLGuard(notify_fn=notify, timeout=1)
    polling, sender, _session = _make_transport(hitl)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-approve"))

    await _wait_for_notification(notification_sent)
    await polling._process_raw_update(_callback_update(1, "approve", "req-approve"))

    assert await pending is Decision.APPROVED
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text=None)
    sender.edit_message.assert_awaited_once_with(
        123,
        77,
        "Request 'req-approve' approved by operator.",
        reply_markup=None,
    )


@pytest.mark.asyncio
async def test_polling_callback_deny_resolves_pending_hitl() -> None:
    notification_sent = asyncio.Event()

    async def notify(_request) -> None:
        notification_sent.set()

    hitl = HITLGuard(notify_fn=notify, timeout=1)
    polling, _sender, _session = _make_transport(hitl)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-deny"))

    await _wait_for_notification(notification_sent)
    await polling._process_raw_update(_callback_update(2, "deny", "req-deny"))

    assert await pending is Decision.DENIED


@pytest.mark.asyncio
async def test_polling_unknown_callback_action_does_not_resolve_request() -> None:
    notification_sent = asyncio.Event()

    async def notify(_request) -> None:
        notification_sent.set()

    hitl = HITLGuard(notify_fn=notify, timeout=1)
    polling, sender, _session = _make_transport(hitl)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-unknown"))

    await _wait_for_notification(notification_sent)
    await polling._process_raw_update(_callback_update(3, "escalate", "req-unknown"))

    assert not pending.done()
    sender.answer_callback_query.assert_awaited_once_with("callback-3", text="Invalid HITL action")
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.asyncio
async def test_polling_unauthorized_callback_does_not_resolve_request() -> None:
    notification_sent = asyncio.Event()

    async def notify(_request) -> None:
        notification_sent.set()

    hitl = HITLGuard(notify_fn=notify, timeout=1)
    polling, sender, _session = _make_transport(hitl)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-unauthorized"))

    await _wait_for_notification(notification_sent)
    await polling._process_raw_update(_callback_update(4, "approve", "req-unauthorized", user_id=999, chat_id=999))

    assert not pending.done()
    # Polling rejects unauthorized chats before handing the update to the
    # webhook dispatcher, so no callback acknowledgement is emitted.
    sender.answer_callback_query.assert_not_awaited()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.asyncio
async def test_duplicate_polling_callback_is_effective_only_once() -> None:
    notification_sent = asyncio.Event()

    async def notify(_request) -> None:
        notification_sent.set()

    hitl = HITLGuard(notify_fn=notify, timeout=1)
    polling, sender, _session = _make_transport(hitl)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-duplicate"))
    update = _callback_update(5, "approve", "req-duplicate")

    await _wait_for_notification(notification_sent)
    await polling._process_raw_update(update)
    await polling._process_raw_update(update)

    assert await pending is Decision.APPROVED
    sender.edit_message.assert_awaited_once()
    sender.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_does_not_allow_late_approval() -> None:
    notification_sent = asyncio.Event()

    async def notify(_request) -> None:
        notification_sent.set()

    hitl = HITLGuard(notify_fn=notify, timeout=0.01)
    polling, sender, _session = _make_transport(hitl)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-timeout"))

    await _wait_for_notification(notification_sent)
    assert await pending is Decision.DENIED

    await polling._process_raw_update(_callback_update(6, "approve", "req-timeout"))

    sender.edit_message.assert_not_awaited()
    sender.send.assert_awaited_once_with(123, "Error: Request 'req-timeout' not found or already processed.")


@pytest.mark.asyncio
async def test_polling_preserves_injected_session() -> None:
    hitl = HITLGuard()
    polling, _sender, session = _make_transport(hitl)
    polling._running = True

    async def stop_after_poll() -> None:
        polling._running = False

    polling._poll_once = AsyncMock(side_effect=stop_after_poll)
    await polling._run_loop()

    assert polling._session is session
    assert polling._owns_session is False
    session.close.assert_not_called()


@pytest.mark.asyncio
async def test_polling_owned_session_is_closed_by_owner() -> None:
    handler = MagicMock()
    gateway = MagicMock()
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=handler,
        gateway=gateway,
        session=None,
        interval=0,
        authorized_chat_id=123,
    )
    owned_session = MagicMock(spec=aiohttp.ClientSession)
    owned_session.closed = False
    owned_session.close = AsyncMock()
    polling._running = True

    async def stop_after_poll() -> None:
        polling._running = False

    polling._poll_once = AsyncMock(side_effect=stop_after_poll)
    with (
        patch("sky_claw.app.comms.telegram_polling.aiohttp.ClientSession", return_value=owned_session),
        patch("sky_claw.app.security.network_gateway.GatewayTCPConnector", return_value=MagicMock()),
    ):
        await polling._run_loop()

    owned_session.close.assert_awaited_once()
    assert polling._session is None
    assert polling._owns_session is False


@pytest.mark.asyncio
async def test_polling_stop_awaits_task_and_is_idempotent() -> None:
    cleanup_complete = asyncio.Event()
    task_started = asyncio.Event()

    async def polling_task() -> None:
        task_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_complete.set()

    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=MagicMock(spec=aiohttp.ClientSession),
        authorized_chat_id=123,
    )
    polling._running = True
    polling._polling_task = asyncio.create_task(polling_task())
    await task_started.wait()

    await polling.stop()
    await polling.stop()

    assert cleanup_complete.is_set()
    assert polling._polling_task is None
    assert polling._running is False


@pytest.mark.asyncio
async def test_polling_stop_propagates_unexpected_task_failure_and_cleans_reference() -> None:
    async def failing_task() -> None:
        raise RuntimeError("polling failed")

    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=MagicMock(spec=aiohttp.ClientSession),
        authorized_chat_id=123,
    )
    polling._running = True
    polling._polling_task = asyncio.create_task(failing_task())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="polling failed"):
        await polling.stop()

    assert polling._polling_task is None
    assert polling._running is False


@pytest.mark.asyncio
async def test_polling_without_gateway_fails_closed_and_cleans_state() -> None:
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=None,
        session=None,
        interval=0,
        authorized_chat_id=123,
    )
    polling._running = True

    with pytest.raises(RuntimeError, match="NetworkGateway"):
        await polling._run_loop()

    assert polling._running is False
    assert polling._session is None
    assert polling._owns_session is False


@pytest.mark.asyncio
async def test_polling_failure_during_poll_closes_owned_session() -> None:
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=None,
        interval=0,
        authorized_chat_id=123,
    )
    owned_session = MagicMock(spec=aiohttp.ClientSession)
    owned_session.closed = False
    owned_session.close = AsyncMock()

    async def fail_and_stop() -> None:
        polling._running = False
        raise RuntimeError("poll once failed")

    polling._running = True
    polling._poll_once = AsyncMock(side_effect=fail_and_stop)
    with (
        patch("sky_claw.app.comms.telegram_polling.aiohttp.ClientSession", return_value=owned_session),
        patch("sky_claw.app.security.network_gateway.GatewayTCPConnector", return_value=MagicMock()),
    ):
        await polling._run_loop()

    owned_session.close.assert_awaited_once()
    assert polling._session is None
    assert polling._running is False
