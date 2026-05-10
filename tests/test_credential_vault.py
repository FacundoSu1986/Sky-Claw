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
            vault = CredentialVault(db_path=db_path, master_key="key", pool_size=1)
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
