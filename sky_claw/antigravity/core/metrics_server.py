"""HTTP server exposing Prometheus /metrics behind X-Auth-Token auth.

Binds to 127.0.0.1 by default; host/port can be overridden via the env vars
``SKYCLAW_METRICS_HOST`` and ``SKYCLAW_METRICS_PORT`` or explicit arguments.
Auth is delegated to an injected validator callable, keeping this module
decoupled from any concrete token manager.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Final

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client.exposition import generate_latest

from sky_claw.antigravity.core.metrics import get_registry

logger = logging.getLogger(__name__)

_DEFAULT_HOST: Final[str] = "127.0.0.1"
_DEFAULT_PORT: Final[int] = 9100
_TOKEN_HEADER: Final[str] = "X-Auth-Token"

Validator = Callable[[str], bool]


def build_metrics_app(*, validator: Validator) -> web.Application:
    app = web.Application()

    async def _metrics_handler(request: web.Request) -> web.Response:
        token = request.headers.get(_TOKEN_HEADER, "")
        if not token or not validator(token):
            return web.Response(status=401, text="unauthorized")
        payload = generate_latest(get_registry())
        return web.Response(
            body=payload,
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    app.router.add_get("/metrics", _metrics_handler)
    return app


async def start_metrics_server(
    *,
    validator: Validator,
    host: str | None = None,
    port: int | None = None,
) -> web.AppRunner:
    bind_host = host if host is not None else os.environ.get("SKYCLAW_METRICS_HOST", _DEFAULT_HOST)
    bind_port = port if port is not None else int(os.environ.get("SKYCLAW_METRICS_PORT", _DEFAULT_PORT))

    app = build_metrics_app(validator=validator)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, bind_host, bind_port)
    await site.start()
    logger.info(
        "metrics_server_started",
        extra={"host": bind_host, "port": bind_port},
    )
    return runner


async def stop_metrics_server(runner: web.AppRunner) -> None:
    await runner.cleanup()
    logger.info("metrics_server_stopped")
