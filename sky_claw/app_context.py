from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import queue
import tempfile
from contextlib import AsyncExitStack
from typing import Any

import aiohttp
import keyring

from sky_claw.agent.animation_hub import AnimationHub, EngineConfig
from sky_claw.agent.providers import ProviderConfigError, create_provider
from sky_claw.agent.router import LLMRouter
from sky_claw.agent.tools_facade import AsyncToolRegistry
from sky_claw.auto_detect import AutoDetector
from sky_claw.comms.telegram import TelegramWebhook
from sky_claw.comms.telegram_polling import TelegramPolling
from sky_claw.comms.telegram_sender import TelegramSender
from sky_claw.config import LOOT_COMMON_PATHS, XEDIT_COMMON_PATHS, Config, SystemPaths
from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.local_config import load as _load_legacy_json
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import SyncEngine
from sky_claw.scraper.masterlist import MasterlistClient
from sky_claw.scraper.nexus_downloader import NexusDownloader
from sky_claw.security.hitl import HITLGuard, HITLRequest
from sky_claw.security.network_gateway import GatewayTCPConnector, NetworkGateway
from sky_claw.security.path_validator import PathValidator
from sky_claw.tools_installer import ToolsInstaller, scan_common_paths

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


class NetworkContext:
    """Administra los recursos de red (sesión HTTP, gateway, downloader)."""

    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.gateway: NetworkGateway | None = None
        self.downloader: NexusDownloader | None = None

    async def initialize(self, nexus_key: str, staging_dir: pathlib.Path | None) -> None:
        self.gateway = NetworkGateway()
        if self.session is None:
            self.session = aiohttp.ClientSession(
                connector=GatewayTCPConnector(self.gateway, limit=20),
            )
        if nexus_key:
            self.downloader = NexusDownloader(
                api_key=nexus_key,
                gateway=self.gateway,
                staging_dir=staging_dir,
            )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None


class DatabaseContext:
    """Administra la base de datos principal de mods."""

    def __init__(self, db_path: str | pathlib.Path) -> None:
        self.db_path = db_path
        self.registry: AsyncModRegistry | None = None

    async def initialize(self) -> None:
        self.registry = AsyncModRegistry(self.db_path)
        await self.registry.open()

    async def close(self) -> None:
        if self.registry is not None:
            await self.registry.close()
            self.registry = None


class AppContext:
    """Manages lifecycle of all async resources."""

    def __init__(self, args) -> None:
        self._args = args
        self.config_path: pathlib.Path | None = None

        # Sub-contextos inyectados dinamicamente
        self.network = NetworkContext()
        self.database = DatabaseContext(self._args.db_path)

        self.hitl: HITLGuard | None = None
        self.router: LLMRouter | None = None
        self.sender: TelegramSender | None = None
        self.polling: TelegramPolling | None = None
        self.tools_installer: ToolsInstaller | None = None
        self.frontend_bridge: Any | None = None  # From sky_claw.comms.frontend_bridge

        # ARC-02: AsyncExitStack para compensación atómica ante fallos
        self._exit_stack = AsyncExitStack()

        # R-06: Lock to prevent re-entrant calls to start_full()
        self._start_full_lock = asyncio.Lock()

        # Background task tracking for proper cleanup
        self._background_tasks: set[asyncio.Task] = set()

        # GUI communication queues
        self.gui_queue: queue.Queue = queue.Queue()
        self.logic_queue: queue.Queue = queue.Queue()

    @property
    def is_configured(self) -> bool:
        """True when the full stack (provider + router) is ready."""
        return self.router is not None

    @property
    def registry(self):
        """Shortcut to the database registry."""
        return self.database.registry

    @property
    def session(self):
        """Shortcut to the network session."""
        return self.network.session

    def _track_task(self, coro, *, name: str = "") -> asyncio.Task:
        """Create a background task and track it for cleanup on shutdown."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def start_minimal(self) -> None:
        """Phase 1: resolve config path, migrate legacy JSON, create HTTP session."""
        self._resolve_config_path()
        self._migrate_legacy_json()
        await self.network.initialize("", None)  # Init base session
        self._exit_stack.push_async_callback(self.network.close)
        logger.info(
            "start_minimal complete — config_path=%s, session ready",
            self.config_path,
        )

    async def start_full(self) -> None:
        """Phase 2: load config, inject env vars, create provider + router.

        Uses AsyncExitStack for atomic rollback: if any service fails to
        initialize, all previously started services are shut down in LIFO
        order, preventing zombie processes and leaked resources (ARC-02).

        R-06: Protected by asyncio.Lock to prevent re-entrant calls
        (e.g. concurrent hot-reload from FrontendBridge).
        """
        async with self._start_full_lock:
            await self._start_full_inner()

    async def _start_full_inner(self) -> None:
        """Internal implementation of start_full (lock-free)."""
        assert self.config_path is not None, "start_minimal() must run first"

        if self.polling is not None:
            await self.polling.stop()
            self.polling = None
        if self.router is not None:
            await self.router.close()
            self.router = None

        await self.database.close()

        # Reset the exit stack for a fresh initialization cycle, keeping
        # only the network.close callback registered by start_minimal.
        self._exit_stack = AsyncExitStack()
        # Re-register the base network session from start_minimal
        self._exit_stack.push_async_callback(self.network.close)

        try:
            config_path = self.config_path
            logger.info(
                "start_full — Config path: %s (exists=%s)",
                config_path,
                config_path.exists(),
            )

            local_cfg = Config(config_path)
            # H-04: Eliminada mutación de os.environ. Los secretos se pasan explícitamente.

            mo2_root = self._args.mo2_root
            config_changed = False

            _mo2_default = str(SystemPaths.get_base_drive() / "MO2Portable")
            if local_cfg.mo2_root and str(mo2_root) == _mo2_default:
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
                # Extraer llave dinámicamente sin tocar os.environ
                api_key = getattr(local_cfg, f"{provider_name}_api_key", None) or local_cfg.llm_api_key

                provider = create_provider(
                    provider_name=provider_name,
                    model=local_cfg.llm_model,
                    api_key=api_key,
                )
                actual_model = getattr(provider, "model", local_cfg.llm_model) or "default"
                logger.info(
                    "Provider created: %s (model: %s)",
                    type(provider).__name__,
                    actual_model,
                )
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

            await self.database.initialize()
            self._exit_stack.push_async_callback(self.database.close)

            install_dir = getattr(self._args, "install_dir", None)
            if local_cfg.install_dir:
                install_dir = pathlib.Path(local_cfg.install_dir)

            sandbox_roots: list[pathlib.Path] = [
                mo2_root,
                pathlib.Path(tempfile.gettempdir()) / "sky_claw",
            ]
            if install_dir and install_dir not in sandbox_roots:
                sandbox_roots.append(install_dir)
            # --- DESPUÉS (Seguro - Zero Trust) ---
            # Solo definir las carpetas estrictamente necesarias
            # Se elimina explícitamente mo2_parent para evitar Path Traversal encubierto
            validator = PathValidator(roots=sandbox_roots)
            mo2 = MO2Controller(mo2_root, validator)

            await self.network.initialize(nexus_key, self._args.staging_dir)

            masterlist = MasterlistClient(gateway=self.network.gateway, api_key=nexus_key)

            if bot_token:
                self.sender = TelegramSender(
                    bot_token=bot_token,
                    gateway=self.network.gateway,
                    session=self.network.session,
                )

            async def _hitl_notify(req: HITLRequest) -> None:
                if self.sender is None or operator_chat_id is None:
                    logger.info("HITL auto-approving: %s", req.request_id)
                    await self.hitl.respond(req.request_id, True)
                    return
                msg = f"🛡️ *HITL Approval Required*\n\nID: `{req.request_id}`\nReason: {req.reason}\n\n{req.detail}"
                # Send using sender directly
                try:
                    await self.sender.send(
                        operator_chat_id,
                        msg,
                        reply_markup={
                            "inline_keyboard": [
                                [
                                    {
                                        "text": "✅ Approve",
                                        "callback_data": f"hitl:approve:{req.request_id}",
                                    },
                                    {
                                        "text": "❌ Deny",
                                        "callback_data": f"hitl:deny:{req.request_id}",
                                    },
                                ]
                            ]
                        },
                    )
                except Exception:
                    logger.exception("Failed to send HITL notification")

            self.hitl = HITLGuard(notify_fn=_hitl_notify)
            sync_engine = SyncEngine(mo2, masterlist, self.database.registry, hitl=self.hitl)

            if await self.database.registry.is_empty():
                logger.info("Database empty, initial Sync from MO2...")
                try:
                    await sync_engine.run(self.network.session, profile="Default")
                except Exception as exc:
                    logger.exception("Initial synchronization failed: %s", exc)

            self.tools_installer = ToolsInstaller(
                hitl=self.hitl,
                gateway=self.network.gateway,
                path_validator=validator,
            )

            loot_exe = self._args.loot_exe
            if local_cfg.loot_exe:
                cfg_loot = pathlib.Path(local_cfg.loot_exe)
                if cfg_loot.exists():
                    loot_exe = cfg_loot
            if loot_exe is None or not loot_exe.exists():
                found = scan_common_paths(LOOT_COMMON_PATHS, "loot.exe")
                if found:
                    loot_exe = found
                    local_cfg.loot_exe = str(found)
                    config_changed = True

            xedit_exe = getattr(self._args, "xedit_exe", None)
            if local_cfg.xedit_exe:
                cfg_xedit = pathlib.Path(local_cfg.xedit_exe)
                if cfg_xedit.exists():
                    xedit_exe = cfg_xedit
            if xedit_exe is None or not xedit_exe.exists():
                found = scan_common_paths(XEDIT_COMMON_PATHS, "SSEEdit.exe")
                if found:
                    xedit_exe = found
                    local_cfg.xedit_exe = str(found)
                    config_changed = True

            if config_changed:
                local_cfg.save()

            tool_registry = AsyncToolRegistry(
                registry=self.database.registry,
                mo2=mo2,
                sync_engine=sync_engine,
                loot_exe=loot_exe,
                hitl=self.hitl,
                downloader=self.network.downloader,
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
                mo2_profile=mo2_profile,
                gateway=self.network.gateway,
            )
            await self.router.open()
            self._exit_stack.push_async_callback(self._close_router)

            # Initialize Frontend Bridge (Gateway port 18789 ↔ Daemon)
            from sky_claw.comms.frontend_bridge import FrontendBridge

            self.frontend_bridge = FrontendBridge(
                router=self.router,
                session=self.network.session,
                config=local_cfg,
                app_context=self,
            )
            self._track_task(self.frontend_bridge.start(), name="frontend-bridge")
            self._exit_stack.push_async_callback(self._stop_frontend_bridge)
            logger.info("Frontend Bridge iniciado en segundo plano.")

            if bot_token and getattr(self._args, "mode", "cli") != "telegram":
                webhook_handler = TelegramWebhook(
                    router=self.router,
                    sender=self.sender,
                    session=self.network.session,
                    hitl=self.hitl,
                    authorized_user_id=operator_chat_id,
                )
                self.polling = TelegramPolling(
                    token=bot_token,
                    webhook_handler=webhook_handler,
                    gateway=self.network.gateway,
                    session=self.network.session,
                    authorized_chat_id=operator_chat_id,
                )
                await self.polling.start()
                self._exit_stack.push_async_callback(self._stop_polling)
                logger.info("Telegram polling started")

            logger.info("start_full complete")

        except Exception:
            logger.critical(
                "start_full FAILED — rolling back all initialized services",
                exc_info=True,
            )
            await self._exit_stack.aclose()
            raise

    async def start(self) -> None:
        """Initialize all components."""
        await self.start_minimal()
        await self.start_full()

    async def _stop_polling(self) -> None:
        """Callback for AsyncExitStack: stop Telegram polling."""
        if self.polling is not None:
            await self.polling.stop()
            self.polling = None

    async def _close_router(self) -> None:
        """Callback for AsyncExitStack: close LLM router."""
        if self.router is not None:
            await self.router.close()
            self.router = None

    async def _stop_frontend_bridge(self) -> None:
        """Callback for AsyncExitStack: stop frontend bridge."""
        if self.frontend_bridge is not None:
            await self.frontend_bridge.stop()
            self.frontend_bridge = None

    async def stop(self) -> None:
        """Gracefully shutdown all resources via AsyncExitStack (LIFO order)."""
        # Cancel all tracked background tasks first
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # AsyncExitStack tears down all registered services in reverse order
        await self._exit_stack.aclose()

    def _resolve_config_path(self) -> None:
        """Always resolves to the canonical TOML config path."""
        self.config_path = Config.DEFAULT_CONFIG_FILE

    def _migrate_legacy_json(self) -> None:
        """CFG-01: Atomic migration from legacy JSON to TOML (Single Source of Truth).

        If sky_claw_config.json exists, reads all values, merges them into
        the TOML Config, saves the TOML, and ONLY THEN deletes the JSON.
        This ensures no data loss — the JSON is purged only after a
        successful TOML write.
        """
        legacy_path = pathlib.Path.cwd() / "sky_claw_config.json"
        if not legacy_path.exists():
            return

        logger.info("CFG-01: Legacy JSON detected at %s — migrating to TOML", legacy_path)
        legacy = _load_legacy_json(legacy_path)

        # Instantiate the TOML config (creates defaults if file doesn't exist)
        toml_cfg = Config(self.config_path)

        # Map non-secret fields (only overwrite if the legacy value is non-empty
        # and the TOML hasn't already been configured with a different value)
        field_map = {
            "loot_exe": "loot_exe",
            "xedit_exe": "xedit_exe",
            "mo2_root": "mo2_root",
            "install_dir": "install_dir",
            "skyrim_path": "skyrim_path",
            "pandora_exe": "pandora_exe",
            "bodyslide_exe": "bodyslide_exe",
            "telegram_chat_id": "telegram_chat_id",
        }

        for legacy_field, toml_field in field_map.items():
            legacy_val = getattr(legacy, legacy_field, None)
            if legacy_val:
                current_toml_val = toml_cfg._data.get(toml_field, "")
                if not current_toml_val:
                    toml_cfg._data[toml_field] = str(legacy_val)

        # Migrate first_run flag
        if not legacy.first_run:
            toml_cfg._data["first_run"] = False

        # Migrate secrets: decode from base64 and store in keyring via Config.save()
        secret_map = {
            "get_api_key": "llm_api_key",
            "get_nexus_api_key": "nexus_api_key",
            "get_telegram_bot_token": "telegram_bot_token",
        }
        for getter_name, toml_key in secret_map.items():
            getter = getattr(legacy, getter_name, None)
            if getter is None:
                continue
            try:
                secret_val = getter()
                if secret_val:
                    current = toml_cfg._data.get(toml_key, "")
                    if not current:
                        toml_cfg._data[toml_key] = secret_val
            except Exception as exc:
                logger.warning("CFG-01: Failed to migrate secret '%s': %s", toml_key, exc)

        # Atomic: save TOML first, then delete JSON
        toml_cfg.save()
        logger.info("CFG-01: TOML config saved to %s", self.config_path)

        legacy_path.unlink(missing_ok=True)
        logger.info("CFG-01: Legacy JSON purged — single source of truth established")


async def start_full(args) -> AppContext:
    """Helper for NiceGUI to initialize the full stack."""
    ctx = AppContext(args)
    await ctx.start()
    return ctx


def _is_configured(config_path: pathlib.Path) -> bool:
    """Verifica si las credenciales minimas existen para saltar el setup."""
    try:
        cfg = Config(config_path)
        if cfg._data.get("first_run", True):
            return False
        provider = cfg._data.get("llm_provider", "ollama")
        if provider == "ollama":
            return True
        key = keyring.get_password("sky_claw", f"{provider}_api_key")
        return bool(key)
    except Exception:
        return False


def _resolve_config_path_static(args) -> pathlib.Path:
    """Resuelve config path sin instanciar AppContext."""
    return Config.DEFAULT_CONFIG_FILE
