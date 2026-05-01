"""
Sky-Claw Governance System v5.5 (Abril 2026)
Gestión de integridad, caché de escaneo y listas blancas (Whitelist).
Implementación persistente en SQLite y JSON.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

from sky_claw.antigravity.security.file_permissions import restrict_to_owner

logger = logging.getLogger(__name__)

# Configuración por defecto de gobernanza
WHITELIST_FILE = ".purple_whitelist.json"
CACHE_DB_PATH = "security_cache.db"


class WhitelistSchema(BaseModel):
    """Esquema Pydantic para validación de whitelist JSON."""

    approved_hashes: list[str]


class GovernanceManager:
    _instance = None
    _lock = threading.Lock()
    _HMAC_KEY_SERVICE = "sky_claw_whitelist_hmac"

    @classmethod
    def get_instance(cls, base_path: str = ".") -> "GovernanceManager":
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

    async def _init_db(self):
        """Inicializa la base de datos de caché de escaneo.

        FASE 1.5.2: Uses hardened pragmas consistent with DatabaseLifecycleManager.
        """
        try:
            db = await aiosqlite.connect(self.cache_db_path)
            # FASE 1.5.2: Hardened pragmas (consistent with db_lifecycle.py)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA temp_store=MEMORY")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scan_cache (
                    file_hash TEXT PRIMARY KEY,
                    file_path TEXT,
                    last_scan_time TEXT,
                    scan_results TEXT,
                    status TEXT
                )
            """)
            await db.commit()
            # Store connection for later shutdown
            self._db_conn = db
        except Exception as e:
            logger.error("Error inicializando DB de gobernanza: %s", e)

    async def _shutdown_db(self) -> None:
        """FASE 1.5.2: Graceful shutdown with WAL checkpoint."""
        conn = getattr(self, "_db_conn", None)
        if conn is not None:
            try:
                await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await conn.close()
                logger.info("GovernanceManager: DB shutdown with checkpoint complete")
            except Exception as e:
                logger.error("GovernanceManager: error during DB shutdown: %s", e)
            finally:
                self._db_conn = None

    def _get_or_create_hmac_key(self) -> bytes:
        """Obtiene la clave HMAC del disco o genera una nueva.

        Writes to a sibling temp file first, restricts permissions, then
        renames atomically — eliminates the TOCTOU window where the key
        would be world-readable between write and chmod.
        """
        if self._hmac_key_path.exists():
            # Best-effort hardening: previous installations may have left the
            # key file world-readable. Tighten ACLs on every load so upgrades
            # converge to owner-only permissions.
            restrict_to_owner(self._hmac_key_path)
            return self._hmac_key_path.read_bytes()
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
        """Persiste la lista blanca en disco con protección HMAC."""
        try:
            content = json.dumps({"approved_hashes": list(self.whitelist)}, indent=4)
            raw = content.encode("utf-8")
            self.whitelist_path.write_bytes(raw)

            # Generar/actualizar HMAC de integridad
            key = self._get_or_create_hmac_key()
            sig = self._compute_hmac(raw, key)
            self._hmac_sig_path.write_text(sig)
            restrict_to_owner(self._hmac_sig_path)
        except Exception as e:
            logger.error("Error guardando whitelist: %s", e)

    def get_file_hash(self, file_path: str) -> str | None:
        """Calcula el hash SHA-256 de un archivo. Retorna None si falla."""
        sha256_hash = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception as e:
            logger.error("Error hasheando archivo %s: %s", file_path, e)
            return None

    async def is_scanned_and_clean(self, file_path: str) -> bool:
        """Verifica si el archivo ya fue escaneado y no ha cambiado (incremental)."""
        file_hash = self.get_file_hash(file_path)
        if file_hash is None:
            return False

        # Primero ver si está en la whitelist manual
        if file_hash in self.whitelist:
            return True

        try:
            async with aiosqlite.connect(self.cache_db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                cursor = await db.execute("SELECT status FROM scan_cache WHERE file_hash = ?", (file_hash,))
                row = await cursor.fetchone()
                if row and row[0] == "CLEAN":
                    return True
        except Exception as e:
            logger.error("Error consultando caché de escaneo: %s", e)
        return False

    async def update_scan_result(self, file_path: str, results: list[dict], status: str):
        """Actualiza el estado de escaneo en la base de datos."""
        file_hash = self.get_file_hash(file_path)
        if file_hash is None:
            return

        try:
            async with aiosqlite.connect(self.cache_db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
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
