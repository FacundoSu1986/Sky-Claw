"""Pruebas enfocadas del dispatcher común de callbacks de Telegram."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.telegram import TelegramWebhook
from sky_claw.app.security.network_gateway import EgressViolationError


def _crear_webhook() -> tuple[TelegramWebhook, AsyncMock, MagicMock]:
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
async def test_accion_callback_desconocida_no_resuelve_la_solicitud() -> None:
    webhook, respond, sender = _crear_webhook()

    await webhook.process_update(_crear_update("hitl:escalate:req-1"))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid HITL action")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_malformado_no_resuelve_la_solicitud() -> None:
    webhook, respond, sender = _crear_webhook()

    await webhook.process_update(_crear_update("hitl:approve"))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Invalid callback")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_no_autorizado_no_resuelve_la_solicitud() -> None:
    webhook, respond, sender = _crear_webhook()

    await webhook.process_update(_crear_update("hitl:approve:req-1", user_id=999, chat_id=999))

    respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Unauthorized")
    sender.edit_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "approved"),
    [("hitl:approve:req-approve", True), ("hitl:deny:req-deny", False)],
)
async def test_callback_valido_mapea_solo_acciones_permitidas(
    data: str,
    approved: bool,
) -> None:
    webhook, respond, sender = _crear_webhook()

    await webhook.process_update(_crear_update(data))

    respond.assert_awaited_once_with(data.rsplit(":", 1)[1], approved)
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text=None)
    sender.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallo_de_transporte_del_ack_no_revierte_la_decision() -> None:
    webhook, respond, sender = _crear_webhook()
    sender.answer_callback_query = AsyncMock(side_effect=EgressViolationError("egress bloqueado"))

    await webhook.process_update(_crear_update("hitl:approve:req-1"))

    respond.assert_awaited_once_with("req-1", True)
    sender.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallo_inesperado_del_ack_se_propaga() -> None:
    webhook, respond, sender = _crear_webhook()
    sender.answer_callback_query = AsyncMock(side_effect=RuntimeError("fallo de programación"))

    with pytest.raises(RuntimeError, match="fallo de programación"):
        await webhook.process_update(_crear_update("hitl:approve:req-1"))

    respond.assert_awaited_once_with("req-1", True)
    sender.edit_message.assert_not_awaited()
