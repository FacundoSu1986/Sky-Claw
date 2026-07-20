"""Tests for ARC-01 and ARC-03: AppContext teardown resilience and zombie prevention.

ARC-01: database.close() failure during teardown must not prevent exit-stack
reconstruction on the next start_full() call.

ARC-03: After a failed start_full(), all mutable references must be nulled so
that is_configured returns False and callers do not use closed/zombie objects.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from sky_claw.app_context import AppContext


@pytest.fixture
def mock_args(tmp_path: pathlib.Path):
    """Minimal argparse-like namespace for AppContext."""
    args = argparse.Namespace(
        db_path=str(tmp_path / "test.db"),
        mo2_root=tmp_path / "MO2",
        staging_dir=tmp_path / "staging",
        provider="ollama",
        operator_chat_id=None,
        loot_exe=None,
        install_dir=None,
        mode="cli",
    )
    return args


class TestAppContextResilience:
    """ARC-01 + ARC-03: Teardown atomicity and zombie reference nulling."""

    @pytest.mark.asyncio
    async def test_teardown_survives_database_close_failure(self, mock_args, caplog):
        """ARC-01: If database.close() raises, exit stack is still rebuilt."""
        ctx = AppContext(mock_args)
        ctx.config_path = pathlib.Path(mock_args.db_path).parent / "config.toml"
        ctx.config_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.config_path.write_text("", encoding="utf-8")

        # Bootstrap minimal state
        with patch.object(ctx.network, "initialize", new_callable=AsyncMock):
            await ctx.start_minimal()

        # Inject a previously-started router so teardown must close it
        ctx.router = MagicMock()
        ctx.router.close = AsyncMock()

        # Force database.close() to fail ONLY on the first call (teardown),
        # but succeed on the second call (aclose callback) so the exception
        # propagated to the caller is the one from LLMRouter, not DB close.
        db_close_calls = 0

        async def fail_once():
            nonlocal db_close_calls
            db_close_calls += 1
            if db_close_calls == 1:
                raise RuntimeError("DB close failure")

        ctx.database.close = fail_once

        # Aggressively mock everything after teardown so we fail fast at a known point
        with (
            patch("sky_claw.app_context.Config") as mock_config,
            patch("sky_claw.app_context.AutoDetector") as mock_auto,
            patch("sky_claw.app_context.MO2Controller"),
            patch("sky_claw.app_context.MasterlistClient"),
            patch("sky_claw.app_context.TelegramSender"),
            patch("sky_claw.app_context.HITLGuard"),
            patch("sky_claw.app_context.SyncEngine") as mock_sync,
            patch("sky_claw.app_context.ToolsInstaller"),
            patch("sky_claw.app_context.AsyncToolRegistry"),
            patch("sky_claw.app_context.LLMRouter") as mock_router,
            caplog.at_level("ERROR", logger="sky_claw"),
        ):
            mock_config.return_value = MagicMock(
                mo2_root="",
                skyrim_path="",
                llm_provider="ollama",
                llm_model="",
                llm_api_key="",
                nexus_api_key="",
                telegram_bot_token="",
                telegram_chat_id="",
                loot_exe="",
                xedit_exe="",
                pandora_exe="",
                bodyslide_exe="",
                install_dir="",
                save=MagicMock(),
            )
            mock_auto.find_mo2 = AsyncMock(return_value=None)
            mock_auto.find_skyrim = AsyncMock(return_value=None)
            mock_sync.return_value.run = AsyncMock()
            mock_router.return_value.open = AsyncMock()
            # Force a late failure to verify exit stack was rebuilt
            mock_router.return_value.open.side_effect = RuntimeError("forced router failure")

            with pytest.raises(RuntimeError, match="forced router failure"):
                await ctx._start_full_inner()

        mock_sync.return_value.run.assert_awaited_once_with(
            ANY,
            profile="Default",
            enrich_remote=False,
        )

        # ARC-01 evidence: the teardown failure was logged but we continued
        assert any("Teardown previo falló" in r.message for r in caplog.records)
        # Exit stack must have been rebuilt (not None and push_async_callback works)
        assert ctx._exit_stack is not None

    @pytest.mark.asyncio
    async def test_cold_boot_con_key_cae_a_local_only_si_enriquecimiento_falla_total(self, mock_args):
        """Con API key pero Nexus caído en el primer arranque, el sync enriquecido
        falla para todos los mods (processed=0, failed>0) y el cold boot reintenta
        en modo local-only para no dejar el registry vacío."""
        from sky_claw.antigravity.orchestrator.sync_engine import SyncResult

        ctx = AppContext(mock_args)
        ctx.config_path = pathlib.Path(mock_args.db_path).parent / "config.toml"
        ctx.config_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.config_path.write_text("", encoding="utf-8")

        with patch.object(ctx.network, "initialize", new_callable=AsyncMock):
            await ctx.start_minimal()

        with (
            patch("sky_claw.app_context.Config") as mock_config,
            patch("sky_claw.app_context.AutoDetector") as mock_auto,
            patch("sky_claw.app_context.MO2Controller"),
            patch("sky_claw.app_context.MasterlistClient"),
            patch("sky_claw.app_context.TelegramSender"),
            patch("sky_claw.app_context.HITLGuard"),
            patch("sky_claw.app_context.SyncEngine") as mock_sync,
            patch("sky_claw.app_context.ToolsInstaller"),
            patch("sky_claw.app_context.AsyncToolRegistry"),
            patch("sky_claw.app_context.LLMRouter") as mock_router,
        ):
            mock_config.return_value = MagicMock(
                mo2_root="",
                skyrim_path="",
                llm_provider="ollama",
                llm_model="",
                llm_api_key="",
                nexus_api_key="fake",
                telegram_bot_token="",
                telegram_chat_id="",
                loot_exe="",
                xedit_exe="",
                pandora_exe="",
                bodyslide_exe="",
                install_dir="",
                save=MagicMock(),
            )
            mock_auto.find_mo2 = AsyncMock(return_value=None)
            mock_auto.find_skyrim = AsyncMock(return_value=None)
            # 1er run enriquecido: 0 procesados, 2 fallidos → gatilla el fallback
            # local-only; 2do run local: 2 procesados.
            mock_sync.return_value.run = AsyncMock(
                side_effect=[
                    SyncResult(processed=0, failed=2),
                    SyncResult(processed=2),
                ]
            )
            # Cortamos el arranque justo después del cold boot (router.open va
            # después) para no montar el resto del stack.
            mock_router.return_value.open = AsyncMock(side_effect=RuntimeError("stop"))

            with pytest.raises(RuntimeError, match="stop"):
                await ctx._start_full_inner()

        run = mock_sync.return_value.run
        assert run.await_count == 2
        assert run.await_args_list[0].kwargs["enrich_remote"] is True
        assert run.await_args_list[1].kwargs["enrich_remote"] is False

    @pytest.mark.asyncio
    async def test_references_nulled_after_failed_start_full(self, mock_args):
        """ARC-03: After start_full() fails, mutable refs must be None."""
        ctx = AppContext(mock_args)
        ctx.config_path = pathlib.Path(mock_args.db_path).parent / "config.toml"
        ctx.config_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.config_path.write_text("[default]\n", encoding="utf-8")

        with patch.object(ctx.network, "initialize", new_callable=AsyncMock):
            await ctx.start_minimal()

        # Inject fake references as if a previous start_full had succeeded
        ctx.router = MagicMock()
        ctx.router.close = AsyncMock()
        ctx.polling = MagicMock()
        ctx.hitl = MagicMock()
        ctx.sender = MagicMock()
        ctx.tools_installer = MagicMock()

        # Force failure inside the try block by making database.initialize raise.
        ctx.database.initialize = AsyncMock(side_effect=RuntimeError("forced init failure"))

        with pytest.raises(RuntimeError, match="forced init failure"):
            await ctx._start_full_inner()

        # ARC-03: After rollback, references must be nulled
        assert ctx.router is None
        assert ctx.polling is None
        assert ctx.hitl is None
        assert ctx.sender is None
        assert ctx.tools_installer is None


class TestAppContextSecretMigrationLogging:
    def test_secret_migration_failure_does_not_log_secret_material(
        self,
        mock_args,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret_value = "sk-" + "A" * 32
        legacy_path = tmp_path / "sky_claw_config.json"
        legacy_path.write_text("{}", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        class LegacyConfig:
            first_run = True

            def get_api_key(self) -> str:
                raise RuntimeError(f"could not decode {secret_value}")

            def get_nexus_api_key(self) -> None:
                return None

            def get_telegram_bot_token(self) -> None:
                return None

        toml_cfg = MagicMock()
        toml_cfg._data = {}

        ctx = AppContext(mock_args)
        ctx.config_path = tmp_path / "config.toml"

        with (
            patch("sky_claw.app_context._load_legacy_json", return_value=LegacyConfig()),
            patch("sky_claw.app_context.Config", return_value=toml_cfg),
            caplog.at_level(logging.WARNING, logger="sky_claw"),
        ):
            ctx._migrate_legacy_json()

        assert "Failed to migrate a legacy credential" in caplog.text
        assert secret_value not in caplog.text
        assert "could not decode" not in caplog.text
        assert "llm_api_key" not in caplog.text
