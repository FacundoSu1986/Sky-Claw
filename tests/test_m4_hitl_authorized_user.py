"""M-4: TelegramWebhook debe recibir authorized_user_id en --mode telegram y en
el hot-reload; sin él, _validate_sender falla cerrado y bloquea todo HITL.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestTelegramModeAuthorizedUser:
    async def test_run_telegram_pasa_authorized_user_id(self) -> None:
        from sky_claw.antigravity.modes import telegram_mode

        ctx = MagicMock()
        ctx.router = MagicMock()
        ctx.session = MagicMock()
        ctx.network.gateway = MagicMock()
        ctx.sender = MagicMock()
        ctx.sender._token = "123:ABC"
        ctx.hitl = MagicMock()
        ctx._args.operator_chat_id = 987654

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()
        fake_polling.stop = AsyncMock()

        with (
            patch.object(telegram_mode, "TelegramWebhook") as mock_webhook,
            patch.object(telegram_mode, "TelegramPolling", return_value=fake_polling),
        ):
            task = asyncio.create_task(telegram_mode._run_telegram(ctx, "127.0.0.1", 0))
            await asyncio.sleep(0.05)  # dejar que construya el webhook y arranque
            task.cancel()
            # _run_telegram captura CancelledError internamente y retorna limpio.
            await task

        assert mock_webhook.call_args.kwargs["authorized_user_id"] == 987654


class TestReloadTelegramAuthorizedUser:
    async def test_reload_pasa_authorized_user_id_como_int(self) -> None:
        from sky_claw.antigravity.comms.frontend_bridge import FrontendBridge

        bridge = FrontendBridge.__new__(FrontendBridge)
        ctx = MagicMock()
        ctx.polling = None
        ctx.router = MagicMock()
        ctx.session = MagicMock()
        ctx.network.gateway = MagicMock()
        ctx.hitl = MagicMock()
        bridge.ctx = ctx  # type: ignore[attr-defined]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()

        with (
            patch("sky_claw.antigravity.comms.telegram.TelegramWebhook") as mock_webhook,
            patch("sky_claw.antigravity.comms.telegram_polling.TelegramPolling", return_value=fake_polling),
            patch("sky_claw.antigravity.comms.telegram_sender.TelegramSender"),
        ):
            ok = await bridge._reload_telegram(token="123:ABC", chat_id="55501")

        assert ok is True
        # chat_id (str) debe convertirse a int para _validate_sender.
        assert mock_webhook.call_args.kwargs["authorized_user_id"] == 55501

    async def test_reload_sin_chat_id_usa_none(self) -> None:
        from sky_claw.antigravity.comms.frontend_bridge import FrontendBridge

        bridge = FrontendBridge.__new__(FrontendBridge)
        ctx = MagicMock()
        ctx.polling = None
        bridge.ctx = ctx  # type: ignore[attr-defined]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()

        with (
            patch("sky_claw.antigravity.comms.telegram.TelegramWebhook") as mock_webhook,
            patch("sky_claw.antigravity.comms.telegram_polling.TelegramPolling", return_value=fake_polling),
            patch("sky_claw.antigravity.comms.telegram_sender.TelegramSender"),
        ):
            ok = await bridge._reload_telegram(token="123:ABC", chat_id="")

        assert ok is True
        assert mock_webhook.call_args.kwargs["authorized_user_id"] is None
