from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from sky_claw.antigravity.comms.telegram import TelegramWebhook
from sky_claw.antigravity.comms.telegram_polling import TelegramPolling

if TYPE_CHECKING:
    from sky_claw.app_context import AppContext

logger = logging.getLogger(__name__)


async def _run_telegram(ctx: AppContext, host: str, port: int) -> None:
    assert ctx.router and ctx.session and ctx.gateway
    if ctx.sender is None:
        logger.error("TELEGRAM_BOT_TOKEN required.")
        sys.exit(1)
    webhook_handler = TelegramWebhook(router=ctx.router, sender=ctx.sender, session=ctx.session, hitl=ctx.hitl)
    polling = TelegramPolling(
        token=ctx.sender._token,
        webhook_handler=webhook_handler,
        gateway=ctx.gateway,
        session=ctx.session,
        authorized_chat_id=ctx._args.operator_chat_id,
    )
    await polling.start()
    logger.info("Telegram polling started. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await polling.stop()
