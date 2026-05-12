"""GUI mode entry point — boots the NiceGUI Forge interface."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sky_claw.antigravity.gui.gui_event_adapter import EventType, SkyClawEvent
from sky_claw.antigravity.gui.gui_event_adapter import event_bus as gui_event_bus
from sky_claw.antigravity.gui.sky_claw_gui import get_state, set_runtime_context, setup_app
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.app_context import AppContext, _resolve_config_path_static, start_full
from sky_claw.logging_config import correlation_id_var

logger = logging.getLogger("sky_claw")


async def _gui_logic_loop(ctx: AppContext) -> None:
    """Process chat messages from the GUI in a background task.

    Reads ``ctx.logic_queue`` items placed by callers that prefer the
    direct-router path (e.g. integration scripts).  The modern GUI chat
    path uses ChatController → EventBus instead; this loop is kept as the
    offline-fallback processor so both paths co-exist.
    """
    consecutive_errors = 0
    while True:
        try:
            item = await asyncio.to_thread(ctx.logic_queue.get)
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                logger.warning("Malformed item in logic_queue: %r", item)
                continue
            if item[0] == "chat":
                text = item[1]
                if not ctx.router:
                    gui_event_bus.publish(
                        SkyClawEvent(
                            type=EventType.LLM_RESPONSE,
                            data={"response": "⚠️ Router no inicializado. Completá el setup primero."},
                            source="logic_loop",
                        )
                    )
                    continue
                correlation_id_var.set(str(uuid.uuid4()))
                try:
                    response = await ctx.router.chat(text, ctx.session, chat_id="gui-session")
                    gui_event_bus.publish(
                        SkyClawEvent(type=EventType.LLM_RESPONSE, data={"response": response}, source="logic_loop")
                    )
                    consecutive_errors = 0
                except Exception as e:
                    logger.exception("Logic error in chat: %s", e)
                    gui_event_bus.publish(
                        SkyClawEvent(
                            type=EventType.LLM_RESPONSE,
                            data={"response": f"⚠️ Error: {type(e).__name__}: {e}"},
                            source="logic_loop",
                        )
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(2**consecutive_errors, 30)
            logger.exception("GUI logic loop error (backoff=%ds): %s", backoff, e)
            await asyncio.sleep(backoff)


async def _gui_mod_update_loop(ctx: AppContext) -> None:
    """Periodically refresh active-mod count and mod list in the reactive store."""
    from sky_claw.antigravity.gui.state import get_store

    while True:
        try:
            if ctx.registry:
                mods_dicts = await ctx.registry.search_mods("")
                get_state().active_mods.set(len(mods_dicts))
                get_store().set("mods_list", mods_dicts)
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error updating modlist in GUI: %s", exc)
            await asyncio.sleep(5)


def run_gui_mode(args) -> None:
    from nicegui import app, ui

    config_path = _resolve_config_path_static(args)

    setup_app()

    _runtime: dict[str, object] = {}

    async def _bootstrap() -> None:
        """Spin up AppContext + supervisor + background loops, then publish."""
        if "ctx" in _runtime:
            return
        ctx = await start_full(args)
        _runtime["ctx"] = ctx

        supervisor = SupervisorAgent()
        _runtime["supervisor"] = supervisor
        ctx._track_task(supervisor.start(), name="supervisor-daemon")
        ctx._track_task(_gui_logic_loop(ctx), name="gui-logic-loop")
        ctx._track_task(_gui_mod_update_loop(ctx), name="gui-mod-update")

        # Start the aiohttp sub-server for /api/chat + Operations Hub WS.
        # Runs on port 8765 so external scripts and AgentCommunicationClient
        # can reach the chat endpoint without conflicting with NiceGUI (8080).
        from aiohttp import web as aiohttp_web

        from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager
        from sky_claw.antigravity.web.app import WebApp

        auth_manager = AuthTokenManager()
        await asyncio.to_thread(auth_manager.generate)
        await auth_manager.start_rotation()
        _runtime["auth_manager"] = auth_manager

        web_app_inst = WebApp(router=ctx.router, session=ctx.session, auth_manager=auth_manager)
        aiohttp_app = web_app_inst.create_app()
        runner = aiohttp_web.AppRunner(aiohttp_app)
        await runner.setup()
        site = aiohttp_web.TCPSite(runner, "127.0.0.1", 8765)
        await site.start()
        _runtime["aiohttp_runner"] = runner
        logger.info("aiohttp API server started on 127.0.0.1:8765 (/api/chat)")

        set_runtime_context(app_context=ctx, config_path=config_path, supervisor=supervisor)
        logger.info("Sky-Claw Daemon Core initialised; runtime context published.")

    app.on_startup(_bootstrap)

    async def _shutdown() -> None:
        auth_manager = _runtime.get("auth_manager")
        if auth_manager is not None:
            await auth_manager.stop_rotation()
            auth_manager.revoke()
        runner = _runtime.get("aiohttp_runner")
        if runner is not None:
            await runner.cleanup()
        ctx = _runtime.get("ctx")
        if ctx is not None:
            await ctx.stop()  # type: ignore[attr-defined]

    app.on_shutdown(_shutdown)

    ui.run(
        title="Sky-Claw",
        dark=True,
        show=True,
        reload=False,
        port=8080,
        host="127.0.0.1",
    )
