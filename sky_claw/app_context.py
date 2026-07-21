from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import queue
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any, TypeVar

import aiohttp
import keyring

from sky_claw.antigravity.agent.providers import ProviderConfigError, create_provider
from sky_claw.antigravity.agent.router import LLMRouter
from sky_claw.antigravity.agent.tools_facade import AsyncToolRegistry
from sky_claw.antigravity.comms.telegram import TelegramWebhook
from sky_claw.antigravity.comms.telegram_polling import TelegramPolling
from sky_claw.antigravity.comms.telegram_sender import TelegramSender
from sky_claw.antigravity.core.metrics_server import (
    start_metrics_server,
    stop_metrics_server,
)
from sky_claw.antigravity.core.tracing import configure_tracing, shutdown_tracing
from sky_claw.antigravity.db.async_registry import AsyncModRegistry
from sky_claw.antigravity.db.journal import OperationJournal
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine
from sky_claw.antigravity.scraper.masterlist import MasterlistClient
from sky_claw.antigravity.scraper.nexus_downloader import NexusDownloader
from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager
from sky_claw.antigravity.security.credential_vault import CredentialVault
from sky_claw.antigravity.security.hitl import HITLGuard, HITLRequest
from sky_claw.antigravity.security.network_gateway import GatewayTCPConnector, NetworkGateway
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.antigravity.security.prompt_armor import build_system_header
from sky_claw.config import LOOT_COMMON_PATHS, XEDIT_COMMON_PATHS, Config, SystemPaths
from sky_claw.local.ai.patch_advisor_llm import LLMCallable
from sky_claw.local.auto_detect import AutoDetector
from sky_claw.local.local_config import load as _load_legacy_json
from sky_claw.local.mo2.vfs import MO2Controller
from sky_claw.local.tools_installer import ToolsInstaller, scan_common_paths

# Audit #190: shared lock-DB staging dir. MUST match the orchestrator's
# BACKUP_STAGING_DIR (sky_claw.antigravity.orchestrator.supervisor) so the
# agent-tools world (LLMRouter / Telegram / /api/chat) and the GUI
# SupervisorAgent serialize LOOT load-order sorts on the SAME locks.db.
_LOCK_STAGING_DIR = pathlib.Path(".skyclaw_backups")

logger = logging.getLogger("sky_claw")

_T = TypeVar("_T")
_CleanupCallback = Callable[[], Awaitable[None]]


SYSTEM_PROMPT = (
    build_system_header() + "Sos Sky-Claw, un agente de modding para Skyrim SE/AE.\n"
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


class LifecycleContext:
    """Propietario único del DatabaseLifecycleManager para el proceso.

    M-01: Todos los sub-contextos deben pedir conexiones vía
    ``await self.manager.get_connection(path)`` en lugar de abrir
    ``aiosqlite.connect`` directamente.
    """

    def __init__(self) -> None:
        from sky_claw.antigravity.core.db_lifecycle import (
            DatabaseLifecycleConfig,
            DatabaseLifecycleManager,
        )

        self.manager: DatabaseLifecycleManager = DatabaseLifecycleManager(
            db_paths=[],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )

    async def initialize(self) -> None:
        # init_all es no-op cuando db_paths está vacío;
        # las DBs se inicializan on-demand vía get_connection.
        await self.manager.init_all()

    async def close(self) -> None:
        await self.manager.shutdown_all()


class NetworkContext:
    """Administra los recursos de red (sesión HTTP, gateway, downloader)."""

    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.gateway: NetworkGateway | None = None
        self.downloader: NexusDownloader | None = None

    async def initialize(self, nexus_key: str, staging_dir: pathlib.Path | None) -> None:
        # Un ÚNICO gateway por NetworkContext: ``initialize`` se llama dos veces
        # (minimal → full). Crear uno nuevo en el 2º llamado partía el pin cache DNS
        # (la session de larga vida seguía atada al connector/caché del gateway #1
        # mientras los componentes nuevos usaban el #2). Reusar el existente mantiene
        # una sola caché compartida app-wide.
        if self.gateway is None:
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
    """Administra la base de datos principal de mods.

    M-01: Recibe el LifecycleContext para que AsyncModRegistry obtenga
    su conexión del DatabaseLifecycleManager centralizado.
    """

    def __init__(self, db_path: str | pathlib.Path, lifecycle: LifecycleContext) -> None:
        self.db_path = db_path
        self._lifecycle = lifecycle
        self.registry: AsyncModRegistry | None = None

    async def initialize(self) -> None:
        self.registry = AsyncModRegistry(self.db_path, lifecycle=self._lifecycle.manager)
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
        # M-01: lifecycle se monta PRIMERO — es la base de todas las conexiones DB
        self.lifecycle = LifecycleContext()
        self.network = NetworkContext()
        self.database = DatabaseContext(self._args.db_path, lifecycle=self.lifecycle)
        # Sandbox PathValidator (modding roots) is built in start(); it is
        # injected into SupervisorAgent so MO2 path resolution validates
        # against the right roots instead of the backup-only rollback validator.
        self.sandbox_validator: PathValidator | None = None

        self.hitl: HITLGuard | None = None
        self.router: LLMRouter | None = None
        # F1 (auditoría Zero-Trust 2026-07-18): bóveda de credenciales para el
        # hot-swap Zero-Trust del router. None hasta que start_full la provisione
        # (solo si SKYCLAW_VAULT_MASTER_KEY está configurada).
        self.credential_vault: CredentialVault | None = None
        self.sender: TelegramSender | None = None
        self.polling: TelegramPolling | None = None
        # Motor de sincronización — lo consume el botón "Buscar actualizaciones"
        # de la GUI (detect_pending_updates). None hasta que corra start_full.
        self.sync_engine: SyncEngine | None = None
        self.tools_installer: ToolsInstaller | None = None
        # Resolved tools install dir, populated in start() — read by the GUI
        # "Instalar" button (Follow-up C). None until the full start path runs.
        self.install_dir: pathlib.Path | None = None

        # ARC-02: AsyncExitStack para compensación atómica ante fallos
        self._exit_stack = AsyncExitStack()
        self._cleanup_lock = asyncio.Lock()
        self._cleanup_collector: list[_CleanupCallback] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

        # Un solo owner muta el lifecycle. stop() invalida/cancela al owner
        # antes de esperar este lock para no quedar detrás de un I/O colgado.
        self._lifecycle_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._active_startup_task: asyncio.Task[Any] | None = None
        self._active_startup_epoch: int | None = None
        self._active_stop_task: asyncio.Task[Any] | None = None
        self._stop_epoch = 0
        self._stopping = False
        self._startup_shutdown_timeout_s = 5.0
        self._minimal_started_epoch: int | None = None
        self._full_start_committed = False

        # Background task tracking for proper cleanup
        self._background_tasks: set[asyncio.Task] = set()

        # GUI communication queues
        self.gui_queue: queue.Queue = queue.Queue()
        self.logic_queue: queue.Queue = queue.Queue()

    @property
    def is_configured(self) -> bool:
        """True when the full stack (provider + router) is ready."""
        return self._full_start_committed and not self._stopping and self.router is not None

    async def reload_llm_provider(self, provider_name: str, api_key: str = "") -> bool:
        """Hot-swap del proveedor LLM del router vivo. Devuelve True si se aplicó.

        Hogar único del swap (antes duplicado en ``frontend_bridge._do_llm_reload``,
        que además metía mano en ``router._provider``): resuelve la clave desde
        keyring (la específica del provider, con fallback a la genérica
        ``llm_api_key``; Ollama no necesita) y el modelo por-provider desde el
        TOML, arma el provider y lo intercambia bajo el lock del router vía
        ``LLMRouter.set_provider``.

        Devuelve ``False`` si el router no está vivo (stack lock-only, p. ej. el
        GUI antes de que el daemon termine de bootear) o si la config del
        provider es inválida — el llamador informa "aplica al reiniciar".
        """
        if self.router is None:
            return False
        provider = provider_name.strip().lower()

        # Algunos backends de keyring lanzan al leer (igual que en Config y
        # save_settings); tragamos la excepción para caer a False/feedback
        # consistente en vez de reventar la task de hot-reload (review Copilot #225).
        def _read_key(name: str) -> str:
            try:
                return keyring.get_password("sky_claw", name) or ""
            except Exception:
                logger.exception("Hot-reload LLM: fallo leyendo keyring '%s'", name)
                return ""

        # Ollama no usa secreto: ni siquiera sondeamos keyring (que puede lanzar
        # en headless/Linux sin backend) — Codex #225. Para los cloud probamos
        # la clave específica del provider y caemos a la genérica (mismo criterio
        # que el arranque y el bridge): un provider recién elegido puede no tener
        # slot propio aún.
        key = api_key
        if not key and provider != "ollama":
            key = _read_key(f"{provider}_api_key") or _read_key("llm_api_key")
            if not key:
                logger.error("Hot-reload LLM: sin API key para el proveedor '%s'.", provider)
                return False

        # Modelo scoped al provider (nunca el llm_model global): al arrancar el
        # provider se crea CON su modelo; el hot-swap debe respetarlo o
        # degradaría al default (bug latente del bridge, que no pasaba modelo).
        model = ""
        if self.config_path is not None:
            try:
                cfg = Config(self.config_path)
                model = getattr(cfg, f"{provider}_model", "") or ""
            except Exception:
                logger.exception("Hot-reload LLM: no se pudo leer el modelo de la config")

        try:
            new_provider = create_provider(provider_name=provider, api_key=key, model=model)
        except ProviderConfigError as exc:
            logger.error("Hot-reload LLM: config de provider inválida: %s", exc)
            return False
        except Exception:
            logger.exception("Hot-reload LLM: fallo inesperado creando el provider")
            return False

        await self.router.set_provider(new_provider)
        logger.info("🚀 Hot-reload LLM: el router ahora usa %s", type(new_provider).__name__)
        return True

    def make_patch_advisor_llm(self) -> LLMCallable:
        """Callable ``(system, user) -> respuesta`` para el advisor de IA (Fase 1).

        El router y la sesión HTTP se resuelven PEREZOSAMENTE en cada llamada,
        no al construir el closure: en la GUI el supervisor se construye antes
        de que ``start_full`` monte el router (misma lección que las deps lazy
        de grass, review Codex #301), y el hot-swap de provider debe verse
        reflejado sin recablear nada. Stack lock-only (router nunca montado) →
        la llamada lanza ``RuntimeError`` accionable y el ``PatchAdvisorLLM``
        degrada a ``manual_only`` (fail-closed).
        """

        async def _call(system_prompt: str, user_prompt: str) -> str:
            router = self.router
            session = self.network.session
            if router is None or session is None:
                raise RuntimeError(
                    "No hay proveedor LLM activo (stack lock-only o boot incompleto). "
                    "Configurá un provider (OpenAI/Anthropic/DeepSeek/Ollama) para "
                    "habilitar el advisor de IA."
                )
            return await router.complete_simple(system_prompt, user_prompt, session)

        return _call

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

    @staticmethod
    def _resolve_allowed_tools(local_cfg) -> set[str] | None:
        """Resolve the session's tool allowlist from config (T2-07).

        Lookup order (first non-None wins):
          1. ``local_cfg.allowed_tools`` (list[str] in config.toml).
          2. ``None`` (default — all registered tools allowed).

        Returns:
            ``None`` (no restriction) or ``set[str]`` of permitted tool names.
        """
        candidate = getattr(local_cfg, "allowed_tools", None)
        if candidate is None:
            return None
        if isinstance(candidate, str):
            # Single-tool string convenience: split on whitespace/comma.
            parts = [p.strip() for p in candidate.replace(",", " ").split() if p.strip()]
            return set(parts) if parts else None
        try:
            return {str(name) for name in candidate}
        except TypeError:
            # Not iterable — treat as no restriction.
            return None

    def _is_reentrant_startup(self) -> bool:
        return asyncio.current_task() is self._active_startup_task

    def _assert_startup_current(self) -> None:
        task = asyncio.current_task()
        if (
            task is None
            or task is not self._active_startup_task
            or self._active_startup_epoch != self._stop_epoch
            or self._stopping
        ):
            raise asyncio.CancelledError("startup superseded by stop()")

    async def _await_startup(self, awaitable: Awaitable[_T]) -> _T:
        result = await awaitable
        self._assert_startup_current()
        return result

    def _push_startup_cleanup(
        self,
        callback: Callable[..., Awaitable[object]],
        /,
        *args: object,
    ) -> None:
        """Registra un callback que conserva ownership si su cierre falla."""

        async def _retained_cleanup() -> None:
            try:
                await callback(*args)
            except BaseException:
                collector = self._cleanup_collector
                if collector is not None:
                    collector.append(_retained_cleanup)
                raise

        self._exit_stack.push_async_callback(_retained_cleanup)

    async def _await_cleanup_terminal(
        self,
        task: asyncio.Task[None],
        *,
        timeout_s: float | None = None,
    ) -> None:
        """Espera cleanup sin cancelarlo y preserva la primera cancelación externa."""
        outer = asyncio.current_task()
        cancellation: asyncio.CancelledError | None = None
        loop = asyncio.get_running_loop()
        deadline = None if timeout_s is None else loop.time() + max(0.0, timeout_s)
        timed_out = False

        while not task.done():
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                timed_out = True
                break
            try:
                if remaining is None:
                    await asyncio.shield(task)
                else:
                    done, _ = await asyncio.wait({task}, timeout=remaining)
                    if task not in done:
                        timed_out = True
                        break
            except asyncio.CancelledError as exc:
                if outer is not None and outer.cancelling():
                    if cancellation is None:
                        cancellation = exc
                    continue
                break
            except BaseException:
                break

        if cancellation is not None:
            raise cancellation
        if timed_out:
            raise TimeoutError(f"cleanup did not stop within {timeout_s:.3f}s")

    @staticmethod
    def _observe_cleanup_task(task: asyncio.Task[None]) -> None:
        """Consume la excepción sin alterar el resultado que drenará el owner."""
        if not task.cancelled():
            task.exception()

    def _finalize_cleanup_generation(self, task: asyncio.Task[None]) -> BaseException | None:
        if task is not self._cleanup_task or not task.done():
            return None

        retained = self._cleanup_collector or []
        self._cleanup_task = None
        self._cleanup_collector = None
        for callback in reversed(retained):
            self._exit_stack.push_async_callback(callback)

        try:
            task.result()
        except BaseException as exc:
            return exc
        return None

    async def _close_cleanup_generation(self, *, timeout_s: float | None = None) -> None:
        """Drena una única generación y conserva ownership si no termina."""
        async with self._cleanup_lock:
            if self._cleanup_task is None:
                closing_stack = self._exit_stack
                self._exit_stack = AsyncExitStack()
                self._cleanup_collector = []
                self._cleanup_task = asyncio.create_task(
                    closing_stack.aclose(),
                    name="app-context-cleanup-generation",
                )
                self._cleanup_task.add_done_callback(self._observe_cleanup_task)

            cleanup_task = self._cleanup_task
            wait_error: BaseException | None = None
            try:
                await self._await_cleanup_terminal(cleanup_task, timeout_s=timeout_s)
            except BaseException as exc:
                wait_error = exc

            cleanup_error = self._finalize_cleanup_generation(cleanup_task)
            if wait_error is not None:
                if isinstance(wait_error, asyncio.CancelledError) and cleanup_error is not None:
                    logger.error(
                        "Cleanup falló durante una cancelación; se preserva la cancelación",
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
                raise wait_error
            if cleanup_error is not None:
                raise cleanup_error

    def _sanitize_full_references(self) -> None:
        self._full_start_committed = False
        self.sandbox_validator = None
        self.install_dir = None
        self.router = None
        self.polling = None
        self.hitl = None
        self.sender = None
        self.sync_engine = None
        self.tools_installer = None

    async def _rollback_startup(self) -> None:
        try:
            await self._close_cleanup_generation(
                timeout_s=self._startup_shutdown_timeout_s,
            )
        except BaseException:
            logger.exception("Rollback de startup incompleto; cleanup retenido para retry")
        finally:
            self._sanitize_full_references()

    async def _run_startup(
        self,
        operation: Callable[[], Awaitable[None]],
        *,
        publishes_full: bool,
    ) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("AppContext startup requiere un asyncio.Task")
        if task is self._active_startup_task:
            await operation()
            return

        requested_epoch = self._stop_epoch
        if self._stopping:
            raise asyncio.CancelledError("startup requested while stop() is active")

        async with self._lifecycle_lock:
            if requested_epoch != self._stop_epoch or self._stopping:
                raise asyncio.CancelledError("startup superseded before ownership")

            self._active_startup_task = task
            self._active_startup_epoch = requested_epoch
            self._minimal_started_epoch = None
            self._full_start_committed = False
            try:
                # Preflight queda fuera de la frontera que compensa adquisiciones
                # nuevas: un callback retenido se intenta una sola vez por start.
                try:
                    await self._close_cleanup_generation(
                        timeout_s=self._startup_shutdown_timeout_s,
                    )
                except BaseException:
                    self._sanitize_full_references()
                    raise
                self._sanitize_full_references()
                self._assert_startup_current()

                try:
                    await operation()
                    self._assert_startup_current()
                except BaseException:
                    logger.critical(
                        "startup FAILED — rolling back initialized services",
                        exc_info=True,
                    )
                    await self._rollback_startup()
                    raise

                if publishes_full:
                    self._full_start_committed = True
            finally:
                if self._active_startup_task is task:
                    self._active_startup_task = None
                    self._active_startup_epoch = None

    async def _start_minimal_inner(self) -> None:
        if self._minimal_started_epoch == self._active_startup_epoch:
            return
        self._resolve_config_path()
        self._migrate_legacy_json()
        # Registrar antes del primer await cubre adquisiciones parciales.
        self._push_startup_cleanup(self.lifecycle.close)
        await self._await_startup(self.lifecycle.initialize())
        self._push_startup_cleanup(self.network.close)
        await self._await_startup(self.network.initialize("", None))
        self._minimal_started_epoch = self._active_startup_epoch
        logger.info(
            "start_minimal complete — config_path=%s, session ready",
            self.config_path,
        )

    async def start_minimal(self) -> None:
        """Phase 1: resolve config path, migrate legacy JSON, create HTTP session."""
        if self._is_reentrant_startup():
            await self._start_minimal_inner()
            return
        await self._run_startup(self._start_minimal_inner, publishes_full=False)

    async def _start_full_with_base(self) -> None:
        if self._minimal_started_epoch != self._active_startup_epoch:
            await self._start_minimal_inner()
        await self._start_full_inner()

    async def start_full(self) -> None:
        """Phase 2: rebuild the full provider/router stack atomically."""
        if self._is_reentrant_startup():
            await self._start_full_with_base()
            return
        await self._run_startup(self._start_full_with_base, publishes_full=True)

    async def _start_full_inner(self) -> None:
        """Internal implementation of start_full (lock-free)."""
        assert self.config_path is not None, "start_minimal() must run first"

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
                detected_mo2 = await self._await_startup(AutoDetector.find_mo2())
                if detected_mo2 is not None:
                    mo2_root = detected_mo2
                    local_cfg.mo2_root = str(detected_mo2)
                    config_changed = True
                    logger.info("Zero-config: MO2 detected at %s", detected_mo2)

            if not local_cfg.skyrim_path:
                detected_skyrim = await self._await_startup(AutoDetector.find_skyrim())
                if detected_skyrim is not None:
                    local_cfg.skyrim_path = str(detected_skyrim)
                    config_changed = True
                    logger.info("Zero-config: Skyrim detected at %s", detected_skyrim)

            provider_name = self._args.provider if self._args.provider else local_cfg.llm_provider
            try:
                # Extraer llave dinámicamente sin tocar os.environ.
                # Usamos keyring directamente para evitar el cortocircuito
                # con cadenas vacías que enmascara fallos de credenciales.
                provider_key_name = f"{provider_name}_api_key"
                api_key = getattr(local_cfg, provider_key_name, None)
                if not api_key:
                    api_key = local_cfg.llm_api_key
                    if api_key:
                        logger.info(
                            "Using generic llm_api_key for provider '%s' (provider-specific key '%s' is empty).",
                            provider_name,
                            provider_key_name,
                        )
                if not api_key:
                    raise ProviderConfigError(
                        f"No API key found for provider '{provider_name}'. "
                        f"Checked keyring keys: '{provider_key_name}' and 'llm_api_key'."
                    )

                # Provider-scoped model: read THIS provider's configured model
                # (never the global llm_model), so switching providers never
                # carries a stale, incompatible model. Empty → provider DEFAULT.
                provider_model = getattr(local_cfg, f"{provider_name}_model", "") or ""
                provider = create_provider(
                    provider_name=provider_name,
                    model=provider_model,
                    api_key=api_key,
                )
                actual_model = getattr(provider, "model", provider_model) or "default"
                logger.info(
                    "Provider created: %s (model: %s)",
                    type(provider).__name__,
                    actual_model,
                )
            except ProviderConfigError as exc:
                logger.warning("LLM provider config error: %s — falling back to Ollama", exc)
                from sky_claw.antigravity.agent.providers import OllamaProvider

                # Honor the configured Ollama model even on the fallback path.
                provider = OllamaProvider(model=getattr(local_cfg, "ollama_model", "") or "")

            nexus_key = local_cfg.nexus_api_key or ""
            bot_token = local_cfg.telegram_bot_token or ""
            operator_chat_id: int | None = self._args.operator_chat_id

            if local_cfg.telegram_chat_id:
                try:
                    operator_chat_id = int(local_cfg.telegram_chat_id)
                    logger.info("Using operator_chat_id from config")
                except ValueError:
                    logger.warning("Invalid telegram_chat_id in config (must be int)")

            self._push_startup_cleanup(self.database.close)
            await self._await_startup(self.database.initialize())

            # M-01 PR C: inyectar lifecycle al singleton GovernanceManager
            # para que is_scanned_and_clean / update_scan_result usen el
            # DatabaseLifecycleManager del proceso en lugar de abrir
            # conexiones efímeras propias. Cierra el contrato M-01.
            #
            # get_instance() puede lanzar RuntimeError si la whitelist está
            # corrupta (_load_whitelist). En ese caso loggeamos un warning y
            # continuamos: los security flows operarán fail-closed
            # (is_scanned_and_clean → False) en lugar de impedir el arranque
            # de modos no-seguridad.
            from sky_claw.antigravity.security.governance import GovernanceManager

            try:
                GovernanceManager.get_instance().set_lifecycle(self.lifecycle.manager)
            except RuntimeError as exc:
                logger.warning(
                    "GovernanceManager lifecycle injection skipped (%s). "
                    "Security scans will operate fail-closed (no incremental cache).",
                    exc,
                )

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

            await self._await_startup(self.network.initialize(nexus_key, self._args.staging_dir))

            masterlist = MasterlistClient(gateway=self.network.gateway, api_key=nexus_key)

            sender: TelegramSender | None = None
            if bot_token:
                sender = TelegramSender(
                    bot_token=bot_token,
                    gateway=self.network.gateway,
                    session=self.network.session,
                )

            hitl: HITLGuard
            full_published = False

            async def _hitl_notify(req: HITLRequest) -> None:
                active_sender = self.sender if full_published else sender
                if active_sender is None or operator_chat_id is None:
                    if req.category in ("tool_execution", "sandbox_promotion"):
                        # Fail-closed: destructive tool executions and sandbox
                        # promotions (T-27b·2: promover un diff sin revisión
                        # vaciaría al sandbox de sentido) are NEVER
                        # auto-approved without an operator channel.
                        logger.critical(
                            "HITL: no operator channel configured — DENYING "
                            "%s request %s (%s). Configure the Telegram bot and "
                            "operator chat id to approve it.",
                            req.category,
                            req.request_id,
                            req.reason,
                        )
                        await hitl.respond(req.request_id, False)
                        return
                    logger.info("HITL auto-approving: %s", req.request_id)
                    await hitl.respond(req.request_id, True)
                    return
                msg = f"🛡️ *HITL Approval Required*\n\nID: `{req.request_id}`\nReason: {req.reason}\n\n{req.detail}"
                # Send using sender directly
                try:
                    await active_sender.send(
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

            hitl = HITLGuard(notify_fn=_hitl_notify)

            # Observability: configure distributed tracing first so spans from the
            # metrics server startup are captured.  NoOp when no OTLP endpoint is set.
            configure_tracing()

            async def _shutdown_tracing_async() -> None:
                shutdown_tracing()

            self._push_startup_cleanup(_shutdown_tracing_async)

            # Observability: best-effort Prometheus /metrics endpoint on 127.0.0.1.
            # Wrapped because a port collision must NOT abort the main app.
            try:
                metrics_token_dir = pathlib.Path.home() / ".sky_claw" / "tokens" / "metrics"
                metrics_auth = AuthTokenManager(token_dir=str(metrics_token_dir))
                metrics_auth.generate()
                await self._await_startup(metrics_auth.start_rotation())
                self._push_startup_cleanup(metrics_auth.stop_rotation)
                metrics_runner = await self._await_startup(start_metrics_server(validator=metrics_auth.validate))
                self._push_startup_cleanup(stop_metrics_server, metrics_runner)
                logger.info("metrics_endpoint_enabled")
            except Exception:
                logger.warning("metrics_endpoint_disabled", exc_info=True)

            # Guardado en self para que la GUI lo alcance (botón "Buscar
            # actualizaciones" → runtime.app_context.sync_engine), sin el
            # reach-around privado que usa Telegram (_router._tools._sync_engine).
            sync_engine = SyncEngine(mo2, masterlist, self.database.registry, hitl=hitl)

            if await self._await_startup(self.database.registry.is_empty()):
                enrich_remote = bool(nexus_key)
                if enrich_remote:
                    logger.info("Database empty, initial Sync from MO2 with Nexus enrichment")
                else:
                    logger.warning(
                        "Database empty and Nexus API key missing; "
                        "importing local MO2 identity without remote enrichment"
                    )
                try:
                    result = await self._await_startup(
                        sync_engine.run(
                            self.network.session,
                            profile="Default",
                            enrich_remote=enrich_remote,
                        )
                    )
                    # Fallback: si el enriquecimiento remoto no persistió NINGUNA
                    # fila pero hubo fallos (Nexus offline/timeout en el primer
                    # arranque), los errores de red se absorben por-mod dentro de
                    # run() y no propagan, así que el except de abajo no corre y el
                    # registry quedaría vacío. Reimportamos la identidad local de
                    # MO2 sin requests a Nexus. Condición estrecha: excluye éxito
                    # parcial (processed>0, esos fallos se reintentan en el próximo
                    # check) y MO2 vacío (failed==0).
                    if enrich_remote and result.processed == 0 and result.failed > 0:
                        logger.warning(
                            "Enriquecimiento remoto falló para todos los mods; reintentando import local-only sin Nexus"
                        )
                        await self._await_startup(
                            sync_engine.run(
                                self.network.session,
                                profile="Default",
                                enrich_remote=False,
                            )
                        )
                except Exception as exc:
                    logger.exception("Initial synchronization failed: %s", exc)

            tools_installer = ToolsInstaller(
                hitl=hitl,
                gateway=self.network.gateway,
                path_validator=validator,
            )

            loot_exe = self._args.loot_exe
            if local_cfg.loot_exe:
                cfg_loot = pathlib.Path(local_cfg.loot_exe)
                if cfg_loot.exists():
                    loot_exe = cfg_loot
            if loot_exe is None or not loot_exe.exists():
                found = await self._await_startup(
                    asyncio.to_thread(
                        scan_common_paths,
                        LOOT_COMMON_PATHS,
                        "loot.exe",
                    )
                )
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
                found = await self._await_startup(
                    asyncio.to_thread(
                        scan_common_paths,
                        XEDIT_COMMON_PATHS,
                        "SSEEdit.exe",
                    )
                )
                if found:
                    xedit_exe = found
                    local_cfg.xedit_exe = str(found)
                    config_changed = True

            if config_changed:
                local_cfg.save()

            # T2-07 (review fix PR #143): wire-through del allowlist desde
            # config/args al AsyncToolRegistry. Por defecto es None (todos
            # los tools permitidos) — backwards compat. Para restringir,
            # pasar `--allowed-tools search_mod download_mod` en CLI o
            # configurar en config.toml [agent.allowed_tools].
            allowed_tools = self._resolve_allowed_tools(local_cfg)
            session_id = getattr(self._args, "session_id", None) or "default"

            # Audit #190: shared distributed lock so the live run_loot_sort path
            # serializes on the same "load-order" lock as the GUI orchestrator /
            # dry-run preview (same locks.db file). target_files=[] in
            # LootSortingService means the snapshot manager is never exercised
            # here, but SnapshotTransactionLock requires the instance.
            _LOCK_STAGING_DIR.mkdir(parents=True, exist_ok=True)
            (_LOCK_STAGING_DIR / "snapshots").mkdir(parents=True, exist_ok=True)
            lock_manager = DistributedLockManager(db_path=_LOCK_STAGING_DIR / "locks.db")
            self._push_startup_cleanup(lock_manager.close)
            await self._await_startup(lock_manager.initialize())
            snapshot_manager = FileSnapshotManager(snapshot_dir=_LOCK_STAGING_DIR / "snapshots")
            await self._await_startup(snapshot_manager.initialize())

            # T-26 (ADR 0002, follow-up de #243): journal para que run_loot_sort
            # de este path del agente también emita+persista el ActionManifest
            # ("caja negra de vuelo") antes de mutar — cerrando el hueco donde la
            # emisión era un no-op fuera del path de la GUI/supervisor. Comparte
            # .skyclaw_backups/journal.db (mismo staging que locks.db, audit #190)
            # y toma la conexión del DatabaseLifecycleManager (WAL recovery +
            # pragmas hardenizadas + shutdown coordinado), igual que la history DB
            # del router más abajo.
            journal = OperationJournal(
                db_path=_LOCK_STAGING_DIR / "journal.db",
                lifecycle=self.lifecycle.manager,
            )
            self._push_startup_cleanup(journal.close)
            await self._await_startup(journal.open())

            tool_registry = AsyncToolRegistry(
                registry=self.database.registry,
                mo2=mo2,
                sync_engine=sync_engine,
                loot_exe=loot_exe,
                hitl=hitl,
                downloader=self.network.downloader,
                tools_installer=tools_installer,
                install_dir=install_dir,
                # Consolidation (obs #187): AnimationHub was removed. run_pandora /
                # run_bodyslide resolve their M-02/M-03 runners lazily from
                # local_cfg.pandora_exe / bodyslide_exe at call time. The
                # validator sandboxes those config-supplied exe paths (PR #171).
                path_validator=validator,
                local_cfg=local_cfg,
                config_path=config_path,
                # Audit #190: shared lock so run_loot_sort serializes with the orchestrator.
                lock_manager=lock_manager,
                snapshot_manager=snapshot_manager,
                # T-26 (follow-up de #243): caja negra de vuelo en el path del agente.
                journal=journal,
                # TASK-013 P1: thread the NetworkGateway so all egress tools enforce
                # Zero-Trust allow-list policy (fixes Copilot review comment on PR #78).
                gateway=self.network.gateway,
                # T2-07: per-session tool allowlist (None = all tools permitted).
                allowed_tools=allowed_tools,
                session_id=session_id,
            )

            history_db = str(self._args.db_path).replace(".db", "_history.db")
            mo2_root = local_cfg.mo2_root or "."
            mo2_profile = os.path.join(mo2_root, "profiles", "Default")

            # F1: provisionar el vault (si SKYCLAW_VAULT_MASTER_KEY está seteada)
            # y cablearlo al router para habilitar el hot-swap Zero-Trust de
            # credenciales. Sin la env var, vault=None y el router se comporta
            # como hasta ahora (reload_provider → False).
            vault_db = str(self._args.db_path).replace(".db", "_vault.db")
            credential_vault = await self._await_startup(
                self._provision_credential_vault(provider_name, api_key or "", vault_db)
            )

            router = LLMRouter(
                provider=provider,
                tool_registry=tool_registry,
                db_path=history_db,
                system_prompt=SYSTEM_PROMPT,
                registry_db=str(self._args.db_path),
                mo2_profile=mo2_profile,
                gateway=self.network.gateway,
                vault=credential_vault,
                # M-01.1: history DB via DatabaseLifecycleManager (WAL recovery,
                # hardened pragmas, coordinated shutdown_all checkpoint).
                lifecycle=self.lifecycle.manager,
            )
            self._push_startup_cleanup(self._close_router, router)
            await self._await_startup(router.open())

            polling: TelegramPolling | None = None
            if bot_token and getattr(self._args, "mode", "cli") != "telegram":
                webhook_handler = TelegramWebhook(
                    router=router,
                    sender=sender,
                    session=self.network.session,
                    hitl=hitl,
                    authorized_user_id=operator_chat_id,
                )
                polling = TelegramPolling(
                    token=bot_token,
                    webhook_handler=webhook_handler,
                    gateway=self.network.gateway,
                    session=self.network.session,
                    authorized_chat_id=operator_chat_id,
                )
                self._push_startup_cleanup(self._stop_polling, polling)
                await self._await_startup(polling.start())
                logger.info("Telegram polling started")

            logger.info("start_full complete")
            self.sandbox_validator = validator
            self.install_dir = install_dir
            self.sender = sender
            self.hitl = hitl
            self.sync_engine = sync_engine
            self.tools_installer = tools_installer
            self.router = router
            self.polling = polling
            self._full_start_committed = True
            full_published = True

        except BaseException:
            raise

    async def _start_all_inner(self) -> None:
        # Estas llamadas públicas son parte del contrato observable. Al correr
        # desde el owner son reentrantes y no vuelven a tomar el lock.
        await self.start_minimal()
        await self.start_full()

    async def start(self) -> None:
        """Initialize all components."""
        if self._is_reentrant_startup():
            await self._start_all_inner()
            return
        await self._run_startup(self._start_all_inner, publishes_full=True)

    async def _stop_polling(self, polling: TelegramPolling) -> None:
        """Callback for AsyncExitStack: stop Telegram polling."""
        try:
            await polling.stop()
        finally:
            if self.polling is polling:
                self.polling = None

    async def _close_router(self, router: LLMRouter) -> None:
        """Callback for AsyncExitStack: close LLM router."""
        try:
            await router.close()
        finally:
            if self.router is router:
                self.router = None

    # ------------------------------------------------------------------
    # F1 — CredentialVault: hot-swap Zero-Trust de credenciales LLM
    # ------------------------------------------------------------------

    @staticmethod
    def _read_vault_master_key() -> str:
        """Master-key del vault, leído UNA vez en el boundary (directiva H-04).

        Fuente: env var ``SKYCLAW_VAULT_MASTER_KEY`` (prefijo ``SKYCLAW_``, no
        ``SKY_CLAW_``, para no ser barrido por los overrides de ``Config``).
        Vacío/whitespace ⇒ ausente (hot-swap deshabilitado, backward-compatible).
        """
        return (os.environ.get("SKYCLAW_VAULT_MASTER_KEY") or "").strip()

    def _construct_credential_vault(self, master_key: str, db_path: str) -> CredentialVault:
        """Construye el vault SIN inicializar (salt junto al DB para aislar).

        No adquiere conexión: el constructor solo hace el hardening del salt y
        arma el pool (lazy — la conexión se crea en el primer ``acquire``). Esto
        permite registrar la compensación de cierre ANTES de ``initialize()``.
        """
        salt_dir = pathlib.Path(db_path).parent / "vault_salt"
        return CredentialVault(db_path=db_path, master_key=master_key, salt_dir=salt_dir)

    async def _build_credential_vault(self, master_key: str, db_path: str) -> CredentialVault:
        """Construye e inicializa el vault (salt colocado junto al DB para aislar)."""
        vault = self._construct_credential_vault(master_key, db_path)
        await vault.initialize()
        return vault

    async def _provision_credential_vault(
        self, provider_name: str, api_key: str, db_path: str
    ) -> CredentialVault | None:
        """Provisiona el vault si hay master-key; None si no (comportamiento actual).

        Cuando se provisiona: registra el cierre ordenado y **siembra** la clave
        del provider activo bajo ``{provider}_api_key`` para que ``reload_provider``
        sea funcional (no solo alcanzable) — sin la semilla, ``get_secret`` daría
        ``None`` y el hot-swap seguiría devolviendo ``False``.
        """
        master_key = self._read_vault_master_key()
        if not master_key:
            logger.info("Hot-swap de credenciales Zero-Trust deshabilitado: SKYCLAW_VAULT_MASTER_KEY no configurada.")
            return None
        vault = self._construct_credential_vault(master_key, db_path)
        # Registrar la compensación ANTES del primer await de adquisición:
        # initialize() abre una conexión en el pool, así que si falla a mitad
        # (DB read-only/locked, o salt débil en Windows) el rollback de startup
        # debe poder cerrar el vault igual. Invariante de #338 (registrar antes
        # de adquirir); antes se registraba después de initialize → fuga.
        self._push_startup_cleanup(self._close_vault, vault)
        await vault.initialize()
        if api_key:
            await vault.set_secret(f"{provider_name}_api_key", api_key)
        self.credential_vault = vault
        logger.info("🔐 CredentialVault cableado al router — hot-swap Zero-Trust habilitado.")
        return vault

    async def _close_vault(self, vault: CredentialVault) -> None:
        """Callback para AsyncExitStack: cierra el vault y nulea la referencia."""
        try:
            await vault.close()
        finally:
            if self.credential_vault is vault:
                self.credential_vault = None

    async def stop(self) -> None:
        """Invalida el startup activo y cierra recursos sin esperar I/O infinito."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("AppContext stop requiere un asyncio.Task")
        if task is self._active_stop_task:
            return
        if task is self._active_startup_task:
            raise RuntimeError("stop() no puede ejecutarse desde el startup owner")

        async with self._stop_lock:
            self._active_stop_task = task
            self._stopping = True
            self._full_start_committed = False
            self._stop_epoch += 1
            active_startup = self._active_startup_task
            try:
                if active_startup is not None and not active_startup.done():
                    active_startup.cancel()
                    done, _ = await asyncio.wait(
                        {active_startup},
                        timeout=self._startup_shutdown_timeout_s,
                    )
                    if active_startup not in done:
                        raise TimeoutError(f"startup did not stop within {self._startup_shutdown_timeout_s:.3f}s")

                if active_startup is not None and active_startup.done():
                    try:
                        active_startup.result()
                    except asyncio.CancelledError:
                        pass
                    except BaseException:
                        logger.warning(
                            "Startup terminó con error durante stop(); se continúa el cleanup",
                            exc_info=True,
                        )

                async with self._lifecycle_lock:
                    background_tasks = set(self._background_tasks)
                    for background_task in background_tasks:
                        background_task.cancel()
                    if background_tasks:
                        done, pending = await asyncio.wait(
                            background_tasks,
                            timeout=self._startup_shutdown_timeout_s,
                        )
                        if pending:
                            raise TimeoutError(
                                f"background tasks did not stop within {self._startup_shutdown_timeout_s:.3f}s"
                            )
                        await asyncio.gather(*done, return_exceptions=True)
                        self._background_tasks.difference_update(done)

                    try:
                        await self._close_cleanup_generation(
                            timeout_s=self._startup_shutdown_timeout_s,
                        )
                    finally:
                        self._sanitize_full_references()

                    self._minimal_started_epoch = None
                    self._stopping = False
            finally:
                if self._active_stop_task is task:
                    self._active_stop_task = None

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
                logger.warning(
                    "CFG-01: Failed to migrate a legacy credential (%s).",
                    type(exc).__name__,
                )

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
