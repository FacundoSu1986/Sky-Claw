from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.telegram import (
    TelegramHITLMessageRegistry,
    TelegramHITLRegistryError,
    TelegramHITLTokenCollisionError,
    TelegramWebhook,
    build_hitl_callback_data,
    parse_hitl_callback_data,
    register_hitl_message_cancellation_safe,
    terminalize_hitl_message,
)
from sky_claw.app.comms.telegram_sender import TelegramMessage
from sky_claw.app.security.hitl import Decision, HITLGuard, HITLRequest


async def _build_tokenized_flow(
    *,
    request_id: str,
    timeout: float = 5,
    token_factory=None,
) -> tuple[HITLGuard, TelegramWebhook, TelegramHITLMessageRegistry, MagicMock, asyncio.Event, dict[str, str]]:
    registry = TelegramHITLMessageRegistry(token_factory=token_factory)
    sender = MagicMock()
    sender.send = AsyncMock(return_value=TelegramMessage(chat_id=123, message_id=77))
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock()
    registered = asyncio.Event()
    tokens: dict[str, str] = {}

    async def notify(req: HITLRequest) -> None:
        token = await registry.reserve_token(req.request_id)
        tokens[req.request_id] = token
        callback_data = build_hitl_callback_data("approve", token)
        assert len(callback_data.encode("utf-8")) <= 64
        message = await sender.send(123, req.reason, reply_markup={"callback_data": callback_data})
        await register_hitl_message_cancellation_safe(
            registry,
            req.request_id,
            message,
            sender,
            token=token,
        )
        registered.set()

    async def terminal(req: HITLRequest, decision: Decision) -> None:
        await terminalize_hitl_message(registry, req.request_id, decision)

    async def cancel(req: HITLRequest) -> None:
        await terminalize_hitl_message(registry, req.request_id, None)

    hitl = HITLGuard(notify_fn=notify, timeout=timeout, on_terminal=terminal, on_cancel=cancel)
    webhook = TelegramWebhook(
        router=MagicMock(),
        sender=sender,
        session=MagicMock(spec=aiohttp.ClientSession),
        hitl=hitl,
        authorized_user_id=123,
        hitl_registry=registry,
    )
    return hitl, webhook, registry, sender, registered, tokens


def _callback(update_id: int, action: str, token: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": 123},
            "message": {"chat": {"id": 123}, "message_id": 77},
            "data": build_hitl_callback_data(action, token),
        },
    }


@pytest.mark.asyncio
async def test_callback_builder_no_contiene_request_id_y_respeta_limite_utf8() -> None:
    request_id = "  abc:def:" + "非常に長い" * 250 + "  "
    registry = TelegramHITLMessageRegistry()
    token = await registry.reserve_token(request_id)

    callback_data = build_hitl_callback_data("approve", token)

    assert request_id not in callback_data
    assert 1 <= len(callback_data.encode("utf-8")) <= 64
    assert parse_hitl_callback_data(callback_data) == ("approve", token)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_callback_builder_solo_acepta_acciones_y_tokens_canónicos(action: str) -> None:
    registry = TelegramHITLMessageRegistry()
    token = await registry.reserve_token("request")

    callback_data = build_hitl_callback_data(action, token)

    assert callback_data.startswith(f"hitl:{action}:")
    assert ":" not in token
    assert token.isascii()
    with pytest.raises(ValueError):
        build_hitl_callback_data("escalate", token)
    assert parse_hitl_callback_data("hitl:approve:bad:token") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    [("approve", Decision.APPROVED), ("deny", Decision.DENIED)],
)
async def test_callback_token_resuelve_id_opaco_exactamente(action: str, expected: Decision) -> None:
    request_id = "  install:mod:非常に長いID:etc  "
    hitl, webhook, registry, _sender, registered, tokens = await _build_tokenized_flow(request_id=request_id)
    pending = asyncio.create_task(hitl.request_approval(request_id=request_id, reason="test"))
    await registered.wait()

    await webhook.process_update(_callback(1, action, tokens[request_id]))

    assert await pending is expected
    assert await registry.resolve_token(tokens[request_id]) is None


@pytest.mark.asyncio
async def test_callback_desconocido_no_llama_respond_ni_afecta_pending() -> None:
    hitl = MagicMock()
    hitl.respond = AsyncMock()
    sender = MagicMock()
    sender.answer_callback_query = AsyncMock()
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

    await webhook.process_update(_callback(1, "approve", "no-existe"))

    hitl.respond.assert_not_awaited()
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Unknown or expired HITL request")
    sender.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_stale_no_puede_decidir_nueva_request_con_mismo_id() -> None:
    request_id = "reused-id"
    hitl, webhook, registry, sender, registered, tokens = await _build_tokenized_flow(request_id=request_id)

    first = asyncio.create_task(hitl.request_approval(request_id=request_id, reason="first"))
    await registered.wait()
    token_a = tokens[request_id]
    await hitl.respond(request_id, approved=False)
    assert await first is Decision.DENIED

    registered.clear()
    second = asyncio.create_task(hitl.request_approval(request_id=request_id, reason="second"))
    await registered.wait()
    token_b = tokens[request_id]
    assert token_b != token_a

    await webhook.process_update(_callback(2, "approve", token_a))
    assert not second.done()
    assert await registry.resolve_token(token_b) == request_id

    await webhook.process_update(_callback(3, "approve", token_b))
    assert await second is Decision.APPROVED
    assert sender.edit_message.await_count == 2


@pytest.mark.asyncio
async def test_approve_deny_concurrentes_dejan_ganar_una_sola_decision_terminal() -> None:
    request_id = "race-id"
    hitl, webhook, registry, sender, registered, tokens = await _build_tokenized_flow(request_id=request_id)
    pending = asyncio.create_task(hitl.request_approval(request_id=request_id, reason="race"))
    await registered.wait()
    token = tokens[request_id]

    await asyncio.gather(
        webhook.process_update(_callback(1, "approve", token)),
        webhook.process_update(_callback(2, "deny", token)),
    )

    assert await pending in {Decision.APPROVED, Decision.DENIED}
    assert await registry.resolve_token(token) is None
    assert sender.edit_message.await_count == 1


@pytest.mark.asyncio
async def test_timeout_elimina_token_y_callback_tardio_es_stale() -> None:
    request_id = "timeout-id"
    hitl, webhook, registry, sender, registered, tokens = await _build_tokenized_flow(
        request_id=request_id,
        timeout=0,
    )
    decision = await hitl.request_approval(request_id=request_id, reason="timeout")

    assert decision is Decision.TIMEOUT
    assert await registry.resolve_token(tokens[request_id]) is None
    await webhook.process_update(_callback(1, "approve", tokens[request_id]))
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text="Unknown or expired HITL request")


@pytest.mark.asyncio
async def test_cancelacion_elimina_token_y_preserva_cancelled_error() -> None:
    request_id = "cancel-id"
    hitl, _webhook, registry, _sender, registered, tokens = await _build_tokenized_flow(request_id=request_id)
    pending = asyncio.create_task(hitl.request_approval(request_id=request_id, reason="cancel"))
    await registered.wait()

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert await registry.resolve_token(tokens[request_id]) is None


@pytest.mark.asyncio
async def test_fallo_de_registro_libera_token_reservado() -> None:
    class FailingRegistry(TelegramHITLMessageRegistry):
        async def register(self, *args, **kwargs) -> None:
            raise TelegramHITLRegistryError("registration failure")

    registry = FailingRegistry()
    token = await registry.reserve_token("request")
    sender = MagicMock()
    message = TelegramMessage(chat_id=123, message_id=77)

    with pytest.raises(TelegramHITLRegistryError):
        await register_hitl_message_cancellation_safe(
            registry,
            "request",
            message,
            sender,
            token=token,
        )

    assert await registry.resolve_token(token) is None


@pytest.mark.asyncio
async def test_capacity_no_expulsa_activos_y_se_recupera_tras_terminalizacion() -> None:
    registry = TelegramHITLMessageRegistry(max_entries=1)
    token_a = await registry.reserve_token("A")

    with pytest.raises(TelegramHITLRegistryError):
        await registry.reserve_token("B")

    assert await registry.resolve_token(token_a) == "A"
    assert await registry.release_token(token_a) is True
    token_b = await registry.reserve_token("B")
    assert token_b != token_a


@pytest.mark.asyncio
async def test_colision_de_token_no_sobrescribe_y_tiene_limite() -> None:
    produced = iter(["same", "same", "unique"])
    registry = TelegramHITLMessageRegistry(token_factory=lambda: next(produced), max_token_attempts=3)
    token_a = await registry.reserve_token("A")
    token_b = await registry.reserve_token("B")

    assert token_a == "same"
    assert token_b == "unique"
    assert await registry.resolve_token(token_a) == "A"
    assert await registry.resolve_token(token_b) == "B"


@pytest.mark.asyncio
async def test_colision_agotada_falla_cerrado_y_preserva_activo() -> None:
    registry = TelegramHITLMessageRegistry(token_factory=lambda: "same", max_token_attempts=2)
    token_a = await registry.reserve_token("A")

    with pytest.raises(TelegramHITLTokenCollisionError):
        await registry.reserve_token("B")

    assert await registry.resolve_token(token_a) == "A"


@pytest.mark.asyncio
async def test_token_retirado_no_se_reutiliza_en_request_secuencial() -> None:
    produced = iter(["same", "same", "fresh"])
    registry = TelegramHITLMessageRegistry(token_factory=lambda: next(produced), max_token_attempts=2)

    token_a = await registry.reserve_token("A")
    assert await registry.release_token(token_a) is True
    token_b = await registry.reserve_token("B")

    assert token_b == "fresh"
    assert await registry.resolve_token(token_a) is None


def test_parser_textual_preserva_whitespace_literal() -> None:
    from sky_claw.app.comms.telegram import _parse_hitl_command

    assert _parse_hitl_command("/approve  abc  ") == (True, " abc  ")
    assert _parse_hitl_command("/deny  abc  ") == (False, " abc  ")
    assert _parse_hitl_command("/approve   ") is None
    assert _parse_hitl_command("/approve") is None
