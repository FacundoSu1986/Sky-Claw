"""
Sky-Claw Governance System v5.5 (Abril 2026)
Gestión de integridad, caché de escaneo y listas blancas (Whitelist).
Implementación persistente en SQLite y JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from sky_claw.antigravity.security.file_permissions import restrict_to_owner

if TYPE_CHECKING:
    from sky_claw.antigravity.core.db_lifecycle import DatabaseLifecycleManager

logger = logging.getLogger(__name__)

# Configuración por defecto de gobernanza
WHITELIST_FILE = ".purple_whitelist.json"
CACHE_DB_PATH = "security_cache.db"


class WhitelistSchema(BaseModel):
    """Esquema Pydantic para validación de whitelist JSON."""

    approved_hashes: list[str]


class GovernanceManager:
    _instance = None
    # G-1/G-2 (ADR 0004): threading.Lock intencional. get_instance() se invoca en el
    # bootstrap y también desde async (audit_resource → _phase_resolve), pero el lock
    # se sostiene microsegundos sin contención cross-thread (un solo event loop) y la
    # carga de whitelist es I/O one-time trivial (_load_whitelist), dominada por el
    # escaneo posterior. Ver el ADR y su criterio de reversión antes de migrar a
    # asyncio.Lock o diferir la carga.
    _lock = threading.Lock()
    _HMAC_KEY_SERVICE = "sky_claw_whitelist_hmac"
    _HASH_CONCURRENCY = 4

    @classmethod
    def get_instance(cls, base_path: str = ".") -> GovernanceManager:
        """Método factory con lazy loading thread-safe.

        Raises RuntimeError if called with a different base_path than the
        existing singleton — prevents silent misconfiguration.
        """
        with cls._lock:
            if cls._instance is not None:
                if str(cls._instance.base_path.resolve()) != str(Path(base_path).resolve()):
                    raise RuntimeError(
                        f"GovernanceManager singleton conflict: "
                        f"existing={cls._instance.base_path}, requested={base_path}"
                    )
                return cls._instance
            cls._instance = cls(base_path)
            return cls._instance

    def __init__(self, base_path: str = "."):
        self.base_path = Path(base_path)
        self.whitelist_path = self.base_path / WHITELIST_FILE
        self._hmac_key_path = Path(str(self.whitelist_path) + ".hmac_key")
        self._hmac_sig_path = Path(str(self.whitelist_path) + ".hmac")
        self.cache_db_path = self.base_path / CACHE_DB_PATH
        self.whitelist = self._load_whitelist()
        self._hash_semaphore: asyncio.Semaphore | None = None
        # M-01 PR C: lifecycle inyectado por AppContext.set_lifecycle().
        # Antes de que se invoque, is_scanned_and_clean retorna False
        # (fail-closed) y update_scan_result loggea error sin persistir.
        self._lifecycle: DatabaseLifecycleManager | None = None

    def _get_hash_semaphore(self) -> asyncio.Semaphore:
        if self._hash_semaphore is None:
            self._hash_semaphore = asyncio.Semaphore(self._HASH_CONCURRENCY)
        return self._hash_semaphore

    def set_lifecycle(self, manager: DatabaseLifecycleManager) -> None:
        """Inyecta el DatabaseLifecycleManager del proceso (M-01 PR C).

        DEBE invocarse antes de la primera llamada a ``is_scanned_and_clean``
        o ``update_scan_result``. Si no se invoca, ambos métodos fallan-cerrado
        (warning + return False / log error sin persistir).

        Pensado para ser invocado UNA VEZ por ``AppContext`` después de que
        el lifecycle esté inicializado. Cierra M-01: governance ya no posee
        conexiones SQLite propias, las pide al lifecycle del proceso.
        """
        self._lifecycle = manager

    async def _ensure_schema(self, db: object) -> None:
        """Bootstrap idempotente del schema ``scan_cache``.

        Se invoca al inicio de cada operación DB para garantizar que la tabla
        exista. El ``commit()`` posterior al DDL cierra cualquier transacción
        implícita que aiosqlite haya podido abrir, evitando que un checkpoint
        WAL en ``shutdown_all()`` encuentre transacciones pendientes.
        """
        await db.execute(  # type: ignore[union-attr]
            """
            CREATE TABLE IF NOT EXISTS scan_cache (
                file_hash TEXT PRIMARY KEY,
                file_path TEXT,
                last_scan_time TEXT,
                scan_results TEXT,
                status TEXT
            )
            """
        )
        await db.commit()  # type: ignore[union-attr]

    def _get_or_create_hmac_key(self) -> bytes:
        """Obtiene la clave HMAC del disco o genera una nueva.

        Writes to a sibling temp file first, restricts permissions, then
        renames atomically — eliminates the TOCTOU window where the key
        would be world-readable between write and chmod.

        M-03: if the existing key file cannot be hardened (restrict_to_owner
        raises PermissionError after destroying the artifact), fall through
        to regeneration so the whitelist HMAC chain is preserved with a
        fresh, secure key rather than silently returning a world-readable one.
        """
        if self._hmac_key_path.exists():
            # Tighten ACLs on every load so upgrades from older installations
            # (which may have left the key world-readable) converge to
            # owner-only permissions.
            try:
                restrict_to_owner(self._hmac_key_path)
                return self._hmac_key_path.read_bytes()
            except PermissionError as exc:
                logger.warning(
                    "HMAC key %s could not be hardened (%s); regenerating to fail closed.",
                    self._hmac_key_path,
                    exc,
                )
                # On Windows, restrict_to_owner (fail-closed) unlinks the file
                # before raising.  On POSIX it raises from os.chmod without
                # unlinking, so the potentially world-readable key may remain.
                # Explicitly remove it in both cases so regeneration never
                # derives a new HMAC from an old, exposed key.
                try:
                    self._hmac_key_path.unlink(missing_ok=True)
                except OSError as del_exc:
                    logger.warning(
                        "Could not delete HMAC key %s before regeneration: %s",
                        self._hmac_key_path,
                        del_exc,
                    )
                # Fall through to regeneration.
        key = os.urandom(32)
        tmp_path = self._hmac_key_path.with_suffix(".tmp")
        tmp_path.write_bytes(key)
        restrict_to_owner(tmp_path)
        tmp_path.replace(self._hmac_key_path)
        return key

    def _compute_hmac(self, content: bytes, key: bytes) -> str:
        """Calcula HMAC-SHA256 del contenido dado."""
        return hmac.new(key, content, hashlib.sha256).hexdigest()

    def _load_whitelist(self) -> set[str]:
        """Carga la lista blanca. Falla cerrado (fail-closed) si hay corrupción."""
        if self.whitelist_path.exists():
            try:
                raw = self.whitelist_path.read_bytes()

                # Validar HMAC si los archivos de integridad existen.
                # Si existe la clave pero falta la firma, fallar cerrado:
                # podría indicar que un atacante borró la firma para evadir la verificación.
                if self._hmac_key_path.exists():
                    if not self._hmac_sig_path.exists():
                        raise RuntimeError(
                            "Archivo de firma HMAC ausente pero la clave existe: "
                            "la integridad de la whitelist no puede verificarse"
                        )
                    key = self._hmac_key_path.read_bytes()
                    expected = self._hmac_sig_path.read_text().strip()
                    actual = self._compute_hmac(raw, key)
                    if not hmac.compare_digest(expected, actual):
                        raise RuntimeError(
                            "Verificación HMAC de whitelist fallida: el archivo pudo haber sido manipulado"
                        )

                json_data = json.loads(raw)
                data = WhitelistSchema.model_validate(json_data)
                return set(data.approved_hashes)
            except RuntimeError:
                raise
            except Exception as e:
                logger.critical("Error crítico cargando whitelist: %s. Abortando para prevenir pérdida de datos.", e)
                raise RuntimeError(f"Integridad de whitelist comprometida: {e}") from e
        return set()

    def save_whitelist(self):
        """Persiste la lista blanca en disco con protección HMAC.

        M-04: Both whitelist and HMAC signature are written atomically.
        The sequence is:
        1. Write whitelist to .tmp, compute HMAC, write sig to .tmp.
        2. Rename whitelist .tmp into place.
        3. Rename sig .tmp into place.

        If the process crashes between (2) and (3), the next load will
        detect the missing sig file and fail-closed (RuntimeError L177).
        This is strictly better than the old behaviour where a crash
        between write and HMAC computation left a whitelist with NO sig
        at all, causing silent data loss on reload.
        """
        _committed = False
        try:
            content = json.dumps({"approved_hashes": list(self.whitelist)}, indent=4)
            raw = content.encode("utf-8")

            # Phase 1: Write to temp files
            wl_tmp = self.whitelist_path.with_suffix(".json.tmp")
            sig_tmp = self._hmac_sig_path.with_suffix(".hmac.tmp")

            wl_tmp.write_bytes(raw)
            restrict_to_owner(wl_tmp)

            key = self._get_or_create_hmac_key()
            sig = self._compute_hmac(raw, key)
            sig_tmp.write_text(sig)
            restrict_to_owner(sig_tmp)

            # Phase 2: Atomic rename (both files exist with valid content)
            wl_tmp.replace(self.whitelist_path)
            sig_tmp.replace(self._hmac_sig_path)
            _committed = True
        except (OSError, TypeError, RuntimeError) as e:
            logger.error("Error guardando whitelist: %s", e)
            raise
        finally:
            # Clean up temp files on any failure, regardless of exception type.
            # Skipped when _committed=True because the renames already moved them.
            if not _committed:
                for tmp in (
                    self.whitelist_path.with_suffix(".json.tmp"),
                    self._hmac_sig_path.with_suffix(".hmac.tmp"),
                ):
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)

    @staticmethod
    def _hash_file_blocking(file_path: str) -> str | None:
        sha256_hash = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception as e:
            logger.error("Error hasheando archivo %s: %s", file_path, e)
            return None

    def get_file_hash(self, file_path: str) -> str | None:
        """Calcula el hash SHA-256 de un archivo. Retorna None si falla.

        Variante síncrona — solo para callers fuera de corutinas (HITL UI,
        approve_file). Desde rutas async, usar :meth:`get_file_hash_async`.
        """
        return self._hash_file_blocking(file_path)

    async def get_file_hash_async(self, file_path: str) -> str | None:
        """Versión async del hash: descarga al thread pool con cap de concurrencia.

        Evita bloquear el event loop al hashear BSAs/BA2s grandes (cientos de
        MB a varios GB). El semáforo limita el fan-out de hilos cuando se
        escanea un perfil completo en paralelo.
        """
        async with self._get_hash_semaphore():
            return await asyncio.to_thread(self._hash_file_blocking, file_path)

    async def is_scanned_and_clean(self, file_path: str) -> bool:
        """Verifica si el archivo ya fue escaneado y no ha cambiado (incremental).

        M-01 PR C: usa la conexión del DatabaseLifecycleManager inyectado vía
        ``set_lifecycle()`` en lugar de abrir una conexión efímera. Si el
        lifecycle no fue inyectado, falla-cerrado (warning + return False).
        """
        file_hash = await self.get_file_hash_async(file_path)
        if file_hash is None:
            return False

        # Primero ver si está en la whitelist manual (no requiere DB)
        if file_hash in self.whitelist:
            return True

        if self._lifecycle is None:
            logger.warning(
                "GovernanceManager.is_scanned_and_clean llamado sin lifecycle "
                "(set_lifecycle() no fue invocado). Retornando False (fail-closed)."
            )
            return False

        try:
            db = await self._lifecycle.get_connection(self.cache_db_path)
            await self._ensure_schema(db)
            async with db.execute("SELECT status FROM scan_cache WHERE file_hash = ?", (file_hash,)) as cursor:
                row = await cursor.fetchone()
                return bool(row and row[0] == "CLEAN")
        except Exception as e:
            logger.error("Error consultando caché de escaneo: %s", e)
            return False

    async def update_scan_result(self, file_path: str, results: list[dict], status: str) -> None:
        """Actualiza el estado de escaneo en la base de datos.

        M-01 PR C: usa la conexión del DatabaseLifecycleManager inyectado vía
        ``set_lifecycle()``. Si el lifecycle no fue inyectado, loggea error y
        retorna sin persistir (fail-closed).
        """
        file_hash = await self.get_file_hash_async(file_path)
        if file_hash is None:
            return

        if self._lifecycle is None:
            logger.error(
                "GovernanceManager.update_scan_result llamado sin lifecycle. Resultado de escaneo NO persistido."
            )
            return

        try:
            db = await self._lifecycle.get_connection(self.cache_db_path)
            await self._ensure_schema(db)
            await db.execute(
                """
                INSERT OR REPLACE INTO scan_cache
                (file_hash, file_path, last_scan_time, scan_results, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    file_hash,
                    str(file_path),
                    datetime.now(UTC).isoformat(),
                    json.dumps(results),
                    status,
                ),
            )
            await db.commit()
        except Exception as e:
            logger.error("Error actualizando caché: %s", e)

    def approve_file(self, file_path: str):
        """Añade un archivo a la lista blanca (HITL)."""
        file_hash = self.get_file_hash(file_path)
        if file_hash is not None:
            self.whitelist.add(file_hash)
            self.save_whitelist()
