"""Tests for SEC-02: CredentialVault get_secret() tampering detection.

Verifies that get_secret() distinguishes three distinct failure modes:
  1. Secret legitimately missing → returns None.
  2. Database error (aiosqlite.Error) → returns None with log.
  3. Ciphertext corruption / invalid master key (InvalidToken) → raises
     SecurityViolationError so callers cannot confuse tampering with absence.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import aiosqlite
import pytest

from sky_claw.antigravity.core.errors import SecurityViolationError
from sky_claw.antigravity.security.credential_vault import CredentialVault


@pytest.fixture
def vault(tmp_path):
    """Return an initialised CredentialVault backed by a tmp DB."""
    db_path = str(tmp_path / "vault_sec02.db")
    vault = CredentialVault(db_path=db_path, master_key="test-master-key")
    return vault


class TestCredentialVaultGetSecret:
    """SEC-02: Distinguish missing secret, DB error, and tampering."""

    @pytest.mark.asyncio
    async def test_get_secret_missing_returns_none(self, vault, caplog):
        """Row absent from DB → legitimate None, no exception."""
        with caplog.at_level(logging.DEBUG):
            result = await vault.get_secret("nonexistent_service")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_secret_db_error_returns_none(self, vault, caplog):
        """aiosqlite.Error during connection/query → return None, log exception."""
        with (
            patch(
                "sky_claw.antigravity.security.credential_vault.aiosqlite.connect",
                side_effect=aiosqlite.Error("disk I/O error"),
            ),
            caplog.at_level(logging.ERROR, logger="SkyClaw.CredentialVault"),
        ):
            result = await vault.get_secret("any_service")

        assert result is None
        assert any("Database error" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_get_secret_invalid_token_raises_security_violation(self, vault, caplog):
        """Corrupted ciphertext → SecurityViolationError, not None."""
        # Prime the vault with a valid secret
        await vault.initialize()
        await vault.set_secret("test_svc", "secret_value")

        # Corrupt the ciphertext in the DB directly
        raw_conn = await aiosqlite.connect(vault.db_path)
        await raw_conn.execute(
            "UPDATE sky_vault SET cipher_text = ? WHERE service = ?",
            ("corrupted_garbage_12345!", "test_svc"),
        )
        await raw_conn.commit()
        await raw_conn.close()

        with (
            pytest.raises(SecurityViolationError) as exc_info,
            caplog.at_level(logging.CRITICAL, logger="SkyClaw.CredentialVault"),
        ):
            await vault.get_secret("test_svc")

        assert "tampering" in str(exc_info.value).lower()
        assert any("tampering detected" in r.message for r in caplog.records)
