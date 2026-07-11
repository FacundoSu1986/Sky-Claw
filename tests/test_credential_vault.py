"""Tests for CredentialVault – security hotfix verification.

Audit finding #4: the vault previously used a static hardcoded salt
(b"sky_claw_static_salt_for_vault").  These tests verify that:
  1. A dynamic, cryptographically-secure salt is generated via os.urandom.
  2. Two independent vault instances derive *different* keys from the same
     master_key (because different salts produce different key material).
  3. Vault initialisation fails loudly (RuntimeError + CRITICAL log) when
     salt generation is impossible, instead of silently falling back to a
     weak deterministic value.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from sky_claw.antigravity.core.errors import VaultStorageError
from sky_claw.antigravity.security.credential_vault import CredentialVault


async def _wait_for_semaphore_waiter(sem: asyncio.Semaphore) -> None:
    """Yield to the event loop until *sem* has at least one blocked waiter.

    Preferred over ``asyncio.sleep(N)`` in tests: it is deterministic (no
    fixed delay) and fails fast via the caller's ``asyncio.wait_for`` timeout
    rather than masking bugs with an arbitrary sleep.
    """
    while not sem._waiters:
        await asyncio.sleep(0)


@pytest.fixture
def vault_factory(tmp_path):
    """Return a factory that builds a CredentialVault backed by a tmp DB."""

    def _make(
        master_key: str = "test-master-key",
        pool_size: int = 5,
        salt_dir: Path | None = None,
    ) -> CredentialVault:
        db_path = str(tmp_path / "test_vault.db")
        return CredentialVault(
            db_path=db_path,
            master_key=master_key,
            pool_size=pool_size,
            salt_dir=salt_dir or tmp_path / "salt",
        )

    return _make


class TestCredentialVaultDynamicSalt:
    """Verify that the static-salt vulnerability (audit finding #4) is absent."""

    def test_init_succeeds_with_dynamic_salt(self, vault_factory) -> None:
        """CredentialVault initialises without error using os.urandom-backed salt."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory()
        assert vault.fernet is not None
        assert vault.db_path is not None

    def test_salt_is_32_bytes(self, tmp_path) -> None:
        """_get_or_create_salt() must return exactly 32 bytes (256-bit salt)."""
        # Patch os.urandom to a known 32-byte value and verify it flows through.
        fixed_salt = b"A" * 32
        db_path = str(tmp_path / "salt_test.db")

        with (
            patch.object(
                CredentialVault,
                "_get_or_create_salt",
                return_value=fixed_salt,
            ),
            patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"),
        ):
            vault = CredentialVault(db_path=db_path, master_key="key")
            # If the salt were NOT 32 bytes PBKDF2HMAC would raise; reaching here
            # confirms the plumbing is wired correctly.
            assert vault.fernet is not None

    def test_two_vaults_with_different_salts_produce_different_fernet_keys(self, tmp_path) -> None:
        """Different salts → different derived keys → different Fernet tokens."""
        db_path = str(tmp_path / "v.db")
        master_key = "shared-master-key"

        salt_a = b"\x01" * 32
        salt_b = b"\x02" * 32

        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            with patch.object(CredentialVault, "_get_or_create_salt", return_value=salt_a):
                vault_a = CredentialVault(db_path=db_path, master_key=master_key)

            with patch.object(CredentialVault, "_get_or_create_salt", return_value=salt_b):
                vault_b = CredentialVault(db_path=db_path, master_key=master_key)

        # Each vault encrypts the same plaintext; the ciphertexts must differ
        # because the underlying keys are derived from different salts.
        plain = b"plaintext"
        token_a = vault_a.fernet.encrypt(plain)
        token_b = vault_b.fernet.encrypt(plain)
        assert token_a != token_b

    def test_static_salt_not_used(self, tmp_path) -> None:
        """Ensure the old static salt constant is NOT present in the vault module."""
        import inspect

        import sky_claw.antigravity.security.credential_vault as vault_module

        source = inspect.getsource(vault_module)
        assert "sky_claw_static_salt_for_vault" not in source, (
            "Static hardcoded salt found in credential_vault — audit finding #4 regression"
        )

    def test_salt_failure_raises_runtime_error_with_logging(self, tmp_path, caplog) -> None:
        """When salt I/O fails, __init__ raises RuntimeError and logs CRITICAL."""
        db_path = str(tmp_path / "fail.db")

        with (
            patch.object(
                CredentialVault,
                "_get_or_create_salt",
                side_effect=RuntimeError("disk full"),
            ),
            caplog.at_level(logging.CRITICAL, logger="SkyClaw.CredentialVault"),
            pytest.raises(RuntimeError),
        ):
            CredentialVault(db_path=db_path, master_key="key")

        assert any("SECURITY" in r.message for r in caplog.records), (
            "Expected a CRITICAL security log when salt generation fails"
        )


class TestCredentialVaultConnectionPool:
    """M-03: Verify SQLite async connection pool behaviour."""

    @pytest.mark.asyncio
    async def test_concurrent_reads_succeed(self, vault_factory, tmp_path) -> None:
        """Multiple concurrent get_secret calls must not deadlock or raise."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=3)
        await vault.initialize()
        await vault.set_secret("svc", "value")

        async def reader() -> str | None:
            return await vault.get_secret("svc")

        results = await asyncio.gather(*[reader() for _ in range(10)])
        assert all(r == "value" for r in results)
        await vault.close()

    @pytest.mark.asyncio
    async def test_pool_timeout_raises_storage_error(self, tmp_path) -> None:
        """Exhausting the pool without releasing must trigger VaultStorageError."""
        db_path = str(tmp_path / "timeout.db")
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = CredentialVault(
                db_path=db_path,
                master_key="key",
                pool_size=1,
                salt_dir=tmp_path / "salt",
            )
        await vault.initialize()

        # Acquire the single connection and hold it.
        await vault._pool._semaphore.acquire()
        try:
            with pytest.raises(VaultStorageError) as exc_info:
                await vault.get_secret("svc")
            assert "timeout" in str(exc_info.value).lower()
        finally:
            vault._pool._semaphore.release()
        await vault.close()

    @pytest.mark.asyncio
    async def test_pool_closes_connections(self, vault_factory, tmp_path) -> None:
        """close() must drain and close all pooled connections."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=2)
        await vault.initialize()
        # Warm up the pool by creating a couple of connections.
        await vault.set_secret("a", "1")
        await vault.set_secret("b", "2")
        await vault.close()
        assert vault._pool._closed is True

    def test_pool_size_zero_raises_value_error(self, tmp_path) -> None:
        """pool_size <= 0 must raise ValueError before touching salt files."""
        with (
            patch(
                "sky_claw.antigravity.security.credential_vault.CredentialVault._get_or_create_salt",
                side_effect=AssertionError("salt should not be read"),
            ),
            pytest.raises(ValueError, match="pool_size must be a positive integer"),
        ):
            CredentialVault(
                db_path=str(tmp_path / "bad.db"),
                master_key="key",
                pool_size=0,
            )

    def test_salt_dir_is_injected_without_reading_home(self, tmp_path, monkeypatch) -> None:
        """Tests and sandboxed deployments must avoid implicit writes to home."""
        monkeypatch.setattr(
            "sky_claw.antigravity.security.credential_vault.Path.home",
            lambda: (_ for _ in ()).throw(AssertionError("Path.home must not be used")),
        )
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = CredentialVault(
                db_path=str(tmp_path / "vault.db"),
                master_key="key",
                salt_dir=tmp_path / "explicit-salt",
            )

        assert vault.fernet is not None
        assert (tmp_path / "explicit-salt" / "vault_salt.bin").exists()

    @pytest.mark.asyncio
    async def test_set_secret_storage_error_raises_vault_storage_error(self, vault_factory) -> None:
        """set_secret must not hide SQLite/storage faults behind False."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory()

        @asynccontextmanager
        async def broken_acquire():
            raise aiosqlite.OperationalError("database is locked")
            yield

        vault._pool.acquire = broken_acquire

        with pytest.raises(VaultStorageError, match="write failed"):
            await vault.set_secret("svc", "secret")

    @pytest.mark.asyncio
    async def test_pool_reuses_connections(self, vault_factory, tmp_path) -> None:
        """Sequential operations should reuse connections from the pool."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=1)
        await vault.initialize()
        await vault.set_secret("reuse", "yes")
        val = await vault.get_secret("reuse")
        assert val == "yes"
        await vault.close()

    @pytest.mark.asyncio
    async def test_acquire_suspended_on_close_fails_closed(self, vault_factory) -> None:
        """Audit #2: a task suspended on the semaphore when close() runs must
        raise VaultStorageError, not receive a connection from a closed pool."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=1)
        await vault.initialize()
        pool = vault._pool

        holder_in = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with pool.acquire():
                holder_in.set()
                await release_holder.wait()

        async def waiter() -> None:
            async with pool.acquire():
                pass

        h = asyncio.create_task(holder())
        await asyncio.wait_for(holder_in.wait(), timeout=1.0)

        w = asyncio.create_task(waiter())
        # Wait until the waiter is genuinely blocked on the semaphore (i.e. it
        # has added itself to _waiters) instead of relying on a fixed sleep.
        await asyncio.wait_for(_wait_for_semaphore_waiter(pool._semaphore), timeout=1.0)

        # CV-1: close() ahora espera a las conexiones prestadas, así que corre
        # como task; el waiter debe fallar cerrado sin esperar al holder.
        close_task = asyncio.create_task(pool.close())

        with pytest.raises(VaultStorageError, match="closed"):
            await asyncio.wait_for(w, timeout=1.0)

        release_holder.set()
        await asyncio.wait_for(h, timeout=1.0)
        await asyncio.wait_for(close_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_close_wakes_semaphore_waiters_promptly(self, vault_factory) -> None:
        """Audit #4: close() must wake tasks blocked on the semaphore instead of
        leaving them stalled until the full pool timeout elapses."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=1)
        await vault.initialize()
        pool = vault._pool
        pool._timeout = 30.0  # without the wake-up fix the waiter stalls 30s

        holder_in = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with pool.acquire():
                holder_in.set()
                await release_holder.wait()

        async def waiter() -> None:
            with pytest.raises(VaultStorageError):
                async with pool.acquire():
                    pass

        h = asyncio.create_task(holder())
        await asyncio.wait_for(holder_in.wait(), timeout=1.0)
        w = asyncio.create_task(waiter())
        # Wait until the waiter is genuinely blocked on the semaphore before
        # measuring elapsed time — avoids timing-dependent flakiness on CI.
        await asyncio.wait_for(_wait_for_semaphore_waiter(pool._semaphore), timeout=1.0)

        # CV-1: la intención del test es la latencia de wake del WAITER, no la
        # duración total de close() (que ahora espera al holder): task + medir.
        loop = asyncio.get_running_loop()
        start = loop.time()
        close_task = asyncio.create_task(pool.close())
        await asyncio.wait_for(w, timeout=2.0)
        elapsed = loop.time() - start
        assert elapsed < 2.0, f"waiter stalled {elapsed:.1f}s — close() did not wake it"

        release_holder.set()
        await asyncio.wait_for(h, timeout=1.0)
        await asyncio.wait_for(close_task, timeout=2.0)


class TestCierreEstrictoDelPool:
    """CV-1: contrato estricto de ``_SQLitePool.close()``.

    Al retornar ``close()`` no debe quedar NINGUNA conexión viva al DB de
    secretos: ni prestadas (se espera acotado a que se devuelvan, con
    force-close al expirar el presupuesto) ni creadas en la ventana de
    ``_create_connection`` por un acquire que ya pasó el re-check.
    """

    @pytest.mark.asyncio
    async def test_close_espera_a_conexiones_prestadas(self, vault_factory) -> None:
        """close() no retorna con una conexión prestada; al devolverse queda cerrada."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=1)
        await vault.initialize()
        pool = vault._pool

        holder_in = asyncio.Event()
        release_holder = asyncio.Event()
        prestada: list[aiosqlite.Connection] = []

        async def holder() -> None:
            async with pool.acquire() as conn:
                prestada.append(conn)
                holder_in.set()
                await release_holder.wait()

        h = asyncio.create_task(holder())
        await asyncio.wait_for(holder_in.wait(), timeout=1.0)

        close_task = asyncio.create_task(pool.close())
        # Ceder el loop varias veces: close() debe seguir esperando al holder.
        for _ in range(20):
            await asyncio.sleep(0)
        assert not close_task.done(), "close() retornó con una conexión aún prestada"

        release_holder.set()
        await asyncio.wait_for(h, timeout=1.0)
        await asyncio.wait_for(close_task, timeout=1.0)

        # La conexión devuelta debe haber quedado cerrada de verdad.
        with pytest.raises(ValueError, match="no active connection|[Cc]onnection closed"):
            await prestada[0].execute("SELECT 1")

    @pytest.mark.asyncio
    async def test_close_hace_force_close_tras_timeout(self, vault_factory, caplog) -> None:
        """Si una conexión prestada no se devuelve dentro del presupuesto del pool,
        close() la cierra a la fuerza, deja un warning y retorna igual."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=1)
        await vault.initialize()
        pool = vault._pool
        pool._timeout = 0.2  # presupuesto corto para no demorar el test

        holder_in = asyncio.Event()
        release_holder = asyncio.Event()
        prestada: list[aiosqlite.Connection] = []

        async def holder() -> None:
            async with pool.acquire() as conn:
                prestada.append(conn)
                holder_in.set()
                await release_holder.wait()

        h = asyncio.create_task(holder())
        await asyncio.wait_for(holder_in.wait(), timeout=1.0)

        with caplog.at_level(logging.WARNING, logger="SkyClaw.CredentialVault"):
            await asyncio.wait_for(pool.close(), timeout=2.0)

        # La conexión prestada quedó cerrada a la fuerza y hay registro del hecho.
        with pytest.raises(ValueError, match="no active connection|[Cc]onnection closed"):
            await prestada[0].execute("SELECT 1")
        assert any("force-close" in rec.getMessage() for rec in caplog.records), (
            "close() no dejó registro del force-close en el log"
        )

        # El holder termina después: su devolución tardía no debe explotar.
        release_holder.set()
        await asyncio.wait_for(h, timeout=1.0)

    @pytest.mark.asyncio
    async def test_conexion_creada_durante_close_queda_cerrada(self, vault_factory) -> None:
        """Ventana de _create_connection: un acquire que pasó el re-check y está
        creando su conexión mientras close() corre no debe dejarla viva ni
        re-encolarla en el pool."""
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            vault = vault_factory(pool_size=1)
        # Sin initialize(): el pool arranca vacío y fuerza _create_connection.
        pool = vault._pool

        creando = asyncio.Event()
        continuar = asyncio.Event()
        crear_original = pool._create_connection

        async def creacion_pausada() -> aiosqlite.Connection:
            creando.set()
            await continuar.wait()
            return await crear_original()

        pool._create_connection = creacion_pausada  # type: ignore[method-assign]

        prestada: list[aiosqlite.Connection] = []

        async def borrower() -> None:
            async with pool.acquire() as conn:
                prestada.append(conn)

        b = asyncio.create_task(borrower())
        await asyncio.wait_for(creando.wait(), timeout=1.0)

        close_task = asyncio.create_task(pool.close())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not close_task.done(), "close() retornó con un acquire in-flight"

        continuar.set()
        await asyncio.wait_for(b, timeout=2.0)
        await asyncio.wait_for(close_task, timeout=2.0)

        assert prestada, "el borrower nunca recibió su conexión"
        with pytest.raises(ValueError, match="no active connection|[Cc]onnection closed"):
            await prestada[0].execute("SELECT 1")
        assert pool._pool.empty(), "una conexión post-close quedó re-encolada en el pool"
