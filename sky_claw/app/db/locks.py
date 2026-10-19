"""Distributed lock manager with TTL-based leases for transactional VFS safety.

Provides :class:`DistributedLockManager` backed by SQLite ``resource_locks``
table, and :class:`SnapshotTransactionLock` — an async context manager that
coordinates lock acquisition, snapshotting via :class:`FileSnapshotManager`,
and automatic rollback on failure.

Sprint 2 (Fase 1): Prevents race conditions and VFS corruption when multiple
agents modify MO2 resources in parallel.  Deadlocks from agent crashes are
mitigated by time-based leases (TTL).

**Design decisions (LÓGICA, ARQUITECTURA, PREVENCIÓN):**

LÓGICA:
    - Atomic INSERT/UPDATE via ``INSERT ... ON CONFLICT DO UPDATE WHERE expires_at < ?``
      ensures only one agent can hold a lock at a time, even under concurrent access.
    - Expired leases are automatically reclaimed — no manual cleanup needed.

ARQUITECTURA:
    - Module lives in ``sky_claw.app.db.locks`` to keep lock infrastructure separate
      from the mod registry (``async_registry.py``) and journaling (``journal.py``).
    - ``SnapshotTransactionLock`` composes ``DistributedLockManager`` +
      ``FileSnapshotManager`` via constructor injection (DI) — no globals.

PREVENCIÓN:
    - 10-minute default TTL is tuned for xEdit/DynDOLOD long-running ops.
    - All DB writes use ``journal_mode=WAL`` for crash safety.
    - If the database fails, the filesystem is never partially mutated:
      snapshot is created AFTER lock acquisition, rollback happens BEFORE
      lock release.
    - ``asyncio.CancelledError`` is not swallowed by ``except Exception``.
      A cancel mid-rollback is recorded in ``rollback_failures`` (so
      ``rollback_completed`` stays False) and re-raised; the lock is still
      released in ``__aexit__`` via ``finally``.
    - A cancellation DURING acquisition does not leak the lease (#741): the
      acquisition runs shielded and its definitive result is awaited, so the
      token (``acquired_at``) is known and only that row is released. Releasing
      by ``resource_id`` + ``agent_id`` is not an option — services share the
      agent id, so it would delete a sibling's reacquired lease.
    - Releasing is itself cancellation-durable (``_liberar_definitivo``): the
      cleanup is the last act of the context and abandoning its DELETE halfway
      leaves the row alive until the TTL.
    - ``renew_lock`` / ``release_lock`` match the acquisition token
      (``acquired_at``), not just ``agent_id``. Services share a constant
      agent id; without the token a stalled holder can renew or delete a
      sibling's reacquired lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import math
import pathlib
import sqlite3
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiosqlite

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleShuttingDownError
from sky_claw.app.db.rollback_veto import exception_forbids_rollback
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTIONS
# =============================================================================


class LockError(Exception):
    """Base exception for distributed lock operations."""


class LockAcquisitionError(LockError):
    """Raised when a lock cannot be acquired after all retry attempts."""

    def __init__(
        self,
        resource_id: str,
        agent_id: str,
        message: str = "",
    ) -> None:
        self.resource_id = resource_id
        self.agent_id = agent_id
        super().__init__(message or f"Failed to acquire lock on '{resource_id}' for agent '{agent_id}'")


class LockReleaseError(LockError):
    """Raised when a lock release fails (non-fatal, logged as warning)."""


class SnapshotRollbackError(LockError):
    """Raised when a FORCED (dry-run) rollback fails to restore a target file.

    A failed restore on the ``force_rollback`` path means a file may have been
    left mutated while the caller believes the preview was side-effect free, so
    the no-mutation guarantee is surfaced loudly instead of being swallowed.
    On the exception path (a real error inside the context) a failed restore is
    only logged — it must never mask the original exception.
    """


class LockLeaseLostError(LockError):
    """Raised on clean exit when the heartbeat could not renew the lease.

    A lost lease means another agent may have acquired the resource while this
    holder was still working — the operation may have raced with a concurrent
    writer, so reporting success would lie about its exclusivity.  Raised only
    when the context body itself did not raise (a body exception always takes
    precedence and is never masked).
    """


# =============================================================================
# CONSTANTS
# =============================================================================

#: Default TTL in seconds — 10 minutes, tuned for long-running xEdit sessions.
DEFAULT_LOCK_TTL_SECONDS: float = 600.0

#: Lowest accepted ``renew_divisor``. A divisor of 1.0 schedules the first
#: renewal at exactly the TTL, racing expiry (``renew_lock`` requires
#: ``expires_at > now``); 2.0 keeps at least half the TTL of margin.
_MIN_RENEW_DIVISOR: float = 2.0

#: Floor for the heartbeat renewal interval so a very large ``renew_divisor``
#: cannot collapse the cadence to ~0 and busy-loop the lock database.
_MIN_HEARTBEAT_INTERVAL_SECONDS: float = 0.05

#: Maximum number of acquisition retry attempts.
DEFAULT_MAX_RETRIES: int = 5

#: Base delay for exponential backoff (seconds).
DEFAULT_BACKOFF_BASE: float = 0.1

#: Maximum delay between retries (seconds).
DEFAULT_BACKOFF_MAX: float = 5.0

#: Bounded deadline (seconds) to wait for an in-flight SQLite acquire/release
#: when the calling task has already been cancelled (#741). If the deadline
#: expires while SQLite is still blocked, the state transitions to
#: ``INDETERMINADO`` and the in-flight operation remains supervised in the
#: background so the caller is never hung indefinitely.
DEFAULT_CANCEL_CLEANUP_TIMEOUT_SECONDS: float = 5.0


class EstadoDeCleanupDeLease(StrEnum):
    """Resultado explícito de la compensación/liberación de una lease (#741)."""

    NO_REQUERIDO = "no_requerido"
    SIN_LEASE = "sin_lease"
    LIBERADA = "liberada"
    YA_EXPIRADA_O_RECLAMADA = "ya_expirada_o_reclamada"
    FALLO_DE_LIBERACION = "fallo_de_liberacion"
    INDETERMINADO = "indeterminado"


_LOCKS_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS resource_locks (
    resource_id   TEXT    PRIMARY KEY,
    agent_id      TEXT    NOT NULL,
    acquired_at   REAL    NOT NULL,
    expires_at    REAL    NOT NULL
);
"""

_ACQUIRE_SQL = """\
INSERT INTO resource_locks (resource_id, agent_id, acquired_at, expires_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(resource_id) DO UPDATE SET
    agent_id    = excluded.agent_id,
    acquired_at = excluded.acquired_at,
    expires_at  = excluded.expires_at
WHERE resource_locks.expires_at < ?
"""
# Parámetros: (resource_id, agent_id, token, expires_at, ahora). El token
# (``acquired_at``) es la IDENTIDAD de la lease y puede quedar por delante del
# reloj; el vencimiento se mide SIEMPRE con el reloj real (``ahora``).

_RENEW_SQL = """\
UPDATE resource_locks
SET expires_at = ?
WHERE resource_id = ? AND agent_id = ? AND acquired_at = ? AND expires_at > ?
"""

_RELEASE_SQL = """\
DELETE FROM resource_locks
WHERE resource_id = ? AND agent_id = ? AND acquired_at = ?
"""

_RELEASE_ANY_SQL = """\
DELETE FROM resource_locks
WHERE resource_id = ?
"""


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass(frozen=True, slots=True)
class LockInfo:
    """Metadata about a held lock."""

    resource_id: str
    agent_id: str
    acquired_at: float
    expires_at: float

    @property
    def remaining_ttl(self) -> float:
        """Seconds remaining before this lease expires."""
        return max(0.0, self.expires_at - time.time())

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


# =============================================================================
# TOKEN DE FENCING
# =============================================================================

# ``acquired_at`` es el token de fencing de la lease (``_RELEASE_SQL`` y
# ``_RENEW_SQL`` lo matchean): tiene que distinguir una lease de la que la
# reemplazó. ``time.time()`` no lo garantiza: en Windows con Python < 3.13 avanza
# a saltos de ~15,6 ms y dos adquisiciones del mismo (recurso, agent_id) dentro del
# mismo tick reciben el MISMO valor. Por eso el token se acuña en un único punto
# (``DistributedLockManager._acuniar_token``) y es estrictamente creciente POR BASE
# dentro del proceso. El estado es del módulo y no de la instancia porque varios
# gestores (uno por servicio, con el mismo ``agent_id``) pueden apuntar a la misma
# base. El token es SOLO identidad: tras un retroceso del reloj puede quedar por
# delante de él, así que el vencimiento (``expires_at`` y el predicado de
# ``_ACQUIRE_SQL``) se mide con la lectura real del reloj, nunca con el token.
# Límite: dos PROCESOS distintos no comparten este estado; cerrarlo exigiría
# una identidad persistida en ``resource_locks`` (cambio de esquema).
_CERROJO_DE_TOKENS = threading.Lock()
_ULTIMO_TOKEN_POR_BASE: dict[str, float] = {}


# =============================================================================
# DISTRIBUTED LOCK MANAGER
# =============================================================================


class DistributedLockManager:
    """SQLite-backed distributed lock manager with TTL leases.

    Uses ``aiosqlite`` for async-safe operations and ``journal_mode=WAL``
    for crash resilience.  Locks are acquired atomically using
    ``INSERT ... ON CONFLICT DO UPDATE WHERE expires_at < ?`` so that
    expired leases are transparently reclaimed.

    Parameters
    ----------
    db_path:
        Path to the SQLite lock database file.
    default_ttl:
        Default lock TTL in seconds.
    max_retries:
        Maximum number of acquisition attempts before raising.
    backoff_base:
        Base delay for exponential backoff.
    backoff_max:
        Maximum delay between retries.
    """

    def __init__(
        self,
        db_path: pathlib.Path | str,
        *,
        default_ttl: float = DEFAULT_LOCK_TTL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        lifecycle=None,  # DatabaseLifecycleManager | None — evita import circular en runtime
    ) -> None:
        self._db_path = str(db_path)
        # Misma clave canónica que usa el lifecycle para conexiones y locks de path.
        self._clave_de_token = str(pathlib.Path(db_path).resolve())
        self._default_ttl = default_ttl
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._lifecycle = lifecycle  # DatabaseLifecycleManager | None (M-01.1 DI)
        self._owns_conn: bool = False  # True when we opened the connection directly (no lifecycle)
        self._conn: aiosqlite.Connection | None = None
        # Fix #473 (C5): serializa la transición de estado de conexión
        # (initialize vs close). Orden de locks de la familia:
        # _conn_lock → boundary del path (nunca al revés).
        self._conn_lock = asyncio.Lock()
        self._pending_cleanup_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open DB, set WAL mode, create the ``resource_locks`` table.

        M-01.1: If a DatabaseLifecycleManager was injected, the connection is
        requested from it (WAL recovery + hardened pragmas already applied).
        Otherwise falls back to a direct ``aiosqlite.connect`` with manual
        pragmas (pre-M-01 behaviour) — same contract as ``journal.py`` /
        ``async_registry.py``.

        PR-1b2a: con lifecycle, el ``executescript`` del schema corre DENTRO de
        ``operation()`` (admisión + lock del path + conexión vigente): un
        ``shutdown_all()`` concurrente espera a que el DDL termine en vez de
        cerrar la conexión debajo de él. Fix #473 (sentinel): ``self._conn`` se
        asigna DESPUÉS del DDL exitoso (dentro del boundary): un fallo del
        schema no deja un sentinel de apertura que convierta el reintento en
        no-op. #474: la rama standalone sigue la misma regla (local + cierre
        ante ``BaseException``). Fix #473 (C5): toda la transición corre bajo ``_conn_lock``
        (instancia): un ``close()`` concurrente espera a que el initialize en
        vuelo termine y luego nulea — no puede haber resurrección.
        """
        async with self._conn_lock:
            if self._conn is not None:
                return

            if self._lifecycle is not None:
                self._owns_conn = False
                async with self._lifecycle.operation(self._db_path) as conn:
                    await conn.executescript(_LOCKS_SCHEMA_SQL)
                    self._conn = conn
            else:
                # #474: hermano standalone del fix #473. La conexión vive en una
                # local hasta que PRAGMAs + DDL terminan; ante cualquier fallo
                # (incluida la cancelación) se cierra y NO se publica en
                # ``self._conn`` — si no, el reintento sería un no-op sobre una
                # conexión sin schema y sin dueño.
                conn = await aiosqlite.connect(self._db_path)
                try:
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await conn.execute("PRAGMA busy_timeout=5000")
                    await conn.executescript(_LOCKS_SCHEMA_SQL)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await conn.close()
                    raise
                self._owns_conn = True
                self._conn = conn
            logger.info(
                "DistributedLockManager initialized",
                extra={"db_path": self._db_path, "default_ttl": self._default_ttl},
            )

    async def close(self) -> None:
        """Close the DB connection (lifecycle-owned connections stay open).

        Fix #473 (C5): corre bajo ``_conn_lock``: un ``initialize()`` en vuelo
        termina antes de que el close nulee ``_conn`` (sin resurrección).
        """
        await drenar_tareas_de_cleanup_pendientes(self, cancel_pending=True)
        async with self._conn_lock:
            if self._conn is not None:
                if self._owns_conn:
                    await self._conn.close()
                    self._owns_conn = False
                # Si el lifecycle es externo, no cerramos la conexión aquí;
                # el propietario la cierra en shutdown_all().
                self._conn = None
                logger.info("DistributedLockManager closed")

    def _ensure_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise LockError("LockManager not initialized — call initialize() first")
        return self._conn

    @contextlib.asynccontextmanager
    async def _operacion(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Boundary por path para LECTURAS del lock manager (PR-1b2a).

        Con lifecycle: admisión + lock del path + conexión vigente durante toda
        la unidad; el SQL debe usar la conexión yielded, nunca ``self._conn``.
        Sin lifecycle: la conexión propia (semántica pre-M-01 intacta). Las
        MUTACIONES no pasan por acá: usan ramas explícitas por lifecycle con
        ``transaction()`` (owner del commit/rollback) o commit manual legacy —
        nunca las dos cosas (Claridad > DRY en concurrencia).
        """
        if self._lifecycle is not None:
            async with self._lifecycle.operation(self._db_path) as conn:
                yield conn
            return
        yield self._ensure_conn()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def _acuniar_token(self, ahora: float) -> float:
        """Acuña el token de fencing (``acquired_at``) de una adquisición.

        ``ahora`` es la lectura del reloj de pared de esta adquisición. Devuelve
        ``ahora`` cuando el reloj avanzó respecto del último token emitido para
        esta base y, si no (reloj grueso o retroceso por NTP), el siguiente
        flotante representable: el token es estrictamente creciente por base
        dentro del proceso. En el caso normal no desplaza el valor del reloj.

        El token es solo la identidad de la lease: puede quedar por delante del
        reloj tras un retroceso, así que NO sirve para medir vencimientos (ese es
        el trabajo de ``ahora``).

        Es síncrono y no cede el control: dos corrutinas del mismo loop no pueden
        intercalarse; el ``threading.Lock`` cubre gestores en otros hilos o loops.
        """
        with _CERROJO_DE_TOKENS:
            ultimo = _ULTIMO_TOKEN_POR_BASE.get(self._clave_de_token)
            token = ahora if ultimo is None or ahora > ultimo else math.nextafter(ultimo, math.inf)
            _ULTIMO_TOKEN_POR_BASE[self._clave_de_token] = token
            return token

    async def acquire_lock(
        self,
        resource_id: str,
        agent_id: str,
        ttl: float | None = None,
    ) -> LockInfo:
        """Attempt to acquire a lock, retrying with exponential backoff.

        Parameters
        ----------
        resource_id:
            Unique identifier for the resource to lock (e.g. file path).
        agent_id:
            Unique identifier for the requesting agent.
        ttl:
            Lock TTL in seconds (overrides default).

        Returns
        -------
        LockInfo
            Metadata about the acquired lock.

        Raises
        ------
        LockAcquisitionError
            If the lock cannot be acquired after ``max_retries`` attempts.
        """
        self._ensure_conn()  # contrato: LockError si initialize() no corrió
        ttl_seconds = ttl if ttl is not None else self._default_ttl
        cancel_event = _CANCELACION_ADQUISICION.get()

        for attempt in range(self._max_retries):
            # Si el llamador de adquirir_lease_definitiva se canceló antes de
            # abrir el intento SQLite (o tras un intento con rowcount == 0),
            # abortar de inmediato sin iniciar una nueva transacción (#741).
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError

            # PR-1b2a (fix #473): cada INTENTO es su propia unidad. Con
            # lifecycle, ``transaction()`` es owner del commit/rollback/
            # cancelación y el reloj se captura DESPUÉS de adquirir el
            # boundary: la espera del path lock no puede dejar vencer la
            # lease antes del INSERT. El backoff queda SIEMPRE fuera.
            try:
                if self._lifecycle is not None:
                    async with self._lifecycle.transaction(self._db_path) as conn:
                        ahora = time.time()
                        token = self._acuniar_token(ahora)
                        expires_at = ahora + ttl_seconds
                        async with conn.execute(
                            _ACQUIRE_SQL,
                            (resource_id, agent_id, token, expires_at, ahora),
                        ) as cursor:
                            rowcount = cursor.rowcount
                    # commit del wrapper ya terminó acá
                else:
                    ahora = time.time()
                    token = self._acuniar_token(ahora)
                    expires_at = ahora + ttl_seconds
                    conn = self._ensure_conn()
                    async with conn.execute(
                        _ACQUIRE_SQL,
                        (resource_id, agent_id, token, expires_at, ahora),
                    ) as cursor:
                        rowcount = cursor.rowcount
                    await conn.commit()

                # SQLite INSERT ... ON CONFLICT: rowcount == 1 if we inserted or
                # updated (i.e. expired lock was reclaimed).  rowcount == 0 if
                # the conflict condition (expires_at < ?) was NOT met (lock held
                # by someone else and not yet expired).
                if rowcount > 0:
                    # C6 (fix #473): la lease nació con el reloj del boundary,
                    # pero si su commit terminó DESPUÉS de expires_at no hay que
                    # devolver éxito: se consume el intento y se reintenta. La
                    # fila stale queda expirada y el próximo intento la reclama
                    # atómicamente vía ON CONFLICT (sin DELETE compensatorio).
                    if self._lifecycle is not None and time.time() >= expires_at:
                        logger.warning(
                            "Lock acquisition expired before commit returned; retrying",
                            extra={
                                "resource_id": resource_id,
                                "agent_id": agent_id,
                                "attempt": attempt + 1,
                            },
                        )
                    else:
                        lock_info = LockInfo(
                            resource_id=resource_id,
                            agent_id=agent_id,
                            acquired_at=token,
                            expires_at=expires_at,
                        )
                        logger.info(
                            "Lock acquired",
                            extra={
                                "resource_id": resource_id,
                                "agent_id": agent_id,
                                "ttl": ttl_seconds,
                                "attempt": attempt + 1,
                            },
                        )
                        return lock_info

            except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                logger.warning(
                    "Lock acquisition DB error (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
            # Cancelación (C4, fix #473): sin compensación eager. El core puede
            # haber hecho COMMIT_WINS y la fila queda persistida sin dueño
            # observable; el schema actual NO posee identidad única de lease
            # (compensar por timestamps fue refutado: podía borrar una
            # adquisición nueva válida), así que la recuperación es TTL + ON
            # CONFLICT reclamable (fail-safe). El CancelledError se propaga
            # naturalmente; nunca se devuelve LockInfo ni LockAcquisitionError.
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError

            # Exponential backoff with jitter-free cap
            if attempt < self._max_retries - 1:
                delay = min(
                    self._backoff_base * (2**attempt),
                    self._backoff_max,
                )
                logger.debug(
                    "Lock busy, retrying in %.2fs (attempt %d/%d)",
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                if cancel_event is not None:
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    else:
                        raise asyncio.CancelledError
                else:
                    await asyncio.sleep(delay)

        raise LockAcquisitionError(resource_id, agent_id)

    async def release_lock(
        self,
        resource_id: str,
        agent_id: str,
        *,
        acquired_at: float | None = None,
    ) -> bool:
        """Release a lock held by the given agent.

        Parameters
        ----------
        resource_id:
            Resource to unlock.
        agent_id:
            Agent that holds the lock.
        acquired_at:
            Acquisition token from the :class:`LockInfo` returned by
            :meth:`acquire_lock`. The DELETE matches this token, so a
            stale holder cannot delete a sibling's reacquired lease
            (services share a constant ``agent_id``). Omitting it is a
            no-op — ``acquired_at = NULL`` never matches a live row —
            and logs a warning: no production caller may omit it, and a
            silent no-op would leave the resource locked until the TTL
            expires (a 10-minute spurious contention incident).

        Returns
        -------
        bool
            ``True`` if the lock was found and deleted.
        """
        self._ensure_conn()  # contrato: LockError si initialize() no corrió
        if acquired_at is None:
            # Fail-safe, no fail-silent: la fila NO se borra (no podemos probar
            # que la lease es nuestra), pero el error de programación deja traza
            # acá. Sin este warning, un caller que olvide el token degrada en
            # disponibilidad (contención espuria hasta el TTL) sin ninguna señal
            # que lo explique. El conjunto de call sites está congelado por AST
            # en test_distributed_locks.py.
            logger.warning(
                "release_lock sin acquired_at: no-op (falta el token de adquisición); la lease queda viva hasta el TTL",
                extra={"resource_id": resource_id, "agent_id": agent_id},
            )
        try:
            # PR-1b2a (fix #473): con lifecycle, ``transaction()`` es owner del
            # commit/rollback/cancelación (la cancelación entre DELETE y commit
            # no deja suciedad que una mutación posterior commitee). El shutdown
            # del lifecycle se propaga (política PROPAGATE): `_safe_release` lo
            # absorbe loggeando y la lease expira por TTL. Legacy: commit manual
            # actual intacto.
            params = (resource_id, agent_id, acquired_at)
            if self._lifecycle is not None:
                async with (
                    self._lifecycle.transaction(self._db_path) as conn,
                    conn.execute(
                        _RELEASE_SQL,
                        params,
                    ) as cursor,
                ):
                    deleted = cursor.rowcount > 0
                # commit del wrapper ya terminó acá
            else:
                conn = self._ensure_conn()
                async with conn.execute(
                    _RELEASE_SQL,
                    params,
                ) as cursor:
                    deleted = cursor.rowcount > 0
                await conn.commit()

            if deleted:
                logger.info(
                    "Lock released",
                    extra={"resource_id": resource_id, "agent_id": agent_id},
                )
            else:
                logger.warning(
                    "Lock release: no matching lock found",
                    extra={"resource_id": resource_id, "agent_id": agent_id},
                )

            return deleted

        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            logger.error(
                "Lock release failed: %s",
                exc,
                extra={"resource_id": resource_id, "agent_id": agent_id},
            )
            raise LockReleaseError(f"Failed to release lock '{resource_id}': {exc}") from exc

    async def renew_lock(
        self,
        resource_id: str,
        agent_id: str,
        ttl: float | None = None,
        *,
        acquired_at: float | None = None,
    ) -> bool:
        """Extend the lease of a lock still held by *agent_id*.

        Atomic single UPDATE: the ``AND expires_at > now`` clause guarantees an
        already-expired lease is never resurrected (another agent may have
        legitimately reclaimed the resource in the meantime). The
        ``AND acquired_at = ?`` clause is the acquisition token: two
        instances of the same service share a constant ``agent_id``, so
        matching only the agent would renew a sibling's reacquired row.

        Returns
        -------
        bool
            ``True`` if the lease was extended; ``False`` if the lock no longer
            belongs to *agent_id*, the token does not match, or already expired
            (lease lost).
        """
        self._ensure_conn()  # contrato: LockError si initialize() no corrió
        ttl_seconds = ttl if ttl is not None else self._default_ttl
        try:
            # PR-1b2a (fix #473): con lifecycle, ``transaction()`` es owner del
            # commit/rollback/cancelación y el reloj se captura DESPUÉS de
            # adquirir el boundary: si la lease venció durante la espera del
            # path lock, la cláusula ``expires_at > now`` la ve expirada y no se
            # renueva (never resurrect). Legacy: orden previo intacto.
            if self._lifecycle is not None:
                async with self._lifecycle.transaction(self._db_path) as conn:
                    now = time.time()
                    new_expires_at = now + ttl_seconds
                    async with conn.execute(
                        _RENEW_SQL,
                        (new_expires_at, resource_id, agent_id, acquired_at, now),
                    ) as cursor:
                        renewed = cursor.rowcount > 0
                # commit del wrapper ya terminó acá
                # C7 (fix #473): si el commit terminó DESPUÉS de la nueva fecha
                # de vencimiento, la renovación ya está expirada al retornar:
                # no se declara éxito (False → lease lost para el heartbeat).
                # La fila queda expirada y es reclamable por el mecanismo
                # normal (ON CONFLICT). Sin DELETE compensatorio.
                if renewed and time.time() >= new_expires_at:
                    logger.warning(
                        "Lock renewal expired before commit returned; lease lost: '%s'",
                        resource_id,
                        extra={"resource_id": resource_id, "agent_id": agent_id},
                    )
                    return False
            else:
                now = time.time()
                conn = self._ensure_conn()
                async with conn.execute(
                    _RENEW_SQL,
                    (now + ttl_seconds, resource_id, agent_id, acquired_at, now),
                ) as cursor:
                    renewed = cursor.rowcount > 0
                await conn.commit()
        except DatabaseLifecycleShuttingDownError:
            # Política explícita PR-1b2a: shutdown del lifecycle en curso → la
            # lease no puede renovarse → lease perdido (fail-closed a False),
            # igual que un error de DB. No se reabre ni revive nada.
            logger.warning(
                "Lock renewal skipped — database lifecycle is shutting down (lease will expire): '%s'",
                resource_id,
                extra={"resource_id": resource_id, "agent_id": agent_id},
            )
            return False
        except sqlite3.Error as exc:
            # Best-effort renewal: ANY sqlite failure (not just Operational/
            # Integrity) degrades to "lease lost" (False) rather than
            # propagating. The heartbeat task calls this on a timer; an
            # uncaught exception there would kill it silently and let the lease
            # expire without flipping lease_lost. PR #181 review (Copilot).
            logger.warning(
                "Lock renewal DB error for '%s': %s",
                resource_id,
                exc,
                extra={"resource_id": resource_id, "agent_id": agent_id},
            )
            return False

        if not renewed:
            logger.warning(
                "Lock renewal failed — lease expired or owned by another agent",
                extra={"resource_id": resource_id, "agent_id": agent_id},
            )
        return renewed

    async def force_release(self, resource_id: str) -> bool:
        """Force-release a lock regardless of agent ownership.

        Use only for emergency recovery (e.g. orphan locks after crash).
        """
        self._ensure_conn()  # contrato: LockError si initialize() no corrió
        try:
            # PR-1b2a (fix #473): transaction() en lifecycle (owner del commit).
            # ShuttingDownError se propaga: no cae en el except de abajo.
            if self._lifecycle is not None:
                async with (
                    self._lifecycle.transaction(self._db_path) as conn,
                    conn.execute(
                        _RELEASE_ANY_SQL,
                        (resource_id,),
                    ) as cursor,
                ):
                    deleted = cursor.rowcount > 0
                # commit del wrapper ya terminó acá
            else:
                conn = self._ensure_conn()
                async with conn.execute(
                    _RELEASE_ANY_SQL,
                    (resource_id,),
                ) as cursor:
                    deleted = cursor.rowcount > 0
                await conn.commit()
            if deleted:
                logger.warning(
                    "Lock force-released",
                    extra={"resource_id": resource_id},
                )
            return deleted
        except sqlite3.OperationalError as exc:
            raise LockReleaseError(f"Failed to force-release lock '{resource_id}': {exc}") from exc

    async def get_lock_info(self, resource_id: str) -> LockInfo | None:
        """Query current lock state for a resource (may be expired)."""
        self._ensure_conn()  # contrato: LockError si initialize() no corrió
        # PR-1b2a: lectura bajo boundary; sin commit propio.
        async with (
            self._operacion() as conn,
            conn.execute(
                "SELECT resource_id, agent_id, acquired_at, expires_at FROM resource_locks WHERE resource_id = ?",
                (resource_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
            if row is None:
                return None
            return LockInfo(
                resource_id=row[0],
                agent_id=row[1],
                acquired_at=row[2],
                expires_at=row[3],
            )

    async def cleanup_expired(self) -> int:
        """Delete all locks whose TTL has expired.  Returns count removed."""
        self._ensure_conn()  # contrato: LockError si initialize() no corrió
        # PR-1b2a (fix #473): con lifecycle, ``transaction()`` es owner del
        # commit y el reloj se captura DENTRO del boundary (misma ventana stale
        # que TS-ACQUIRE/TS-RENEW). Legacy: orden previo intacto.
        if self._lifecycle is not None:
            async with self._lifecycle.transaction(self._db_path) as conn:
                now = time.time()
                async with conn.execute(
                    "DELETE FROM resource_locks WHERE expires_at < ?",
                    (now,),
                ) as cursor:
                    count = cursor.rowcount
            # commit del wrapper ya terminó acá
        else:
            now = time.time()
            conn = self._ensure_conn()
            async with conn.execute(
                "DELETE FROM resource_locks WHERE expires_at < ?",
                (now,),
            ) as cursor:
                count = cursor.rowcount
            await conn.commit()
        if count > 0:
            logger.info("Cleaned up %d expired lock(s)", count)
        return count


# =============================================================================
# SHARED CANCELLATION-DURABLE ACQUIRE / RELEASE PRIMITIVES (#741)
# =============================================================================

_TAREAS_DE_CLEANUP_SUPERVISADAS: set[asyncio.Task[None]] = set()
_SUBYACENTES_DE_CLEANUP: dict[asyncio.Task[None], asyncio.Future[Any]] = {}
_CANCELACION_ADQUISICION: contextvars.ContextVar[asyncio.Event | None] = contextvars.ContextVar(
    "_CANCELACION_ADQUISICION",
    default=None,
)


def _invocar_release_lock(
    lock_manager: Any,
    resource_id: str,
    agent_id: str,
    *,
    acquired_at: float | None,
    usar_kwargs: bool,
) -> Any:
    if usar_kwargs:
        return lock_manager.release_lock(
            resource_id=resource_id,
            agent_id=agent_id,
            acquired_at=acquired_at,
        )
    return lock_manager.release_lock(
        resource_id,
        agent_id,
        acquired_at=acquired_at,
    )


def _invocar_acquire_lock(
    lock_manager: Any,
    resource_id: str,
    agent_id: str,
    ttl: float | None,
    *,
    usar_kwargs: bool,
) -> Any:
    if usar_kwargs:
        return lock_manager.acquire_lock(
            resource_id=resource_id,
            agent_id=agent_id,
            ttl=ttl,
        )
    return lock_manager.acquire_lock(
        resource_id,
        agent_id,
        ttl=ttl,
    )


def tareas_de_cleanup_pendientes(lock_manager: Any | None = None) -> set[asyncio.Task[None]]:
    """Devuelve las tareas supervisoras de cleanup en vuelo para ``lock_manager``."""
    if lock_manager is not None:
        conjunto = getattr(lock_manager, "_pending_cleanup_tasks", None)
        if isinstance(conjunto, set):
            return {t for t in conjunto if not t.done()}
        return set()
    return {t for t in _TAREAS_DE_CLEANUP_SUPERVISADAS if not t.done()}


async def drenar_tareas_de_cleanup_pendientes(
    lock_manager: Any | None = None,
    *,
    cancel_pending: bool = True,
    timeout: float | None = 1.0,
) -> None:
    """Espera o cancela las tareas supervisoras de cleanup en vuelo.

    Si ``cancel_pending=True`` (usado en ``DistributedLockManager.close()``),
    cancela en bucle tanto cada tarea supervisora como su operación subyacente
    en vuelo hasta vaciar el conjunto pendiente, garantizando cero tareas
    huérfanas al apagar incluso si una supervisión tardía transitó de acquire a
    release durante el drenado. Si ``cancel_pending=False``, espera hasta
    ``timeout`` a que las tareas supervisoras terminen por sí mismas.
    """
    if cancel_pending:
        vistas: set[asyncio.Task[None]] = set()
        while True:
            pendientes = [t for t in tareas_de_cleanup_pendientes(lock_manager) if t not in vistas]
            if not pendientes:
                break
            vistas.update(pendientes)
            subyacentes: list[asyncio.Future[Any]] = []
            for tarea in pendientes:
                subyacente = _SUBYACENTES_DE_CLEANUP.get(tarea)
                if isinstance(subyacente, asyncio.Future) and not subyacente.done():
                    subyacente.cancel()
                    subyacentes.append(subyacente)
                tarea.cancel()
            await asyncio.gather(*pendientes, *subyacentes, return_exceptions=True)
        return

    pendientes = list(tareas_de_cleanup_pendientes(lock_manager))
    if not pendientes:
        return
    espera = asyncio.gather(*pendientes, return_exceptions=True)
    if timeout is not None:
        await asyncio.wait_for(espera, timeout=timeout)
    else:
        await espera


def _registrar_tarea_supervisada(
    lock_manager: Any,
    tarea_supervisora: asyncio.Task[None],
    *,
    tarea_subyacente: asyncio.Future[Any] | None = None,
) -> None:
    """Registra una tarea supervisora para que conserve propietario y seguimiento."""
    if tarea_subyacente is not None:
        _SUBYACENTES_DE_CLEANUP[tarea_supervisora] = tarea_subyacente
        tarea_supervisora.add_done_callback(lambda t: _SUBYACENTES_DE_CLEANUP.pop(t, None))
    _TAREAS_DE_CLEANUP_SUPERVISADAS.add(tarea_supervisora)
    tarea_supervisora.add_done_callback(_TAREAS_DE_CLEANUP_SUPERVISADAS.discard)
    conjunto = getattr(lock_manager, "_pending_cleanup_tasks", None)
    if not isinstance(conjunto, set):
        with contextlib.suppress(Exception):
            conjunto = set()
            lock_manager._pending_cleanup_tasks = conjunto
    if isinstance(conjunto, set):
        conjunto.add(tarea_supervisora)
        tarea_supervisora.add_done_callback(conjunto.discard)


async def _supervisar_liberacion_tardia(
    *,
    tarea: asyncio.Future[bool],
    resource_id: str,
    agent_id: str,
    on_estado: Callable[[EstadoDeCleanupDeLease], None] | None,
) -> None:
    """Supervisa un ``release_lock`` que superó ``cleanup_timeout`` tras cancelación."""
    try:
        liberada = await asyncio.shield(tarea)
    except asyncio.CancelledError:
        if not tarea.done():
            tarea.cancel()
            with contextlib.suppress(BaseException):
                await tarea
        elif not tarea.cancelled():
            with contextlib.suppress(BaseException):
                _ = tarea.exception()
        return
    except Exception as exc:
        if on_estado is not None:
            on_estado(EstadoDeCleanupDeLease.FALLO_DE_LIBERACION)
        logger.error(
            "Liberación tardía supervisada falló para '%s' (expira por TTL): %s",
            resource_id,
            exc,
            extra={"resource_id": resource_id, "agent_id": agent_id},
        )
        return
    if on_estado is not None:
        on_estado(EstadoDeCleanupDeLease.LIBERADA if liberada else EstadoDeCleanupDeLease.YA_EXPIRADA_O_RECLAMADA)


async def liberar_lease_definitiva(
    lock_manager: Any,
    resource_id: str,
    agent_id: str,
    *,
    acquired_at: float | None,
    cleanup_timeout: float = DEFAULT_CANCEL_CLEANUP_TIMEOUT_SECONDS,
    propagar_cancelacion: bool = True,
    fallo_silencioso: bool = False,
    on_estado: Callable[[EstadoDeCleanupDeLease], None] | None = None,
    _usar_kwargs: bool = False,
) -> bool:
    """Libera una lease por su token ``acquired_at`` con espera acotada ante cancelación.

    Si la tarea llamadora se cancela mientras el DELETE está en vuelo, espera
    hasta ``cleanup_timeout`` segundos para obtener un resultado definitivo. Si
    vence el plazo con la operación aún bloqueada en SQLite, registra estado
    ``INDETERMINADO`` (nunca afirma que la lease fue liberada) y transfiere la
    operación en vuelo a una tarea supervisada antes de propagar ``CancelledError``.
    """
    tarea = asyncio.ensure_future(
        _invocar_release_lock(
            lock_manager,
            resource_id,
            agent_id,
            acquired_at=acquired_at,
            usar_kwargs=_usar_kwargs,
        )
    )
    cancelacion: asyncio.CancelledError | None = None
    plazo: float | None = None
    indeterminado = False
    resultado = False
    try:
        while True:
            try:
                if cancelacion is None:
                    resultado = await asyncio.shield(tarea)
                else:
                    assert plazo is not None
                    restante = max(0.0, plazo - time.monotonic())
                    resultado = await asyncio.wait_for(asyncio.shield(tarea), timeout=restante)
                break
            except TimeoutError:
                if tarea.done() and not tarea.cancelled():
                    resultado = tarea.result()
                    break
                indeterminado = True
                if on_estado is not None:
                    on_estado(EstadoDeCleanupDeLease.INDETERMINADO)
                logger.warning(
                    "Timeout (%.3fs) esperando release_lock de '%s' durante cancelación; "
                    "estado INDETERMINADO (supervisando en segundo plano hasta completar o expirar por TTL)",
                    cleanup_timeout,
                    resource_id,
                    extra={"resource_id": resource_id, "agent_id": agent_id},
                )
                supervisora = asyncio.create_task(
                    _supervisar_liberacion_tardia(
                        tarea=tarea,
                        resource_id=resource_id,
                        agent_id=agent_id,
                        on_estado=on_estado,
                    ),
                    name=f"lock-cleanup-release-{resource_id}",
                )
                _registrar_tarea_supervisada(lock_manager, supervisora, tarea_subyacente=tarea)
                break
            except asyncio.CancelledError as exc:
                if cancelacion is None:
                    cancelacion = exc
                    plazo = time.monotonic() + max(0.0, cleanup_timeout)
                if not tarea.done():
                    # Cancelaron al que espera, no a la liberación: se vuelve a
                    # esperar el MISMO task hasta el plazo cleanup_timeout.
                    continue
                if not tarea.cancelled():
                    # La liberación terminó justo cuando llegó la cancelación:
                    # el resultado ya está disponible (puede levantar LockReleaseError).
                    resultado = tarea.result()
                    break
                if plazo is not None and time.monotonic() >= plazo:
                    indeterminado = True
                    if on_estado is not None:
                        on_estado(EstadoDeCleanupDeLease.INDETERMINADO)
                    break
                logger.warning(
                    "Liberación cancelada a mitad; reintentando el DELETE (idempotente)",
                    extra={"resource_id": resource_id, "agent_id": agent_id},
                )
                tarea = asyncio.ensure_future(
                    _invocar_release_lock(
                        lock_manager,
                        resource_id,
                        agent_id,
                        acquired_at=acquired_at,
                        usar_kwargs=_usar_kwargs,
                    )
                )
    except Exception as release_exc:
        if on_estado is not None:
            on_estado(EstadoDeCleanupDeLease.FALLO_DE_LIBERACION)
        if fallo_silencioso or (cancelacion is not None and propagar_cancelacion):
            logger.error(
                "Failed to release lock (will expire by TTL): %s",
                release_exc,
                extra={
                    "resource_id": resource_id,
                    "agent_id": agent_id,
                },
            )
            if cancelacion is not None and propagar_cancelacion:
                raise cancelacion from release_exc
            return False
        raise

    if not indeterminado and on_estado is not None:
        on_estado(EstadoDeCleanupDeLease.LIBERADA if resultado else EstadoDeCleanupDeLease.YA_EXPIRADA_O_RECLAMADA)
    if cancelacion is not None and propagar_cancelacion:
        raise cancelacion
    return False if indeterminado else resultado


async def _supervisar_adquisicion_tardia(
    *,
    lock_manager: Any,
    tarea: asyncio.Task[LockInfo],
    resource_id: str,
    agent_id: str,
    on_estado: Callable[[EstadoDeCleanupDeLease], None] | None,
    usar_kwargs: bool,
) -> None:
    """Supervisa un ``acquire_lock`` que superó ``cleanup_timeout`` tras cancelación.

    No cancela la adquisición a ciegas (lo que perdería ``acquired_at`` si el
    commit gana en SQLite): espera a que termine y, si obtuvo ``LockInfo``,
    libera únicamente esa lease demostrando su token ``acquired_at`` dentro de
    la misma tarea supervisora (sin engendrar una segunda tarea supervisora).
    """
    try:
        info = await asyncio.shield(tarea)
    except asyncio.CancelledError:
        if not tarea.done():
            tarea.cancel()
            with contextlib.suppress(BaseException):
                await tarea
            return
        if tarea.cancelled() or tarea.exception() is not None:
            if not tarea.cancelled():
                with contextlib.suppress(BaseException):
                    _ = tarea.exception()
            if on_estado is not None:
                on_estado(EstadoDeCleanupDeLease.SIN_LEASE)
            return
        info = tarea.result()
    except BaseException:
        if on_estado is not None:
            on_estado(EstadoDeCleanupDeLease.SIN_LEASE)
        return

    tarea_release = asyncio.ensure_future(
        _invocar_release_lock(
            lock_manager,
            resource_id,
            agent_id,
            acquired_at=info.acquired_at,
            usar_kwargs=usar_kwargs,
        )
    )
    tarea_actual = asyncio.current_task()
    if tarea_actual is not None:
        _SUBYACENTES_DE_CLEANUP[tarea_actual] = tarea_release
    await _supervisar_liberacion_tardia(
        tarea=tarea_release,
        resource_id=resource_id,
        agent_id=agent_id,
        on_estado=on_estado,
    )


async def adquirir_lease_definitiva(
    lock_manager: Any,
    resource_id: str,
    agent_id: str,
    ttl: float | None = None,
    *,
    cleanup_timeout: float = DEFAULT_CANCEL_CLEANUP_TIMEOUT_SECONDS,
    on_estado: Callable[[EstadoDeCleanupDeLease], None] | None = None,
    _usar_kwargs: bool = False,
) -> LockInfo:
    """Adquiere una lease garantizando compensación por token ante cancelación (#741).

    Ejecuta ``lock_manager.acquire_lock(...)`` en una tarea protegida con un
    evento de cancelación cooperativa para los sleeps de backoff. Si el llamador
    es cancelado mientras la adquisición está en vuelo:

    - Si ``acquire_lock`` estaba durmiendo en backoff entre reintentos (sin
      transacción SQLite en curso), despierta de inmediato con ``SIN_LEASE``.
    - Si había un intento SQLite en vuelo, espera hasta ``cleanup_timeout``
      segundos por el resultado definitivo.
    - Si la adquisición completó y devolvió ``LockInfo(acquired_at=T)``, libera
      únicamente esa lease pasando ``acquired_at=T`` antes de re-lanzar
      ``CancelledError``.
    - Si vence ``cleanup_timeout`` con la adquisición aún bloqueada, NO cancela
      la operación en vuelo (evitando perder ``acquired_at`` en ``COMMIT_WINS``):
      registra estado ``INDETERMINADO`` y delega la tarea a
      :func:`_supervisar_adquisicion_tardia` para que la libere por token apenas
      complete o deje que se recupere por TTL.
    """
    cancel_event = asyncio.Event()
    token_ctx = _CANCELACION_ADQUISICION.set(cancel_event)
    try:
        tarea = asyncio.create_task(
            _invocar_acquire_lock(
                lock_manager,
                resource_id,
                agent_id,
                ttl,
                usar_kwargs=_usar_kwargs,
            ),
            name=f"lock-acquire-{resource_id}",
        )
    finally:
        _CANCELACION_ADQUISICION.reset(token_ctx)

    cancelacion: asyncio.CancelledError | None = None
    plazo: float | None = None
    info: LockInfo | None = None
    fallo: BaseException | None = None
    indeterminado = False
    while True:
        try:
            if cancelacion is None:
                info = await asyncio.shield(tarea)
            else:
                assert plazo is not None
                restante = max(0.0, plazo - time.monotonic())
                info = await asyncio.wait_for(asyncio.shield(tarea), timeout=restante)
            break
        except TimeoutError:
            if tarea.done():
                if not tarea.cancelled():
                    exc_tardia = tarea.exception()
                    if exc_tardia is None:
                        info = tarea.result()
                    else:
                        fallo = exc_tardia
                break
            indeterminado = True
            if on_estado is not None:
                on_estado(EstadoDeCleanupDeLease.INDETERMINADO)
            logger.warning(
                "Timeout (%.3fs) esperando acquire_lock de '%s' durante cancelación; "
                "estado INDETERMINADO (supervisando en segundo plano para liberar con acquired_at si completa)",
                cleanup_timeout,
                resource_id,
                extra={"resource_id": resource_id, "agent_id": agent_id},
            )
            supervisora = asyncio.create_task(
                _supervisar_adquisicion_tardia(
                    lock_manager=lock_manager,
                    tarea=tarea,
                    resource_id=resource_id,
                    agent_id=agent_id,
                    on_estado=on_estado,
                    usar_kwargs=_usar_kwargs,
                ),
                name=f"lock-cleanup-acquire-{resource_id}",
            )
            _registrar_tarea_supervisada(lock_manager, supervisora, tarea_subyacente=tarea)
            break
        except asyncio.CancelledError as exc:
            if cancelacion is None:
                cancelacion = exc
                plazo = time.monotonic() + max(0.0, cleanup_timeout)
            cancel_event.set()
            if not tarea.done():
                # Sigue en vuelo: NO se abandona dentro de cleanup_timeout.
                continue
            if not tarea.cancelled():
                exc_interna = tarea.exception()
                if exc_interna is None:
                    # La adquisición terminó EXACTAMENTE cuando se entregó la
                    # cancelación: el token existe y hay que liberar con él.
                    info = tarea.result()
                else:
                    fallo = exc_interna
            break
        except BaseException as exc:
            # Falla real de la adquisición: se re-propaga tal cual (el
            # contrato LockAcquisitionError / LockError no cambia).
            fallo = exc
            break

    if cancelacion is None:
        if fallo is not None:
            raise fallo
        assert info is not None
        if on_estado is not None:
            on_estado(EstadoDeCleanupDeLease.NO_REQUERIDO)
        return info

    if indeterminado:
        raise cancelacion

    if info is not None:
        await liberar_lease_definitiva(
            lock_manager,
            resource_id,
            agent_id,
            acquired_at=info.acquired_at,
            cleanup_timeout=cleanup_timeout,
            propagar_cancelacion=True,
            fallo_silencioso=True,
            on_estado=on_estado,
            _usar_kwargs=_usar_kwargs,
        )
    elif on_estado is not None:
        on_estado(EstadoDeCleanupDeLease.SIN_LEASE)
    raise cancelacion


# =============================================================================
# SNAPSHOT TRANSACTION LOCK (Context Manager)
# =============================================================================


class SnapshotTransactionLock:
    """Async context manager that coordinates locking + snapshotting.

    Usage::

        async with SnapshotTransactionLock(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            resource_id="Skyrim.esm",
            agent_id="synthesis-pipeline",
            target_files=[Path("mods/Skyrim.esm")],
        ) as ctx:
            # Safe zone — lock held, snapshots created
            ctx.snapshots  # list of SnapshotInfo for rollback reference
            ... do work on the files ...

        # On normal exit: lock released.
        # On exception: files rolled back, then lock released.

    Parameters
    ----------
    lock_manager:
        Instance of :class:`DistributedLockManager`.
    snapshot_manager:
        Instance of :class:`FileSnapshotManager`.
    resource_id:
        Name/path of the resource to lock.
    agent_id:
        Agent requesting the lock.
    target_files:
        List of file paths to snapshot on entry.
    ttl:
        Lock TTL override in seconds.
    metadata:
        Extra metadata for the snapshot entries.
    force_rollback:
        If True, restore every target file on exit even when no exception
        occurred.  Powers dry-run / preview: run the chain for real inside the
        lock, capture the diff, then revert SIEMPRE.  Default False (production
        behavior — a clean exit keeps its mutations).
    auto_renew:
        If True (default), a background heartbeat renews the lease at
        TTL/``renew_divisor`` intervals so operations longer than the TTL never
        lose exclusivity.  If a renewal fails (lease lost), ``lease_lost`` flips
        to True and a clean exit raises :class:`LockLeaseLostError`.
    renew_divisor:
        Heartbeat renews every ``TTL/renew_divisor`` seconds (default 3.0). A
        higher value renews more often, shrinking the window between a lost lease
        and its detection, at the cost of more renewal queries. Must be a finite
        number >= 2.0 (a divisor of 1.0 would renew at exactly the TTL, racing
        expiry); the interval is floored so very large values cannot busy-loop
        the DB. Pair with :meth:`assert_owned` before critical writes for ~0 window.
    cleanup_timeout:
        Maximum seconds to wait for an in-flight SQLite acquire or release
        operation when the calling task is cancelled (default
        :data:`DEFAULT_CANCEL_CLEANUP_TIMEOUT_SECONDS`). If exceeded,
        ``estado_cleanup`` becomes :attr:`EstadoDeCleanupDeLease.INDETERMINADO`
        and the in-flight operation remains supervised in the background.
    """

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        resource_id: str,
        agent_id: str,
        target_files: list[pathlib.Path] | None = None,
        ttl: float | None = None,
        metadata: dict[str, Any] | None = None,
        force_rollback: bool = False,
        auto_renew: bool = True,
        renew_divisor: float = 3.0,
        cleanup_timeout: float = DEFAULT_CANCEL_CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        # Reject non-finite (NaN/inf would make the heartbeat interval non-finite:
        # asyncio.sleep(nan) crashes the task, sleep(inf) never renews) and any
        # divisor below the floor (renews too close to / after expiry, racing a
        # concurrent writer). See _MIN_RENEW_DIVISOR.
        if not math.isfinite(renew_divisor) or renew_divisor < _MIN_RENEW_DIVISOR:
            raise ValueError(
                f"renew_divisor must be a finite number >= {_MIN_RENEW_DIVISOR} "
                f"(got {renew_divisor!r}); lower values renew too close to (or "
                "after) lease expiry, racing a concurrent writer"
            )
        if not math.isfinite(cleanup_timeout) or cleanup_timeout < 0.0:
            raise ValueError(f"cleanup_timeout must be a finite number >= 0.0 (got {cleanup_timeout!r})")
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._resource_id = resource_id
        self._agent_id = agent_id
        self._target_files = target_files or []
        self._ttl = ttl
        self._metadata = metadata
        self._force_rollback = force_rollback
        self._auto_renew = auto_renew
        self._renew_divisor = renew_divisor
        self._cleanup_timeout = cleanup_timeout

        # Populated during __aenter__
        self.lock_info: LockInfo | None = None
        self.snapshots: list[SnapshotInfo] = []
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lease_lost = False
        self.estado_cleanup: EstadoDeCleanupDeLease = EstadoDeCleanupDeLease.NO_REQUERIDO

        # Resultado del rollback, poblado en __aexit__. En la ruta de excepción
        # un restore fallido solo se loguea (para no enmascarar el error
        # original), así que el caller NO puede asumir que hubo rollback: debe
        # consultar rollback_completed (review Codex PR #238).
        self.rollback_attempted: bool = False
        self.rollback_failures: list[str] = []
        self.rollback_vetoed_unknown_terminality: bool = False

    def _registrar_estado_cleanup(self, estado: EstadoDeCleanupDeLease) -> None:
        self.estado_cleanup = estado

    @property
    def lease_liberada(self) -> bool:
        """True sólo cuando está demostrado que la lease fue borrada por su token."""
        return self.estado_cleanup == EstadoDeCleanupDeLease.LIBERADA

    @property
    def cleanup_indeterminado(self) -> bool:
        """True cuando el cleanup venció por timeout con la operación SQLite aún en vuelo."""
        return self.estado_cleanup == EstadoDeCleanupDeLease.INDETERMINADO

    @property
    def lease_lost(self) -> bool:
        """True if the heartbeat could not renew the lease (exclusivity gone)."""
        return self._lease_lost

    @property
    def rollback_completed(self) -> bool:
        """True si el rollback corrió y restauró TODOS los snapshots (≥1)."""
        return self.rollback_attempted and bool(self.snapshots) and not self.rollback_failures

    async def assert_owned(self, *, verify_db: bool = True) -> None:
        """Raise :class:`LockLeaseLostError` if exclusivity can no longer be guaranteed.

        Call this immediately before each critical mutation to shrink the window
        between a silently lost lease and its detection — otherwise bounded only
        by the heartbeat interval (``TTL/renew_divisor``), during which the holder
        still believes it owns the lock.

        ``verify_db=False`` is a pure fast-path: it only checks the heartbeat flag
        (no DB round-trip). The default ``verify_db=True`` adds a fresh ownership
        check against the lock table, narrowing the window to ~0 right before the
        write. A failed check flips ``lease_lost`` so the rest of the transaction
        (and the clean-exit guard in ``__aexit__``) sees the loss too.

        The DB check matches the row's ``acquired_at`` against THIS context's
        acquisition token, not just ``agent_id``: two instances of the same
        service share a constant ``agent_id``, so an expire-then-reacquire by a
        sibling would otherwise pass an ``agent_id``-only check even though this
        context's original lease is gone.
        """
        if self._lease_lost:
            raise LockLeaseLostError(
                f"Lease for '{self._resource_id}' (agent '{self._agent_id}') "
                "already lost — exclusivity cannot be guaranteed"
            )
        if not verify_db:
            return
        info = await self._lock_manager.get_lock_info(self._resource_id)
        if (
            info is None
            or self.lock_info is None
            or info.agent_id != self._agent_id
            or info.acquired_at != self.lock_info.acquired_at
            or info.is_expired
        ):
            self._lease_lost = True
            raise LockLeaseLostError(
                f"Lease for '{self._resource_id}' (agent '{self._agent_id}') "
                "lost — ownership check failed before mutation"
            )

    async def _adquirir_lease_definitiva(self) -> LockInfo:
        """Acquire the lease with a DEFINITIVE outcome, even under cancellation (#741)."""
        return await adquirir_lease_definitiva(
            self._lock_manager,
            self._resource_id,
            self._agent_id,
            ttl=self._ttl,
            cleanup_timeout=self._cleanup_timeout,
            on_estado=self._registrar_estado_cleanup,
            _usar_kwargs=True,
        )

    async def __aenter__(self) -> SnapshotTransactionLock:
        """Acquire the distributed lock, then create snapshots."""
        # Step 1: Acquire lock — con resultado DEFINITIVO ante cancelación (#741).
        # La durabilidad vive acá y no dentro de ``acquire_lock`` porque la
        # identidad que prueba la propiedad de la lease es el ``LockInfo`` que
        # esa llamada devuelve: sin el token, la única liberación posible sería
        # por ``resource_id + agent_id``, que borra la lease de un hermano.
        self.lock_info = await self._adquirir_lease_definitiva()

        # Step 2: Create snapshots of target files
        try:
            for file_path in self._target_files:
                if file_path.exists() and file_path.is_file():
                    snap = await self._snapshot_manager.create_snapshot(
                        file_path,
                        metadata=self._metadata,
                    )
                    self.snapshots.append(snap)
                    logger.debug(
                        "Snapshot created under transaction lock",
                        extra={
                            "resource_id": self._resource_id,
                            "file": str(file_path),
                            "snapshot_id": snap.snapshot_id,
                        },
                    )
        except BaseException:
            # Includes asyncio.CancelledError: never leak the lock if snapshot
            # creation fails OR the task is cancelled before the context is
            # fully entered (in which case __aexit__ never runs to release it).
            try:
                await self._safe_release()
            finally:
                self.lock_info = None
            raise

        if self._auto_renew:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"lock-heartbeat-{self._resource_id}",
            )

        return self

    @staticmethod
    def _heartbeat_interval(ttl_seconds: float, divisor: float) -> float:
        """Renewal cadence (``TTL/divisor``), floored so a large divisor cannot
        collapse it to ~0 and busy-loop the lock database."""
        return max(ttl_seconds / divisor, _MIN_HEARTBEAT_INTERVAL_SECONDS)

    async def _heartbeat_loop(self) -> None:
        """Renew the lease at TTL/divisor intervals until cancelled or lost.

        A failed renewal (``False``) means the lease expired or was reclaimed:
        the loop stops and ``lease_lost`` flips so the holder can abort instead
        of racing a concurrent writer.  DB errors inside ``renew_lock`` are
        already swallowed there (returning False), so a transient outage close
        to expiry degrades to lease-lost rather than crashing the heartbeat.
        """
        assert self.lock_info is not None  # set by __aenter__ before the task starts
        ttl_seconds = max(self.lock_info.expires_at - self.lock_info.acquired_at, 0.15)
        interval = self._heartbeat_interval(ttl_seconds, self._renew_divisor)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._lock_manager.renew_lock(
                    self._resource_id,
                    self._agent_id,
                    ttl=ttl_seconds,
                    acquired_at=self.lock_info.acquired_at,
                )
            except Exception:
                # renew_lock() catches the full sqlite3.Error family and
                # returns False, so this is a last-resort guard for a
                # NON-sqlite escape (e.g. the connection being torn down
                # mid-renewal during shutdown): degrade to lease-lost instead
                # of letting this background task die silently.
                logger.critical(
                    "Lock heartbeat for '%s' crashed during renewal — treating lease as lost",
                    self._resource_id,
                    exc_info=True,
                )
                renewed = False
            if not renewed:
                self._lease_lost = True
                logger.critical(
                    "Lock lease LOST mid-operation — another agent may hold '%s' now",
                    self._resource_id,
                    extra={"resource_id": self._resource_id, "agent_id": self._agent_id},
                )
                return

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Rollback snapshots on exception OR when ``force_rollback`` is set.

        ``force_rollback=True`` (dry-run / preview) reverts every target file
        even on a CLEAN exit, reusing the same restore path as the exception
        case — no exceptions are raised for control flow.  The lock is ALWAYS
        released in ``finally``.

        A lost lease raises :class:`LockLeaseLostError` — on a CLEAN exit (the
        heartbeat flagged it) or from the body itself (``assert_owned`` raised).
        Either way lease loss does NOT trigger the exception-path rollback
        (another agent may already have mutated the files; restoring ours would
        clobber theirs), but ``force_rollback=True`` (dry-run) still restores:
        the preview no-mutation contract takes precedence, and the conflict is
        surfaced via :class:`LockLeaseLostError` anyway.  A non-lease body
        exception always takes precedence and is never masked.
        """
        await self._stop_heartbeat()
        # A lost lease — flagged by the heartbeat OR raised by assert_owned() from
        # the body as LockLeaseLostError — must skip the exception-path rollback so
        # we never clobber a concurrent owner's mutations. force_rollback still
        # restores (its no-mutation contract wins).
        lease_loss = self._lease_lost or (exc_type is not None and issubclass(exc_type, LockLeaseLostError))
        vetoed_unknown = exception_forbids_rollback(exc_val)
        if vetoed_unknown:
            logger.critical(
                "Rollback de snapshots para '%s' VETADO por terminalidad desconocida del worker/proceso.",
                self._resource_id,
                extra={"resource_id": self._resource_id, "agent_id": self._agent_id},
            )
            self.rollback_vetoed_unknown_terminality = True
        should_rollback = not vetoed_unknown and (self._force_rollback or (exc_type is not None and not lease_loss))
        self.rollback_attempted = should_rollback
        try:
            if should_rollback:
                await self._rollback_snapshots(exc_val=exc_val)
        finally:
            # Always release the lock, even if rollback fails
            await self._safe_release()

        if exc_type is None and self._lease_lost:
            raise LockLeaseLostError(
                f"Lease for '{self._resource_id}' (agent '{self._agent_id}') was lost "
                "mid-operation — exclusivity cannot be guaranteed for this result"
            )

    async def _stop_heartbeat(self) -> None:
        """Cancel the renewal heartbeat and wait for it to finish."""
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass
        finally:
            self._heartbeat_task = None

    async def _rollback_snapshots(self, *, exc_val: BaseException | None = None) -> None:
        """Restore every snapshot in reverse creation order.

        On the EXCEPTION path (``exc_val is not None``) a failed restore is only
        logged as CRITICAL so it cannot mask the original exception.  On a FORCED
        (dry-run) rollback (``exc_val is None``) a failed restore raises
        :class:`SnapshotRollbackError` — a preview must never report success
        while leaving a file mutated.

        Parameters
        ----------
        exc_val:
            The exception that triggered rollback, if any.  ``None`` means the
            rollback was forced on a clean exit (``force_rollback=True``).
        """
        reason = "Exception detected" if exc_val is not None else "force_rollback (dry-run)"
        logger.warning(
            "%s — rolling back %d snapshot(s)",
            reason,
            len(self.snapshots),
            extra={
                "resource_id": self._resource_id,
                "agent_id": self._agent_id,
                "exception": str(exc_val) if exc_val is not None else None,
            },
        )
        failures: list[str] = []
        restored: set[str] = set()
        try:
            for snap in reversed(self.snapshots):
                try:
                    # verify_checksum=True: el rollback es la última barrera
                    # contra un master oficial corrupto. Sin la verificación, un
                    # snapshot con bit-rot (o una copia truncada) se reinstalaba
                    # sobre el archivo mutado, el lock reportaba
                    # rollback_completed=True y el operador quedaba con un master
                    # corrupto creyendo que la TX se revirtió. Con verificación, el
                    # mismatch de checksum sale por el `except` de abajo: el
                    # archivo queda en rollback_failures (fail-closed, recuperación
                    # manual) en vez de dar por buena una restauración corrupta.
                    # Snapshots sin sidecar .meta.json (pre-SSP-001) no tienen
                    # checksum esperado → la verificación se omite sola.
                    await self._snapshot_manager.restore_snapshot(
                        snap.snapshot_path,
                        pathlib.Path(snap.original_path),
                        verify_checksum=True,
                    )
                except Exception as rollback_exc:
                    logger.critical(
                        "ROLLBACK FAILED for %s: %s — manual recovery required",
                        snap.original_path,
                        rollback_exc,
                        exc_info=True,
                    )
                    failures.append(snap.original_path)
                else:
                    restored.add(snap.original_path)
                    logger.info(
                        "Rolled back file to snapshot",
                        extra={
                            "original_path": snap.original_path,
                            "snapshot_id": snap.snapshot_id,
                        },
                    )
        finally:
            # CancelledError / BaseException abortan el loop ANTES de asignar
            # failures. Sin este finally, rollback_failures queda [] y
            # rollback_completed miente (True) con archivos todavía mutados —
            # el consumidor marca la TX rolled_back y la recuperación manual
            # nunca se dispara.
            interrumpidos = [
                snap.original_path
                for snap in reversed(self.snapshots)
                if snap.original_path not in restored and snap.original_path not in failures
            ]
            if interrumpidos:
                logger.critical(
                    "ROLLBACK INTERRUMPIDO — %d archivo(s) no restaurados: %s",
                    len(interrumpidos),
                    interrumpidos,
                )
                failures.extend(interrumpidos)
            self.rollback_failures = failures

        # A forced (dry-run) rollback that could not restore a file means we may
        # have left it mutated while the caller believes the preview was
        # side-effect free — surface it instead of silently breaking the
        # no-mutation guarantee.  On the exception path we stay silent so the
        # original exception is never masked.
        if failures and exc_val is None:
            raise SnapshotRollbackError(f"force_rollback failed to restore {len(failures)} file(s): {failures}")

    async def _liberar_definitivo(
        self,
        acquired_at: float | None,
        *,
        fallo_silencioso: bool = False,
    ) -> bool:
        """Release the lease, completing the DELETE within ``cleanup_timeout`` (#741)."""
        return await liberar_lease_definitiva(
            self._lock_manager,
            self._resource_id,
            self._agent_id,
            acquired_at=acquired_at,
            cleanup_timeout=self._cleanup_timeout,
            propagar_cancelacion=True,
            fallo_silencioso=fallo_silencioso,
            on_estado=self._registrar_estado_cleanup,
            _usar_kwargs=True,
        )

    async def _safe_release(self, *, acquired_at: float | None = None) -> bool:
        """Release the lock, swallowing non-cancellation errors so the original exception wins."""
        token = (
            acquired_at if acquired_at is not None else (None if self.lock_info is None else self.lock_info.acquired_at)
        )
        return await self._liberar_definitivo(token, fallo_silencioso=True)
