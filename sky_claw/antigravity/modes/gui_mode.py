from __future__ import annotations

import asyncio
import logging
import uuid

from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.app_context import AppContext, _resolve_config_path_static, start_full
from sky_claw.logging_config import correlation_id_var

logger = logging.getLogger("sky_claw")


async def _gui_logic_loop(ctx: AppContext) -> None:
    """Processes chat messages from GUI in a background task."""
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
                    ctx.gui_queue.put(("error", "Router no inicializado. Completá el setup primero."))
                    continue
                correlation_id_var.set(str(uuid.uuid4()))
                try:
                    response = await ctx.router.chat(text, ctx.session, chat_id="gui-session")
                    ctx.gui_queue.put(("response", response))
                    consecutive_errors = 0
                except Exception as e:
                    logger.exception("Logic error in chat: %s", e)
                    ctx.gui_queue.put(("error", f"Error procesando comando: {type(e).__name__}: {e}"))
        except asyncio.CancelledError:
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(2**consecutive_errors, 30)
            logger.exception("GUI logic loop error (backoff=%ds): %s", backoff, e)
            await asyncio.sleep(backoff)


async def _gui_mod_update_loop(ctx: AppContext) -> None:
    """Periodically fetches modlist for the GUI."""
    while True:
        try:
            if ctx.registry:
                mods_dicts = await ctx.registry.search_mods("")
                mods = [m["name"] for m in mods_dicts]
                ctx.gui_queue.put(("modlist", mods))
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error updating modlist in GUI: %s", exc)
            await asyncio.sleep(5)


def run_gui_mode(args):
    from nicegui import app, ui

    from sky_claw.antigravity.gui.app import DashboardGUI

    config_path = _resolve_config_path_static(args)

    # Shared state — initialized once, survives page reloads
    _ctx_holder: dict[str, object] = {}

    async def _ensure_context() -> AppContext:
        """Initialize AppContext once, reuse on page reloads."""
        if "ctx" not in _ctx_holder:
            ctx = await start_full(args)
            _ctx_holder["ctx"] = ctx

            supervisor = SupervisorAgent()
            _ctx_holder["supervisor"] = supervisor
            ctx._track_task(supervisor.start(), name="supervisor-daemon")
            ctx._track_task(_gui_logic_loop(ctx), name="gui-logic-loop")
            ctx._track_task(_gui_mod_update_loop(ctx), name="gui-mod-update")
            logger.info("Sky-Claw Daemon Core inicializado en background.")
        return _ctx_holder["ctx"]

    @ui.page("/setup")
    async def setup_page():
        """Legacy: redirige al dashboard (el wizard ahora es un modal overlay)."""
        ui.navigate.to("/")

    @ui.page("/")
    async def main_page():
        # Siempre renderiza el dashboard; si no está configurado,
        # el SetupWizardModal se abre automáticamente como overlay.
        ctx = await _ensure_context()
        ctx.config_path = config_path  # Exponer para el wizard modal
        gui = DashboardGUI(ctx)
        gui.build()

        supervisor = _ctx_holder.get("supervisor")
        if supervisor and hasattr(gui, "on_ejecutar"):
            gui.on_ejecutar = supervisor.handle_execution_signal

        logger.info("Dashboard page rendered (context reused).")

    async def _shutdown():
        ctx = _ctx_holder.get("ctx")
        if ctx:
            await ctx.stop()

    app.on_shutdown(_shutdown)

    ui.run(
        title="Sky-Claw",
        dark=True,
        show=True,
        reload=False,
        port=8080,
        host="127.0.0.1",
    )
