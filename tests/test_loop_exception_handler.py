"""M-5: unhandled event-loop exceptions (e.g. from fire-and-forget tasks) must be
routed to the structured logger, not asyncio's default stderr handler, so they are
captured by the JSON log + redaction pipeline for root-cause analysis.
"""

from __future__ import annotations

import asyncio
import logging

from sky_claw.__main__ import _install_loop_exception_handler


async def test_loop_exception_handler_logs_via_logger(caplog):
    _install_loop_exception_handler()
    loop = asyncio.get_running_loop()
    with caplog.at_level(logging.ERROR, logger="sky_claw"):
        loop.call_exception_handler({"message": "boom", "exception": ValueError("kaboom")})
    assert "Unhandled event-loop exception" in caplog.text
    assert "kaboom" in caplog.text  # exception rendered via exc_info
