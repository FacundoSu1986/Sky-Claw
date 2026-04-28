import base64
import hashlib
import logging
import os
import platform
from pathlib import Path

import aiosqlite
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from sky_claw.core.errors import SecurityViolationError
from sky_claw.security.file_permissions import restrict_to_owner

logger = logging.getLogger("SkyClaw.CredentialVault")


class CredentialVault:
    """Bóveda Criptográfica asíncrona para Zero-Trust y secretos en WAL."""

    @staticmethod
    def _write_salt_atomic(path: str, data: bytes) -> None:
        """Write *data* to *path* atomically to prevent TOCTOU and partial writes.

        Mirrors the pattern used in GovernanceManager._get_or_create_hmac_key:
        write to a sibling .tmp file, restrict permissions, then rename into place.
        The temp file is unlinked on any error so no partial artifact is left behind.
        """
        target = Path(path)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_bytes(data)
            restrict_to_owner(tmp)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read_and_validate_salt(path: str) -> bytes | None:
        """Lee el salt desde *path*. Devuelve None si no existe, no se puede leer o
        no tiene exactamente 32 bytes (formato inválido = corrupto)."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if len(data) != 32:
            logger.warning("Salt en %s con longitud %d (esperado 32) — descartado.", path, len(data))
            return None
        return data

    @staticmethod
    def _get_or_create_salt() -> bytes:
        """
        Genera o recupera el salt único por máquina con fallback de backup.

        El salt se persiste en DOS archivos espejo para tolerar corrupción:
          - primary: ~/.sky_claw/vault_salt.bin
          - backup:  ~/.sky_claw/vault_salt.bin.backup

        Estrategia de recuperación:
          1. Si ambos existen y coinciden → devolver primary.
          2. Si divergen → loguear CRITICAL y preferir primary (autoridad), reescribir backup.
          3. Si solo primary existe → sincronizar backup desde primary.
          4. Si solo backup existe → restaurar primary desde backup.
          5. Si ninguno existe → generar nuevo (LOG CRITICAL: secretos previos irrecuperables).

        Returns:
            bytes: Salt de 32 bytes (256 bits) para PBKDF2HMAC.
        """
        if platform.system() == "Windows":
            base_dir = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        else:
            base_dir = os.path.expanduser("~")

        salt_dir = os.path.join(base_dir, ".sky_claw")
        salt_file = os.path.join(salt_dir, "vault_salt.bin")
        backup_file = os.path.join(salt_dir, "vault_salt.bin.backup")

        try:
            os.makedirs(salt_dir, mode=0o700, exist_ok=True)
            restrict_to_owner(Path(salt_dir))

            primary = CredentialVault._read_and_validate_salt(salt_file)
            backup = CredentialVault._read_and_validate_salt(backup_file)

            if primary is not None and backup is not None:
                if primary == backup:
                    logger.debug("Salt existente recuperado correctamente (primary == backup).")
                    return primary
                logger.critical(
                    "SECURITY: %s y %s divergen. Reteniendo primary y reescribiendo backup. "
                    "Investigar posible manipulación o copia parcial entre máquinas.",
                    salt_file,
                    backup_file,
                )
                CredentialVault._write_salt_atomic(backup_file, primary)
                return primary

            if primary is not None:
                logger.warning("Salt backup ausente — sincronizando desde primary.")
                CredentialVault._write_salt_atomic(backup_file, primary)
                return primary

            if backup is not None:
                logger.warning("Salt primary corrupto/ausente — restaurando desde backup.")
                CredentialVault._write_salt_atomic(salt_file, backup)
                return backup

            # Caso 5: ningún salt válido — generar nuevo (operación destructiva).
            salt = os.urandom(32)
            CredentialVault._write_salt_atomic(salt_file, salt)
            CredentialVault._write_salt_atomic(backup_file, salt)
            logger.critical(
                "SECURITY: nuevo salt generado en %s (+ backup). "
                "Cualquier secreto cifrado con un salt anterior queda IRRECUPERABLE.",
                salt_file,
            )
            return salt

        except OSError as e:
            logger.critical(
                "CRITICAL: Vault salt file I/O failed: %s. "
                "Please manually create %s with 32 random bytes (and a sibling .backup) and retry.",
                e,
                salt_file,
            )
            raise RuntimeError("Vault salt initialization failed — refusing to use weak deterministic fallback") from e
        # Non-OSError exceptions (programming bugs, permission model errors, etc.)
        # propagate unwrapped so the full traceback is preserved for diagnosis.

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
            key_material = master_key if isinstance(master_key, bytes) else master_key.encode("utf-8")
            derived_key = base64.urlsafe_b64encode(kdf.derive(key_material))
            self.fernet = Fernet(derived_key)
        except Exception as exc:
            logger.critical(
                "SECURITY: KDF key derivation failed during CredentialVault init: %s",
                exc,
            )
            raise RuntimeError("CredentialVault KDF initialization failed — vault is unusable") from exc
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

    async def get_secret(self, service_name: str) -> str | None:
        """Recupera y descifra asincrónicamente con aislamiento de transacción."""
        svc_hash = hashlib.sha256(service_name.encode()).hexdigest()[:8]
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await self._execute_pragmas(conn)
                async with conn.execute(
                    "SELECT cipher_text FROM sky_vault WHERE service = ?",
                    (service_name,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        return None  # Secreto legítimamente no configurado
                    cipher_text = row[0].encode("utf-8")
                    try:
                        plain_secret = self.fernet.decrypt(cipher_text).decode("utf-8")
                    except Exception as decrypt_exc:
                        if "InvalidToken" in type(decrypt_exc).__name__:
                            logger.critical(
                                "SECURITY: Vault tampering detected for service_hash=%s. "
                                "Ciphertext integrity check failed — possible corruption or key mismatch.",
                                svc_hash,
                            )
                            raise SecurityViolationError(
                                "Vault integrity check failed — possible tampering"
                            ) from decrypt_exc
                        raise
                    return plain_secret
        except aiosqlite.Error:
            logger.exception("RCA (Vault): Database error accessing service_hash=%s.", svc_hash)
            return None

    async def set_secret(self, service_name: str, plain_secret: str) -> bool:
        """Cifra en memoria y almacena en SQLite safely."""
        try:
            cipher_text = self.fernet.encrypt(plain_secret.encode("utf-8")).decode("utf-8")
            async with aiosqlite.connect(self.db_path) as conn:
                await self._execute_pragmas(conn)
                await conn.execute(
                    "INSERT OR REPLACE INTO sky_vault (service, cipher_text) VALUES (?, ?)",
                    (service_name, cipher_text),
                )
                await conn.commit()
            svc_hash = hashlib.sha256(service_name.encode()).hexdigest()[:8]
            logger.info("🛡️ Secreto guardado exitosamente en bóveda (service_hash=%s).", svc_hash)
            return True
        except Exception:
            logger.exception(
                "RCA (Vault): Error cifrando secreto (service_hash=%s).",
                hashlib.sha256(service_name.encode()).hexdigest()[:8],
            )
            return False
