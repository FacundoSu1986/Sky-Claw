"""Sky-Claw CLI entry point.

Usage::

    python -m sky_claw --mode cli          # interactive REPL
    python -m sky_claw --mode telegram     # Telegram webhook server
    python -m sky_claw --mode oneshot "install Requiem"
    python -m sky_claw --mode web --port 8888  # local web UI
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import uuid

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
from sky_claw.config import Config, ALLOWED_HOSTS, ALLOWED_METHODS, XEDIT_COMMON_PATHS, LOOT_COMMON_PATHS
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
    LOOT_COMMON_PATHS,
    XEDIT_COMMON_PATHS,
)
from sky_claw.agent.animation_hub import AnimationHub, EngineConfig
from sky_claw.local_config import load as load_local_config

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
        default=pathlib.Path(os.environ.get("SKY_CLAW_MO2_ROOT", "C:/MO2Portable")),
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
            os.environ.get("SKY_CLAW_STAGING_DIR", "C:/MO2Portable/downloads")
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
            os.environ.get("SKY_CLAW_INSTALL_DIR", "C:/Modding")
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
    """Manages lifecycle of all async resources.

    Supports two initialization modes:

    * **Full** (CLI / Telegram / oneshot): call :meth:`start` which runs
      ``start_minimal`` + ``start_full`` in sequence.
    * **Lazy** (Web UI / .exe): call :meth:`start_minimal` to create just the
      HTTP session (enough to serve the setup wizard), then call
      :meth:`start_full` after the user completes configuration.
    """

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
        
        # GUI communication queues
        import queue
        self.gui_queue: queue.Queue = queue.Queue()
        self.logic_queue: queue.Queue = queue.Queue()

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        """True when the full stack (provider + router) is ready."""
        return self.router is not None

    # ── Lifecycle: minimal (HTTP session only) ────────────────────────

    async def start_minimal(self) -> None:
        """Phase 1: resolve config path + create HTTP session.

        This is enough to serve the web UI setup wizard.  No LLM provider,
        database, or tool registry is initialised.
        """
        self._resolve_config_path()
        self.session = aiohttp.ClientSession()
        logger.info(
            "start_minimal complete — config_path=%s, session ready",
            self.config_path,
        )

    # ── Lifecycle: full initialisation ────────────────────────────────

    async def start_full(self) -> None:
        """Phase 2: load config, inject env vars, create provider + router.

        Can be called after :meth:`start_minimal` (lazy mode) or
        standalone from :meth:`start`.  Safe to call multiple times —
        the second call will close the existing router first.
        """
        assert self.config_path is not None, "start_minimal() must run first"

        # If called a second time (e.g. user re-ran setup wizard), tear down
        # the previous router so we can rebuild it cleanly.
        if self.polling is not None:
            await self.polling.stop()
            self.polling = None
        if self.router is not None:
            await self.router.close()
            self.router = None
        if self.registry is not None:
            await self.registry.close()
            self.registry = None

        # Ensure we have an HTTP session.
        if self.session is None:
            self.session = aiohttp.ClientSession()

        config_path = self.config_path
        logger.info("start_full — Config path: %s (exists=%s)", config_path, config_path.exists())

        # ── Load local config ────────────────────────────────────────
        local_cfg = Config(config_path)

        # ── Inject API keys into env vars ────────────────────────────
        self._apply_config_to_env(local_cfg)

        # ── Override CLI defaults with config values ─────────────────
        mo2_root = self._args.mo2_root
        config_changed = False

        _MO2_DEFAULT = str(pathlib.Path("C:/MO2Portable"))
        if local_cfg.mo2_root and str(mo2_root) == _MO2_DEFAULT:
            cfg_mo2 = pathlib.Path(local_cfg.mo2_root)
            mo2_root = cfg_mo2
            logger.info("Using mo2_root from config: %s", mo2_root)
        elif local_cfg.mo2_root:
            cfg_mo2 = pathlib.Path(local_cfg.mo2_root)
            if cfg_mo2.exists():
                mo2_root = cfg_mo2
                logger.info("Using mo2_root from config (exists): %s", mo2_root)

        if local_cfg.skyrim_path:
            logger.info("Skyrim path from config: %s", local_cfg.skyrim_path)
        if local_cfg.install_dir:
            logger.info("Install dir from config: %s", local_cfg.install_dir)

        # ── Auto-detect MO2 / Skyrim if still missing ────────────────
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

        # ── Create LLM provider ──────────────────────────────────────
        provider_name = self._args.provider if self._args.provider else local_cfg.llm_provider
        try:
            provider = create_provider(provider_name=provider_name)
            logger.info("Provider created: %s (model: %s)", type(provider).__name__, local_cfg.llm_model)
        except ProviderConfigError as exc:
            logger.warning("LLM provider config error: %s — falling back to Ollama", exc)
            from sky_claw.agent.providers import OllamaProvider
            provider = OllamaProvider()

        nexus_key = os.environ.get("NEXUS_API_KEY", "")
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        operator_chat_id: int | None = self._args.operator_chat_id
        
        if local_cfg.telegram_chat_id:
            try:
                operator_chat_id = int(local_cfg.telegram_chat_id)
                logger.info("Using operator_chat_id from config")
            except ValueError:
                logger.warning("Invalid telegram_chat_id in config (must be int)")

        # Database
        self.registry = AsyncModRegistry(self._args.db_path)
        await self.registry.open()

        # MO2 controller — build sandbox roots dynamically.
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

        # Scraper + sync engine
        self.gateway = NetworkGateway()
        masterlist = MasterlistClient(gateway=self.gateway, api_key=nexus_key)
        sync_engine = SyncEngine(mo2, masterlist, self.registry)

        # TelegramSender
        if bot_token:
            self.sender = TelegramSender(
                bot_token=bot_token,
                gateway=self.gateway,
                session=self.session,
            )

        # HITL Notification
        async def _hitl_notify(req: HITLRequest) -> None:
            if self.sender is None or operator_chat_id is None:
                logger.info(
                    "HITL auto-approving (no sender/operator_chat_id): %s",
                    req.request_id,
                )
                self.hitl.respond(req.request_id, True)
                return
            msg = (
                f"🛡️ *HITL Approval Required*\n\n"
                f"ID: `{req.request_id}`\n"
                f"Reason: {req.reason}\n\n"
                f"{req.detail}"
            )
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": f"hitl:approve:{req.request_id}"},
                    {"text": "❌ Deny", "callback_data": f"hitl:deny:{req.request_id}"}
                ]]
            }
            try:
                await self.sender.send(operator_chat_id, msg, reply_markup=reply_markup)
            except Exception as exc:
                logger.exception(
                    "Failed to send HITL notification for %s", req.request_id
                )

        self.hitl = HITLGuard(notify_fn=_hitl_notify)

        # ToolsInstaller
        self.tools_installer = ToolsInstaller(
            hitl=self.hitl,
            gateway=self.gateway,
            path_validator=validator,
        )

        # Auto-detect LOOT
        loot_exe = self._args.loot_exe
        if local_cfg.loot_exe:
            cfg_loot = pathlib.Path(local_cfg.loot_exe)
            if cfg_loot.exists():
                loot_exe = cfg_loot
        if loot_exe is None or not loot_exe.exists():
            found = scan_common_paths(LOOT_COMMON_PATHS, "loot.exe")
            if found is not None:
                loot_exe = found
                local_cfg.loot_exe = str(found)
                config_changed = True
                logger.info("Auto-detected LOOT at %s", loot_exe)
            else:
                detected_loot = await AutoDetector.find_loot()
                if detected_loot is not None:
                    loot_exe = detected_loot
                    local_cfg.loot_exe = str(detected_loot)
                    config_changed = True
                    logger.info("Zero-config: LOOT detected at %s", detected_loot)

        # Auto-detect SSEEdit
        xedit_exe = getattr(self._args, "xedit_exe", None)
        if local_cfg.xedit_exe:
            cfg_xedit = pathlib.Path(local_cfg.xedit_exe)
            if cfg_xedit.exists():
                xedit_exe = cfg_xedit
        if xedit_exe is None or not xedit_exe.exists():
            found = scan_common_paths(XEDIT_COMMON_PATHS, "SSEEdit.exe")
            if found is not None:
                xedit_exe = found
                local_cfg.xedit_exe = str(found)
                config_changed = True
                logger.info("Auto-detected SSEEdit at %s", xedit_exe)
            else:
                detected_xedit = await AutoDetector.find_xedit()
                if detected_xedit is not None:
                    xedit_exe = detected_xedit
                    local_cfg.xedit_exe = str(detected_xedit)
                    config_changed = True
                    logger.info("Zero-config: SSEEdit detected at %s", detected_xedit)

        # Persist auto-detected paths.
        if config_changed:
            local_cfg.save()

        # NexusDownloader
        if nexus_key:
            self.downloader = NexusDownloader(
                api_key=nexus_key,
                gateway=self.gateway,
                staging_dir=self._args.staging_dir,
            )
        else:
            logger.warning("NEXUS_API_KEY not set — download_mod tool will be unavailable")

        # Tool registry
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

        # LLM router
        history_db = str(self._args.db_path).replace(".db", "_history.db")
        self.router = LLMRouter(
            provider=provider,
            tool_registry=tool_registry,
            db_path=history_db,
            system_prompt=SYSTEM_PROMPT,
        )
        await self.router.open()

        # Telegram Polling (for local HITL responses without webhooks)
        # We start this AFTER the router is open and self.router is set.
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
                session=self.session,
                authorized_chat_id=operator_chat_id,
            )
            await self.polling.start()
            logger.info("Auto-started Telegram long polling for HITL responses (router ready)")

        logger.info("start_full complete — router ready")

    # ── Lifecycle: full start (CLI / Telegram / oneshot shortcut) ─────

    async def start(self) -> None:
        """Initialize all components (convenience for non-web modes).

        Equivalent to ``start_minimal()`` + ``start_full()``.
        """
        await self.start_minimal()
        await self.start_full()

    async def stop(self) -> None:
        """Gracefully shutdown all resources."""
        if self.session is not None:
            await self.session.close()
        if self.router is not None:
            await self.router.close()
        if self.registry is not None:
            await self.registry.close()

    # ── Private helpers ───────────────────────────────────────────────

    def _resolve_config_path(self) -> None:
        """Set ``self.config_path`` to the standard TOML location.
        Supports legacy sky_claw_config.json if it exists in CWD for test compatibility.
        """
        legacy_path = pathlib.Path.cwd() / "sky_claw_config.json"
        if legacy_path.exists():
            self.config_path = legacy_path
            logger.info("Config path resolved to legacy JSON: %s", self.config_path)
            return

        from sky_claw.config import Config
        self.config_path = Config.DEFAULT_CONFIG_FILE
        logger.info("Config path resolved to TOML: %s", self.config_path)

    @staticmethod
    def _apply_config_to_env(cfg: Config) -> None:
        """Inject API keys from Config into environment variables."""
        # Main LLM Keys
        llm_key = getattr(cfg, "llm_api_key", None)
        if llm_key:
            if "ANTHROPIC_API_KEY" not in os.environ:
                os.environ["ANTHROPIC_API_KEY"] = llm_key
            if "OPENAI_API_KEY" not in os.environ:
                os.environ["OPENAI_API_KEY"] = llm_key
            if "DEEPSEEK_API_KEY" not in os.environ:
                os.environ["DEEPSEEK_API_KEY"] = llm_key

        # Provider-specific keys (highest priority)
        if (val := getattr(cfg, "anthropic_api_key", None)) and "ANTHROPIC_API_KEY" not in os.environ:
            os.environ["ANTHROPIC_API_KEY"] = val
        if (val := getattr(cfg, "openai_api_key", None)) and "OPENAI_API_KEY" not in os.environ:
            os.environ["OPENAI_API_KEY"] = val
        if (val := getattr(cfg, "deepseek_api_key", None)) and "DEEPSEEK_API_KEY" not in os.environ:
            os.environ["DEEPSEEK_API_KEY"] = val

        if (val := getattr(cfg, "nexus_api_key", None)) and "NEXUS_API_KEY" not in os.environ:
            os.environ["NEXUS_API_KEY"] = val
        if (val := getattr(cfg, "telegram_bot_token", None)) and "TELEGRAM_BOT_TOKEN" not in os.environ:
            os.environ["TELEGRAM_BOT_TOKEN"] = val
        if (val := getattr(cfg, "telegram_chat_id", None)) and "TELEGRAM_CHAT_ID" not in os.environ:
            os.environ["TELEGRAM_CHAT_ID"] = str(val)


async def _run_cli(ctx: AppContext) -> None:
    """Interactive REPL: read user input, send to router, print response."""
    assert ctx.router is not None
    assert ctx.session is not None

    print("Sky-Claw interactive mode. Type 'exit' or 'quit' to leave.\n")
    chat_id = "cli-session"

    while True:
        try:
            user_input = await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        text = user_input.strip()
        if not text:
            continue
        correlation_id_var.set(str(uuid.uuid4()))
        try:
            response = await ctx.router.chat(text, ctx.session, chat_id=chat_id)
            print(f"\nsky-claw> {response}\n")
        except RuntimeError as exc:
            logger.error("CLI error: %s", exc)
            print(f"\n[error] {exc}\n")


async def _run_oneshot(ctx: AppContext, command: str) -> None:
    """Execute a single command and exit."""
    assert ctx.router is not None
    assert ctx.session is not None

    correlation_id_var.set(str(uuid.uuid4()))
    try:
        response = await ctx.router.chat(command, ctx.session, chat_id="oneshot")
        print(response)
    except RuntimeError as exc:
        logger.error("Oneshot error: %s", exc)
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)


async def _run_telegram(ctx: AppContext, host: str, port: int) -> None:
    """Start the Telegram webhook server."""
    assert ctx.router is not None
    assert ctx.session is not None
    assert ctx.gateway is not None

    if ctx.sender is None:
        print(
            "Error: TELEGRAM_BOT_TOKEN environment variable is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")

    webhook = TelegramWebhook(
        router=ctx.router,
        sender=ctx.sender,
        session=ctx.session,
        hitl=ctx.hitl,
        secret_token=webhook_secret,
    )

    app = web.Application()
    app.router.add_post("/webhook", webhook.handle_update)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("Telegram webhook server listening on %s:%d", host, port)
    print(f"Telegram webhook server listening on {host}:{port}")
    print("Press Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()  # Block until cancelled
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


async def _run_web(ctx: AppContext, port: int) -> None:
    """Start the local web UI server with lazy initialization.

    Phase 1 — start_minimal(): HTTP session only, enough to serve the
    setup wizard.  Phase 2 — start_full(): triggered either immediately
    (if config already exists) or after the user completes the wizard.
    """
    # Phase 1: minimal — just session, config path resolved.
    assert ctx.session is not None

    # Check whether the user already has a valid config.
    local_cfg = load_local_config(ctx.config_path)
    already_configured = not local_cfg.first_run and bool(local_cfg.get_api_key())

    if already_configured:
        # Config exists → full init right away (normal restart).
        logger.info("Config found — running start_full immediately")
        await ctx.start_full()

    # Build the web app.  Router may still be None at this point (first run).
    web_app = WebApp(
        router=ctx.router,
        session=ctx.session,
        config_path=ctx.config_path,
        tools_installer=ctx.tools_installer,
        on_setup_complete=_make_setup_callback(ctx),
    )
    app = web_app.create_app()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    url = f"http://localhost:{port}"
    logger.info("Web UI listening on %s", url)
    print(f"\n  Sky-Claw Web UI: {url}")
    print("  Press Ctrl+C to stop.\n")

    import webbrowser
    webbrowser.open(url)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def _make_setup_callback(ctx: AppContext):
    """Return an async callback that WebApp calls after POST /api/setup."""

    async def _on_setup_complete(web_app: WebApp) -> None:
        logger.info("Setup wizard complete — running start_full()")
        await ctx.start_full()
        # Update the WebApp's references so future requests use the new router.
        web_app._router = ctx.router
        web_app._tools_installer = ctx.tools_installer
        logger.info("WebApp references updated — chat is now available")

    return _on_setup_complete


async def _main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Initialize structured logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=log_level)

    logger.info("Sky-Claw starting in %s mode", args.mode)

    if args.mode == "oneshot" and not args.command:
        print("Error: oneshot mode requires a command argument.", file=sys.stderr)
        sys.exit(1)

    ctx = AppContext(args)

    if args.mode == "web":
        # Lazy initialization: minimal first, full after config.
        await ctx.start_minimal()
        try:
            await _run_web(ctx, args.port)
        finally:
            await ctx.stop()
    else:
        # CLI / Telegram / oneshot: full init at start.
        await ctx.start()
        try:
            if args.mode == "cli":
                await _run_cli(ctx)
            elif args.mode == "oneshot":
                await _run_oneshot(ctx, args.command)
            elif args.mode == "telegram":
                await _run_telegram(ctx, args.webhook_host, args.webhook_port)
            elif args.mode == "gui":
                await _run_gui(ctx)
        finally:
            await ctx.stop()


def main(argv: list[str] | None = None) -> None:
    """Synchronous entry point."""
    asyncio.run(_main(argv))


async def _run_gui(ctx: AppContext) -> None:
    """Start the Desktop GUI."""
    from sky_claw.gui.app import SkyClawGUI
    import queue
    import threading

    ctx.gui_queue = queue.Queue()  # For logic -> GUI
    ctx.logic_queue = queue.Queue() # For GUI -> logic
    
    gui = SkyClawGUI(ctx)
    
    # Background task to process logic_queue
    async def process_logic():
        while True:
            try:
                # Use to_thread to Avoid blocking event loop
                item = await asyncio.to_thread(ctx.logic_queue.get)
                if item[0] == "chat":
                    text = item[1]
                    correlation_id_var.set(str(uuid.uuid4()))
                    response = await ctx.router.chat(text, ctx.session, chat_id="gui-session")
                    ctx.gui_queue.put(("response", response))
            except Exception as e:
                logger.exception("GUI Logic error: %s", e)
                ctx.gui_queue.put(("response", f"Error: {e}"))

    # Periodic background task to update modlist in GUI
    async def update_mods_periodic():
        while True:
            try:
                mods = [m.name for m in await ctx.registry.get_all()]
                ctx.gui_queue.put(("modlist", mods))
            except Exception as exc:
                logger.error("Error updating modlist in GUI: %s", exc)
            await asyncio.sleep(10)

    asyncio.create_task(process_logic())
    asyncio.create_task(update_mods_periodic())
    
    # Run Tkinter in main thread
    await asyncio.to_thread(gui.run)

if __name__ == "__main__":
    main()
