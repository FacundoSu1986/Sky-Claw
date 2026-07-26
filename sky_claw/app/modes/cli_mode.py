from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from typing import TYPE_CHECKING

from sky_claw.logging_config import correlation_id_var

if TYPE_CHECKING:
    from sky_claw.app_context import AppContext

logger = logging.getLogger(__name__)

# P1 §3.2 — bounded LLM call. A hung provider must not freeze the CLI;
# 300 s is the documented ceiling for any single chat turn.
_CHAT_TIMEOUT_SECONDS: float = 300.0


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
            response = await asyncio.wait_for(
                ctx.router.chat(text, ctx.session, chat_id=chat_id),
                timeout=_CHAT_TIMEOUT_SECONDS,
            )
            logger.info("sky-claw> %s", response)
        except TimeoutError:
            logger.error(
                "[error] chat timed out after %.0fs — provider may be unresponsive",
                _CHAT_TIMEOUT_SECONDS,
            )
        except RuntimeError as exc:
            logger.error("[error] %s", exc)


async def _run_oneshot(ctx: AppContext, command: str) -> None:
    assert ctx.router and ctx.session
    correlation_id_var.set(str(uuid.uuid4()))
    try:
        response = await asyncio.wait_for(
            ctx.router.chat(command, ctx.session, chat_id="oneshot"),
            timeout=_CHAT_TIMEOUT_SECONDS,
        )
        logger.info("%s", response)
    except TimeoutError:
        logger.error(
            "[error] chat timed out after %.0fs — provider may be unresponsive",
            _CHAT_TIMEOUT_SECONDS,
        )
        sys.exit(1)
    except RuntimeError as exc:
        logger.error("[error] %s", exc)
        sys.exit(1)
