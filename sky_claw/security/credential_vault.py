import os
import platform
import aiosqlite
import logging
import base64
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from typing import Optional

from sky_claw.security.file_permissions import restrict_to_owner

logger = logging.getLogger("SkyClaw.CredentialVault")


class CredentialVault:
    """Bóveda Criptográfica asíncrona para Zero-Trust y secretos en WAL."""

    @staticmethod
    def _get_or_create_salt() -> bytes:
        """
        Genera o recupera el salt único por máquina de forma segura.

        El salt se almacena en el perfil del usuario:- Windows: %USERPROFILE%\\.sky_claw\\vault_salt.bin
        - Linux/Mac: ~/.sky_claw/vault_salt.bin

        Returns:
            bytes: Salt de 32 bytes (256 bits) para PBKDF2HMAC.
        """
        # Determinar ruta base según SO
        if platform.system() == "Windows":
            base_dir = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        else:
            base_dir = os.path.expanduser("~")

        salt_dir = os.path.join(base_dir, ".sky_claw")
        salt_file = os.path.join(salt_dir, "vault_salt.bin")

        try:
            # Si el archivo existe, leer y retornar el salt
            if os.path.exists(salt_file):
                with open(salt_file, "rb") as f:
                    salt = f.read()
                    if len(salt) == 32:
                        logger.debug("Salt existente recuperado correctamente")
                        return salt
                    else:
                        logger.warning(
                            f"Salt con longitud inválida ({len(salt)} bytes), regenerando..."
                        )

            # Crear directorio padre con permisos 0700 (solo propietario)
            os.makedirs(salt_dir, mode=0o700, exist_ok=True)
            restrict_to_owner(Path(salt_dir))

            # Generar salt criptográficamente seguro de 32 bytes
            salt = os.urandom(32)

            # Guardar el salt en el archivo
            with open(salt_file, "wb") as f:
                f.write(salt)

            # Establecer permisos del archivo a 0600 (solo propietario: lectura/escritura)
            restrict_to_owner(Path(salt_file))

            logger.info(f"Nuevo salt generado y almacenado en: {salt_file}")
            return salt

        except Exception as e:
            logger.critical(
                f"CRITICAL: Vault salt file I/O failed: {e}. "
                f"Please manually create {salt_file} with 32 random bytes and retry."
            )
            raise RuntimeError(
                "Vault salt initialization failed — refusing to use weak deterministic fallback"
            ) from e

    def __init__(self, db_path: str, master_key: bytes | str) -> None:
        """Inicializa la bóveda con el path a la DB SQLite local para almacenar
        los cibercódigos. La clave maestra inyectada se deriva con PBKDF2
        para obtener una clave fuerte de 32 bytes para Fernet.

        El salt es generado dinámicamente (os.urandom) y persistido en disco
        de forma segura por _get_or_create_salt(). Nunca se usa un salt estático.
        """
        try:
            salt = self._get_or_create_salt()
        except RuntimeError:
            logger.critical(
                "SECURITY: CredentialVault cannot obtain a cryptographic salt. "
                "Vault initialization aborted to prevent insecure key derivation."
            )
            raise
        try:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=480000,
            )
            key_material = (
                master_key
                if isinstance(master_key, bytes)
                else master_key.encode("utf-8")
            )
            derived_key = base64.urlsafe_b64encode(kdf.derive(key_material))
            self.fernet = Fernet(derived_key)
        except Exception as exc:
            logger.critical(
                "SECURITY: KDF key derivation failed during CredentialVault init: %s",
                exc,
            )
            raise RuntimeError(
                "CredentialVault KDF initialization failed — vault is unusable"
            ) from exc
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
            logger.info(
                "🔐 Bóveda de credenciales instanciada e inicializada (Zero Trust local SQLite)."
            )
        except Exception as e:
            logger.error(f"❌ Fallo al inicializar Bóveda Criptográfica: {e}")
            raise

    async def get_secret(self, service_name: str) -> Optional[str]:
        """Recupera y descifra asincrónicamente con aislamiento de transacción."""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await self._execute_pragmas(conn)
                async with conn.execute(
                    "SELECT cipher_text FROM sky_vault WHERE service = ?",
                    (service_name,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        cipher_text = row[0].encode("utf-8")
                        plain_secret = self.fernet.decrypt(cipher_text).decode("utf-8")
                        return plain_secret
            return None
        except Exception as e:
            logger.error(
                f"RCA (Vault): Error descifrando secreto para {service_name}. Posible corrupción o clave maestra inválida - {e}"
            )
            return None

    async def set_secret(self, service_name: str, plain_secret: str) -> bool:
        """Cifra en memoria y almacena en SQLite safely."""
        try:
            cipher_text = self.fernet.encrypt(plain_secret.encode("utf-8")).decode(
                "utf-8"
            )
            async with aiosqlite.connect(self.db_path) as conn:
                await self._execute_pragmas(conn)
                await conn.execute(
                    "INSERT OR REPLACE INTO sky_vault (service, cipher_text) VALUES (?, ?)",
                    (service_name, cipher_text),
                )
                await conn.commit()
            logger.info(
                f"🛡️ Secreto guardado exitosamente en bóveda para: {service_name}"
            )
            return True
        except Exception as e:
            logger.error(
                f"RCA (Vault): Error cifrando secreto para {service_name} - {e}"
            )
            return False
