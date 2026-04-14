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
        """Método factory con lazy loading thread-safe."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
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
        """Inicializa la base de datos de caché de escaneo."""
        try:
            async with aiosqlite.connect(self.cache_db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
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
        except Exception as e:
            logger.error(f"Error inicializando DB de gobernanza: {e}")

    def _get_or_create_hmac_key(self) -> bytes:
        """Obtiene la clave HMAC del disco o genera una nueva."""
        if self._hmac_key_path.exists():
            return self._hmac_key_path.read_bytes()
        key = os.urandom(32)
        self._hmac_key_path.write_bytes(key)
        return key

    def _compute_hmac(self, content: bytes, key: bytes) -> str:
        """Calcula HMAC-SHA256 del contenido dado."""
        return hmac.new(key, content, hashlib.sha256).hexdigest()

    def _load_whitelist(self) -> set[str]:
        """Carga la lista blanca. Falla cerrado (fail-closed) si hay corrupción."""
        if self.whitelist_path.exists():
            try:
                raw = self.whitelist_path.read_bytes()

                # Validar HMAC si los archivos de integridad existen
                if self._hmac_key_path.exists() and self._hmac_sig_path.exists():
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
                logger.critical(
                    f"Error crítico cargando whitelist: {e}. Abortando para prevenir pérdida de datos."
                )
                # Evita que el framework inicie con una whitelist comprometida
                raise RuntimeError(f"Integridad de whitelist comprometida: {e}")
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
        except Exception as e:
            logger.error(f"Error guardando whitelist: {e}")

    def get_file_hash(self, file_path: str) -> str | None:
        """Calcula el hash SHA-256 de un archivo. Retorna None si falla."""
        sha256_hash = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception as e:
            logger.error(f"Error hasheando archivo {file_path}: {e}")
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
                cursor = await db.execute(
                    "SELECT status FROM scan_cache WHERE file_hash = ?", (file_hash,)
                )
                row = await cursor.fetchone()
                if row and row[0] == "CLEAN":
                    return True
        except Exception as e:
            logger.error(f"Error consultando caché de escaneo: {e}")
        return False

    async def update_scan_result(
        self, file_path: str, results: list[dict], status: str
    ):
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
            logger.error(f"Error actualizando caché: {e}")

    def approve_file(self, file_path: str):
        """Añade un archivo a la lista blanca (HITL)."""
        file_hash = self.get_file_hash(file_path)
        if file_hash is not None:
            self.whitelist.add(file_hash)
            self.save_whitelist()
