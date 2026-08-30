from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.telegram import (
    TelegramHITLMessageRegistry,
    TelegramWebhook,
    build_hitl_callback_data,
)
from sky_claw.app.security.network_gateway import EgressViolationError


async def _crear_webhook() -> tuple[TelegramWebhook, AsyncMock, MagicMock, TelegramHITLMessageRegistry]:
    hitl = MagicMock()
    hitl.respond = AsyncMock(return_value=True)
    sender = MagicMock()
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock()
    sender.send = AsyncMock()
    registry = TelegramHITLMessageRegistry()
    webhook = TelegramWebhook(
        router=MagicMock(),
        sender=sender,
        session=MagicMock(spec=aiohttp.ClientSession),
        hitl=hitl,
        authorized_user_id=123,
        hitl_registry=registry,
    )
    return webhook, hitl.respond, sender, registry


async def _registrar(
    registry: TelegramHITLMessageRegistry,
    request_id: str,
    sender: MagicMock,
    *,
    message_id: int = 9,
) -> str:
    token = await registry.reserve_token(request_id)
    await registry.register(request_id, 123, message_id, sender, token=token)
    return token


def _crear_update(data: str, *, user_id: int = 123, chat_id: int = 123) -> dict:
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
async def test_callback_hitl_sin_guard_falla_de_forma_explicita() -> None:
    sender = MagicMock()
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock()
    webhook = TelegramWebhook(
        router=MagicMock(),
        sender=sender,
        session=MagicMock(spec=aiohttp.ClientSession),
        hitl=None,
        authorized_user_id=123,
    )

    await webhook.process_update(_crear_update("hitl:approve:token-1"))

    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="HITL unavailable")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_id_opaco_con_espacios_se_preserva_literalmente() -> None:
    webhook, respond, sender, registry = await _crear_webhook()
    token = await _registrar(registry, " req-1 ", sender)

    await webhook.process_update(_crear_update(build_hitl_callback_data("approve", token)))

    respond.assert_awaited_once_with(" req-1 ", True)
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text=None)
    sender.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_con_whitespace_se_rechaza_sin_resolver() -> None:
    webhook, respond, sender, _registry = await _crear_webhook()

    await webhook.process_update(_crear_update("hitl:approve:   "))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid callback")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_accion_callback_desconocida_no_resuelve_la_solicitud() -> None:
    webhook, respond, sender, _registry = await _crear_webhook()

    await webhook.process_update(_crear_update("hitl:escalate:req-1"))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid callback")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_malformado_no_resuelve_la_solicitud() -> None:
    webhook, respond, sender, _registry = await _crear_webhook()

    await webhook.process_update(_crear_update("hitl:approve"))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid callback")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_no_autorizado_no_resuelve_la_solicitud() -> None:
    webhook, respond, sender, _registry = await _crear_webhook()

    await webhook.process_update(_crear_update("hitl:approve:req-1", user_id=999, chat_id=999))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Unauthorized")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_callback_valido_mapea_solo_acciones_permitidas(approved: bool) -> None:
    webhook, respond, sender, registry = await _crear_webhook()
    request_id = "req-approve" if approved else "req-deny"
    action = "approve" if approved else "deny"
    token = await _registrar(registry, request_id, sender)

    await webhook.process_update(_crear_update(build_hitl_callback_data(action, token)))

    respond.assert_awaited_once_with(request_id, approved)
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text=None)
    sender.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallo_de_transporte_del_ack_no_revierte_la_decision() -> None:
    webhook, respond, sender, registry = await _crear_webhook()
    token = await _registrar(registry, "req-1", sender)
    sender.answer_callback_query = AsyncMock(side_effect=EgressViolationError("egress bloqueado"))

    await webhook.process_update(_crear_update(build_hitl_callback_data("approve", token)))

    respond.assert_awaited_once_with("req-1", True)
    sender.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallo_inesperado_del_ack_se_propaga() -> None:
    webhook, respond, sender, registry = await _crear_webhook()
    token = await _registrar(registry, "req-1", sender)
    sender.answer_callback_query = AsyncMock(side_effect=RuntimeError("fallo de programación"))

    with pytest.raises(RuntimeError, match="fallo de programación"):
        await webhook.process_update(_crear_update(build_hitl_callback_data("approve", token)))

    respond.assert_awaited_once_with("req-1", True)
    sender.edit_message.assert_not_awaited()
