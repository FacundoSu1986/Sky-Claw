"""F1 — cableado del CredentialVault al hot-swap del LLMRouter.

Auditoría Zero-Trust 2026-07-18, hallazgo F1 (ALTO, camino vivo): el router se
construía sin `vault=`, así que `reload_provider` caía siempre en su rama
temprana (`False`) y el hot-swap Zero-Trust de credenciales quedaba inerte.
Aquí se cablea el vault sourcing el master-key desde la env var
`SKYCLAW_VAULT_MASTER_KEY`, de forma backward-compatible (sin env var, el
comportamiento actual se preserva).
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from sky_claw.antigravity.agent.router import LLMRouter
from sky_claw.antigravity.security.credential_vault import CredentialVault
from sky_claw.app_context import AppContext

if TYPE_CHECKING:
    import pathlib

_ENV = "SKYCLAW_VAULT_MASTER_KEY"
_MASTER = "clave-maestra-de-prueba-0123456789"
_API_KEY = "sk-deepseek-test-key-0123456789"


def _make_args(tmp_path: pathlib.Path) -> argparse.Namespace:
    """Args mínimos: AppContext.__init__ solo consume ``db_path``."""
    return argparse.Namespace(db_path=str(tmp_path / "registry.db"))


def _ctx(tmp_path: pathlib.Path) -> AppContext:
    return AppContext(_make_args(tmp_path))


# ---------------------------------------------------------------------------
# Lectura del master-key desde la env var (boundary; no os.environ profundo)
# ---------------------------------------------------------------------------


class TestReadMasterKey:
    def test_devuelve_valor_de_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, _MASTER)
        assert AppContext._read_vault_master_key() == _MASTER

    def test_strip_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, f"  {_MASTER}  ")
        assert AppContext._read_vault_master_key() == _MASTER

    def test_ausente_devuelve_vacio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert AppContext._read_vault_master_key() == ""

    def test_solo_whitespace_es_ausente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "   ")
        assert AppContext._read_vault_master_key() == ""


# ---------------------------------------------------------------------------
# Construcción del vault
# ---------------------------------------------------------------------------


class TestBuildVault:
    async def test_build_e_inicializa_con_roundtrip(self, tmp_path: pathlib.Path) -> None:
        ctx = _ctx(tmp_path)
        db_path = str(tmp_path / "reg_vault.db")
        vault = await ctx._build_credential_vault(_MASTER, db_path)
        try:
            assert isinstance(vault, CredentialVault)
            await vault.set_secret("deepseek_api_key", _API_KEY)
            assert await vault.get_secret("deepseek_api_key") == _API_KEY
        finally:
            await vault.close()

    async def test_artefactos_bajo_path_aislado(self, tmp_path: pathlib.Path) -> None:
        """El DB y el salt del vault viven bajo el path dado, no en ~/.sky_claw."""
        ctx = _ctx(tmp_path)
        db_path = str(tmp_path / "reg_vault.db")
        vault = await ctx._build_credential_vault(_MASTER, db_path)
        try:
            await vault.initialize()
            assert (tmp_path / "reg_vault.db").exists()
            assert (tmp_path / "vault_salt" / "vault_salt.bin").exists()
        finally:
            await vault.close()


# ---------------------------------------------------------------------------
# Provisión (env-var-gated): construir + sembrar + registrar cleanup
# ---------------------------------------------------------------------------


class TestProvisionVault:
    async def test_con_env_var_devuelve_vault_sembrado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_ENV, _MASTER)
        ctx = _ctx(tmp_path)
        db_path = str(tmp_path / "reg_vault.db")
        vault = await ctx._provision_credential_vault("deepseek", _API_KEY, db_path)
        try:
            assert isinstance(vault, CredentialVault)
            assert ctx.credential_vault is vault
            # La clave del provider activo quedó sembrada → hot-swap funcional.
            assert await vault.get_secret("deepseek_api_key") == _API_KEY
        finally:
            await vault.close()

    async def test_sin_env_var_devuelve_none(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        ctx = _ctx(tmp_path)
        db_path = str(tmp_path / "reg_vault.db")
        vault = await ctx._provision_credential_vault("deepseek", _API_KEY, db_path)
        assert vault is None
        assert ctx.credential_vault is None

    async def test_close_vault_nulea_referencia(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, _MASTER)
        ctx = _ctx(tmp_path)
        db_path = str(tmp_path / "reg_vault.db")
        vault = await ctx._provision_credential_vault("deepseek", _API_KEY, db_path)
        assert ctx.credential_vault is vault
        await ctx._close_vault(vault)
        assert ctx.credential_vault is None


# ---------------------------------------------------------------------------
# End-to-end: el hot-swap del router funciona con el vault cableado
# ---------------------------------------------------------------------------


class TestReloadProviderConVault:
    async def test_reload_provider_true_con_vault_sembrado(self, tmp_path: pathlib.Path) -> None:
        ctx = _ctx(tmp_path)
        db_path = str(tmp_path / "reg_vault.db")
        vault = await ctx._build_credential_vault(_MASTER, db_path)
        try:
            await vault.set_secret("deepseek_api_key", _API_KEY)
            router = LLMRouter(vault=vault)
            assert await router.reload_provider("deepseek") is True
        finally:
            await vault.close()

    async def test_reload_provider_false_sin_vault(self) -> None:
        """Backward-compat: sin vault, reload_provider devuelve False sin crashear."""
        router = LLMRouter(provider=MagicMock())
        assert await router.reload_provider("deepseek") is False
