"""DatabaseLifecycleManager — SQLite WAL lifecycle management for Sky-Claw.

Manages the full lifecycle of SQLite database connections with WAL mode:
- Initialization with orphaned WAL recovery
- Periodic checkpointing (PASSIVE) to prevent unbounded WAL growth
- Graceful shutdown with TRUNCATE checkpoint to eliminate WAL files
- Health monitoring of WAL file sizes
- Signal handlers for SIGINT/SIGTERM to ensure checkpoint on process exit

Design invariants:
- Every init must check for orphaned WAL files and recover them.
- Every shutdown must execute PRAGMA wal_checkpoint(TRUNCATE) before close.
- Signal handlers have a hard 10-second timeout to prevent zombie processes.
- All pragmas are applied once per connection, not per transaction.
- No circular imports with database.py — this module is standalone.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import sqlite3
import time
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple

import aiosqlite
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("SkyClaw.DatabaseLifecycle")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CHECKPOINT_INTERVAL_SECONDS = 300  # 5 minutes
WAL_WARNING_THRESHOLD_BYTES = 10_485_760  # 10 MB
WAL_CRITICAL_THRESHOLD_BYTES = 52_428_800  # 50 MB
SHUTDOWN_CHECKPOINT_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DatabaseLifecycleConfig(BaseModel):
    """Immutable configuration for DatabaseLifecycleManager."""

    model_config = ConfigDict(strict=True, frozen=True)

    wal_checkpoint_interval_seconds: int = DEFAULT_CHECKPOINT_INTERVAL_SECONDS
    wal_warning_threshold_bytes: int = WAL_WARNING_THRESHOLD_BYTES
    wal_critical_threshold_bytes: int = WAL_CRITICAL_THRESHOLD_BYTES
    enable_auto_checkpoint: bool = True
    enable_signal_handlers: bool = True
    busy_timeout_ms: int = 5000
    synchronous_mode: str = "NORMAL"


class WALHealth(BaseModel):
    """Health status of a single database's WAL file."""

    model_config = ConfigDict(strict=True, frozen=True)

    db_path: str
    wal_exists: bool
    wal_size_bytes: int
    status: str  # "healthy" | "warning" | "critical" | "unknown"
    message: str = ""


class _DatabaseOperationCancelled(asyncio.CancelledError):
    """Distingue una operación DB cancelada de la cancelación del llamador."""

    def __init__(self, cause: asyncio.CancelledError) -> None:
        super().__init__(*cause.args)
        self.cause = cause


class _DatabaseOperationFailedAfterCancellation(asyncio.CancelledError):
    """Conserva cancelación externa y fallo posterior de la operación DB."""

    def __init__(
        self,
        cancellation: asyncio.CancelledError,
        operation_error: BaseException,
    ) -> None:
        super().__init__(*cancellation.args)
        self.cancellation = cancellation
        self.operation_error = operation_error


class DatabaseLifecycleShuttingDownError(RuntimeError):
    """El manager dejó de admitir trabajo porque el shutdown ya empezó.

    Fail-closed deliberado: entregar una conexión acá reabriría una DB que el
    shutdown ya cerró —o está por cerrar— y nadie volvería a cerrarla. La
    reapertura existe, pero es EXPLÍCITA (``init_all()``) y está serializada
    contra el shutdown; nunca es un efecto lateral de pedir una conexión.
    """


class DatabaseShutdownIncompleteError(RuntimeError):
    """Shutdown reintentable porque aun quedan conexiones gestionadas."""

    def __init__(self, retained_paths: tuple[str, ...]) -> None:
        self.retained_paths = retained_paths
        super().__init__("Database shutdown incomplete; retained managed connections: " + ", ".join(retained_paths))


class DatabaseConnectionQuarantinedError(RuntimeError):
    """El path quedó fail-closed y no puede entregar ni abrir conexiones.

    Dos situaciones lo producen y ambas son el MISMO hecho: la conexión vigente
    dejó de ser utilizable y todavía no hay un reemplazo coherente.

    - Un cierre NO confirmado: la instancia sigue registrada porque perder el
      ownership de una conexión que quizá siga viva es peor que retenerla, pero
      entregarla otra vez devolvería un objeto dañado. Se recupera reintentando
      ``shutdown_all()``, que es quien conserva el derecho a cerrarla.
    - Una ventana de ``recover_connection()`` en curso: el archivo está por
      moverse y abrir una conexión "fresh" ahí la ataría al inodo que el rename
      se lleva. Se levanta sola si la ventana COMPLETA la reconstrucción; si
      aborta —el rename falló y el archivo declarado corrupto sigue en el
      path—, sobrevive, y tampoco la levanta ``init_all()``: reabrir publicaría
      ese mismo archivo sin que nadie lo haya vuelto a validar. Sale por
      ``shutdown_all()`` completo, igual que el cierre no confirmado.
    """


class DatabaseConnectionReplacedError(RuntimeError):
    """La conexión que se pedía invalidar ya no es la vigente del path.

    Guard de identidad (ABA): el caller quería retirar A y el registro apunta a
    B. Cerrar o expulsar B sería destruir una conexión sana de otro dueño, así
    que la invalidación no toca nada y avisa.
    """


class _CloseOutcome(NamedTuple):
    """Desenlace REAL de un cierre físico, con sus tres ejes separados.

    Existe porque ``BaseException | None`` no alcanza: colapsa CLOSE-3
    (cancelación externa y la conexión SÍ cerró) contra CLOSE-5 (el cierre se
    canceló solo y la conexión sigue viva). Ambos son ``CancelledError`` y sin
    embargo tienen ownership opuesto, así que el llamador no puede distinguirlos
    y termina eligiendo mal quién conserva la conexión.

    ``closed`` es la ÚNICA señal que gobierna ownership; ``cancellation`` es
    ortogonal (hay que restaurarla haya cerrado o no) y ``error`` es diagnóstico.
    """

    closed: bool
    error: BaseException | None
    cancellation: asyncio.CancelledError | None


# ---------------------------------------------------------------------------
# DatabaseLifecycleManager
# ---------------------------------------------------------------------------


class DatabaseLifecycleManager:
    """Manages SQLite WAL lifecycle across multiple database files.

    CONTRATO M-01: Punto de entrada centralizado para conexiones aiosqlite en
    los módulos cubiertos por M-01. Todos ellos deben pedir su conexión vía
    ``await lifecycle.get_connection(path)`` en lugar de ejecutar
    ``aiosqlite.connect(path)`` directamente. Esto garantiza:

    - WAL recovery automática para archivos huérfanos.
    - Pragmas hardenizadas consistentes según la ``DatabaseLifecycleConfig``
      activa (defaults: synchronous=NORMAL, busy_timeout=5000,
      foreign_keys=ON, temp_store=MEMORY).
    - Shutdown coordinado vía ``shutdown_all()`` que ejecuta TRUNCATE
      checkpoint y elimina los ``-wal``/``-shm``.

    Módulos cubiertos por M-01/M-01.1: ``core/database.py``,
    ``db/async_registry.py``, ``db/journal.py``, ``security/governance.py``,
    ``db/locks.py``, ``agent/router.py``, ``core/dlq_manager.py`` (los tres
    últimos migrados en M-01.1 con DI opcional ``lifecycle=``; sin él
    conservan su fallback directo pre-M-01 para tests/standalone).

    Exenciones deliberadas (triage M-01.1 — NO migrar sin nueva razón):
    - ``security/credential_vault.py``: pool propio acotado
      (BoundedSemaphore + timeout) sobre un archivo con ACL owner-only;
      es un boundary de seguridad con su propio modelo de concurrencia.
    - ``agent/context_manager.py``: lecturas read-only por-operación
      contra el registry (WAL: los lectores no bloquean escritores).
    - ``db/registry.py``: legacy sin usos en producción.

    Usage::

        lifecycle = DatabaseLifecycleManager(
            db_paths=[Path("sky_claw_state.db")],
        )
        await lifecycle.init_all()
        # ... application runs ...
        await lifecycle.shutdown_all()

    For signal-based graceful shutdown::

        lifecycle.register_graceful_shutdown()
    """

    def __init__(
        self,
        db_paths: list[Path] | None = None,
        config: DatabaseLifecycleConfig | None = None,
    ) -> None:
        self._config = config or DatabaseLifecycleConfig()
        self._db_paths: list[Path] = list(db_paths) if db_paths else []
        self._connections: dict[str, aiosqlite.Connection] = {}
        # Per-path write lock shared by every wrapper reusing the same managed
        # connection (see get_write_lock) — closes the shared-connection write race.
        self._write_locks: dict[str, asyncio.Lock] = {}
        # Paths fail-closed: `_resolve_connection` no los entrega NI abre un
        # reemplazo. Retener una conexión que no se pudo cerrar sin marcar el
        # path sólo cambia una fuga de ownership por una conexión envenenada —
        # el siguiente `operation()` recibía la entrada retenida y toda query
        # fallaba. Un set por path, no una FSM: el único estado que las carreras
        # reproducidas obligan a distinguir.
        self._quarantined_paths: set[str] = set()
        self._registered_signals: bool = False
        # Lock para garantizar singleton por path ante llamadas concurrentes
        self._init_lock = asyncio.Lock()

        # Estado de admisión. Una sola transición viva, y serializada:
        #
        #     RUNNING --shutdown_all()--> SHUTTING_DOWN --...--> CLOSED
        #        ^                                                  |
        #        +---------------- init_all() ----------------------+
        #
        # `_transition_lock` se sostiene durante TODO `init_all()` y TODO
        # `shutdown_all()`: sin eso, una reapertura puede colarse entre el
        # drenaje y el cierre y volver a admitir trabajo a mitad del apagado.
        # El booleano solo tampoco alcanza — el contrato exige ESPERAR lo ya
        # admitido, y para eso hace falta saber cuánto hay en vuelo.
        self._transition_lock = asyncio.Lock()
        self._shutting_down: bool = False
        self._closed: bool = False
        self._admitted: int = 0
        self._drained: asyncio.Event = asyncio.Event()
        self._drained.set()

    def add_db_path(self, path: Path) -> None:
        """Register an additional database path for lifecycle management."""
        resolved = path.resolve()
        if resolved not in [p.resolve() for p in self._db_paths]:
            self._db_paths.append(path)

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def init_all(self) -> None:
        """Initialize all registered databases with WAL recovery + pragmas.

        For each database:
        1. Check for orphaned WAL/SHM files → recover if found.
        2. Open connection with WAL mode and hardened pragmas.
        3. Verify pragmas are correctly applied.

        P0-3: además arma acá la red de ``atexit``, para que la garantía viaje
        con el objeto que la necesita en vez de depender de que cada caller se
        acuerde de registrarla (que es exactamente cómo
        ``register_atexit_handler`` terminó sin un solo caller en el árbol).
        Se registra aunque ``_db_paths`` esté vacío: ``get_connection`` da de
        alta los paths on-demand y ``_sync_shutdown`` los lee recién al salir.

        Raises:
            DatabaseLifecycleShuttingDownError: si el shutdown anterior quedó
                incompleto.
            DatabaseConnectionQuarantinedError: si algún path registrado sigue
                fail-closed. Reabrir NO levanta cuarentenas —de ningún origen—;
                la salida es ``shutdown_all()`` completo.
        """
        # Reapertura EXPLÍCITA, serializada e idempotente por path.
        #
        # * `_transition_lock` cubre TODA la reapertura, así que no puede
        #   colarse entre el drenaje y el cierre de un shutdown en curso. Si hay
        #   uno activo, esto espera a que termine y recién entonces reabre. Es
        #   también lo que impide que el shutdown observe la init a medio abrir:
        #   sin poseer el lock, el shutdown no alcanza el drenaje, y la init lo
        #   libera recién después de registrar sus conexiones.
        # * La admisión de la reapertura es uniformidad/defensa en profundidad,
        #   no el mecanismo necesario para el caso init-vs-shutdown: ese
        #   interleaving ya está cerrado por el transition lock. Se conserva
        #   para que init_all participe del mismo régimen que las demás rutas.
        # * Por path, la decisión de abrir participa del boundary completo
        #   (transition → path → init, el mismo orden por path que
        #   `operation()`): `_resolve_connection` es la única pieza que abre,
        #   y re-chequea el registro DENTRO de `_init_lock` antes de decidir.
        #   RUNNING → init_all es por tanto idempotente: un path ya
        #   inicializado conserva SU conexión — nunca la reemplaza.
        # * Fail-closed ESTRECHO (P2): tras un shutdown INCOMPLETO queda una
        #   entrada retenida cuya conexión ya está muerta, y la idempotencia
        #   la reutilizaría entregando un objeto inservible. Sólo ese estado
        #   (`_shutting_down` sin `_closed`) rechaza la reapertura: RUNNING
        #   sano y CLOSED siguen permitidos.
        async with self._transition_lock:
            if self._shutting_down and not self._closed:
                raise DatabaseLifecycleShuttingDownError(
                    "Previous shutdown did not complete; retry shutdown_all() before init_all()."
                )
            self._shutting_down = False
            self._closed = False
            # `init_all()` NO levanta cuarentenas. Ninguna, de ningún origen.
            #
            # Antes rescataba las que no tenían conexión retenida, razonando que
            # "no quedó nada dañado que reutilizar, así que reabrir es seguro (el
            # consumidor revalida)". Las dos mitades del razonamiento son falsas:
            #
            # * Ausencia de conexión retenida prueba OWNERSHIP DEL HANDLE, no
            #   coherencia del ARCHIVO. Y es peor que eso: los únicos dos lugares
            #   que marcan un path son el cierre NO confirmado —que retiene la
            #   entrada— y la ventana de `recover_connection()` —que la retira
            #   tras cerrar—, así que `p not in _connections` seleccionaba
            #   EXACTAMENTE las recoveries abortadas. El caso que rescataba era
            #   precisamente aquel en que el archivo declarado corrupto puede
            #   seguir en el path porque el rename que iba a retirarlo falló.
            # * "El consumidor revalida" no es una garantía que este lado pueda
            #   hacer valer. El único `PRAGMA quick_check` del árbol vive en
            #   `AsyncModRegistry.open()`; `init_all()` ni lo ejecuta ni puede
            #   exigirlo, y `_resolve_connection` publica la conexión reabierta a
            #   CUALQUIER consumidor vía `operation()`/`get_connection()` — así
            #   que la reapertura llegaba antes que la revalidación, si es que
            #   alguna vez llegaba.
            #
            # Una recovery incompleta queda fail-closed hasta la transición
            # TERMINAL: `shutdown_all()` completo, que es el único punto donde
            # consta que no hay nada a medio reconstruir (ver el `clear()` de
            # ambas ramas del shutdown). Un path marcado que esté en `_db_paths`
            # hace que esta reapertura falle con
            # `DatabaseConnectionQuarantinedError` desde `_resolve_connection`,
            # mismo desenlace que ya tenía el cierre no confirmado: las dos
            # cuarentenas sobreviven a `init_all()`, y por el mismo motivo.
            if self._config.enable_auto_checkpoint:
                self.register_atexit_handler()
            self._admit()
            try:
                for db_path in self._db_paths:
                    async with self.get_write_lock(db_path):
                        await self._resolve_connection(db_path)
            finally:
                self._release()

    async def _init_single(self, db_path: Path) -> None:
        """Initialize a single database with recovery and pragmas."""
        # Usar la misma clave canonizada que get_connection para consistencia
        path_str = str(db_path.resolve())

        # Step 1: Check for orphaned WAL files (crash recovery)
        wal_path = Path(path_str + "-wal")
        shm_path = Path(path_str + "-shm")

        if wal_path.exists() or shm_path.exists():
            logger.warning(
                "Recovering from orphaned WAL file at %s (wal=%s, shm=%s)",
                path_str,
                wal_path.exists(),
                shm_path.exists(),
            )
            await self._recover_orphaned_wal(db_path, wal_path, shm_path)

        # Step 2: Open connection and apply pragmas. Until registration succeeds,
        # this local scope is the connection's only owner.
        conn = await aiosqlite.connect(path_str)
        try:
            conn.row_factory = aiosqlite.Row
            await self._apply_pragmas(conn, path_str)
        except BaseException:
            # CancelledError is a BaseException on Python 3.11. The close must
            # finish before the original startup failure is propagated.
            # Sin `_invalidate_under_boundary`: esta conexión NUNCA llegó a
            # registrarse (el registro es la línea siguiente al try), así que no
            # hay entrada que mutar ni identidad que custodiar — sólo cerrar.
            close_error = (await self._close_connection(conn, shielded=True)).error
            if close_error is not None and not isinstance(close_error, asyncio.CancelledError):
                logger.critical(
                    "DatabaseLifecycle: no se pudo cerrar la conexión no registrada para %s",
                    path_str,
                    exc_info=(
                        type(close_error),
                        close_error,
                        close_error.__traceback__,
                    ),
                )
            raise

        self._connections[path_str] = conn
        logger.info("DatabaseLifecycle: initialized %s with WAL mode", path_str)

    async def _recover_orphaned_wal(
        self,
        db_path: Path,
        wal_path: Path,
        shm_path: Path,
    ) -> None:
        """Recover data from orphaned WAL files before normal init.

        Opens a temporary connection, forces a TRUNCATE checkpoint,
        then closes. If recovery fails, renames the WAL for forensics.
        """
        path_str = str(db_path)
        try:
            # Open temporary connection for recovery
            conn = await aiosqlite.connect(path_str)
            await conn.execute("PRAGMA journal_mode=WAL")
            # Force checkpoint to flush WAL contents into main DB
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await conn.close()

            # Verify WAL files were cleaned up
            if wal_path.exists() or shm_path.exists():
                logger.warning(
                    "WAL files still present after recovery checkpoint for %s",
                    path_str,
                )

            logger.info(
                "DatabaseLifecycle: successfully recovered orphaned WAL for %s",
                path_str,
            )

        except Exception as e:
            logger.error(
                "DatabaseLifecycle: WAL recovery FAILED for %s: %s",
                path_str,
                e,
            )
            # Rename corrupted WAL for forensics
            timestamp = int(time.time())
            if wal_path.exists():
                corrupted_name = f"{path_str}-wal.corrupted.{timestamp}"
                try:
                    import shutil

                    shutil.move(str(wal_path), corrupted_name)
                    logger.error(
                        "Renamed corrupted WAL to %s for forensics",
                        corrupted_name,
                    )
                except OSError:
                    logger.error("Could not rename corrupted WAL file")

    async def _apply_pragmas(self, conn: aiosqlite.Connection, path_str: str) -> None:
        """Apply hardened SQLite pragmas to a connection."""
        cfg = self._config

        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(f"PRAGMA synchronous={cfg.synchronous_mode}")
        await conn.execute(f"PRAGMA busy_timeout={cfg.busy_timeout_ms}")
        await conn.execute("PRAGMA temp_store=MEMORY")

        # Verify critical pragmas
        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
            mode = row[0] if row else ""
            if mode != "wal":
                logger.error(
                    "PRAGMA journal_mode returned '%s' instead of 'wal' for %s",
                    mode,
                    path_str,
                )

        async with conn.execute("PRAGMA foreign_keys") as cursor:
            row = await cursor.fetchone()
            fk = row[0] if row else 0
            if fk != 1:
                logger.error(
                    "PRAGMA foreign_keys returned %s instead of 1 for %s",
                    fk,
                    path_str,
                )

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    async def checkpoint_all(self, mode: str = "PASSIVE") -> dict[str, dict[str, Any]]:
        """Execute WAL checkpoint on all managed connections.

        Args:
            mode: Checkpoint mode — "PASSIVE" (non-blocking) for periodic
                maintenance, "TRUNCATE" for shutdown.

        Returns:
            Dict mapping db_path → checkpoint result info.
        """
        # El checkpoint periódico (`MaintenanceDaemon`) emite SQL sobre la
        # conexión compartida, así que participa del boundary como cualquier
        # otra operación. No ser un wrapper no lo exime: el inventario que sólo
        # miraba consumidores de `get_connection` se lo perdía justamente por
        # eso — éste usa conexiones YA resueltas.
        #
        # El checkpoint del shutdown NO pasa por acá: usa
        # `_checkpoint_all_under_boundary`, porque a esa altura el drenaje ya
        # garantizó exclusividad y volver a tomar el lock sería innecesario.
        self._admit()
        try:
            return await self._checkpoint_all_under_boundary(mode, con_boundary=True)
        finally:
            self._release()

    async def _checkpoint_all_under_boundary(
        self,
        mode: str = "PASSIVE",
        *,
        con_boundary: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Cuerpo del checkpoint.

        ``con_boundary=True`` toma el lock de cada path (camino periódico).
        ``False`` asume exclusividad ya garantizada por el drenaje del shutdown:
        tomarlo ahí no aportaría nada y arriesga una doble adquisición.
        """
        results: dict[str, dict[str, Any]] = {}
        for path_str, conn in list(self._connections.items()):
            lock = self.get_write_lock(path_str) if con_boundary else contextlib.nullcontext()
            async with lock:  # type: ignore[attr-defined]
                if self._connections.get(path_str) is not conn:
                    continue
                await self._checkpoint_one(path_str, conn, mode, results)
        return results

    async def _checkpoint_one(
        self,
        path_str: str,
        conn: aiosqlite.Connection,
        mode: str,
        results: dict[str, dict[str, Any]],
    ) -> None:
        """Checkpoint de una sola conexión; el boundary lo decide el llamador."""
        if True:  # noqa: SIM103 — conserva la indentación del cuerpo original
            try:
                # Log WAL size before checkpoint
                wal_size_before = self._get_wal_size(path_str)

                async with conn.execute(f"PRAGMA wal_checkpoint({mode})") as cursor:
                    row = await cursor.fetchone()
                    result = {
                        "busy": row[0] if row else -1,
                        "log_frames": row[1] if row else -1,
                        "checkpointed_frames": row[2] if row else -1,
                        "wal_size_before": wal_size_before,
                        "wal_size_after": self._get_wal_size(path_str),
                        "mode": mode,
                    }
                    results[path_str] = result

                    logger.info(
                        "WAL checkpoint (%s) for %s: %d frames checkpointed, WAL size %d→%d bytes",
                        mode,
                        path_str,
                        result["checkpointed_frames"],
                        wal_size_before,
                        result["wal_size_after"],
                    )

                    # Exceptional TRUNCATE if WAL exceeds critical threshold
                    if mode == "PASSIVE" and wal_size_before > self._config.wal_critical_threshold_bytes:
                        logger.warning(
                            "WAL size %d bytes exceeds critical threshold %d — executing emergency TRUNCATE for %s",
                            wal_size_before,
                            self._config.wal_critical_threshold_bytes,
                            path_str,
                        )
                        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            except Exception as e:
                logger.error("WAL checkpoint failed for %s: %s", path_str, e)
                results[path_str] = {"error": str(e), "mode": mode}

    def _get_wal_size(self, db_path: str) -> int:
        """Get the size of the WAL file for a database, or 0 if not found."""
        wal_path = Path(db_path + "-wal")
        try:
            return wal_path.stat().st_size if wal_path.exists() else 0
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown_all(self) -> None:
        """Gracefully shutdown all database connections.

        1. Execute TRUNCATE checkpoint on all connections.
        2. Close all connections.
        3. Verify WAL/SHM files were eliminated.
        """
        async with self._transition_lock:
            await self._shutdown_all_under_transition()

    async def _shutdown_all_under_transition(self) -> None:
        """Cuerpo del shutdown; el llamador sostiene ``_transition_lock``.

        Ese lock es lo que impide que un ``init_all()`` concurrente reabra la
        admisión entre el drenaje y el cierre.
        """
        # 1. Cerrar la puerta ANTES de cualquier await. Va antes del
        #    early-return porque "shutdown pedido" es un hecho aunque no haya
        #    nada registrado.
        self._shutting_down = True

        # 2. Esperar lo YA admitido: operaciones e inicializaciones. Sin esto,
        #    una init suspendida registra su conexión después de que el shutdown
        #    se declaró completo y queda abierta para siempre; y el cierre puede
        #    correr bajo una operación en vuelo. El drenaje termina porque la
        #    puerta ya está cerrada.
        await self._drained.wait()

        if not self._connections:
            self._closed = True
            # Sin conexiones registradas no queda nada que proteger: arrastrar
            # cuarentenas de recoveries abortadas bloquearía el próximo
            # init_all() sobre un path que ya está limpio.
            self._quarantined_paths.clear()
            return

        logger.info(
            "DatabaseLifecycle: shutting down %d connections...",
            len(self._connections),
        )

        # Step 1: TRUNCATE checkpoint — por la variante interna: el drenaje ya
        # garantizó exclusividad, y la pública rechazaría por SHUTTING_DOWN.
        checkpoints: dict[str, dict[str, Any]] = {}
        try:
            checkpoints = await self._checkpoint_all_under_boundary(mode="TRUNCATE")
        except Exception as e:
            logger.critical("DatabaseLifecycle: checkpoint during shutdown FAILED: %s", e)

        # Step 2: Close all connections. Cada entrada se retira solamente si
        # esa misma instancia termino de cerrar; un fallo conserva ownership
        # para que un shutdown posterior pueda reintentarlo.
        first_close_error: Exception | None = None
        for path_str, conn in list(self._connections.items()):
            # Bajo el lock del path: el cierre es EL OTRO mutador real del
            # registro, y dejarlo afuera es la asimetría que este contrato
            # elimina. Tras el drenaje no debería haber contención, pero
            # participar no es opcional según el mecanismo, sino su definición.
            async with self.get_write_lock(path_str):
                if self._connections.get(path_str) is not conn:
                    continue
                try:
                    await conn.close()
                    logger.info("DatabaseLifecycle: closed %s", path_str)
                except Exception as e:
                    logger.error("Error closing %s: %s", path_str, e)
                    if first_close_error is None:
                        first_close_error = e
                else:
                    if self._connections.get(path_str) is conn:
                        self._connections.pop(path_str)
                        # El shutdown es el owner que puede reintentar un cierre
                        # que falló antes; cuando lo consigue, el path deja de
                        # estar fail-closed y vuelve a ser reabrible.
                        self._quarantined_paths.discard(path_str)

        # Step 3: Verify WAL/SHM elimination.
        #
        # SQLite borra ``-wal``/``-shm`` solo al cerrar la ULTIMA conexion al
        # archivo. Si otro owner sigue abierto (la GUI y el SupervisorAgent
        # tienen cada uno su DatabaseAgent sobre el mismo sky_claw_state.db),
        # el que cierra primero SIEMPRE los ve presentes: avisar ahi de
        # "checkpoint incompleto" es un falso positivo garantizado en todo
        # apagado limpio, y envenena el triage real de WAL.
        #
        # El checkpoint TRUNCATE del paso 1 es el que decide: si reporto
        # ``busy == 0`` los datos ya estan en el archivo principal y los
        # sidecars restantes los explica el otro owner. Solo cuando el
        # checkpoint no pudo completarse hay motivo de alarma.
        for db_path in self._db_paths:
            # Canonizar: ``get_connection`` indexa ``_connections`` con
            # ``str(path.resolve())`` y ``checkpoint_all`` hereda esa clave,
            # pero ``_db_paths`` guarda la ruta tal cual se pasó — y el default
            # de ``DatabaseAgent`` es la relativa "sky_claw_state.db". Sin
            # resolver, el lookup de abajo falla siempre justo en producción.
            path_str = str(db_path.resolve())
            wal_path = Path(path_str + "-wal")
            shm_path = Path(path_str + "-shm")
            if not (wal_path.exists() or shm_path.exists()):
                continue
            checkpoint = checkpoints.get(path_str)
            if checkpoint is not None and checkpoint.get("busy") == 0:
                logger.info(
                    "Post-shutdown: WAL/SHM siguen presentes para %s (wal=%s, shm=%s), "
                    "pero el checkpoint TRUNCATE completo: otra conexion sigue abierta "
                    "sobre el archivo y SQLite los borrara al cerrarse la ultima.",
                    path_str,
                    wal_path.exists(),
                    shm_path.exists(),
                )
                continue
            logger.warning(
                "Post-shutdown: WAL/SHM files still present for %s "
                "(wal=%s, shm=%s). This may indicate incomplete checkpoint.",
                path_str,
                wal_path.exists(),
                shm_path.exists(),
            )

        if self._connections:
            logger.warning(
                "DatabaseLifecycle: shutdown incomplete; %d connections retained",
                len(self._connections),
            )
        else:
            logger.info("DatabaseLifecycle: shutdown complete")
            # CLOSED sólo cuando el cierre terminó de verdad. Si quedan
            # conexiones retenidas el shutdown es reintentable, y marcarlo
            # cerrado mentiría sobre un estado que todavía no se alcanzó.
            self._closed = True
            # El cierre completo es la transición terminal: ya no hay conexión
            # que retener ni recovery a medio camino que proteger.
            self._quarantined_paths.clear()

        if first_close_error is not None:
            raise first_close_error
        if self._connections:
            raise DatabaseShutdownIncompleteError(tuple(self._connections))

    # ------------------------------------------------------------------
    # Health Check
    # ------------------------------------------------------------------

    async def health_check(self) -> dict[str, WALHealth]:
        """Check WAL health for all managed databases.

        Returns:
            Dict mapping db_path → WALHealth status.
        """
        results: dict[str, WALHealth] = {}
        for db_path in self._db_paths:
            path_str = str(db_path)
            wal_size = self._get_wal_size(path_str)
            wal_exists = wal_size > 0

            if wal_size > self._config.wal_critical_threshold_bytes:
                status = "critical"
                message = f"WAL size {wal_size} bytes exceeds critical threshold"
            elif wal_size > self._config.wal_warning_threshold_bytes:
                status = "warning"
                message = f"WAL size {wal_size} bytes exceeds warning threshold"
            else:
                status = "healthy"
                message = "WAL size within normal range"

            results[path_str] = WALHealth(
                db_path=path_str,
                wal_exists=wal_exists,
                wal_size_bytes=wal_size,
                status=status,
                message=message,
            )

        return results

    # ------------------------------------------------------------------
    # Atexit Handler
    # ------------------------------------------------------------------

    def register_atexit_handler(self) -> None:
        """Register atexit handler for synchronous WAL checkpoint on process exit.

        NOTE: Signal handling (SIGINT/SIGTERM) is NOT done here. The application
        entrypoint (AppContext or __main__.py) should use ``loop.add_signal_handler()``
        to set a ``shutdown_event`` that allows the async ``AsyncExitStack`` to
        unwind naturally. The WAL checkpoint will then execute via
        ``shutdown_all()`` as part of the async teardown.

        This atexit handler is a safety net for normal process exit paths
        where the async loop may have already stopped.
        """
        if self._registered_signals:
            return

        # Vía weakref, NO ``atexit.register(self._sync_shutdown)``: un bound
        # method ancla ``self`` con una referencia fuerte que el registro de
        # atexit conserva hasta el final del proceso, y con ella
        # ``self._connections``. Las conexiones aiosqlite dejarían de
        # recolectarse, su ``__del__`` —que es quien llama a ``stop()``— nunca
        # correría, y sus worker threads NO-daemon sobrevivirían. La red de
        # seguridad debe checkpointear un manager vivo, no mantenerlo vivo.
        ref = weakref.ref(self)

        def _checkpoint_si_sigue_vivo() -> None:
            manager = ref()
            if manager is not None:
                manager._sync_shutdown()

        atexit.register(_checkpoint_si_sigue_vivo)
        self._registered_signals = True
        logger.info("DatabaseLifecycle: registered atexit handler for WAL checkpoint")

    def _sync_shutdown(self) -> None:
        """Synchronous shutdown for atexit handler.

        Uses synchronous sqlite3 (not aiosqlite) since the async loop
        may have already stopped. Hard timeout of 10 seconds.
        """
        import time as _time

        start = _time.monotonic()
        for db_path in self._db_paths:
            path_str = str(db_path)
            try:
                # ``Path.as_uri()`` exige una ruta ABSOLUTA. ``_db_paths`` puede
                # contener rutas relativas: tanto ``add_db_path`` como el
                # registro perezoso de ``get_connection`` guardan el ``Path``
                # tal cual llega, sin ``.resolve()``, y ``DatabaseAgent()`` usa
                # ``"sky_claw_state.db"`` (relativo) por defecto — el path que
                # usan tanto la GUI como el SupervisorAgent. Sin ``.resolve()``
                # acá, ``as_uri()`` levanta ``ValueError`` para ese caso y el
                # ``except Exception`` de abajo lo traga: la red de atexit para
                # la DB de producción nunca checkpointearía nada.
                uri = f"{Path(path_str).resolve().as_uri()}?mode=rw"
                try:
                    conn = sqlite3.connect(uri, uri=True, timeout=2)
                except sqlite3.OperationalError:
                    continue
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
                elapsed = _time.monotonic() - start
                logger.info(
                    "Sync shutdown checkpoint for %s completed in %.2fs",
                    path_str,
                    elapsed,
                )
            except Exception as e:
                elapsed = _time.monotonic() - start
                if elapsed > SHUTDOWN_CHECKPOINT_TIMEOUT_SECONDS:
                    logger.critical(
                        "Sync shutdown TIMEOUT (%.1fs) for %s — aborting checkpoint. Last error: %s",
                        elapsed,
                        path_str,
                        e,
                    )
                    break
                logger.error("Sync shutdown checkpoint failed for %s: %s", path_str, e)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_key(db_path: Path | str) -> str:
        """Clave canónica del path, compartida por conexiones y locks."""
        return str((db_path if isinstance(db_path, Path) else Path(db_path)).resolve())

    def _admit(self) -> None:
        """Registra una unidad de trabajo, o la rechaza si el shutdown empezó."""
        if self._shutting_down:
            raise DatabaseLifecycleShuttingDownError(
                "DatabaseLifecycleManager is shutting down; no new work is admitted. "
                "Call init_all() to reopen it explicitly."
            )
        self._admitted += 1
        self._drained.clear()

    def _release(self) -> None:
        self._admitted -= 1
        if self._admitted <= 0:
            self._admitted = 0
            self._drained.set()

    @asynccontextmanager
    async def operation(self, db_path: Path | str) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Boundary por path: admisión + conexión vigente + derecho a usarla.

        La conexión se resuelve DESPUÉS de tomar el lock, nunca antes:
        resolverla afuera y usarla adentro reintroduce resolve → await → stale,
        que es exactamente lo que este boundary existe para impedir.

        La admisión se toma ANTES de esperar el lock, y es deliberado: una
        operación ya encolada cuenta como admitida, así que el shutdown la
        espera en vez de dejarla entrar después de haber cerrado.

        Lo que NO debe quedar adentro: red, filesystem, sleeps y backoff. El
        lock protege la conexión compartida, no el trabajo ajeno a SQLite.
        """
        self._admit()
        try:
            async with self.get_write_lock(db_path):
                yield await self._resolve_connection(db_path)
        finally:
            self._release()

    async def _resolve_connection(
        self,
        db_path: Path | str,
        *,
        ignorar_cuarentena: bool = False,
    ) -> aiosqlite.Connection:
        """Devuelve la conexión del path, inicializándola si hace falta.

        Interna y SIN admisión: la toma el llamador. Separarlas evita que
        entrar al boundary cuente dos veces y, sobre todo, que un shutdown que
        arranca a mitad de camino rechace una operación ya admitida.

        Es además la ÚNICA pieza que decide abrir una conexión
        (singleton-open): el fast-path de afuera es solo ahorro de lock, y la
        decisión se re-chequea DENTRO de ``_init_lock`` inmediatamente antes de
        abrir. La usan ``operation``/``get_connection`` (bajo admisión) y
        ``init_all`` (bajo transition + lock del path): un path se inicializa
        a lo sumo una vez, y uno ya inicializado conserva su conexión.
        """
        path_obj = db_path if isinstance(db_path, Path) else Path(db_path)
        path_str = self._resolve_key(path_obj)

        # Fail-closed ANTES del fast-path: un path en cuarentena no entrega la
        # entrada retenida (estaría dañada) ni abre un reemplazo por su cuenta
        # (durante una recovery, ese reemplazo quedaría atado al inodo que el
        # rename se lleva). Sólo el dueño de la ventana lo atraviesa.
        if not ignorar_cuarentena and path_str in self._quarantined_paths:
            raise DatabaseConnectionQuarantinedError(
                f"Database path is quarantined and cannot serve connections: {path_str}"
            )

        if path_str in self._connections:
            return self._connections[path_str]

        async with self._init_lock:
            if path_str in self._connections:
                return self._connections[path_str]
            if not any(p.resolve() == path_obj.resolve() for p in self._db_paths):
                self._db_paths.append(path_obj)
            await self._init_single(path_obj)
            return self._connections[path_str]

    async def get_connection(self, db_path: Path | str) -> aiosqlite.Connection:
        """Return the managed connection for db_path, initializing on demand.

        CONTRATO M-01: ningún módulo cubierto debe llamar ``aiosqlite.connect``
        directamente; pedirla acá garantiza WAL recovery, pragmas hardenizadas y
        shutdown coordinado.

        Devuelve la conexión, no el derecho a usarla: para emitir SQL sobre la
        conexión compartida hay que entrar por ``operation()``, que sostiene el
        lock del path durante toda la unidad.

        Raises:
            DatabaseLifecycleShuttingDownError: si el shutdown ya empezó. No hay
                resurrección implícita; reabrir es ``init_all()``.
        """
        self._admit()
        try:
            return await self._resolve_connection(db_path)
        finally:
            self._release()

    @staticmethod
    async def _await_db_operation(awaitable: Awaitable[None]) -> None:
        """Completa una operación SQLite aunque el llamador sea cancelado."""
        task = asyncio.ensure_future(awaitable)
        cancelación: asyncio.CancelledError | None = None
        current_task = asyncio.current_task()

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancelación_externa = not task.cancelled() or (
                    current_task is not None and current_task.cancelling() > 0
                )
                if cancelación_externa and cancelación is None:
                    cancelación = error
            except BaseException:
                if task.done():
                    break
                raise

        try:
            task.result()
        except asyncio.CancelledError as error:
            if cancelación is not None:
                raise _DatabaseOperationFailedAfterCancellation(
                    cancelación,
                    error,
                ) from error
            raise _DatabaseOperationCancelled(error) from error
        except BaseException as error:
            if cancelación is not None:
                raise _DatabaseOperationFailedAfterCancellation(
                    cancelación,
                    error,
                ) from error
            raise

        if cancelación is not None:
            raise cancelación

    async def _close_connection(
        self,
        conn: aiosqlite.Connection,
        *,
        shielded: bool,
    ) -> _CloseOutcome:
        """Cierra una conexión y CARACTERIZA el resultado, sin decidir ownership.

        ``shielded`` decide si la cancelación del llamador puede abortar el
        cierre, y las dos respuestas son correctas para distintos dueños:

        - ``True`` — el cierre corre DENTRO de una unidad ajena (rollback fallido
          de ``transaction()``, rescate de ``_init_single``). Ahí la conexión es
          compartida y dejarla a medio cerrar rompe a terceros, así que el cierre
          se completa aunque cancelen al llamador.
        - ``False`` — el cierre ES el trabajo del llamador (ventana de
          ``recover_connection()``). Blindarlo volvería incancelable una recovery
          trabada, y además rompe el contrato de que una cancelación externa
          llegue al cierre conservando su identidad.

        Los cinco desenlaces de la matriz, y por qué ninguno se infiere del tipo
        de la excepción sola:

        - CLOSE-1 retorno limpio ................ cerró.
        - CLOSE-2 excepción ordinaria ........... NO cerró (fd probablemente vivo).
        - CLOSE-3 ``CancelledError`` pelada ..... SÍ cerró. Blindado,
          ``_await_db_operation`` sólo la levanta DESPUÉS de que ``task.result()``
          salió bien: la cancelación es del llamador y el cierre ya terminó.
        - CLOSE-4 ``…FailedAfterCancellation`` .. NO cerró, y además hay una
          cancelación externa que conservar. Devolver sólo el error —como se hacía
          antes— la descartaba en silencio.
        - CLOSE-5 ``…OperationCancelled`` ....... NO cerró; la cancelación nació
          dentro del cierre y no hay nada externo que restaurar, así que viaja
          como error de diagnóstico y no como cancelación.

        El orden de los ``except`` importa: las dos privadas son subclases de
        ``CancelledError`` y tienen que filtrarse antes que la pelada.
        """
        if not shielded:
            try:
                await conn.close()
            except asyncio.CancelledError as error:
                tarea = asyncio.current_task()
                # `cancelling() > 0` es la evidencia de que la cancelación se
                # pidió sobre ESTA tarea: sólo entonces hay identidad externa que
                # conservar. Si no, la cancelación nació dentro del cierre
                # (CLOSE-5) y es un fallo más, no algo que restaurar.
                if tarea is not None and tarea.cancelling() > 0:
                    return _CloseOutcome(False, None, error)
                return _CloseOutcome(False, error, None)
            except BaseException as error:
                return _CloseOutcome(False, error, None)
            return _CloseOutcome(True, None, None)

        try:
            await self._await_db_operation(conn.close())
        except _DatabaseOperationFailedAfterCancellation as error:  # CLOSE-4
            return _CloseOutcome(False, error.operation_error, error.cancellation)
        except _DatabaseOperationCancelled as error:  # CLOSE-5
            return _CloseOutcome(False, error.cause, None)
        except asyncio.CancelledError as error:  # CLOSE-3
            return _CloseOutcome(True, None, error)
        except BaseException as error:  # CLOSE-2
            return _CloseOutcome(False, error, None)
        return _CloseOutcome(True, None, None)  # CLOSE-1

    async def _invalidate_under_boundary(
        self,
        db_path: Path | str,
        expected_connection: aiosqlite.Connection,
        *,
        shielded: bool,
    ) -> _CloseOutcome | None:
        """Invalida ``expected_connection``. ASUME admisión y ``L_path`` sostenidos.

        Es el nivel interno del mecanismo compartido y NO vuelve a pedir
        ``L_path``: el camino de fallo de ``transaction()`` lo llama con el lock
        ya en la mano, y ``asyncio.Lock`` no es reentrante — pedirlo de nuevo
        cuelga la transacción fallida para siempre en vez de propagar el error.
        Quien no sostenga el boundary entra por ``recover_connection()``.

        El orden es el contrato: **cerrar primero, mutar el registro después**.
        Retirar la entrada antes del cierre deja —cuando el cierre falla— una
        conexión con su fd vivo, fuera del registro y sin ningún owner que pueda
        reintentarla; ``shutdown_all()`` después declara un cierre completo que
        nunca ocurrió. Es la misma política que ya usa el shutdown: se retira
        sólo lo que se confirmó cerrado.

        Devuelve ``None`` cuando la entrada vigente no es ``expected_connection``
        (guard de identidad ABA, por ``is``): esa conexión es de otro dueño y no
        se cierra ni se expulsa.
        """
        path_str = self._resolve_key(db_path)

        # El cierre NO se condiciona a la identidad: ``expected_connection`` es la
        # conexión que el caller tiene en la mano y ya decidió invalidar, y si
        # nadie la cierra queda viva, fuera del registro y sin owner — la fuga
        # exacta que este contrato existe para impedir. El guard de identidad
        # protege al REEMPLAZO, no exime de cerrar lo propio.
        outcome = await self._close_connection(expected_connection, shielded=shielded)

        # Guard de identidad, aplicado a la MUTACIÓN del registro. El cierre es un
        # punto de suspensión: la entrada pudo pasar a apuntar a otra conexión
        # mientras corría, y esa conexión es sana y de otro dueño. Ni pop ni
        # cuarentena: sólo se informa que no había nada nuestro que retirar.
        if self._connections.get(path_str) is not expected_connection:
            return None

        if outcome.closed:
            self._connections.pop(path_str)
        else:
            # Cierre NO confirmado: se conserva el ownership —alguien tiene que
            # poder reintentar el cierre— y el path queda fail-closed, porque la
            # entrada retenida ya no sirve para trabajo nuevo.
            self._quarantined_paths.add(path_str)
        return outcome

    async def _rollback_after_failure(
        self,
        db_path: Path | str,
        conn: aiosqlite.Connection,
        operation_error: BaseException,
        cancellation_to_restore: asyncio.CancelledError | None = None,
    ) -> None:
        """Observa rollback, cuarentena si falla y decide qué propagar."""
        rollback_error: BaseException | None = None
        rollback_cancellation: asyncio.CancelledError | None = None
        try:
            await self._await_db_operation(conn.rollback())
        except _DatabaseOperationFailedAfterCancellation as error:
            rollback_error = error.operation_error
            rollback_cancellation = error.cancellation
        except _DatabaseOperationCancelled as error:
            rollback_error = error.cause
        except asyncio.CancelledError:
            if cancellation_to_restore is None:
                raise
        except BaseException as error:
            rollback_error = error

        if rollback_error is None:
            if cancellation_to_restore is not None:
                if cancellation_to_restore is operation_error:
                    return
                raise cancellation_to_restore from operation_error
            return

        # Ya se sostienen admisión y `L_path` (venimos de `operation()`), así que
        # entra por el nivel interno. Usar el público acá self-deadlockearía.
        outcome = await self._invalidate_under_boundary(db_path, conn, shielded=True)
        close_error = outcome.error if outcome is not None else None
        # CLOSE-3/CLOSE-4: la cancelación externa observada durante el cierre es
        # tan restaurable como la del rollback; descartarla perdía la identidad.
        close_cancellation = outcome.cancellation if outcome is not None else None
        if cancellation_to_restore is operation_error:
            diagnostic_error = rollback_error
        else:
            try:
                raise rollback_error from operation_error
            except BaseException as chained_rollback_error:
                diagnostic_error = chained_rollback_error

        if close_error is not None:
            try:
                raise close_error from diagnostic_error
            except BaseException as chained_close_error:
                diagnostic_error = chained_close_error

        final_cancellation = cancellation_to_restore or rollback_cancellation or close_cancellation
        if final_cancellation is not None:
            raise final_cancellation from diagnostic_error
        raise diagnostic_error

    @asynccontextmanager
    async def transaction(
        self,
        db_path: Path | str,
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Serializa una transacción completa sobre la conexión compartida."""
        async with self.operation(db_path) as conn:
            try:
                yield conn
            except BaseException as error:
                cancellation = error if isinstance(error, asyncio.CancelledError) else None
                await self._rollback_after_failure(
                    db_path,
                    conn,
                    error,
                    cancellation,
                )
                raise

            try:
                await self._await_db_operation(conn.commit())
            except _DatabaseOperationFailedAfterCancellation as error:
                await self._rollback_after_failure(
                    db_path,
                    conn,
                    error.operation_error,
                    error.cancellation,
                )
            except _DatabaseOperationCancelled as error:
                await self._rollback_after_failure(db_path, conn, error.cause)
                raise error.cause from error
            except asyncio.CancelledError:
                # La cancelación se propaga después de completar el commit.
                raise
            except BaseException as error:
                await self._rollback_after_failure(db_path, conn, error)
                raise

    @asynccontextmanager
    async def _operation_under_recovery(
        self,
        db_path: Path | str,
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Boundary del path para el dueño de una ventana de recovery.

        Difiere de ``operation()`` en exactamente dos cosas, ambas deliberadas:
        NO vuelve a admitir —la ventana ya sostiene esa admisión, y contarla dos
        veces desalinearía el drenaje— y atraviesa la cuarentena que esa misma
        ventana instaló, que es lo que le permite abrir la conexión fresh
        mientras el resto del proceso sigue bloqueado.
        """
        async with self.get_write_lock(db_path):
            yield await self._resolve_connection(db_path, ignorar_cuarentena=True)

    @asynccontextmanager
    async def recover_connection(
        self,
        db_path: Path | str,
        expected_connection: aiosqlite.Connection,
    ) -> AsyncGenerator[Callable[[], contextlib.AbstractAsyncContextManager[aiosqlite.Connection]], None]:
        """Ventana admitida para reemplazar una conexión dañada de un path.

        Es el nivel PÚBLICO del mecanismo compartido: adquiere la admisión y
        ``L_path``, delega en ``_invalidate_under_boundary`` y suelta el lock
        antes de devolver el control. Los consumidores externos entran por acá;
        el camino de fallo de ``transaction()`` —que ya sostiene el boundary—
        entra por el helper interno.

        Entre el cierre de la conexión vieja y la apertura de la nueva hay una
        ventana donde el archivo se mueve. Sostener ``L_path`` durante todo eso
        violaría la regla de no meter filesystem, ``to_thread`` ni backoff dentro
        del lock de DB; soltarlo sin más dejaba que un ``operation()`` hermano
        abriera una conexión "fresh" sobre el archivo que estaba por renombrarse
        —y el rename se llevaba el inodo, con lo que la recovery terminaba
        escribiendo el schema dentro del backup corrupto. La cuarentena por path
        es lo que cubre esa ventana sin sostener el lock.

        Rinde un invocable que abre el boundary del path para el dueño de la
        ventana. La conexión fresh y su schema se establecen ahí adentro, y ESE
        boundary es el que define "recovery completa": la cuarentena se levanta
        sólo si su cuerpo salió limpio. Un rename que agota los reintentos, un
        schema que falla con la conexión nueva ya registrada, una cancelación a
        mitad del backoff o un cuerpo que directamente no reconstruye dejan el
        path fail-closed — antes los cuatro lo devolvían a servicio igual,
        porque el ``finally`` levantaba la cuarentena por el mero hecho de que
        la ventana hubiera terminado.

        Salir de la ventana sin haber reconstruido nada es un error de uso y se
        reporta como tal: un cierre limpio que no reabrió no puede pasar por
        recovery exitosa sólo porque nadie lanzó una excepción.

        Cómo vuelve a servicio un path cuya recovery abortó: por la transición
        TERMINAL, nunca por un resolve oportunista ni por una reapertura.
        ``shutdown_all()`` limpia las cuarentenas al completar el cierre, y ese
        es el único punto donde consta que no queda nada a medio reconstruir.
        ``init_all()`` NO las levanta: "no quedó conexión retenida" habla del
        ownership del handle, no de la coherencia del archivo, y cuando la
        ventana abortó porque el rename falló, el archivo que sigue en el path
        es exactamente el que se declaró corrupto. Reabrirlo ahí lo publicaría
        vía ``operation()`` sin que ningún ``quick_check`` lo hubiera vuelto a
        mirar — la revalidación vive en el consumidor, y este lado no puede
        exigirla. Por eso las dos cuarentenas (cierre no confirmado y recovery
        abortada) sobreviven a ``init_all()``.

        La admisión abarca TODA la ventana —invalidación, rename, reapertura—,
        de modo que un ``shutdown_all()`` concurrente la espera en vez de cerrar
        por debajo y dejar la recovery a medio camino.

        Raises:
            DatabaseConnectionReplacedError: si ``expected_connection`` ya no es
                la vigente. No se cierra ni se expulsa el reemplazo.
            DatabaseConnectionQuarantinedError: si el cierre no se pudo
                confirmar. El ownership queda retenido y el path fail-closed;
                se recupera reintentando ``shutdown_all()``.
        """
        path_str = self._resolve_key(db_path)
        self._admit()
        cuarentena_propia = False
        reconstruido = False

        @asynccontextmanager
        async def reabrir() -> AsyncGenerator[aiosqlite.Connection, None]:
            """Boundary de reapertura; salir limpio de acá ES la señal de completitud.

            Se recalcula en cada uso: si la recovery reabre más de una vez, manda
            el último intento, no el primero que salió bien.
            """
            nonlocal reconstruido
            reconstruido = False
            async with self._operation_under_recovery(db_path) as conn:
                yield conn
            reconstruido = True

        try:
            async with self.get_write_lock(db_path):
                outcome = await self._invalidate_under_boundary(
                    db_path,
                    expected_connection,
                    # El cierre ES el trabajo de esta ventana: una cancelación
                    # externa debe poder abortarla y llegar al close con su
                    # identidad, no quedar blindada afuera.
                    shielded=False,
                )
                if outcome is None:
                    raise DatabaseConnectionReplacedError(
                        f"Expected connection is no longer the current one for {path_str}; nothing was invalidated"
                    )
                if not outcome.closed:
                    # `_invalidate_under_boundary` ya retuvo la entrada y marcó
                    # el path: acá sólo se traduce a excepción, conservando la
                    # cancelación externa con su identidad y el fallo del cierre
                    # como cadena diagnóstica.
                    quarantined = DatabaseConnectionQuarantinedError(
                        f"Could not confirm close of the managed connection for {path_str}; ownership retained"
                    )
                    if outcome.cancellation is not None:
                        # CLOSE-4: la cancelación externa manda —viaja con su
                        # identidad intacta— y el estado del cierre queda como
                        # cadena diagnóstica en vez de perderse.
                        if outcome.error is not None:
                            quarantined.__cause__ = outcome.error
                        raise outcome.cancellation from quarantined
                    if outcome.error is not None:
                        raise quarantined from outcome.error
                    raise quarantined
                if outcome.cancellation is not None:
                    # CLOSE-3: el cierre SÍ terminó, así que el ownership ya se
                    # actualizó arriba; la cancelación externa se propaga recién
                    # ahora, con su identidad intacta.
                    raise outcome.cancellation
                self._quarantined_paths.add(path_str)
                cuarentena_propia = True

            yield reabrir

            # Sólo se llega acá si el cuerpo salió limpio. Que no haya excepción
            # no prueba que se haya reconstruido nada: sin esto, una ventana que
            # nunca llamó a ``reabrir()`` devolvía el path a servicio sin
            # conexión nueva y sin que nadie se enterara.
            if not reconstruido:
                raise DatabaseConnectionQuarantinedError(
                    f"Recovery window for {path_str} ended without reopening the connection; the path stays quarantined"
                )
        finally:
            # La cuarentena se levanta sólo si ESTA ventana la instaló Y la
            # recovery llegó a un estado coherente. Las dos que sobreviven lo
            # hacen a propósito: la de un cierre no confirmado (su conexión sigue
            # retenida y sin cerrar) y la de una recovery abortada (el archivo
            # quedó a mitad de reconstruir). Ambas se levantan por transición
            # explícita, no por el próximo resolve que pase.
            if cuarentena_propia and reconstruido:
                self._quarantined_paths.discard(path_str)
            self._release()

    def get_write_lock(self, db_path: Path | str) -> asyncio.Lock:
        """Return the write lock bound to the connection for *db_path*.

        Connections are singletons per resolved path (see get_connection), so
        every ``AsyncModRegistry`` wrapper reusing the same connection must
        serialize writes through the SAME lock; a per-wrapper lock would leave
        the transaction-interleaving race open. Created lazily and cached under
        the same resolved-path key as the connection. No ``await`` between lookup
        and insert keeps this atomic on the single-threaded event loop.
        """
        path_str = str((db_path if isinstance(db_path, Path) else Path(db_path)).resolve())
        lock = self._write_locks.get(path_str)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[path_str] = lock
        return lock

    def evict_connection(
        self,
        db_path: Path | str,
        expected_connection: aiosqlite.Connection | None = None,
    ) -> bool:
        """Remove a connection from the managed pool without closing it.

        Intended for recovery paths where the caller has already closed the
        connection (e.g. after renaming a corrupt DB file for forensics).
        After eviction, the next ``get_connection()`` call for the same path
        will open a fresh connection.

        Cuando ``expected_connection`` se proporciona, la entrada se elimina
        solo si todavia apunta a esa misma instancia. Esto evita expulsar una
        conexion reemplazada concurrentemente.

        The path remains registered in ``_db_paths`` so health_check and
        shutdown verification still track it.

        Args:
            db_path: Path to the database whose connection should be evicted.
            expected_connection: Instancia que el caller cerro y espera retirar.

        Returns:
            ``True`` si se retiro una entrada; ``False`` si no existia o habia
            sido reemplazada.
        """
        path_obj = db_path if isinstance(db_path, Path) else Path(db_path)
        path_str = str(path_obj.resolve())
        current_connection = self._connections.get(path_str)
        if current_connection is None:
            return False
        if expected_connection is not None and current_connection is not expected_connection:
            return False
        self._connections.pop(path_str)
        return True

    @property
    def managed_paths(self) -> list[str]:
        """List of currently managed database path strings."""
        return list(self._connections.keys())
