from __future__ import annotations

import argparse
import asyncio
import hashlib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.comms.telegram import TelegramWebhook, parse_hitl_callback_data
from sky_claw.app.comms.telegram_sender import TelegramMessage
from sky_claw.app.security.hitl import Decision
from sky_claw.app.security.hitl import HITLGuard as RealHITLGuard
from sky_claw.app_context import AppContext


class _StopAfterHITLError(RuntimeError):
    pass


async def _build_productive_hitl(
    tmp_path: Path,
) -> tuple[AppContext, RealHITLGuard, MagicMock, asyncio.Event]:
    args = argparse.Namespace(
        db_path=str(tmp_path / "test.db"),
        mo2_root=tmp_path / "MO2",
        staging_dir=tmp_path / "staging",
        provider="ollama",
        operator_chat_id=123,
        loot_exe=None,
        install_dir=None,
        mode="cli",
    )
    args.mo2_root.mkdir()
    args.staging_dir.mkdir()
    ctx = AppContext(args)
    ctx.config_path = tmp_path / "config.toml"
    ctx._resolve_config_path = MagicMock()
    ctx._migrate_legacy_json = MagicMock()
    ctx.lifecycle.initialize = AsyncMock()
    ctx.lifecycle.close = AsyncMock()
    ctx.network.initialize = AsyncMock()
    ctx.network.close = AsyncMock()
    ctx.network.gateway = MagicMock()
    ctx.network.session = MagicMock(spec=aiohttp.ClientSession)
    ctx.database.initialize = AsyncMock()
    ctx.database.close = AsyncMock()
    ctx.database.registry = MagicMock()
    ctx.database.registry.is_empty = AsyncMock(return_value=False)

    local_cfg = MagicMock(
        mo2_root=str(args.mo2_root),
        skyrim_path=str(tmp_path),
        llm_provider="ollama",
        ollama_api_key="",
        ollama_model="",
        llm_api_key="",
        nexus_api_key="",
        telegram_bot_token="bot-token",
        telegram_chat_id="",
        loot_exe="",
        xedit_exe="",
        pandora_exe="",
        bodyslide_exe="",
        install_dir="",
        allowed_tools=None,
    )
    sender = MagicMock()
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock()
    sent = asyncio.Event()

    async def send_message(*_args: object, **_kwargs: object) -> TelegramMessage:
        sent.set()
        return TelegramMessage(chat_id=123, message_id=77)

    sender.send = AsyncMock(side_effect=send_message)
    captured: dict[str, object] = {}

    def make_hitl(**kwargs: object) -> RealHITLGuard:
        hitl = RealHITLGuard(**kwargs)
        captured["hitl"] = hitl
        return hitl

    patches = [
        patch("sky_claw.app_context.Config", return_value=local_cfg),
        patch("sky_claw.app_context.MO2Controller"),
        patch("sky_claw.app_context.MasterlistClient"),
        patch("sky_claw.app_context.TelegramSender", return_value=sender),
        patch("sky_claw.app_context.HITLGuard", side_effect=make_hitl),
        patch("sky_claw.app_context.configure_tracing", side_effect=_StopAfterHITLError("captured")),
        patch("sky_claw.app.security.governance.GovernanceManager"),
    ]
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        with pytest.raises(_StopAfterHITLError):
            await ctx.start_full()

    hitl = captured["hitl"]
    assert isinstance(hitl, RealHITLGuard)
    return ctx, hitl, sender, sent


@pytest.mark.asyncio
async def test_id_largo_no_rompe_sendmessage_y_resuelve_id_integro(tmp_path: Path) -> None:
    ctx, hitl, sender, sent = await _build_productive_hitl(tmp_path)
    request_id = "request-" + ("á秘密/" * 1200)
    hitl_respond = AsyncMock(wraps=hitl.respond)
    hitl.respond = hitl_respond
    pending = asyncio.create_task(hitl.request_approval(request_id=request_id, reason="long id"))

    await asyncio.wait_for(sent.wait(), timeout=1)
    assert sender.send.await_count == 1
    sent_args = sender.send.await_args
    assert sent_args is not None
    visible_text = sent_args.args[1]
    assert len(visible_text.encode("utf-8")) < 4096
    assert request_id not in visible_text
    assert "sha256:" in visible_text
    assert hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12] in visible_text

    reply_markup = sent_args.kwargs["reply_markup"]
    callback_data = reply_markup["inline_keyboard"][0][0]["callback_data"]
    parsed = parse_hitl_callback_data(callback_data)
    assert parsed is not None
    action, token = parsed
    assert action == "approve"
    assert await ctx.telegram_hitl_registry.resolve_token(token) == request_id

    webhook = TelegramWebhook(
        router=MagicMock(),
        sender=sender,
        session=ctx.network.session,
        hitl=hitl,
        authorized_user_id=123,
        hitl_registry=ctx.telegram_hitl_registry,
    )
    await webhook.process_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "callback-long-id",
                "from": {"id": 123},
                "message": {"chat": {"id": 123}, "message_id": 77},
                "data": callback_data,
            },
        }
    )

    assert await pending is Decision.APPROVED
    hitl_respond.assert_awaited_once_with(request_id, True)
    assert sender.edit_message.await_count == 1
    assert await ctx.telegram_hitl_registry.resolve_token(token) is None


@pytest.mark.asyncio
async def test_fallo_real_de_sendmessage_libera_token_y_falla_cerrado(tmp_path: Path) -> None:
    ctx, hitl, sender, _sent = await _build_productive_hitl(tmp_path)
    sender.send = AsyncMock(side_effect=RuntimeError("sendMessage unavailable"))

    decision = await hitl.request_approval(request_id="request-send-failure", reason="send failure")

    assert decision is Decision.TIMEOUT
    assert len(ctx.telegram_hitl_registry) == 0
    assert await ctx.telegram_hitl_registry.token_for_request("request-send-failure") is None
