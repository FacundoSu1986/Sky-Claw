"""Pruebas TDD del lifecycle terminal HITL de Telegram (Slice C1)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.telegram import TelegramHITLMessageRegistry, TelegramWebhook
from sky_claw.app.comms.telegram_sender import TelegramMessage
from sky_claw.app.security.hitl import Decision, HITLGuard, HITLRequest


async def _build_lifecycle(
    *,
    timeout: float = 5,
    edit_side_effect: BaseException | None = None,
) -> tuple[HITLGuard, TelegramWebhook, TelegramHITLMessageRegistry, MagicMock, asyncio.Event]:
    registry = TelegramHITLMessageRegistry()
    sender = MagicMock()
    sender.send = AsyncMock()
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock(side_effect=edit_side_effect)
    registered = asyncio.Event()
    webhook_box: list[TelegramWebhook] = []

    async def notify(req: HITLRequest) -> None:
        await registry.register(req.request_id, chat_id=123, message_id=77)
        registered.set()

    async def terminal(req: HITLRequest, decision: Decision) -> None:
        await webhook_box[0].terminalize_hitl(req.request_id, decision)

    hitl = HITLGuard(notify_fn=notify, timeout=timeout, on_terminal=terminal)
    webhook = TelegramWebhook(
        router=MagicMock(),
        sender=sender,
        session=MagicMock(spec=aiohttp.ClientSession),
        hitl=hitl,
        authorized_user_id=123,
        hitl_registry=registry,
    )
    webhook_box.append(webhook)
    return hitl, webhook, registry, sender, registered


def _callback(update_id: int, action: str, request_id: str, message_id: int = 77) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": 123},
            "message": {"chat": {"id": 123}, "message_id": message_id},
            "data": f"hitl:{action}:{request_id}",
        },
    }


@pytest.mark.asyncio
async def test_registry_es_concurrent_safe_y_limpia_al_terminalizar() -> None:
    hitl, _webhook, registry, _sender, registered = await _build_lifecycle()

    pending = asyncio.create_task(hitl.request_approval(request_id="req-clean", reason="test"))
    await registered.wait()
    assert await registry.get("req-clean") is not None

    assert await hitl.respond("req-clean", approved=True) is True
    assert await pending is Decision.APPROVED
    assert await registry.get("req-clean") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "decision", "text"),
    [
        ("approve", Decision.APPROVED, "✅ Solicitud aprobada"),
        ("deny", Decision.DENIED, "❌ Solicitud denegada"),
    ],
)
async def test_callback_ganador_terminaliza_el_mensaje_correcto(
    action: str,
    decision: Decision,
    text: str,
) -> None:
    hitl, webhook, registry, sender, registered = await _build_lifecycle()

    pending = asyncio.create_task(hitl.request_approval(request_id="req-inline", reason="test"))
    await registered.wait()
    await webhook.process_update(_callback(1, action, "req-inline"))

    assert await pending is decision
    sender.edit_message.assert_awaited_once_with(123, 77, text, reply_markup=None)
    assert await registry.get("req-inline") is None


@pytest.mark.asyncio
async def test_timeout_terminaliza_como_expirado_y_bloquea_callback_tardio() -> None:
    hitl, webhook, registry, sender, registered = await _build_lifecycle(timeout=0)

    decision = await hitl.request_approval(request_id="req-timeout", reason="test")
    assert decision is Decision.TIMEOUT
    sender.edit_message.assert_awaited_once_with(
        123,
        77,
        "⌛ Solicitud expirada",
        reply_markup=None,
    )
    assert await registry.get("req-timeout") is None

    await webhook.process_update(_callback(2, "approve", "req-timeout"))
    assert sender.edit_message.await_count == 1
    sender.send.assert_awaited_once_with(123, "Error: Request 'req-timeout' not found or already processed.")


@pytest.mark.asyncio
async def test_resolucion_externa_sincroniza_ui_y_callback_tardio_no_pisa_decision() -> None:
    hitl, webhook, registry, sender, registered = await _build_lifecycle()

    pending = asyncio.create_task(hitl.request_approval(request_id="req-external", reason="test"))
    await registered.wait()
    assert await hitl.respond("req-external", approved=False) is True

    assert await pending is Decision.DENIED
    sender.edit_message.assert_awaited_once_with(
        123,
        77,
        "❌ Solicitud denegada",
        reply_markup=None,
    )
    assert await registry.get("req-external") is None

    await webhook.process_update(_callback(3, "approve", "req-external"))
    assert await hitl.respond("req-external", approved=True) is False
    assert sender.edit_message.await_count == 1


@pytest.mark.asyncio
async def test_dos_hitl_concurrentes_no_cruzan_message_id() -> None:
    registry = TelegramHITLMessageRegistry()
    sender = MagicMock()
    sender.edit_message = AsyncMock()
    sender.send = AsyncMock()
    sender.answer_callback_query = AsyncMock()
    ids = {"req-a": 701, "req-b": 702}

    async def notify(req: HITLRequest) -> None:
        await registry.register(req.request_id, chat_id=123, message_id=ids[req.request_id])

    async def terminal(req: HITLRequest, decision: Decision) -> None:
        reference = await registry.pop(req.request_id)
        if reference is not None:
            await sender.edit_message(123, reference.message_id, decision.value, reply_markup=None)

    hitl = HITLGuard(notify_fn=notify, timeout=5, on_terminal=terminal)
    first = asyncio.create_task(hitl.request_approval(request_id="req-a", reason="a"))
    second = asyncio.create_task(hitl.request_approval(request_id="req-b", reason="b"))
    await asyncio.sleep(0)

    await hitl.respond("req-b", approved=True)
    await hitl.respond("req-a", approved=False)
    assert await first is Decision.DENIED
    assert await second is Decision.APPROVED
    assert sender.edit_message.await_args_list[0].args[1] == 702
    assert sender.edit_message.await_args_list[1].args[1] == 701


@pytest.mark.asyncio
async def test_fallo_de_edicion_no_cambia_decision_ni_deja_mapping() -> None:
    hitl, _webhook, registry, _sender, registered = await _build_lifecycle(
        edit_side_effect=RuntimeError("Telegram edit failed")
    )

    pending = asyncio.create_task(hitl.request_approval(request_id="req-edit-fail", reason="test"))
    await registered.wait()
    assert await hitl.respond("req-edit-fail", approved=True) is True

    assert await pending is Decision.APPROVED
    assert await registry.get("req-edit-fail") is None


@pytest.mark.asyncio
async def test_send_message_contract_captura_chat_y_message_id() -> None:
    from sky_claw.app.comms.telegram_sender import TelegramSender

    gateway = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value={"ok": True, "result": {"message_id": 909, "chat": {"id": 123}}})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    gateway.request = AsyncMock(return_value=response)

    sender = TelegramSender(
        bot_token="123:ABC",
        gateway=gateway,
        session=MagicMock(spec=aiohttp.ClientSession),
    )

    result = await sender.send(123, "prompt")

    assert result == TelegramMessage(chat_id=123, message_id=909)


@pytest.mark.asyncio
async def test_delivery_failure_no_crea_mapping_fantasma() -> None:
    registry = TelegramHITLMessageRegistry()
    sender = MagicMock()
    sender.send = AsyncMock(side_effect=OSError("Telegram down"))

    async def notify(req: HITLRequest) -> None:
        message = await sender.send(123, req.reason)
        await registry.register(req.request_id, message.chat_id, message.message_id)

    hitl = HITLGuard(notify_fn=notify, timeout=5)
    assert await hitl.request_approval(request_id="req-delivery", reason="test") is Decision.TIMEOUT
    assert await registry.get("req-delivery") is None
    sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelacion_invalida_el_prompt_y_elimina_botones() -> None:
    registry = TelegramHITLMessageRegistry()
    sender = MagicMock()
    sender.edit_message = AsyncMock()
    registered = asyncio.Event()

    async def notify(req: HITLRequest) -> None:
        await registry.register(req.request_id, 123, 808)
        registered.set()

    async def cancel(req: HITLRequest) -> None:
        reference = await registry.pop(req.request_id)
        if reference is not None:
            await sender.edit_message(
                reference.chat_id,
                reference.message_id,
                "⚠️ Solicitud no accionable",
                reply_markup=None,
            )

    guard = HITLGuard(notify_fn=notify, timeout=5, on_cancel=cancel)
    task = asyncio.create_task(guard.request_approval(request_id="req-cancel", reason="test"))
    await registered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    sender.edit_message.assert_awaited_once_with(
        123,
        808,
        "⚠️ Solicitud no accionable",
        reply_markup=None,
    )
    assert await registry.get("req-cancel") is None


@pytest.mark.asyncio
async def test_comando_textual_terminaliza_el_prompt_original() -> None:
    hitl, webhook, registry, sender, registered = await _build_lifecycle()
    pending = asyncio.create_task(hitl.request_approval(request_id="req-command", reason="test"))
    await registered.wait()

    await webhook.process_update(
        {
            "update_id": 4,
            "message": {
                "chat": {"id": 123},
                "from": {"id": 123},
                "text": "/deny req-command",
            },
        }
    )

    assert await pending is Decision.DENIED
    sender.edit_message.assert_awaited_once_with(
        123,
        77,
        "❌ Solicitud denegada",
        reply_markup=None,
    )
    sender.send.assert_awaited_once_with(123, "Request 'req-command' denied.")
    assert await registry.get("req-command") is None


@pytest.mark.asyncio
async def test_edit_message_envia_keyboard_vacio_para_remover_botones() -> None:
    from sky_claw.app.comms.telegram_sender import TelegramSender

    gateway = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value={"ok": True, "result": True})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    gateway.request = AsyncMock(return_value=response)
    sender = TelegramSender(
        bot_token="123:ABC",
        gateway=gateway,
        session=MagicMock(spec=aiohttp.ClientSession),
    )

    await sender.edit_message(123, 77, "✅ Solicitud aprobada", reply_markup=None)

    payload = gateway.request.call_args.kwargs["json"]
    assert payload["reply_markup"] == {"inline_keyboard": []}


@pytest.mark.asyncio
async def test_stalled_on_terminal_no_bloquea_respond() -> None:
    terminal_started = asyncio.Event()
    never = asyncio.Event()

    async def stalled_terminal(req: HITLRequest, decision: Decision) -> None:
        terminal_started.set()
        await never.wait()

    hitl = HITLGuard(timeout=5, on_terminal=stalled_terminal, observer_timeout=0.01)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-stalled-terminal"))
    await asyncio.sleep(0)

    responder = asyncio.create_task(hitl.respond("req-stalled-terminal", approved=True))
    await terminal_started.wait()

    assert await asyncio.wait_for(responder, timeout=0.2) is True
    assert await pending is Decision.APPROVED
    assert "req-stalled-terminal" not in hitl._pending


@pytest.mark.asyncio
async def test_timeout_con_observer_colgado_retorna_bounded() -> None:
    terminal_started = asyncio.Event()
    never = asyncio.Event()

    async def stalled_terminal(req: HITLRequest, decision: Decision) -> None:
        terminal_started.set()
        await never.wait()

    hitl = HITLGuard(timeout=0, on_terminal=stalled_terminal, observer_timeout=0.01)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-stalled-timeout"))

    assert await asyncio.wait_for(pending, timeout=0.2) is Decision.TIMEOUT
    await terminal_started.wait()
    assert "req-stalled-timeout" not in hitl._pending


@pytest.mark.asyncio
async def test_stalled_on_cancel_no_suprime_cancelled_error() -> None:
    notify_done = asyncio.Event()
    cancel_started = asyncio.Event()
    never = asyncio.Event()

    async def notify(req: HITLRequest) -> None:
        notify_done.set()

    async def stalled_cancel(req: HITLRequest) -> None:
        cancel_started.set()
        await never.wait()

    hitl = HITLGuard(
        notify_fn=notify,
        timeout=5,
        on_cancel=stalled_cancel,
        observer_timeout=0.01,
    )
    pending = asyncio.create_task(hitl.request_approval(request_id="req-stalled-cancel"))
    await notify_done.wait()

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=0.2)
    await cancel_started.wait()
    assert "req-stalled-cancel" not in hitl._pending


@pytest.mark.asyncio
async def test_send_success_cancel_before_register_terminaliza_el_mensaje() -> None:
    from sky_claw.app.comms.telegram import (
        register_hitl_message_cancellation_safe,
        terminalize_hitl_message,
    )

    class GatedRegistry(TelegramHITLMessageRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.register_started = asyncio.Event()
            self.release_register = asyncio.Event()

        async def register(self, request_id: str, chat_id: int, message_id: int) -> None:
            self.register_started.set()
            await self.release_register.wait()
            await super().register(request_id, chat_id, message_id)

    registry = GatedRegistry()
    sender = MagicMock()
    sender.send = AsyncMock(return_value=TelegramMessage(chat_id=123, message_id=777))
    sender.edit_message = AsyncMock()

    async def notify(req: HITLRequest) -> None:
        message = await sender.send(123, "prompt")
        await register_hitl_message_cancellation_safe(registry, req.request_id, message)

    async def cancel(req: HITLRequest) -> None:
        await terminalize_hitl_message(registry, sender, req.request_id, None)

    hitl = HITLGuard(notify_fn=notify, timeout=5, on_cancel=cancel, observer_timeout=0.01)
    pending = asyncio.create_task(hitl.request_approval(request_id="req-send-cancel"))
    await registry.register_started.wait()

    pending.cancel()
    registry.release_register.set()
    with pytest.raises(asyncio.CancelledError):
        await pending

    sender.edit_message.assert_awaited_once_with(
        123,
        777,
        "⚠️ Solicitud no accionable",
        reply_markup=None,
    )
    assert await registry.get("req-send-cancel") is None
