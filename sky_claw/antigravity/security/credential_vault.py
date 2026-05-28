from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from sky_claw.antigravity.core.errors import SecurityViolationError, VaultStorageError
from sky_claw.antigravity.security.file_permissions import restrict_to_owner

logger = logging.getLogger("SkyClaw.CredentialVault")


class _SQLitePool:
    """Async-safe SQLite connection pool with Zero-Trust timeouts.

    Uses a semaphore to cap concurrent connections and a queue for
    reuse. Connections are created lazily and live until the pool is
    closed. Each connection is born with WAL pragmas applied.
    """

    def __init__(self, db_path: str, max_size: int = 5, timeout: float = 5.0) -> None:
        if max_size <= 0:
            raise ValueError("pool_size must be a positive integer")
        if timeout <= 0:
            raise ValueError("pool timeout must be positive")
        self._db_path = db_path
        self._max_size = max_size
        self._timeout = timeout
        # BoundedSemaphore (not plain Semaphore) is required so that
        # release() raises ValueError when the counter would exceed max_size.
        # This lets close() and the CancelledError handler detect over-release.
        self._semaphore = asyncio.BoundedSemaphore(max_size)
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=max_size)
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def _create_connection(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self._db_path)
        try:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
        except BaseException:
            # Ensure the raw connection is closed on any error (including
            # CancelledError) so shutdown/cancellation cannot leak file handles.
            await conn.close()
            raise
        return conn

    @asynccontextmanager
    async def acquire(self):
        """Obtain a connection from the pool, returning it on exit."""
        if self._closed:
            raise VaultStorageError("SQLite connection pool is closed")

        # Wait for a slot (bounded concurrency).
        _semaphore_acquired = False
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._timeout)
            _semaphore_acquired = True
        except TimeoutError as exc:
            raise VaultStorageError(f"SQLite pool timeout ({self._timeout}s) acquiring connection") from exc
        except BaseException:
            # asyncio.wait_for may internally decrement the semaphore before
            # propagating CancelledError (Python 3.11 simultaneous-completion
            # race).  If it did, _semaphore_acquired is still False here but
            # the permit is gone.  BoundedSemaphore.release() raises ValueError
            # when already at max_size (i.e. acquire never completed), so
            # suppressing ValueError makes this safe in both branches.
            if not _semaphore_acquired:
                with suppress(ValueError):
                    self._semaphore.release()
            raise

        # Semaphore is now held unconditionally; release it in every exit path
        # to avoid permit leaks on CancelledError or close()-race.
        try:
            # Re-check after the await: close() may have flipped _closed while
            # this coroutine was suspended on the semaphore.
            if self._closed:
                raise VaultStorageError("SQLite connection pool is closed")

            conn: aiosqlite.Connection | None = None
            # Try to reuse an existing connection first.
            try:
                conn = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                try:
                    conn = await asyncio.wait_for(self._create_connection(), timeout=self._timeout)
                except TimeoutError as exc:
                    raise VaultStorageError(f"SQLite pool timeout ({self._timeout}s) creating connection") from exc

            try:
                yield conn
            finally:
                if conn is not None:
                    if self._closed:
                        await conn.close()
                    else:
                        try:
                            self._pool.put_nowait(conn)
                        except asyncio.QueueFull:
                            await conn.close()
        finally:
            # suppress(ValueError) handles the close()-race: close() releases
            # permits to wake waiting tasks, inflating the counter; an active
            # holder that finishes after that would exceed the bound on release.
            # Since _closed is set, the pool is dead anyway so the permit count
            # no longer matters.
            with suppress(ValueError):
                self._semaphore.release()

    async def close(self) -> None:
        """Drain the pool and close every connection."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True

        # Unblock any coroutines suspended on the semaphore so they observe
        # _closed immediately via the post-acquire re-check and raise cleanly,
        # instead of waiting up to self._timeout seconds for a slot.
        # suppress(ValueError): BoundedSemaphore raises when released above its
        # bound. This happens if close() is called while no tasks are waiting
        # (all permits already free). Stop releasing as soon as the bound is hit.
        for _ in range(self._max_size):
            with suppress(ValueError):
                self._semaphore.release()

        while True:
            try:
                conn = self._pool.get_nowait()
                await conn.close()
            except asyncio.QueueEmpty:
                break


class CredentialVault:
    """Bóveda Criptográfica asíncrona para Zero-Trust y secretos en WAL."""

    @staticmethod
    def _write_salt_atomic(path: str | Path, data: bytes) -> None:
        """Write *data* to *path* atomically to prevent TOCTOU and partial writes.

        Mirrors the pattern used in GovernanceManager._get_or_create_hmac_key:
        write to a sibling .tmp file, restrict permissions, then rename into place.
        The temp file is unlinked on any error so no partial artifact is left behind.

        T1-03 — Symlink hardening:

        - If ``target`` already exists as a symbolic link, refuse: an
          attacker who can write to the directory could swap the existing
          salt with a symlink pointing to an arbitrary file, and our
          ``tmp.replace(target)`` would happily overwrite the link target.
        - If ``tmp`` exists as a symbolic link before we write, remove it
          (it was placed by an attacker to redirect our write).
        - The temp file is created with ``O_NOFOLLOW | O_EXCL`` so an
          attacker cannot race-inject a symlink between our existence check
          and the actual open.
        - After hardening permissions and before the final ``replace``, the
          target is re-checked: if a symlink appeared in the meantime,
          unlink the tmp and raise.
        """
        target = Path(path)
        tmp = target.with_name(target.name + ".tmp")

        # Pre-checks (TOCTOU window is tiny because we use O_NOFOLLOW on open).
        try:
            if target.is_symlink():
                raise PermissionError(
                    f"Refusing to write through pre-existing symlink: {target}"
                )
            if tmp.is_symlink():
                # Hostile symlink placed by an attacker; remove before write.
                # If a real concurrent writer were holding the tmp, replace
                # would have failed downstream anyway.
                tmp.unlink()
        except OSError:
            # If the symlink check itself failed for an IO reason, fall
            # through and let the write/open attempt produce the real error.
            pass

        # Build open flags: O_NOFOLLOW prevents following a symlink at the
        # last moment, O_EXCL ensures we are the creator (no pre-existing
        # regular file we'd inadvertently truncate-and-poison).
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            fd = os.open(tmp, flags, 0o600)
            try:
                # Write in a loop because os.write may return fewer bytes
                # than requested under signal interruption on some platforms.
                view = memoryview(data)
                written = 0
                while written < len(view):
                    n = os.write(fd, view[written:])
                    if n <= 0:
                        raise OSError("os.write returned non-positive count")
                    written += n
            finally:
                os.close(fd)

            restrict_to_owner(tmp)

            # Re-validate target right before replace: an attacker may have
            # raced a symlink into place after our pre-check.
            if target.exists() and target.is_symlink():
                tmp.unlink(missing_ok=True)
                raise PermissionError(
                    f"Symlink appeared at {target} between pre-check and replace"
                )

            tmp.replace(target)
        except (OSError, RuntimeError):
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read_and_validate_salt(path: str | Path) -> bytes | None:
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
    def _get_or_create_salt(salt_dir: Path | None = None) -> bytes:
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
        resolved_salt_dir = salt_dir or (Path.home() / ".sky_claw")
        salt_file = resolved_salt_dir / "vault_salt.bin"
        backup_file = resolved_salt_dir / "vault_salt.bin.backup"

        try:
            resolved_salt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            restrict_to_owner(resolved_salt_dir)

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

    def __init__(
        self,
        db_path: str,
        master_key: bytes | str,
        *,
        pool_size: int = 5,
        salt_dir: Path | None = None,
    ) -> None:
        """Inicializa la bóveda con el path a la DB SQLite local para almacenar
        los cibercódigos. La clave maestra inyectada se deriva con PBKDF2
        para obtener una clave fuerte de 32 bytes para Fernet.

        El salt es generado dinámicamente (os.urandom) y persistido en disco
        de forma segura por _get_or_create_salt(). Nunca se usa un salt estático.
        """
        if pool_size <= 0:
            raise ValueError("pool_size must be a positive integer")

        try:
            salt = self._get_or_create_salt(salt_dir)
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
        except (TypeError, ValueError) as exc:
            logger.critical(
                "SECURITY: KDF key derivation failed during CredentialVault init: %s",
                exc,
            )
            raise RuntimeError("CredentialVault KDF initialization failed — vault is unusable") from exc
        self.db_path = db_path
        self._pool = _SQLitePool(db_path, max_size=pool_size)

    async def initialize(self):
        """Asegura que el schema necesario de la bóveda esté creado."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """CREATE TABLE IF NOT EXISTS sky_vault (
                        service TEXT PRIMARY KEY,
                        cipher_text TEXT NOT NULL
                    )"""
                )
                await conn.commit()
            logger.info("🔐 Bóveda de credenciales instanciada e inicializada (Zero Trust local SQLite).")
        except VaultStorageError:
            raise
        except aiosqlite.Error as exc:
            logger.error("Fallo al inicializar Bóveda Criptográfica: %s", exc)
            raise VaultStorageError("Vault storage initialization failed") from exc

    async def get_secret(self, service_name: str) -> str | None:
        """Retrieve and decrypt a stored secret.

        Returns:
            The plaintext secret, or ``None`` ONLY when no row exists for
            ``service_name`` (secret legitimately not configured).

        Raises:
            VaultStorageError: The underlying SQLite store failed
                (disk I/O, lock, schema). Caller should treat as transient
                operational fault; eligible for retry/alerting.
            SecurityViolationError: Ciphertext failed integrity check
                (possible tampering or key/salt mismatch). Caller MUST NOT
                swallow — this is a security incident.
        """
        svc_hash = hashlib.sha256(service_name.encode()).hexdigest()[:8]
        try:
            async with (
                self._pool.acquire() as conn,
                conn.execute(
                    "SELECT cipher_text FROM sky_vault WHERE service = ?",
                    (service_name,),
                ) as cursor,
            ):
                row = await cursor.fetchone()
                if not row:
                    return None  # Secreto legítimamente no configurado
                cipher_text = row[0].encode("utf-8")
                try:
                    plain_secret = self.fernet.decrypt(cipher_text).decode("utf-8")
                except InvalidToken as decrypt_exc:
                    logger.critical(
                        "SECURITY: Vault tampering detected for service_hash=%s. "
                        "Ciphertext integrity check failed — possible corruption or key mismatch.",
                        svc_hash,
                    )
                    raise SecurityViolationError("Vault integrity check failed — possible tampering") from decrypt_exc
                return plain_secret
        except (VaultStorageError, SecurityViolationError):
            raise
        except aiosqlite.Error as db_exc:
            logger.exception("RCA (Vault): Database error accessing service_hash=%s.", svc_hash)
            raise VaultStorageError(f"Vault storage read failed for service_hash={svc_hash}") from db_exc

    async def set_secret(self, service_name: str, plain_secret: str) -> bool:
        """Cifra en memoria y almacena en SQLite safely."""
        svc_hash = hashlib.sha256(service_name.encode()).hexdigest()[:8]
        try:
            cipher_text = self.fernet.encrypt(plain_secret.encode("utf-8")).decode("utf-8")
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO sky_vault (service, cipher_text) VALUES (?, ?)",
                    (service_name, cipher_text),
                )
                await conn.commit()
            logger.info("🛡️ Secreto guardado exitosamente en bóveda (service_hash=%s).", svc_hash)
            return True
        except VaultStorageError:
            raise
        except aiosqlite.Error as exc:
            logger.exception("RCA (Vault): Error persistiendo secreto (service_hash=%s).", svc_hash)
            raise VaultStorageError(f"Vault storage write failed for service_hash={svc_hash}") from exc
        except (TypeError, ValueError) as exc:
            logger.exception("RCA (Vault): Error cifrando secreto (service_hash=%s).", svc_hash)
            raise VaultStorageError(f"Vault encryption write failed for service_hash={svc_hash}") from exc

    async def close(self) -> None:
        """Cierra ordenadamente el pool de conexiones subyacente."""
        await self._pool.close()
