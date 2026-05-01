"""Tests for ARC-01 and ARC-03: AppContext teardown resilience and zombie prevention.

ARC-01: database.close() failure during teardown must not prevent exit-stack
reconstruction on the next start_full() call.

ARC-03: After a failed start_full(), all mutable references must be nulled so
that is_configured returns False and callers do not use closed/zombie objects.
"""

from __future__ import annotations

import argparse
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

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

        # ARC-01 evidence: the teardown failure was logged but we continued
        assert any("Teardown previo falló" in r.message for r in caplog.records)
        # Exit stack must have been rebuilt (not None and push_async_callback works)
        assert ctx._exit_stack is not None

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
        ctx.frontend_bridge = MagicMock()
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
        assert ctx.frontend_bridge is None
        assert ctx.tools_installer is None
