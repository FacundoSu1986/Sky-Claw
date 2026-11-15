from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from sky_claw.app.comms.golden_master_hitl import asegurar_coordinador_golden_master
from sky_claw.app.comms.telegram import TelegramWebhook
from sky_claw.app.comms.telegram_polling import TelegramPolling

if TYPE_CHECKING:
    from sky_claw.app_context import AppContext

logger = logging.getLogger(__name__)


async def _run_telegram(ctx: AppContext, host: str, port: int) -> None:
    missing_components = []
    if ctx.router is None:
        missing_components.append("router")
    if ctx.session is None:
        missing_components.append("session")
    if ctx.network.gateway is None:
        missing_components.append("network.gateway")
    if missing_components:
        raise RuntimeError("Missing required Telegram runtime components: " + ", ".join(missing_components))
    if ctx.sender is None:
        logger.error("TELEGRAM_BOT_TOKEN required.")
        sys.exit(1)
    from sky_claw.app_context import resolver_operador_telegram_desde_contexto

    operador_efectivo = resolver_operador_telegram_desde_contexto(ctx)
    golden_master_coordinator = await asegurar_coordinador_golden_master(
        ctx,
        sender=ctx.sender,
        authorized_user_id=operador_efectivo,
    )
    # M-4 / CR-03: pasar la única identidad efectiva resuelta al coordinador,
    # TelegramWebhook y TelegramPolling sin sobrescribirla con ctx._args.operator_chat_id.
    webhook_handler = TelegramWebhook(
        router=ctx.router,
        sender=ctx.sender,
        session=ctx.session,
        hitl=ctx.hitl,
        authorized_user_id=operador_efectivo,
        hitl_registry=ctx.telegram_hitl_registry,
        golden_master_coordinator=golden_master_coordinator,
    )
    polling = TelegramPolling(
        token=ctx.sender._token,
        webhook_handler=webhook_handler,
        gateway=ctx.network.gateway,
        session=ctx.session,
        authorized_chat_id=operador_efectivo,
    )
    await polling.start()
    logger.info("Telegram polling started. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await polling.stop()
        await golden_master_coordinator.cerrar()
