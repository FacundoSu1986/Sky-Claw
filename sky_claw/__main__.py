"""Sky-Claw CLI entry point.

Usage::

    python -m sky_claw --mode cli          # interactive REPL
    python -m sky_claw --mode telegram     # Telegram webhook server
    python -m sky_claw --mode oneshot "install Requiem"
    python -m sky_claw --mode web --port 8888  # local web UI
    python -m sky_claw --mode gui         # local desktop UI (NiceGUI)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import uuid
import queue

import aiohttp
from aiohttp import web

from sky_claw.agent.providers import create_provider, ProviderConfigError
from sky_claw.agent.router import LLMRouter
from sky_claw.agent.tools import AsyncToolRegistry
from sky_claw.web.app import WebApp
from sky_claw.logging_config import setup_logging, correlation_id_var
from sky_claw.comms.telegram import TelegramWebhook
from sky_claw.comms.telegram_sender import TelegramSender
from sky_claw.comms.telegram_polling import TelegramPolling
from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.config import Config, ALLOWED_HOSTS, ALLOWED_METHODS, XEDIT_COMMON_PATHS, LOOT_COMMON_PATHS, SystemPaths
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import SyncEngine
from sky_claw.scraper.masterlist import MasterlistClient
from sky_claw.scraper.nexus_downloader import NexusDownloader
from sky_claw.security.hitl import HITLGuard, HITLRequest
from sky_claw.security.network_gateway import NetworkGateway
from sky_claw.security.path_validator import PathValidator
from sky_claw.auto_detect import AutoDetector
from sky_claw.tools_installer import (
    ToolsInstaller,
    scan_common_paths,
)
from sky_claw.agent.animation_hub import AnimationHub, EngineConfig
from sky_claw.local_config import load as load_local_config
from sky_claw.orchestrator.supervisor import SupervisorAgent

logger = logging.getLogger("sky_claw")

SYSTEM_PROMPT = (
    "Sos Sky-Claw, un agente de modding para Skyrim SE/AE.\n"
    "REGLA CRÍTICA DE LENGUAJE: SIEMPRE responder en español argentino, "
    "sin importar en qué idioma hable el usuario. Prohibido usar otro idioma en tu respuesta final.\n"
    "REGLA DE PENSAMIENTO: Antes de responder o usar herramientas, "
    "DEBES reflexionar sobre el problema usando un bloque <thought>interno, "
    "oculto al usuario</thought> paso a paso.\n"
    "Sé directo y conciso en tu respuesta final. "
    "Cuando el usuario pregunte sobre mods, load order o conflictos, "
    "usá el perfil 'Default' automáticamente sin preguntar. "
    "Si una herramienta (LOOT, xEdit) no está disponible, ofrecé instalarla. "
    "Si un mod no está en la base de datos, buscá primero en Nexus Mods antes de rendirte. "
    "Tenés soporte para Pandora Behavior Engine (animaciones) y BodySlide (físicas/cuerpos); "
    "usá las herramientas correspondientes si el usuario instala mods de este tipo. "
    "Nunca pidas información que puedas detectar o deducir por tu cuenta."
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sky_claw",
        description="Sky-Claw — Autonomous Skyrim mod management agent",
    )
    parser.add_argument(
        "--mode",
        choices=["cli", "telegram", "oneshot", "web", "gui"],
        default="cli",
        help="Operation mode (default: cli)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SKY_CLAW_WEB_PORT", "8888")),
        help="Port for the web UI server (default: 8888)",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "deepseek", "ollama"],
        default=os.environ.get("LLM_PROVIDER"),
        help="LLM provider (auto-detected if not specified)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="Command to execute in oneshot mode",
    )
    parser.add_argument(
        "--mo2-root",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("SKY_CLAW_MO2_ROOT", str(SystemPaths.get_base_drive() / "MO2Portable"))),
        help="Path to the MO2 portable instance",
    )
    parser.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("SKY_CLAW_DB_PATH", "mod_registry.db")),
        help="Path to the mod registry database",
    )
    parser.add_argument(
        "--loot-exe",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("SKY_CLAW_LOOT_EXE", "loot.exe")),
        help="Path to the LOOT CLI executable",
    )
    parser.add_argument(
        "--webhook-host",
        default=os.environ.get("SKY_CLAW_WEBHOOK_HOST", "0.0.0.0"),
        help="Host for the Telegram webhook server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=int(os.environ.get("SKY_CLAW_WEBHOOK_PORT", "8080")),
        help="Port for the Telegram webhook server (default: 8080)",
    )
    parser.add_argument(
        "--operator-chat-id",
        type=int,
        default=int(_env) if (_env := os.environ.get("SKY_CLAW_OPERATOR_CHAT_ID", "")) else None,
        help="Telegram chat ID for HITL operator notifications (env: SKY_CLAW_OPERATOR_CHAT_ID)",
    )
    parser.add_argument(
        "--staging-dir",
        type=pathlib.Path,
        default=pathlib.Path(
            os.environ.get("SKY_CLAW_STAGING_DIR", str(SystemPaths.get_base_drive() / "MO2Portable/downloads"))
        ),
        help="MO2 staging directory for mod downloads (env: SKY_CLAW_STAGING_DIR)",
    )
    parser.add_argument(
        "--xedit-exe",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("SKY_CLAW_XEDIT_EXE", ""))
        if os.environ.get("SKY_CLAW_XEDIT_EXE")
        else None,
        help="Path to the SSEEdit executable (env: SKY_CLAW_XEDIT_EXE)",
    )
    parser.add_argument(
        "--install-dir",
        type=pathlib.Path,
        default=pathlib.Path(
            os.environ.get("SKY_CLAW_INSTALL_DIR", str(SystemPaths.modding_root()))
        ),
        help="Directory for auto-installing tools like LOOT/SSEEdit (env: SKY_CLAW_INSTALL_DIR)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    if getattr(sys, "frozen", False):
        parser.set_defaults(mode="web")

    return parser.parse_args(argv)


class AppContext:
    """Manages lifecycle of all async resources."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self.config_path: pathlib.Path | None = None
        self.session: aiohttp.ClientSession | None = None
        self.gateway: NetworkGateway | None = None
        self.hitl: HITLGuard | None = None
        self.registry: AsyncModRegistry | None = None
        self.router: LLMRouter | None = None
        self.sender: TelegramSender | None = None
        self.polling: TelegramPolling | None = None
        self.downloader: NexusDownloader | None = None
        self.tools_installer: ToolsInstaller | None = None
        self.telegram_daemon: Any | None = None # From sky_claw.comms.ws_daemon
        
        # GUI communication queues
        self.gui_queue: queue.Queue = queue.Queue()
        self.logic_queue: queue.Queue = queue.Queue()

    @property
    def is_configured(self) -> bool:
        """True when the full stack (provider + router) is ready."""
        return self.router is not None

    async def start_minimal(self) -> None:
        """Phase 1: resolve config path + create HTTP session."""
        self._resolve_config_path()
        self.session = aiohttp.ClientSession()
        logger.info(
            "start_minimal complete — config_path=%s, session ready",
            self.config_path,
        )

    async def start_full(self) -> None:
        """Phase 2: load config, inject env vars, create provider + router."""
        assert self.config_path is not None, "start_minimal() must run first"

        if self.polling is not None:
            await self.polling.stop()
            self.polling = None
        if self.router is not None:
            await self.router.close()
            self.router = None
        if self.registry is not None:
            await self.registry.close()
            self.registry = None

        if self.session is None:
            self.session = aiohttp.ClientSession()

        config_path = self.config_path
        logger.info("start_full — Config path: %s (exists=%s)", config_path, config_path.exists())

        local_cfg = Config(config_path)
        self._apply_config_to_env(local_cfg)

        mo2_root = self._args.mo2_root
        config_changed = False

        _MO2_DEFAULT = str(SystemPaths.get_base_drive() / "MO2Portable")
        if local_cfg.mo2_root and str(mo2_root) == _MO2_DEFAULT:
            cfg_mo2 = pathlib.Path(local_cfg.mo2_root)
            mo2_root = cfg_mo2
            logger.info("Using mo2_root from config: %s", mo2_root)
        elif local_cfg.mo2_root:
            cfg_mo2 = pathlib.Path(local_cfg.mo2_root)
            if cfg_mo2.exists():
                mo2_root = cfg_mo2
                logger.info("Using mo2_root from config (exists): %s", mo2_root)

        if not mo2_root.exists():
            detected_mo2 = await AutoDetector.find_mo2()
            if detected_mo2 is not None:
                mo2_root = detected_mo2
                local_cfg.mo2_root = str(detected_mo2)
                config_changed = True
                logger.info("Zero-config: MO2 detected at %s", detected_mo2)

        if not local_cfg.skyrim_path:
            detected_skyrim = await AutoDetector.find_skyrim()
            if detected_skyrim is not None:
                local_cfg.skyrim_path = str(detected_skyrim)
                config_changed = True
                logger.info("Zero-config: Skyrim detected at %s", detected_skyrim)

        provider_name = self._args.provider if self._args.provider else local_cfg.llm_provider
        try:
            provider = create_provider(provider_name=provider_name, model=local_cfg.llm_model)
            actual_model = getattr(provider, "model", local_cfg.llm_model) or "default"
            logger.info("Provider created: %s (model: %s)", type(provider).__name__, actual_model)
        except ProviderConfigError as exc:
            logger.warning("LLM provider config error: %s — falling back to Ollama", exc)
            from sky_claw.agent.providers import OllamaProvider
            provider = OllamaProvider()

        nexus_key = os.environ.get("NEXUS_API_KEY") or local_cfg.nexus_api_key or ""
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or local_cfg.telegram_bot_token or ""
        operator_chat_id: int | None = self._args.operator_chat_id
        
        if local_cfg.telegram_chat_id:
            try:
                operator_chat_id = int(local_cfg.telegram_chat_id)
                logger.info("Using operator_chat_id from config")
            except ValueError:
                logger.warning("Invalid telegram_chat_id in config (must be int)")

        self.registry = AsyncModRegistry(self._args.db_path)
        await self.registry.open()

        install_dir = getattr(self._args, "install_dir", None)
        if local_cfg.install_dir:
            install_dir = pathlib.Path(local_cfg.install_dir)

        sandbox_roots: list[pathlib.Path] = [mo2_root, pathlib.Path("/tmp/sky_claw")]
        if install_dir and install_dir not in sandbox_roots:
            sandbox_roots.append(install_dir)
        mo2_parent = mo2_root.parent
        if mo2_parent != mo2_root and mo2_parent not in sandbox_roots:
            sandbox_roots.append(mo2_parent)
        validator = PathValidator(roots=sandbox_roots)
        mo2 = MO2Controller(mo2_root, validator)

        self.gateway = NetworkGateway()
        masterlist = MasterlistClient(gateway=self.gateway, api_key=nexus_key)

        if bot_token:
            self.sender = TelegramSender(
                bot_token=bot_token,
                gateway=self.gateway,
                session=self.session,
            )

        async def _hitl_notify(req: HITLRequest) -> None:
            if self.sender is None or operator_chat_id is None:
                logger.info("HITL auto-approving: %s", req.request_id)
                self.hitl.respond(req.request_id, True)
                return
            msg = f"🛡️ *HITL Approval Required*\n\nID: `{req.request_id}`\nReason: {req.reason}\n\n{req.detail}"
            # Send using sender directly
            try:
                await self.sender.send(
                    operator_chat_id, 
                    msg, 
                    reply_markup={
                        "inline_keyboard": [[
                            {"text": "✅ Approve", "callback_data": f"hitl:approve:{req.request_id}"},
                            {"text": "❌ Deny", "callback_data": f"hitl:deny:{req.request_id}"}
                        ]]
                    }
                )
            except Exception:
                logger.exception("Failed to send HITL notification")

        self.hitl = HITLGuard(notify_fn=_hitl_notify)
        sync_engine = SyncEngine(mo2, masterlist, self.registry, hitl=self.hitl)

        if await self.registry.is_empty():
            logger.info("Database empty, initial Sync from MO2...")
            try:
                await sync_engine.run(self.session, profile="Default")
            except Exception as exc:
                logger.exception("Initial synchronization failed: %s", exc)

        self.tools_installer = ToolsInstaller(
            hitl=self.hitl,
            gateway=self.gateway,
            path_validator=validator,
        )

        loot_exe = self._args.loot_exe
        if local_cfg.loot_exe:
            cfg_loot = pathlib.Path(local_cfg.loot_exe)
            if cfg_loot.exists(): loot_exe = cfg_loot
        if loot_exe is None or not loot_exe.exists():
            found = scan_common_paths(LOOT_COMMON_PATHS, "loot.exe")
            if found:
                loot_exe = found
                local_cfg.loot_exe = str(found)
                config_changed = True

        xedit_exe = getattr(self._args, "xedit_exe", None)
        if local_cfg.xedit_exe:
            cfg_xedit = pathlib.Path(local_cfg.xedit_exe)
            if cfg_xedit.exists(): xedit_exe = cfg_xedit
        if xedit_exe is None or not xedit_exe.exists():
            found = scan_common_paths(XEDIT_COMMON_PATHS, "SSEEdit.exe")
            if found:
                xedit_exe = found
                local_cfg.xedit_exe = str(found)
                config_changed = True

        if config_changed:
            local_cfg.save()

        if nexus_key:
            self.downloader = NexusDownloader(
                api_key=nexus_key,
                gateway=self.gateway,
                staging_dir=self._args.staging_dir,
            )

        tool_registry = AsyncToolRegistry(
            registry=self.registry,
            mo2=mo2,
            sync_engine=sync_engine,
            loot_exe=loot_exe,
            hitl=self.hitl,
            downloader=self.downloader,
            tools_installer=self.tools_installer,
            install_dir=install_dir,
            animation_hub=AnimationHub(
                mo2=mo2,
                config=EngineConfig(
                    pandora_exe=pathlib.Path(local_cfg.pandora_exe) if local_cfg.pandora_exe else None,
                    bodyslide_exe=pathlib.Path(local_cfg.bodyslide_exe) if local_cfg.bodyslide_exe else None,
                ),
                path_validator=validator,
            ),
            local_cfg=local_cfg,
            config_path=config_path,
        )

        history_db = str(self._args.db_path).replace(".db", "_history.db")
        mo2_root = local_cfg.mo2_root or "."
        mo2_profile = os.path.join(mo2_root, "profiles", "Default") 
        
        self.router = LLMRouter(
            provider=provider,
            tool_registry=tool_registry,
            db_path=history_db,
            system_prompt=SYSTEM_PROMPT,
            registry_db=str(self._args.db_path),
            mo2_profile=mo2_profile
        )
        await self.router.open()

        # Initialize Telegram WS Daemon (Standard 2026 Bridge)
        from sky_claw.comms.ws_daemon import TelegramDaemon
        self.telegram_daemon = TelegramDaemon(
            router=self.router, 
            session=self.session,
            gateway_url=os.environ.get("SKY_CLAW_TG_GATEWAY", "ws://localhost:8080")
        )
        # Run as a background task in the main event loop
        asyncio.create_task(self.telegram_daemon.start())
        logger.info("Telegram Daemon (WS Bridge) iniciado en segundo plano.")

        if bot_token and getattr(self._args, "mode", "cli") != "telegram":
            webhook_handler = TelegramWebhook(
                router=self.router,
                sender=self.sender,
                session=self.session,
                hitl=self.hitl,
            )
            self.polling = TelegramPolling(
                token=bot_token,
                webhook_handler=webhook_handler,
                authorized_chat_id=operator_chat_id,
            )
            await self.polling.start()
            logger.info("Telegram polling started")

        logger.info("start_full complete")

    async def start(self) -> None:
        """Initialize all components."""
        await self.start_minimal()
        await self.start_full()

    async def stop(self) -> None:
        """Gracefully shutdown all resources."""
        if self.polling is not None:
            await self.polling.stop()
            self.polling = None
        if self.telegram_daemon is not None:
            await self.telegram_daemon.stop()
            self.telegram_daemon = None
        if self.router is not None:
            await self.router.close()
            self.router = None
        if self.registry is not None:
            await self.registry.close()
            self.registry = None
        if self.session is not None:
            await self.session.close()
            self.session = None

    def _resolve_config_path(self) -> None:
        legacy_path = pathlib.Path.cwd() / "sky_claw_config.json"
        if legacy_path.exists():
            self.config_path = legacy_path
            return
        from sky_claw.config import Config
        self.config_path = Config.DEFAULT_CONFIG_FILE

    @staticmethod
    def _apply_config_to_env(cfg: Config) -> None:
        llm_key = getattr(cfg, "llm_api_key", None)
        if llm_key:
            for env in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]:
                if env not in os.environ: os.environ[env] = llm_key
        if (v := getattr(cfg, "anthropic_api_key", None)) and "ANTHROPIC_API_KEY" not in os.environ:
            os.environ["ANTHROPIC_API_KEY"] = v
        if (v := getattr(cfg, "openai_api_key", None)) and "OPENAI_API_KEY" not in os.environ:
            os.environ["OPENAI_API_KEY"] = v
        if (v := getattr(cfg, "deepseek_api_key", None)) and "DEEPSEEK_API_KEY" not in os.environ:
            os.environ["DEEPSEEK_API_KEY"] = v
        if (v := getattr(cfg, "nexus_api_key", None)) and "NEXUS_API_KEY" not in os.environ:
            os.environ["NEXUS_API_KEY"] = v
        if (v := getattr(cfg, "telegram_bot_token", None)) and "TELEGRAM_BOT_TOKEN" not in os.environ:
            os.environ["TELEGRAM_BOT_TOKEN"] = v


async def _run_cli(ctx: AppContext) -> None:
    assert ctx.router and ctx.session
    print("Sky-Claw interactive mode. Type 'exit' or 'quit' to leave.\n")
    chat_id = "cli-session"
    while True:
        try:
            user_input = await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        text = user_input.strip()
        if not text: continue
        correlation_id_var.set(str(uuid.uuid4()))
        try:
            response = await ctx.router.chat(text, ctx.session, chat_id=chat_id)
            print(f"\nsky-claw> {response}\n")
        except RuntimeError as exc:
            print(f"\n[error] {exc}\n")


async def _run_oneshot(ctx: AppContext, command: str) -> None:
    assert ctx.router and ctx.session
    correlation_id_var.set(str(uuid.uuid4()))
    try:
        response = await ctx.router.chat(command, ctx.session, chat_id="oneshot")
        print(response)
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)


async def _run_telegram(ctx: AppContext, host: str, port: int) -> None:
    assert ctx.router and ctx.session and ctx.gateway
    if ctx.sender is None:
        print("Error: TELEGRAM_BOT_TOKEN required.", file=sys.stderr)
        sys.exit(1)
    webhook_handler = TelegramWebhook(
        router=ctx.router, sender=ctx.sender, session=ctx.session, hitl=ctx.hitl
    )
    polling = TelegramPolling(
        token=ctx.sender._token,
        webhook_handler=webhook_handler,
        authorized_chat_id=ctx._args.operator_chat_id,
    )
    await polling.start()
    print("Telegram polling started. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await polling.stop()


async def _run_web(ctx: AppContext, port: int) -> None:
    assert ctx.session is not None
    local_cfg = load_local_config(ctx.config_path)
    already_configured = not local_cfg.first_run and bool(local_cfg.get_api_key())
    if already_configured: await ctx.start_full()

    web_app = WebApp(
        router=ctx.router,
        session=ctx.session,
        config_path=ctx.config_path,
        tools_installer=ctx.tools_installer,
        on_setup_complete=_make_setup_callback(ctx),
    )
    runner = web.AppRunner(web_app.create_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    url = f"http://localhost:{port}"
    print(f"\n  Sky-Claw Web UI: {url}\n")
    import webbrowser
    webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def _make_setup_callback(ctx: AppContext):
    async def _on_setup_complete(web_app: WebApp) -> None:
        await ctx.start_full()
        web_app._router = ctx.router
        web_app._tools_installer = ctx.tools_installer
    return _on_setup_complete

# --- GUI BACKGROUND TASKS ---

async def _gui_logic_loop(ctx: AppContext) -> None:
    """Processes chat messages from GUI in a background task."""
    while True:
        try:
            item = await asyncio.to_thread(ctx.logic_queue.get)
            if item[0] == "chat":
                text = item[1]
                correlation_id_var.set(str(uuid.uuid4()))
                try:
                    response = await ctx.router.chat(text, ctx.session, chat_id="gui-session")
                    ctx.gui_queue.put(("response", response))
                except Exception as e:
                    logger.exception("Logic error in chat: %s", e)
                    ctx.gui_queue.put(("response", f"Error arcano: {e}"))
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("GUI logic loop error: %s", e)
            await asyncio.sleep(1)

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

# --- ENTRY POINTS ---

async def _main_async(args: argparse.Namespace) -> None:
    """Asynchronous runner for CLI, Web, and Telegram modes."""
    logger.info("Sky-Claw starting in %s mode", args.mode)
    if args.mode == "oneshot" and not args.command:
        print("Error: oneshot mode requires a command argument.", file=sys.stderr)
        sys.exit(1)

    ctx = AppContext(args)
    if args.mode == "web":
        await ctx.start_minimal()
        try:
            await _run_web(ctx, args.port)
        finally:
            await ctx.stop()
    else:
        await ctx.start()
        try:
            if args.mode == "cli":
                await _run_cli(ctx)
            elif args.mode == "oneshot":
                await _run_oneshot(ctx, args.command)
            elif args.mode == "telegram":
                await _run_telegram(ctx, args.webhook_host, args.webhook_port)
        finally:
            await ctx.stop()

async def start_full(args: argparse.Namespace) -> AppContext:
    """Helper for NiceGUI to initialize the full stack."""
    ctx = AppContext(args)
    await ctx.start()
    return ctx

def run_gui_mode(args):
    from sky_claw.gui.app import SkyClawGUI
    from nicegui import ui, app

    # Creamos la página principal
    @ui.page('/')
    async def main_page():
        # Iniciamos el contexto y la UI
        ctx = await start_full(args)
        gui = SkyClawGUI(ctx)
        gui.build_ui()
        
        # Log de éxito en la consola de la UI
        logger.info("GUI y Contexto iniciados correctamente.")

        supervisor = SupervisorAgent()
        gui.on_ejecutar = supervisor.handle_execution_signal
        
        asyncio.create_task(supervisor.start())
        logger.info("Sky-Claw Daemon Core inicializado en background.")

    # Arrancamos el servidor
    ui.run(
        title="Sky-Claw | Elder Scrolls Agent",
        dark=True,
        show=True,
        reload=False,
        port=8080,
        host="0.0.0.0"
    )

def main(argv: list[str] | None = None) -> None:
    """Unified entry point controller."""
    args = _parse_args(argv)
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=log_level)

    if args.mode == "gui":
        run_gui_mode(args)
    else:
        try:
            asyncio.run(_main_async(args))
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main(sys.argv)
