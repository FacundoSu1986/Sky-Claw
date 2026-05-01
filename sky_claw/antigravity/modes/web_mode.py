from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager
from sky_claw.antigravity.web.app import WebApp
from sky_claw.local.local_config import load as load_local_config

if TYPE_CHECKING:
    from sky_claw.app_context import AppContext

logger = logging.getLogger(__name__)


async def _run_web(ctx: AppContext, port: int) -> None:
    assert ctx.session is not None
    local_cfg = load_local_config(ctx.config_path)
    already_configured = not local_cfg.first_run and bool(local_cfg.get_api_key())
    if already_configured:
        await ctx.start_full()

    auth_manager = AuthTokenManager()
    auth_manager.generate()
    await auth_manager.start_rotation()

    web_app = WebApp(
        router=ctx.router,
        session=ctx.session,
        config_path=ctx.config_path,
        tools_installer=ctx.tools_installer,
        on_setup_complete=_make_setup_callback(ctx),
        auth_manager=auth_manager,
    )
    runner = web.AppRunner(web_app.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    url = f"http://localhost:{port}"
    logger.info("Sky-Claw Web UI: %s", url)
    import webbrowser

    webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await auth_manager.stop_rotation()
        await runner.cleanup()


def _make_setup_callback(ctx: AppContext):
    async def _on_setup_complete(web_app: WebApp) -> None:
        await ctx.start_full()
        web_app._router = ctx.router
        web_app._tools_installer = ctx.tools_installer

    return _on_setup_complete
