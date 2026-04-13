from __future__ import annotations
import asyncio
import logging
import sys
import uuid

from sky_claw.logging_config import correlation_id_var
from sky_claw.app_context import AppContext

logger = logging.getLogger(__name__)


async def _run_cli(ctx: AppContext) -> None:
    assert ctx.router and ctx.session
    logger.info("Sky-Claw interactive mode. Type 'exit' or 'quit' to leave.")
    chat_id = "cli-session"
    while True:
        try:
            user_input = await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            logger.info("Bye!")
            break
        text = user_input.strip()
        if not text:
            continue
        correlation_id_var.set(str(uuid.uuid4()))
        try:
            response = await ctx.router.chat(text, ctx.session, chat_id=chat_id)
            logger.info("sky-claw> %s", response)
        except RuntimeError as exc:
            logger.error("[error] %s", exc)


async def _run_oneshot(ctx: AppContext, command: str) -> None:
    assert ctx.router and ctx.session
    correlation_id_var.set(str(uuid.uuid4()))
    try:
        response = await ctx.router.chat(command, ctx.session, chat_id="oneshot")
        logger.info("%s", response)
    except RuntimeError as exc:
        logger.error("[error] %s", exc)
        sys.exit(1)
