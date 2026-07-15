"""GrassCacheService — orquestador del ritual de grass cache NGIO (PR-5).

Compone las piezas ya mergeadas del plan (Stage 8 del SOP):

- **Fase A** (read-only, sin lock): :class:`GrassAnalyzer` (PR-2) — worldspaces
  con pasto para ``Only-pregenerate-world-spaces`` + detección del fallo
  silencioso de zero-bounds.
- **Fase B**: :class:`GrassProfileManager` (PR-3) — perfil MO2 dedicado clonado
  + mod de configuración; el perfil real jamás se toca.
- **Fase C**: :class:`GrassCacheRunner` (PR-4) — crash-loop supervisor.
- **Fase D**: restauración del entorno (``teardown()`` SIEMPRE, incluso en
  éxito); el cache generado queda en ``overwrite/Grass`` (MO2 lo sirve vía el
  overwrite; empaquetarlo como mod dedicado es follow-up documentado).

Disciplinas del repo:
- El ritual entero corre bajo DOS locks distribuidos: ``grass-cache`` (un solo
  ritual a la vez) y ``load-order`` (el juego lee ``loadorder.txt`` durante
  todo el crash-loop — un sort de LOOT concurrente lo corrompería).
  ``SnapshotTransactionLock`` sin snapshots: lease con auto-renew — un run de
  12 h excede cualquier TTL fijo. El cache parcial se conserva SIEMPRE, y el
  ``teardown()`` corre DENTRO del lock (fuera, borraría estado de otro run).
- **Journal por transacción** (patrón LOOT/DynDOLOD): commit en éxito,
  ``mark_transaction_rolled_back`` en fallo y en cancelación (nunca PENDING).
- **Guard Stage 5→8 (NUEVO)**: las dependencias de pipeline estaban
  documentadas pero no aplicadas (§5.2 del SOP exige guards en paths nuevos).
  Se verifica en el journal que LOOT (``loot-sorting-service``) tenga un
  **FlightReport commiteado** (el ActionManifest pre-sort queda COMPLETED
  aunque el sort falle, así que una operación COMPLETED pelada no alcanza);
  fail-closed sin journal. ``force_stage_guard=True`` lo saltea de forma
  explícita y VISIBLE en el prompt HITL.
- Contrato de retorno: dict ``success: bool`` + ``message: str`` (vacío en
  éxito) — ``normalize_tool_result`` jamás cae en "error desconocido".
- Eventos lifecycle al bus (``pipeline.grass_cache.started/completed``) +
  traducción del callback de progreso del runner a
  ``pipeline.grass_cache.progress`` (el runner es agnóstico del bus — D3).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from sky_claw.antigravity.db.journal import OperationStatus
from sky_claw.antigravity.db.journal_contracts import is_flight_report_committed
from sky_claw.antigravity.db.locks import LockAcquisitionError, SnapshotTransactionLock
from sky_claw.local.tools.grass_cache_runner import (
    GrassCacheConfig,
    GrassCacheProgress,
    GrassCacheRunner,
    GrassCacheRunResult,
)
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
from sky_claw.local.xedit.grass_analyzer import GrassAnalyzer

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from sky_claw.antigravity.core.event_bus import CoreEventBus
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.locks import DistributedLockManager
    from sky_claw.antigravity.db.snapshots import FileSnapshotManager
    from sky_claw.local.mo2.grass_profile import GrassProfileManager
    from sky_claw.local.mo2.vfs import MO2Controller
    from sky_claw.local.xedit.runner import XEditRunner

logger = logging.getLogger(__name__)

#: Recurso del lock distribuido: un solo ritual de grass a la vez.
GRASS_CACHE_RESOURCE_ID = "grass-cache"


class GrassCacheServiceError(Exception):
    """Configuración/estado inválido del servicio (mensaje accionable)."""


@dataclasses.dataclass(frozen=True, slots=True)
class GrassRuntimeDeps:
    """Deps de Fases B/C resueltas de forma perezosa (perfil MO2 + paths).

    Se arman al EJECUTAR el ritual, no al construir el servicio: en producción
    ``MO2_PATH``/``SKYRIM_PATH`` se hidratan después de crear el supervisor.
    """

    profile_manager: GrassProfileManager
    mo2: MO2Controller
    game_path: pathlib.Path
    overwrite_grass_dir: pathlib.Path


class GenerateGrassCacheParams(BaseModel):
    """Payload de ``generate`` (validación única, compartida con la strategy).

    Attributes:
        worldspaces: EditorIDs con pasto (salida de la Fase A) — obligatorio y
            no vacío: un precache sin filtro escanearía TODO el load order.
        conflicting_mods: Mods a desactivar SOLO en el clon (ENB helper, etc.).
        max_runtime_s / max_restarts / stall_threshold: Overrides opcionales de
            los presupuestos del runner.
        force_stage_guard: Saltea el guard Stage 5→8 (visible en el prompt
            HITL — un bypass jamás es silencioso).
    """

    model_config = ConfigDict(strict=True)

    worldspaces: list[str] = Field(min_length=1)
    conflicting_mods: list[str] = Field(default_factory=list)
    max_runtime_s: float | None = None
    max_restarts: int | None = None
    stall_threshold: int | None = None
    force_stage_guard: bool = False


class GrassCacheService:
    """Orquesta las Fases A→D del precache de grass bajo lock + journal.

    Args:
        lock_manager / snapshot_manager: Infra del lease distribuido.
        journal: :class:`OperationJournal` (None solo en tests: el guard
            Stage 5→8 es fail-closed sin journal).
        event_bus: Bus de eventos lifecycle (opcional).
        profile_manager: Fase B (inyectable; requerido para ``generate``).
        analyzer: Fase A (default: :class:`GrassAnalyzer` real).
        xedit_runner_provider: Provider lazy del runner de xEdit para la
            Fase A (patrón ``XEditPipelineService._ensure_xedit_runner``).
        mo2: Controller para lanzar el juego (Fase C).
        game_path: Dir de ``SkyrimSE.exe`` (para el flag del runner).
        overwrite_grass_dir: ``<mo2>/overwrite/Grass``.
        runner_factory: Fábrica del runner (inyectable para tests).
        lock_factory: Fábrica del ``SnapshotTransactionLock`` (tests).
    """

    RESOURCE_ID: str = GRASS_CACHE_RESOURCE_ID
    AGENT_ID: str = "grass-cache-service"
    #: Agent id con el que LOOT journaliza (guard Stage 5→8).
    LOOT_AGENT_ID: str = "loot-sorting-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal | None = None,
        event_bus: CoreEventBus | None = None,
        profile_manager: GrassProfileManager | None = None,
        analyzer: GrassAnalyzer | None = None,
        xedit_runner_provider: Callable[[], XEditRunner] | None = None,
        mo2: MO2Controller | None = None,
        game_path: pathlib.Path | None = None,
        overwrite_grass_dir: pathlib.Path | None = None,
        runtime_deps_provider: Callable[[], GrassRuntimeDeps | None] | None = None,
        runner_factory: Callable[..., GrassCacheRunner] | None = None,
        lock_factory: Callable[..., SnapshotTransactionLock] | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._event_bus = event_bus
        self._profile_manager = profile_manager
        self._analyzer = analyzer if analyzer is not None else GrassAnalyzer()
        self._xedit_runner_provider = xedit_runner_provider
        self._mo2 = mo2
        self._game_path = game_path
        self._overwrite_grass_dir = overwrite_grass_dir
        # Provider lazy de las deps de Fases B/C: se llama al ejecutar el ritual
        # (no en __init__), así los paths se resuelven DESPUÉS de la hidratación
        # de entorno de la GUI (review Codex #301). Las deps concretas de arriba
        # (inyectadas en tests) tienen precedencia.
        self._runtime_deps_provider = runtime_deps_provider
        self._runner_factory = runner_factory if runner_factory is not None else GrassCacheRunner
        self._lock_factory = lock_factory if lock_factory is not None else SnapshotTransactionLock

    def _ensure_runtime_deps(self) -> None:
        """Puebla (una vez) las deps de Fases B/C desde el provider lazy.

        En producción ``MO2_PATH``/``SKYRIM_PATH`` se hidratan DESPUÉS de
        construir el supervisor (escaneo de entorno de la GUI), así que
        resolverlas en ``__init__`` daría ``None`` permanente. Se resuelven al
        correr el ritual: si el entorno ya está listo, se pueblan; si no, quedan
        ``None`` y se reintenta en la próxima corrida.
        """
        if self._profile_manager is not None:
            return  # deps concretas (tests) o ya pobladas
        if self._runtime_deps_provider is None:
            return
        deps = self._runtime_deps_provider()
        if deps is None:
            return
        self._profile_manager = deps.profile_manager
        self._mo2 = deps.mo2
        self._game_path = deps.game_path
        self._overwrite_grass_dir = deps.overwrite_grass_dir

    # ------------------------------------------------------------------
    # Fase A — diagnóstico read-only (sin lock, sin HITL)
    # ------------------------------------------------------------------

    async def analyze_prerequisites(self, plugins: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        """Worldspaces con pasto + zero-bounds; ``ready`` resume si se puede seguir."""
        try:
            if self._xedit_runner_provider is None:
                raise GrassCacheServiceError(
                    "El runner de xEdit no está configurado (XEDIT_PATH/SKYRIM_PATH): "
                    "la Fase A necesita xEdit para el scan de worldspaces."
                )
            xedit_runner = self._xedit_runner_provider()
            ws = await self._analyzer.list_grass_worldspaces(plugins, xedit_runner, timeout=timeout)
            zb = await self._analyzer.detect_zero_bound_grass(plugins, xedit_runner, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — el contrato exige dict, no excepción
            logger.warning("Fase A de grass falló", exc_info=True)
            return {"success": False, "message": str(exc)}
        ready = bool(ws.editor_ids) and not zb.has_findings
        return {
            "success": True,
            "message": "",
            "worldspaces": ws.to_dict(),
            "zero_bounds": zb.to_dict(),
            "editor_ids": ws.editor_ids,
            "ready": ready,
        }

    # ------------------------------------------------------------------
    # Fases B→D — el ritual mutante (lock + journal + HITL vía strategy)
    # ------------------------------------------------------------------

    async def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ejecuta el precache completo; siempre devuelve el dict del contrato.

        Cualquier ``BaseException`` (``CancelledError``, ``KeyboardInterrupt``,
        ``SystemExit``) propaga tras cerrar el journal y correr el teardown —
        es la única no-devolución, semántica estándar del repo.

        Nota operativa: el ritual toma el lock ``load-order`` durante TODO su
        transcurso (hasta ``max_runtime_s``, 12 h por defecto), así que un sort
        de LOOT u otra mutación del orden de carga queda bloqueada mientras dura
        — es a propósito (correctitud > conveniencia): el juego lee
        ``loadorder.txt`` en cada relanzamiento. El escape es cancelar el ritual.
        """
        inicio = time.monotonic()
        try:
            params = GenerateGrassCacheParams(**payload)
        except Exception as exc:  # noqa: BLE001 — ValidationError y afines → contrato
            return self._error(f"Payload inválido para generate_grass_cache: {exc}", inicio)

        guard = await self._stage_guard(force=params.force_stage_guard)
        if guard is not None:
            return self._error(guard, inicio)

        try:
            self._ensure_runtime_deps()  # resuelve MO2/paths lazy (post-hidratación)
            pm = self._require(self._profile_manager, "profile_manager (GrassProfileManager)")
            mo2 = self._require(self._mo2, "mo2 (MO2Controller)")
            config = self._build_runner_config(params, pm)
        except GrassCacheServiceError as exc:
            return self._error(str(exc), inicio)

        await self._publish(
            "pipeline.grass_cache.started",
            {"worldspaces": params.worldspaces, "conflicting_mods": params.conflicting_mods},
        )

        tx = self._lock_factory(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.RESOURCE_ID,
            agent_id=self.AGENT_ID,
            metadata={"source": "grass_cache", "worldspaces": len(params.worldspaces)},
        )
        # El juego lee loadorder.txt/plugins.txt durante TODO el crash-loop: sin
        # el lock load-order, un sort de LOOT concurrente reescribiría el orden
        # a mitad del precache y el cache saldría de un orden inconsistente
        # (review Codex #291). Orden de adquisición fijo (grass-cache →
        # load-order); nadie toma load-order → grass-cache, así que no hay
        # deadlock. LOOT queda bloqueado mientras dura el ritual — es correcto.
        lo_tx = self._lock_factory(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=LOAD_ORDER_RESOURCE_ID,
            agent_id=self.AGENT_ID,
            metadata={"source": "grass_cache"},
        )
        journal_tx: int | None = None
        run_result: GrassCacheRunResult | None = None
        error_msg: str | None = None
        teardown_failures: list[str] = []
        try:
            async with tx, lo_tx:
                if self._journal is not None:
                    journal_tx = await self._journal.begin_transaction(
                        "Grass precache (NGIO, Stage 8)", agent_id=self.AGENT_ID
                    )
                try:
                    # -- Fase B: preparación aislada (solo el clon se muta) --
                    await pm.create_clone_profile()
                    await pm.build_config_mod(params.worldspaces)
                    if params.conflicting_mods:
                        await pm.disable_conflicting_mods(params.conflicting_mods)
                    # -- Fase C: crash-loop --
                    runner = self._runner_factory(config, mo2, on_progress=self._make_progress_publisher())
                    run_result = await runner.run()
                finally:
                    # -- Fase D: restaurar el entorno SIEMPRE que ESTA invocación
                    # haya empezado el setup, y DENTRO del lock: con el lock ya
                    # suelto (o nunca adquirido) el teardown borraría el clon/mod
                    # de otro run activo (review Codex #291). El cache queda en
                    # overwrite/Grass — jamás se borra (parcial reanuda; completo
                    # es el producto). Los fallos de limpieza se exponen en el
                    # resultado en vez de tragarse (§1.6).
                    teardown_failures = await self._teardown_best_effort()
        except LockAcquisitionError as exc:
            # Sin lock no somos dueños de NINGÚN estado: ni Fase B ni teardown.
            return self._error(f"No se pudo tomar el lock del ritual de grass: {exc}", inicio)
        except Exception as exc:  # noqa: BLE001 — GrassProfileError y afines → contrato
            # SOP §5 regla 5: el stage index es la señal primaria de debugging.
            logger.warning(
                "Stage 8 (No Grass In Objects): el ritual de grass falló",
                exc_info=True,
                extra={"pipeline_stage": 8},
            )
            error_msg = str(exc)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # Estos tres NO heredan de Exception (CancelledError deriva directo
            # de BaseException en 3.8+), así que sin este handler el cierre del
            # journal de más abajo se saltearía y la TX quedaría PENDING hasta el
            # sweep de 24 h. Se enumeran EXPLÍCITAMENTE (no `except BaseException`
            # desnudo, prohibido por coding_conventions §3) para no interceptar
            # excepciones interpreter-level inesperadas como GeneratorExit o
            # BaseExceptionGroup. Cierre best-effort ANTES de propagar (§1.3).
            await self._journal_close(journal_tx, exito=False)
            raise

        # Un fallo posterior al runner (p.ej. lease perdido al liberar el lock)
        # invalida el éxito: la exclusividad no estuvo garantizada, así que el
        # journal NO se commitea aunque run_result diga success (Codex #291).
        exito = error_msg is None and run_result is not None and run_result.success
        await self._journal_close(journal_tx, exito=exito)
        resultado = self._componer_resultado(run_result, error_msg, inicio)
        if teardown_failures:
            # El cache en overwrite/Grass está OK, pero el clon/mod no se
            # limpiaron: el operador debe borrarlos a mano o el próximo run
            # fallará con "el clon ya existe" (fail-closed de create_clone_profile).
            resultado["teardown_failures"] = teardown_failures
        await self._publish(
            "pipeline.grass_cache.completed",
            {
                "success": resultado["success"],
                "outcome": resultado.get("outcome"),
                "duration_seconds": resultado["duration_seconds"],
            },
        )
        return resultado

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    async def _stage_guard(self, *, force: bool) -> str | None:
        """Guard Stage 5→8: LOOT completado según el journal, o mensaje de error."""
        if force:
            logger.warning("Guard Stage 5→8 SALTEADO por force_stage_guard=True (visible en HITL).")
            return None
        if self._journal is None:
            return (
                "Guard Stage 5→8: sin journal no puede verificarse que LOOT (Stage 5) haya "
                "completado. Configurá el journal o pasá force_stage_guard=True (visible en HITL)."
            )
        entry = await self._journal.get_last_operation(self.LOOT_AGENT_ID, statuses=[OperationStatus.COMPLETED])
        if entry is None:
            return (
                "Stage 5 (LOOT) no consta como completado en el journal: corré "
                "execute_loot_sorting antes del precache de grass (orden del SOP), "
                "o pasá force_stage_guard=True si sabés lo que hacés."
            )
        # Una operación COMPLETED pelada NO prueba un sort exitoso: LOOT
        # persiste el ActionManifest (COMPLETED) ANTES de runner.sort(), y si
        # el sort falla solo la TRANSACCIÓN se marca rolled-back — la operación
        # queda. El marcador confiable es el FlightReport commiteado; el contrato
        # LOOT↔grass vive en journal_contracts (review Codex #291 + §2.2).
        if not is_flight_report_committed(entry.metadata):
            return (
                "Stage 5 (LOOT) no consta como completado Y commiteado: la última "
                "operación journalizada de LOOT no es el informe de vuelo de un sort "
                "exitoso (un manifiesto pre-sort queda COMPLETED aunque el sort falle). "
                "Corré execute_loot_sorting hasta el éxito, o pasá force_stage_guard=True."
            )
        return None

    def _build_runner_config(self, params: GenerateGrassCacheParams, pm: GrassProfileManager) -> GrassCacheConfig:
        game_path = self._require(self._game_path, "game_path (dir de SkyrimSE.exe — SKYRIM_PATH)")
        grass_dir = self._require(self._overwrite_grass_dir, "overwrite_grass_dir (<mo2>/overwrite/Grass)")
        overrides: dict[str, Any] = {}
        if params.max_runtime_s is not None:
            overrides["max_runtime_s"] = params.max_runtime_s
        if params.max_restarts is not None:
            overrides["max_restarts"] = params.max_restarts
        if params.stall_threshold is not None:
            overrides["stall_threshold"] = params.stall_threshold
        try:
            return GrassCacheConfig(
                game_path=game_path,
                overwrite_grass_dir=grass_dir,
                profile=pm.clone_profile,
                **overrides,
            )
        except ValueError as exc:
            raise GrassCacheServiceError(f"Configuración del runner inválida: {exc}") from exc

    def _make_progress_publisher(self) -> Callable[[GrassCacheProgress], Any]:
        """Callback runner→bus (el runner es agnóstico del bus — D3)."""

        async def _publicar(progreso: GrassCacheProgress) -> None:
            await self._publish("pipeline.grass_cache.progress", dataclasses.asdict(progreso))

        return _publicar

    async def _teardown_best_effort(self) -> list[str]:
        """Teardown de Fase D; devuelve los paths que no se pudieron limpiar.

        Nunca lanza (el teardown jamás enmascara el resultado del ritual), pero
        tampoco traga en silencio: la lista de fallos vuelve al caller para
        exponerla en el resultado (análisis hostil §1.6).
        """
        if self._profile_manager is None:
            return []
        try:
            fallidos = await self._profile_manager.teardown()
        except Exception:  # noqa: BLE001 — el teardown jamás enmascara el resultado
            logger.warning("El teardown del ritual de grass lanzó (limpiar a mano el clon/mod)", exc_info=True)
            return ["<el teardown lanzó una excepción inesperada — revisar logs>"]
        return [str(p) for p in fallidos]

    async def _journal_close(self, journal_tx: int | None, *, exito: bool) -> None:
        if self._journal is None or journal_tx is None:
            return
        with contextlib.suppress(Exception):
            if exito:
                await self._journal.commit_transaction(journal_tx)
            else:
                await self._journal.mark_transaction_rolled_back(journal_tx)

    def _componer_resultado(
        self, run_result: GrassCacheRunResult | None, error_msg: str | None, inicio: float
    ) -> dict[str, Any]:
        duracion = time.monotonic() - inicio
        if error_msg is not None:
            # Prioridad al error aunque el runner haya terminado: un fallo
            # posterior (lease perdido, teardown que propagó) invalida el
            # éxito. Los datos del runner viajan como diagnóstico.
            resultado = self._error(error_msg, inicio)
            if run_result is not None:
                resultado["outcome"] = run_result.outcome
                resultado["cgid_count"] = run_result.cgid_count
            return resultado
        if run_result is None:
            return self._error("El ritual de grass no llegó a ejecutar el runner.", inicio)
        return {
            "success": run_result.success,
            "message": run_result.message,
            "outcome": run_result.outcome,
            "crash_count": run_result.crash_count,
            "cgid_count": run_result.cgid_count,
            "cache_size_mb": run_result.cache_size_mb,
            "elapsed_s": run_result.elapsed_s,
            "cancelled": run_result.cancelled,
            "stalled": run_result.stalled,
            "duration_seconds": duracion,
        }

    async def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            from sky_claw.antigravity.core.event_bus import Event

            await self._event_bus.publish(Event(topic=topic, payload=payload, source=self.AGENT_ID))
        except Exception:  # noqa: BLE001 — un bus roto no corta el ritual
            logger.warning("No se pudo publicar %s al bus", topic, exc_info=True)

    @staticmethod
    def _error(mensaje: str, inicio: float) -> dict[str, Any]:
        return {"success": False, "message": mensaje, "duration_seconds": time.monotonic() - inicio}

    @staticmethod
    def _require(valor: Any, nombre: str) -> Any:
        if valor is None:
            raise GrassCacheServiceError(f"Dependencia no configurada: {nombre}.")
        return valor


__all__ = [
    "GRASS_CACHE_RESOURCE_ID",
    "GenerateGrassCacheParams",
    "GrassCacheService",
    "GrassCacheServiceError",
    "GrassRuntimeDeps",
]
