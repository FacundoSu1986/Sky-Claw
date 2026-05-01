"""Tests for sky_claw.__main__ (CLI entry point)."""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.__main__ import AppContext, _main, _parse_args

# ------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------


class TestParseArgs:
    """Tests for _parse_args."""

    def test_defaults(self) -> None:
        args = _parse_args([])
        assert args.mode == "cli"
        assert args.command is None
        assert args.verbose is False

    def test_mode_cli_explicit(self) -> None:
        args = _parse_args(["--mode", "cli"])
        assert args.mode == "cli"

    def test_mode_telegram(self) -> None:
        args = _parse_args(["--mode", "telegram"])
        assert args.mode == "telegram"

    def test_mode_oneshot_with_command(self) -> None:
        args = _parse_args(["--mode", "oneshot", "install Requiem"])
        assert args.mode == "oneshot"
        assert args.command == "install Requiem"

    def test_verbose_flag(self) -> None:
        args = _parse_args(["-v"])
        assert args.verbose is True

    def test_custom_mo2_root(self, tmp_path: pathlib.Path) -> None:
        args = _parse_args(["--mo2-root", str(tmp_path)])
        assert args.mo2_root == tmp_path

    def test_custom_db_path(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "test.db"
        args = _parse_args(["--db-path", str(db)])
        assert args.db_path == db

    @pytest.mark.asyncio
    async def test_custom_loot_exe(self, tmp_path: pathlib.Path) -> None:
        loot = tmp_path / "loot.exe"
        args = _parse_args(["--loot-exe", str(loot)])
        assert args.loot_exe == loot


# ------------------------------------------------------------------
# AppContext lifecycle
# ------------------------------------------------------------------


class TestAppContext:
    """Tests for AppContext start/stop."""

    @pytest.fixture()
    def args(self, tmp_path: pathlib.Path) -> MagicMock:
        a = MagicMock()
        a.db_path = tmp_path / "test.db"
        a.mo2_root = tmp_path / "MO2"
        a.mo2_root.mkdir()
        a.loot_exe = pathlib.Path("loot.exe")
        a.provider = None
        a.operator_chat_id = None
        return a

    @pytest.mark.asyncio
    async def test_start_and_stop(self, args: MagicMock, tmp_path: pathlib.Path) -> None:
        # Use a clean temp config to avoid reading real ~/.sky_claw/config.toml
        clean_config = tmp_path / "config.toml"
        clean_config.write_text("")

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "test-key",
                "NEXUS_API_KEY": "",
                "TELEGRAM_BOT_TOKEN": "",
            },
            clear=False,
        ):
            ctx = AppContext(args)
            await ctx.start_minimal()
            ctx.config_path = clean_config
            await ctx.start_full()

        assert ctx.registry is not None
        assert ctx.router is not None
        assert ctx.session is not None

        await ctx.stop()

        assert ctx.session is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, args: MagicMock) -> None:
        """Calling stop without start should not raise."""
        ctx = AppContext(args)
        await ctx.stop()  # no-op, should not raise


# ------------------------------------------------------------------
# Mode dispatch
# ------------------------------------------------------------------


class TestOneshot:
    """Tests for oneshot mode."""

    @pytest.mark.asyncio
    async def test_oneshot_missing_command_exits(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            await _main(["--mode", "oneshot"])
        assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_oneshot_calls_router(self, tmp_path: pathlib.Path) -> None:
        """Oneshot mode should call the router with the command."""
        mock_router = AsyncMock()
        mock_router.chat.return_value = "Found 3 mods matching 'Requiem'"

        with patch("sky_claw.__main__.AppContext") as mock_app_context:
            # Configure the mock instance that will be returned
            mock_instance = mock_app_context.return_value
            mock_instance.router = mock_router
            mock_instance.start = AsyncMock()
            mock_instance.stop = AsyncMock()
            mock_instance.session = MagicMock()

            await _main(["--mode", "oneshot", "search Requiem"])

            mock_instance.start.assert_awaited_once()
            mock_router.chat.assert_awaited_once_with("search Requiem", mock_instance.session, chat_id="oneshot")
            mock_instance.stop.assert_awaited_once()


class TestTelegram:
    """Tests for telegram mode."""

    @pytest.mark.asyncio
    async def test_telegram_exits_without_token(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """Without TELEGRAM_BOT_TOKEN, telegram mode exits with code 1."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        with patch("sky_claw.__main__.AppContext") as mock_app_context:
            mock_instance = mock_app_context.return_value
            mock_instance.start = AsyncMock()
            mock_instance.stop = AsyncMock()
            mock_instance.router = MagicMock()
            mock_instance.session = MagicMock()
            mock_instance.gateway = MagicMock()
            mock_instance.sender = None  # No bot token → no sender

            with pytest.raises(SystemExit) as exc_info:
                await _main(
                    [
                        "--mode",
                        "telegram",
                        "--db-path",
                        str(tmp_path / "test.db"),
                        "--mo2-root",
                        str(tmp_path),
                    ]
                )
            assert exc_info.value.code == 1
            mock_instance.stop.assert_awaited_once()


class TestCli:
    """Tests for CLI mode argument parsing."""

    def test_cli_is_default_mode(self) -> None:
        args = _parse_args([])
        assert args.mode == "cli"
