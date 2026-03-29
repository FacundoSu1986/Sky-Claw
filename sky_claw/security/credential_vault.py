import os
import aiosqlite
import logging
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from typing import Optional

logger = logging.getLogger("SkyClaw.CredentialVault")

class CredentialVault:
    """Bóveda Criptográfica asíncrona para Zero-Trust y secretos en WAL."""
    def __init__(self, db_path: str, master_key: bytes | str):
        """
        Inicializa la bóveda con el path a la DB SQLite local para almacenar
        los cibercódigos. La clave maestra inyectada se deriva con PBKDF2
        para obtener una clave fuerte de 32 bytes para Fernet.
        """
        salt = b"sky_claw_static_salt_for_vault" # Idealmente debería ser dinámico/almacenado
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        key_material = master_key if isinstance(master_key, bytes) else master_key.encode('utf-8')
        derived_key = base64.urlsafe_b64encode(kdf.derive(key_material))
        self.fernet = Fernet(derived_key)
        self.db_path = db_path

    async def _execute_pragmas(self, conn: aiosqlite.Connection):
        """Aplica aislación SRE para las DBs."""
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")

    async def initialize(self):
        """Asegura que el schema necesario de la bóveda esté creado."""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await self._execute_pragmas(conn)
                await conn.execute(
                    """CREATE TABLE IF NOT EXISTS sky_vault (
                        service TEXT PRIMARY KEY,
                        cipher_text TEXT NOT NULL
                    )"""
                )
                await conn.commit()
            logger.info("🔐 Bóveda de credenciales instanciada e inicializada (Zero Trust local SQLite).")
        except Exception as e:
            logger.error(f"❌ Fallo al inicializar Bóveda Criptográfica: {e}")
            raise

    async def get_secret(self, service_name: str) -> Optional[str]:
        """Recupera y descifra asincrónicamente con aislamiento de transacción."""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await self._execute_pragmas(conn)
                async with conn.execute("SELECT cipher_text FROM sky_vault WHERE service = ?", (service_name,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        cipher_text = row[0].encode('utf-8')
                        plain_secret = self.fernet.decrypt(cipher_text).decode('utf-8')
                        return plain_secret
            return None
        except Exception as e:
            logger.error(f"RCA (Vault): Error descifrando secreto para {service_name}. Posible corrupción o clave maestra inválida - {e}")
            return None

    async def set_secret(self, service_name: str, plain_secret: str) -> bool:
        """Cifra en memoria y almacena en SQLite safely."""
        try:
            cipher_text = self.fernet.encrypt(plain_secret.encode('utf-8')).decode('utf-8')
            async with aiosqlite.connect(self.db_path) as conn:
                await self._execute_pragmas(conn)
                await conn.execute(
                    "INSERT OR REPLACE INTO sky_vault (service, cipher_text) VALUES (?, ?)",
                    (service_name, cipher_text)
                )
                await conn.commit()
            logger.info(f"🛡️ Secreto guardado exitosamente en bóveda para: {service_name}")
            return True
        except Exception as e:
            logger.error(f"RCA (Vault): Error cifrando secreto para {service_name} - {e}")
            return False
