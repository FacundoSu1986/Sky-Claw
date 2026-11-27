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
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sky_claw.app.db.journal import OperationStatus, OperationType, TransactionStatus
from sky_claw.app.db.journal_contracts import (
    GRASS_VERDICT_KIND,
    TEARDOWN_INCOMPLETE_KIND,
    is_flight_report_committed,
)
from sky_claw.app.db.locks import (
    LockAcquisitionError,
    LockReleaseError,
    SnapshotTransactionLock,
    adquirir_lease_definitiva,
    liberar_lease_definitiva,
)
from sky_claw.config import SystemPaths
from sky_claw.local.tools.grass_cache_fence import (
    FenceScope,
    bloqueo_por_fence_scope,
    liberar_fence_scope,
)
from sky_claw.local.tools.grass_cache_runner import (
    _PRECACHE_FLAG,  # fuente de verdad del nombre del flag (evita duplicar el string mágico)
    GrassCacheConfig,
    GrassCacheProgress,
    GrassCacheRunner,
    GrassCacheRunResult,
    esperar_terminar,
)
from sky_claw.local.tools.grass_cache_safety import (
    APROBACION_REBUILD,
    GrassCacheMode,
    preparar_arranque,
)
from sky_claw.local.tools.grass_cache_verdict import (
    VeredictoCache,
    invalidar_certificacion,
)
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
from sky_claw.local.xedit.grass_analyzer import GrassAnalyzer

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from sky_claw.app.core.event_bus import CoreEventBus
    from sky_claw.app.db.journal import OperationJournal
    from sky_claw.app.db.locks import DistributedLockManager
    from sky_claw.app.db.snapshots import FileSnapshotManager
    from sky_claw.local.mo2.grass_profile import GrassProfileManager
    from sky_claw.local.mo2.vfs import MO2Controller
    from sky_claw.local.xedit.runner import XEditRunner

logger = logging.getLogger(__name__)

#: Recurso del lock distribuido: un solo ritual de grass a la vez.
GRASS_CACHE_RESOURCE_ID = "grass-cache"

#: Agente del reconciliador de arranque — distinto del servicio de grass, así el
#: lock que toma para barrer el flag no se confunde con el del ritual en sí.
_RECONCILE_AGENT_ID = "precache-flag-reconciler"
#: TTL corto: el lock solo cubre un exists()+unlink(). Si el proceso muere con él
#: tomado, expira solo (el TTL se auto-recupera; no wedgea el próximo ritual).
_RECONCILE_TTL_SECONDS = 30.0


async def reconcile_orphan_precache_flag(
    game_path: pathlib.Path,
    lock_manager: DistributedLockManager,
) -> bool:
    """Barre un ``PrecacheGrass.txt`` huérfano de una muerte dura previa (U-03).

    Una muerte dura del proceso Python (SIGKILL/OOM/corte de luz) durante el
    precache evade el ``finally`` que borra el flag en :class:`GrassCacheRunner`,
    dejando ``PrecacheGrass.txt`` junto a ``SkyrimSE.exe``. Si el usuario abre el
    juego con NGIO activo, ese flag lo re-mete en modo precache (800x400,
    crash-loop) — corrupción silenciosa de la experiencia de juego, sin rastro en
    el pipeline. Este hook idempotente de arranque lo detecta y lo borra.

    **Seguro ante concurrencia (atómico):** el borrado se hace bajo el MISMO lock
    ``grass-cache`` que serializa el ritual, adquirido de forma atómica. Si un
    ritual está en curso (aun en OTRA instancia de Sky-Claw) el flag es legítimo y
    no se toca. Un simple ``get_lock_info`` no alcanzaría: entre el chequeo y el
    ``unlink`` otra instancia podría arrancar el ritual y escribir el flag, que
    este proceso borraría (TOCTOU). Adquirir el lock cierra esa ventana — el que
    lo posee gana, y el perdedor no borra nada. El ``get_lock_info`` previo es solo
    un fast-path para no pagar el backoff de ``acquire_lock`` cuando ya hay dueño.

    Returns:
        ``True`` si se borró un flag huérfano; ``False`` si no había nada que
        hacer (sin flag, o hay un ritual activo que lo posee legítimamente).
    """
    flag = game_path / _PRECACHE_FLAG
    if not await asyncio.to_thread(flag.exists):
        return False

    # Fast-path: si el ritual ya está en curso, ni intentamos adquirir (evita el
    # backoff/retry de acquire_lock ante un lock tomado, que colgaría el arranque).
    info = await lock_manager.get_lock_info(GRASS_CACHE_RESOURCE_ID)
    if info is not None and not info.is_expired:
        logger.info(
            "PrecacheGrass.txt presente pero el lock '%s' sigue vivo (ritual en curso); no se toca el flag.",
            GRASS_CACHE_RESOURCE_ID,
        )
        return False

    # Cierre del TOCTOU: adquirimos el lock del ritual de forma atómica. Si otra
    # instancia arrancó un precache entre el chequeo de arriba y este punto,
    # acquire_lock falla (el ritual ya lo posee) y NO tocamos su flag legítimo.
    try:
        lease = await adquirir_lease_definitiva(
            lock_manager, GRASS_CACHE_RESOURCE_ID, _RECONCILE_AGENT_ID, ttl=_RECONCILE_TTL_SECONDS
        )
    except LockAcquisitionError:
        logger.info(
            "No se pudo adquirir '%s' para reconciliar (ritual activo); no se toca el flag.",
            GRASS_CACHE_RESOURCE_ID,
        )
        return False

    try:
        # Re-chequeo bajo el lock: entre el primer exists() y la adquisición el
        # estado pudo cambiar; solo borramos un flag que sigue presente.
        if not await asyncio.to_thread(flag.exists):
            return False

        def _unlink() -> bool:
            try:
                flag.unlink(missing_ok=True)
                return True
            except OSError:
                logger.warning("No se pudo borrar el PrecacheGrass.txt huérfano en %s", flag, exc_info=True)
                return False

        removed = await asyncio.to_thread(_unlink)
        if removed:
            logger.info(
                "PrecacheGrass.txt huérfano barrido de %s (sin ritual de grass activo): "
                "evita que el juego arranque en modo precache tras una muerte dura previa.",
                game_path,
            )
        return removed
    finally:
        try:
            await liberar_lease_definitiva(
                lock_manager,
                GRASS_CACHE_RESOURCE_ID,
                _RECONCILE_AGENT_ID,
                acquired_at=lease.acquired_at,
            )
        except LockReleaseError:
            logger.warning(
                "Fallo al liberar '%s' tras reconciliar (el TTL lo recupera).",
                GRASS_CACHE_RESOURCE_ID,
                exc_info=True,
            )


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
        mode: Contrato de arranque (P0 SAFE CACHE). ``new`` (default) sólo
            arranca con el caché vacío; ``resume`` continúa desde un progreso
            demostrable sin truncarlo; ``rebuild`` regenera desde cero y exige
            ``rebuild_approval``. NGIO borra el caché cuando el flag no tiene
            progreso, así que el default es el que no destruye nada.
        rebuild_approval: Token literal (``REBUILD``) que autoriza el modo
            destructivo. Viaja al prompt HITL en ``describe_for_approval``.
        ack_usvfs_no_verificado: Reconoce explícitamente que la cobertura USVFS
            del respaldo NO está demostrada (cubre ``overwrite/Grass``, no el
            ``Grass/`` de los mods que NGIO ve por la VFS). Sin esto, REBUILD
            queda en HOLD. También viaja al prompt HITL.
    """

    model_config = ConfigDict(strict=True)

    worldspaces: list[str] = Field(min_length=1)
    conflicting_mods: list[str] = Field(default_factory=list)
    max_runtime_s: float | None = None
    max_restarts: int | None = None
    stall_threshold: int | None = None
    force_stage_guard: bool = False
    mode: GrassCacheMode = "new"
    rebuild_approval: str | None = None
    ack_usvfs_no_verificado: bool = False

    @model_validator(mode="after")
    def _rebuild_exige_aprobacion(self) -> GenerateGrassCacheParams:
        """REBUILD sin el token no llega ni al HITL: se rechaza en el payload.

        La verificación de fondo (respaldo verificado) es del runner, que es el
        punto sin retorno; esto sólo evita pedirle al operador que apruebe algo
        que de todos modos va a ser rechazado.
        """
        if self.mode == "rebuild" and self.rebuild_approval != APROBACION_REBUILD:
            raise ValueError(
                f"mode='rebuild' exige rebuild_approval='{APROBACION_REBUILD}': regenera el caché desde cero "
                "y NGIO borra el anterior."
            )
        return self


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
        try:
            deps = self._runtime_deps_provider()
        except RuntimeError as exc:
            raise GrassCacheServiceError(f"Error resolviendo dependencias de MO2 para grass cache: {exc}") from exc
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
        except (GrassCacheServiceError, RuntimeError) as exc:
            return self._error(str(exc), inicio)

        # F1: el fence se consulta ANTES de cualquier mutación del ritual. Un marcador ilegible o de
        # un proceso vivo/indeterminado bloquea; uno de un proceso ya terminado se considera obsoleto.
        if config.backup_root is not None:
            scope = FenceScope.desde_config(game_path=config.game_path, overwrite_grass_dir=config.overwrite_grass_dir)
            bloqueo = await asyncio.to_thread(bloqueo_por_fence_scope, config.backup_root, scope)
            if bloqueo is not None:
                logger.warning("Ritual de grass bloqueado por el fence: %s", bloqueo, extra={"pipeline_stage": 8})
                return self._error(bloqueo, inicio)

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
                # I5: la propiedad del lease se comprueba con la primitiva REAL antes de la primera
                # mutación, no sólo antes del unlink final. Estar dentro del `async with` no prueba
                # que el lease siga vigente.
                try:
                    from sky_claw.app.db.locks import LockLeaseLostError

                    await tx.assert_owned(verify_db=True)
                    await lo_tx.assert_owned(verify_db=True)
                except LockLeaseLostError as exc:
                    logger.error("Lease perdido antes de mutar: %s", exc, extra={"pipeline_stage": 8})
                    return self._error(f"Lease del ritual perdido antes de mutar: {exc}", inicio)

                # F1 (contrato): el chequeo previo a los locks es sólo una optimización. La
                # revalidación DENTRO de grass-cache + load-order es la que manda: entre el primer
                # chequeo y la adquisición, otro ritual pudo dejar su marcador.
                if config.backup_root is not None:
                    scope = FenceScope.desde_config(
                        game_path=config.game_path, overwrite_grass_dir=config.overwrite_grass_dir
                    )
                    bloqueo_bajo_locks = await asyncio.to_thread(bloqueo_por_fence_scope, config.backup_root, scope)
                    if bloqueo_bajo_locks is not None:
                        logger.warning(
                            "Ritual de grass bloqueado por el fence bajo locks: %s",
                            bloqueo_bajo_locks,
                            extra={"pipeline_stage": 8},
                        )
                        return self._error(bloqueo_bajo_locks, inicio)

                # P0 SAFE CACHE (F-01): el guard de arranque corre ANTES de
                # clonar el perfil. Es la MISMA función que usa el runner
                # (``preparar_arranque``), así no hay dos políticas que puedan
                # divergir: acá se evita el trabajo inútil de un run que el
                # runner va a rechazar, y el runner vuelve a verificar sobre el
                # inventario fresco (el punto sin retorno es el flag).
                # `esperar_terminar`: si el llamador se cancela mientras el hilo
                # COPIA EL RESPALDO, no se puede propagar hasta que el hilo
                # termine. Soltar los dos locks con la copia en vuelo dejaría a
                # un reintento escribir los mismos caminos de respaldo/progreso
                # en paralelo con el hilo abandonado.
                await esperar_terminar(
                    asyncio.to_thread(
                        preparar_arranque,
                        modo=config.mode,
                        game_path=config.game_path,
                        grass_dir=config.overwrite_grass_dir,
                        aprobacion=config.rebuild_approval,
                        raiz_estado=config.backup_root,
                        ack_usvfs_no_verificado=config.ack_usvfs_no_verificado,
                        perfil=config.perfil_de_progreso,
                    ),
                    descripcion="preparar_arranque",
                )
                if self._journal is not None:
                    journal_tx = await self._journal.begin_transaction(
                        "Grass precache (NGIO, Stage 8)", agent_id=self.AGENT_ID
                    )
                runner: GrassCacheRunner | None = None
                terminalidad_sin_probar: str | None = None
                # F2: cada ejecución sólo limpia recursos cuya PROPIEDAD estableció. Un fallo de
                # setup (p. ej. el clon ya existe, creado por otro ritual) no autoriza destruirlo.
                clone_creado = False
                try:
                    # -- Fase B: preparación aislada (solo el clon se muta) --
                    await pm.create_clone_profile()
                    clone_creado = True
                    await pm.build_config_mod(params.worldspaces)
                    if params.conflicting_mods:
                        await pm.disable_conflicting_mods(params.conflicting_mods)
                    # -- Fase C: crash-loop --
                    runner = self._runner_factory(config, mo2, on_progress=self._make_progress_publisher())
                    run_result = await runner.run()
                    # H2/H5: si no se demostró que el juego terminó, el teardown NO destruye el
                    # clon/mod: un proceso vivo puede seguir usándolos. Se preserva y se reporta.
                    terminalidad_sin_probar = self._terminalidad_no_verificada(run_result)
                finally:
                    # -- Fase D: restaurar el entorno SIEMPRE que ESTA invocación
                    # haya empezado el setup, y DENTRO del lock: con el lock ya
                    # suelto (o nunca adquirido) el teardown borraría el clon/mod
                    # de otro run activo (review Codex #291). El cache queda en
                    # overwrite/Grass — jamás se borra (parcial reanuda; completo
                    # es el producto). Los fallos de limpieza se exponen en el
                    # resultado en vez de tragarse (§1.6).
                    sin_verificar = terminalidad_sin_probar
                    if sin_verificar is None and runner is not None:
                        # Camino de excepción: el resultado no se construyó, pero el runner sí
                        # clasificó la terminalidad antes de propagar.
                        propia = runner.terminalidad
                        if propia in ("vivo", "indeterminado"):
                            sin_verificar = str(propia)
                        elif runner.terminalidad_integral is False:
                            # P1/C3: la DECISIÓN la toma la evidencia integral, no un string aislado.
                            # Un `terminado` sin ausencia integral demostrada (camino de excepción
                            # dura) NO autoriza destruir el clon/mod: se preservan perfil y config.
                            sin_verificar = "ausencia_integral_no_demostrada"
                    if sin_verificar is not None:
                        teardown_failures = [
                            "teardown del clon/mod OMITIDO: terminalidad del juego NO demostrada "
                            f"({sin_verificar}); el perfil temporal y su configuración se "
                            "conservan porque un proceso vivo puede estar usándolos"
                        ]
                    elif not clone_creado:
                        # F2: el clon es de OTRA ejecución (setup falló antes de crearlo, o ya
                        # existía). No se destruye.
                        teardown_failures = [
                            *teardown_failures,
                            "teardown del clon OMITIDO: esta ejecución no creó el clon (propiedad "
                            "de otra corrida); no se destruyen recursos ajenos",
                        ]
                    else:
                        teardown_failures = await self._teardown_best_effort()

                    # I5/I8: el fence se libera ÚLTIMO, con los leases todavía en mano y verificados
                    # de nuevo: si la exclusividad ya no se puede garantizar, se conserva. El runner
                    # sólo aporta evidencia; la autoridad es del servicio.
                    # P1/C3: sin resultado del runner NO hay evidencia integral de cierre, así que
                    # NO se intenta siquiera una liberación `autorizado=False`: el fence se conserva.
                    evidencia_final = (run_result.evidencia or {}) if run_result is not None else {}
                    run_id_fence = evidencia_final.get("fence_run_id") if run_result is not None else None
                    integral = evidencia_final.get("terminalidad_integral") is True
                    autorizado = False
                    if isinstance(run_id_fence, str) and integral:
                        try:
                            from sky_claw.app.db.locks import LockLeaseLostError

                            await tx.assert_owned(verify_db=True)
                            await lo_tx.assert_owned(verify_db=True)
                            autorizado = True
                        except LockLeaseLostError as exc:
                            logger.error(
                                "Fence CONSERVADO: lease perdido antes de liberar (%s).",
                                exc,
                                extra={"pipeline_stage": 8},
                            )
                            teardown_failures = [*teardown_failures, f"fence conservado: lease perdido ({exc})"]
                    if isinstance(run_id_fence, str) and config.backup_root is not None:
                        liberacion = await asyncio.to_thread(
                            liberar_fence_scope,
                            config.backup_root,
                            FenceScope.desde_config(
                                game_path=config.game_path,
                                overwrite_grass_dir=config.overwrite_grass_dir,
                            ),
                            run_id=run_id_fence,
                            autorizado=autorizado,
                        )
                        if not liberacion.ok:
                            teardown_failures = [
                                *teardown_failures,
                                "fence conservado: " + "; ".join((*liberacion.conservados, *liberacion.fallos)),
                            ]
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
        # Una limpieza incompleta invalida el éxito: dejar el flag en disco pone
        # al juego del usuario en modo precache, y borrarlo sin archivar su
        # progreso se lleva la única evidencia de reanudación. Las dos cosas
        # viajan por la misma vía (resultado + marca en el journal).
        limpieza_incompleta = run_result is not None and (
            run_result.flag_residual or run_result.progreso_perdido or run_result.flag_alternativo_residual
        )
        exito = error_msg is None and run_result is not None and run_result.success and not limpieza_incompleta
        if run_result is not None and run_result.flag_residual:
            teardown_failures = [
                *teardown_failures,
                f"{_PRECACHE_FLAG} residual: " + ("; ".join(run_result.limpieza_errores) or "causa desconocida"),
            ]
        if run_result is not None and run_result.progreso_perdido:
            teardown_failures = [
                *teardown_failures,
                "progreso no archivado: " + ("; ".join(run_result.limpieza_errores) or "causa desconocida"),
            ]
        if run_result is not None and run_result.flag_alternativo_residual:
            # El alternativo no es nuestro y no se borra, pero mientras exista NGIO
            # puede abrirlo como flag de progreso: la limpieza no está completa.
            teardown_failures = [
                *teardown_failures,
                "flag alternativo residual: " + ("; ".join(run_result.limpieza_errores) or "causa desconocida"),
            ]
        # H6: la certificación del runner es sobre lo que OBSERVÓ. Si la operación quedó invalidada
        # (lease perdido, teardown a medias, error posterior), el resultado, el journal y el evento
        # no pueden publicar una certificación válida. El resultado se compone ANTES del cierre para
        # que las tres superficies digan lo mismo.
        resultado = self._componer_resultado(run_result, error_msg, inicio)
        resultado["success"] = exito
        if run_result is not None and run_result.certificado and not exito:
            self._invalidar_certificacion(
                resultado,
                "limpieza_incompleta" if limpieza_incompleta else "operacion_invalidada",
            )
        if limpieza_incompleta and error_msg is None:
            resultado["message"] = "El ritual terminó, pero la limpieza del flag quedó incompleta: " + "; ".join(
                teardown_failures
            )
        if teardown_failures:
            # El cache en overwrite/Grass está OK, pero el clon/mod no se
            # limpiaron: el operador debe borrarlos a mano o el próximo run
            # fallará con "el clon ya existe" (fail-closed de create_clone_profile).
            resultado["teardown_failures"] = teardown_failures
        try:
            # U-09: la constancia del cleanup a medias va ANTES del cierre, para
            # que la entrada quede dentro de la TX que se está por commitear. Va
            # DENTRO del try: una cancelación acá tampoco puede dejar la TX
            # pendiente (antes escapaba sin ningún intento de cierre).
            await self._journalizar_teardown_incompleto(journal_tx, teardown_failures)
            # H6: se journaliza el veredicto RECONCILIADO, no el del runner.
            await self._journalizar_veredicto(journal_tx, resultado)
            await self._journal_close(journal_tx, exito=exito)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # Auditoría hostil (V-1): este cierre corre DESPUÉS de soltar los
            # locks, fuera del except de cancelación de arriba — sin este
            # reintento, una cancelación exacta durante el commit dejaría la
            # TX sin ningún intento de cierre.
            await self._journal_rollback_si_sigue_pendiente(journal_tx)
            raise
        await self._publish(
            "pipeline.grass_cache.completed",
            {
                "success": resultado["success"],
                "outcome": resultado.get("outcome"),
                "duration_seconds": resultado["duration_seconds"],
                # P1: el evento lleva el veredicto. Un suscriptor que necesite certificación NO
                # puede inferirla de `success` (D1 del propietario).
                "verdict": resultado.get("verdict"),
                "certificado": resultado.get("certificado"),
                "alcance_certificado": resultado.get("alcance_certificado"),
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
                mode=params.mode,
                rebuild_approval=params.rebuild_approval,
                backup_root=self._raiz_de_estado(),
                ack_usvfs_no_verificado=params.ack_usvfs_no_verificado,
                # `profile` es el CLON (constante, para lanzar); la evidencia de
                # progreso se ata al perfil de ORIGEN para que dos perfiles de la
                # misma instalación no se presten progreso.
                progress_profile=pm.source_profile,
                # P1 (F-12/C1): los worldspaces pedidos son parte de la identidad de la corrida y
                # requisito de certificación (un worldspace pedido sin artefactos no certifica).
                worldspaces=tuple(params.worldspaces),
                **overrides,
            )
        except ValueError as exc:
            raise GrassCacheServiceError(f"Configuración del runner inválida: {exc}") from exc

    @staticmethod
    def _raiz_de_estado() -> pathlib.Path:
        """Raíz durable del estado del ritual: respaldo + progreso archivado.

        En ``SystemPaths.runtime_state_dir()`` (per usuario, estable entre runs)
        por el mismo motivo que el resto de las etapas: una raíz relativa al
        ``cwd`` haría que el respaldo de un ``rebuild`` quedara en un lugar
        distinto según desde dónde se lanzó el ritual.
        """
        return SystemPaths.runtime_state_dir() / "grass_cache"

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
        try:
            if exito:
                await self._journal.commit_transaction(journal_tx)
            else:
                await self._journal.mark_transaction_rolled_back(journal_tx)
        except Exception:  # noqa: BLE001 — cierre best-effort: nunca enmascara el resultado del ritual
            # Auditoría hostil (A-1): antes se tragaba en silencio total; un
            # fallo de BD acá desincroniza el journal sin dejar rastro.
            logger.warning(
                "No se pudo cerrar la transacción del journal de grass (tx=%s, exito=%s)",
                journal_tx,
                exito,
                exc_info=True,
            )

    async def _journalizar_teardown_incompleto(self, journal_tx: int | None, fallos: list[str]) -> None:
        """Registra en el journal que el teardown dejó residuos en disco (U-09).

        El caso que motivó el ítem es el ritual que termina con el cache bien
        generado (y preservado en ``overwrite/Grass``) pero con el clon de perfil
        o el mod de config sin borrar: eso solo viajaba en el dict de retorno, así
        que el journal commiteaba un éxito limpio y el audit trail afirmaba que el
        FS había quedado consistente.

        Degradar la TX a ``ROLLED_BACK`` NO es la respuesta — mentiría sobre un
        cache que sí existe y contradiría el ``success=True`` del resultado. Como
        ``transactions`` solo admite ``pending``/``committed``/``rolled_back``, ese
        estado mixto se expresa con una operación **fallida dentro de la TX
        commiteada**, marcada con :data:`TEARDOWN_INCOMPLETE_KIND`.

        **Se registra sea cual sea el desenlace del ritual, no solo en éxito.** El
        marcador afirma "estos paths quedaron sin limpiar", no "el producto está
        OK": los residuos son igual de reales cuando el run falló, y ahí el dato
        es aún más útil para el operador. Condicionarlo a ``exito`` reabriría la
        divergencia que este ítem cierra, porque ``result["teardown_failures"]``
        tampoco se condiciona: el journal quedaría afirmando menos que el dict de
        retorno sobre el mismo run.

        Best-effort, igual que :meth:`_journal_close`: el journal nunca enmascara
        el resultado del ritual. La metadata estructurada va en
        ``begin_operation`` porque ``fail_operation`` solo persiste ``$.error``.
        """
        if self._journal is None or journal_tx is None or not fallos:
            return
        try:
            entry_id = await self._journal.begin_operation(
                agent_id=self.AGENT_ID,
                operation_type=OperationType.FILE_DELETE,  # lo que falló es borrar el clon/mod
                target_path=fallos[0],
                transaction_id=journal_tx,
                metadata={"kind": TEARDOWN_INCOMPLETE_KIND, "paths": list(fallos)},
            )
            await self._journal.fail_operation(entry_id, error="; ".join(fallos))
        except Exception:  # noqa: BLE001 — best-effort: perder la anotación no puede tumbar el run
            logger.warning(
                "No se pudo journalizar el teardown incompleto del ritual de grass (tx=%s, fallos=%s)",
                journal_tx,
                fallos,
                exc_info=True,
            )

    @staticmethod
    def _terminalidad_no_verificada(run_result: GrassCacheRunResult | None) -> str | None:
        """Terminalidad observada cuando NO permite afirmar que el juego terminó (H2/H5)."""
        if run_result is None:
            return None
        evidencia = run_result.evidencia or {}
        terminalidad = evidencia.get("terminalidad") if isinstance(evidencia, dict) else None
        # Sólo los estados explícitamente no verificables bloquean el teardown; cualquier otro
        # valor (incluido el de un doble de test sin evidencia) no lo bloquea.
        return str(terminalidad) if terminalidad in ("vivo", "indeterminado") else None

    async def _journalizar_veredicto(self, journal_tx: int | None, resultado: dict[str, Any] | None) -> None:
        """Registra el VEREDICTO ya reconciliado dentro de la TX (P1 / H6).

        Una TX commiteada puede afirmar «el ritual produjo un caché» y a la vez «ese caché NO se
        pudo certificar». ``transactions`` no tiene metadata, así que el estado mixto se expresa con
        una operación —el mismo mecanismo que U-09—: ``complete_operation`` cuando certifica y
        ``fail_operation`` con las razones cuando no. La metadata **no** lleva ``files_touched``:
        sembrar evidencia de artefactos falsearía el oráculo de recovery.

        **El veredicto que se registra es el final, no el del runner**: si la operación perdió la
        exclusividad o el teardown quedó a medias, la certificación ya fue degradada antes de llegar
        acá y el journal no puede afirmar lo contrario.

        Best-effort, como el resto del cierre: el journal nunca enmascara el resultado del ritual.
        """
        if self._journal is None or journal_tx is None or resultado is None:
            return
        if "verdict" not in resultado:
            return
        evidencia = resultado.get("evidencia") or {}
        worldspaces = evidencia.get("worldspaces_solicitados") if isinstance(evidencia, dict) else None
        razones = list(resultado.get("verdict_razones") or ())
        metadata: dict[str, Any] = {
            "kind": GRASS_VERDICT_KIND,
            "verdict": resultado.get("verdict"),
            "certificado": bool(resultado.get("certificado")),
            "razones": razones,
            "alcance_certificado": resultado.get("alcance_certificado"),
            "mode": resultado.get("mode"),
            "worldspaces": list(worldspaces) if isinstance(worldspaces, list) else [],
        }
        try:
            entry_id = await self._journal.begin_operation(
                agent_id=self.AGENT_ID,
                # No hay un OperationType «verificar»: el veredicto describe la MUTACIÓN del caché
                # que esta corrida produjo (o no produjo). El discriminador real es `kind`.
                operation_type=OperationType.FILE_MODIFY,
                target_path=str(evidencia.get("overwrite_grass_dir") if isinstance(evidencia, dict) else None)
                or "overwrite/Grass",
                transaction_id=journal_tx,
                metadata=metadata,
            )
            if metadata["certificado"]:
                await self._journal.complete_operation(entry_id)
            else:
                detalle = "; ".join(razones) or "sin evidencia suficiente"
                await self._journal.fail_operation(entry_id, error=f"caché no certificado: {detalle}")
        except Exception:  # noqa: BLE001 — best-effort: perder el veredicto no puede tumbar el run
            logger.warning(
                "No se pudo journalizar el veredicto del ritual de grass (tx=%s, verdict=%s)",
                journal_tx,
                metadata["verdict"],
                exc_info=True,
                extra={"pipeline_stage": 8},
            )

    @staticmethod
    def _invalidar_certificacion(resultado: dict[str, Any], razon: str) -> None:
        """Degrada la certificación del resultado cuando la OPERACIÓN quedó invalidada (H6).

        La evidencia observada no se falsea: se marca la certificación como no válida, se deja la
        razón y se alinea el ``evidencia`` serializado para que ninguna superficie lea lo anterior.
        """
        veredicto = VeredictoCache(
            verdict=resultado.get("verdict", "unknown"),
            certificado=bool(resultado.get("certificado")),
            exito_operacional=bool(resultado.get("success")),
            razones=tuple(resultado.get("verdict_razones") or ()),
            alcance_certificado=resultado.get("alcance_certificado", "ninguno"),
            trabajo_de_la_corrida="demostrado",
            integridad_observable="no_evaluada",
        )
        final = invalidar_certificacion(veredicto, razon=razon)
        resultado["verdict"] = final.verdict
        resultado["certificado"] = final.certificado
        resultado["verdict_razones"] = list(final.razones)
        resultado["alcance_certificado"] = final.alcance_certificado
        evidencia = resultado.get("evidencia")
        if isinstance(evidencia, dict):
            evidencia["verdict"] = final.verdict
            evidencia["certificado"] = final.certificado
            evidencia["verdict_razones"] = list(final.razones)
            evidencia["alcance_certificado"] = final.alcance_certificado
            # B5: el eje anidado no puede seguir afirmando éxito operacional cuando el `success`
            # final es False por invalidación de la operación.
            evidencia["exito_operacional"] = False
        logger.warning(
            "Certificación invalidada por la operación (%s): %s → %s",
            razon,
            veredicto.verdict,
            final.verdict,
            extra={"pipeline_stage": 8},
        )

    async def _journal_rollback_si_sigue_pendiente(self, journal_tx: int | None) -> None:
        """Reintento de cierre tras una cancelación en la ventana post-lock.

        ``mark_transaction_rolled_back`` no valida el estado (mismo gap que
        ``loot_service.py`` resolvió con un flag local — review Codex #249);
        acá no alcanza un flag local porque la cancelación puede llegar
        DESPUÉS de que el UPDATE de ``commit_transaction`` ya corrió en la
        BD, aunque el `await` de Python la reciba como interrumpida (Codex
        #317, review de este mismo PR). Rollback ciego sobre una TX ya
        COMMITTED corrompería el audit trail de un ritual exitoso. Se
        confirma el estado real antes de decidir.
        """
        if self._journal is None or journal_tx is None:
            return
        try:
            tx = await self._journal.get_transaction(journal_tx)
        except Exception:  # noqa: BLE001 — best-effort: no relanzar por sobre la cancelación real
            logger.warning(
                "No se pudo confirmar el estado del journal de grass tras la cancelación (tx=%s)",
                journal_tx,
                exc_info=True,
            )
            return
        if tx is not None and tx.status is not TransactionStatus.PENDING:
            # Ya COMMITTED (o ROLLED_BACK) por otra vía: no sobre-escribir.
            return
        await self._journal_close(journal_tx, exito=False)

    def _componer_resultado(
        self, run_result: GrassCacheRunResult | None, error_msg: str | None, inicio: float
    ) -> dict[str, Any]:
        duracion = time.monotonic() - inicio
        if error_msg is not None:
            # Prioridad al error aunque el runner haya terminado: un fallo
            # posterior (lease perdido, teardown que propagó) invalida el
            # éxito. Los datos del runner viajan como diagnóstico —incluido el
            # veredicto, que el caller reconcilia después (H6)—.
            resultado = self._error(error_msg, inicio)
            if run_result is not None:
                resultado["outcome"] = run_result.outcome
                resultado["cgid_count"] = run_result.cgid_count
                resultado["mode"] = run_result.mode
                resultado["verdict"] = run_result.verdict
                resultado["certificado"] = run_result.certificado
                resultado["verdict_razones"] = list(run_result.verdict_razones)
                resultado["alcance_certificado"] = run_result.alcance_certificado
                resultado["evidencia"] = run_result.evidencia
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
            "mode": run_result.mode,
            "backup_dir": run_result.backup_dir,
            "flag_residual": run_result.flag_residual,
            "progreso_perdido": run_result.progreso_perdido,
            "flag_alternativo_residual": run_result.flag_alternativo_residual,
            "limpieza_errores": list(run_result.limpieza_errores),
            # P1: el veredicto viaja SIEMPRE. `success` no es certificación (D1 del propietario):
            # un consumidor que necesite una generación certificada mira `certificado`/`verdict`/
            # `alcance_certificado`, no `success`.
            "verdict": run_result.verdict,
            "certificado": run_result.certificado,
            "verdict_razones": list(run_result.verdict_razones),
            "alcance_certificado": run_result.alcance_certificado,
            "evidencia": run_result.evidencia,
            "duration_seconds": duracion,
        }

    async def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            from sky_claw.app.core.event_bus import Event

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
