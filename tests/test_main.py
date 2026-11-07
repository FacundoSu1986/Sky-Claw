"""Tests for sky_claw.__main__ (CLI entry point)."""

from __future__ import annotations

import json
import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.__main__ import AppContext, _build_parser, _install_vfs_bridge, _main, _parse_args
from sky_claw.config import Config

#: Qué hace cada ``--mode`` con el posicional ``command``: ``True`` si ``_main`` lo consume (``oneshot`` lo ejecuta,
#: ``security`` lo despacha a ``_run_security``), ``False`` si lo ignoraría en silencio. Se congela por igualdad
#: literal contra los ``choices`` reales del parser: agregar un modo sin clasificarlo rompe el test.
_MODOS_Y_SU_POSICIONAL: dict[str, bool] = {
    "cli": False,
    "telegram": False,
    "oneshot": True,
    "gui": False,
    "security": True,
    "install-vfs-bridge": False,
    "vfs-health": False,
}

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

    def test_mode_install_vfs_bridge(self) -> None:
        args = _parse_args(["--mode", "install-vfs-bridge"])
        assert args.mode == "install-vfs-bridge"

    def test_mode_vfs_health_con_perfil(self) -> None:
        args = _parse_args(["--mode", "vfs-health", "--vfs-profile", "Alternate"])
        assert args.mode == "vfs-health"
        assert args.vfs_profile == "Alternate"

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

    def test_profile_explicit(self) -> None:
        args = _parse_args(["--profile", "Requiem"])
        assert args.profile == "Requiem"

    def test_custom_db_path(self, tmp_path: pathlib.Path) -> None:
        db = tmp_path / "test.db"
        args = _parse_args(["--db-path", str(db)])
        assert args.db_path == db

    @pytest.mark.asyncio
    async def test_custom_loot_exe(self, tmp_path: pathlib.Path) -> None:
        loot = tmp_path / "loot.exe"
        args = _parse_args(["--loot-exe", str(loot)])
        assert args.loot_exe == loot

    @pytest.mark.parametrize(
        "modo_posicional",
        ["cli", "telegram", "oneshot", "gui", "security", "install-vfs-bridge", "vfs-health"],
    )
    def test_rechaza_modo_pasado_como_argumento_posicional_sin_flag_mode(self, modo_posicional: str) -> None:
        """Pasar p. ej. ``python -m sky_claw vfs-health`` sin ``--mode`` no debe caer en silencio al REPL."""
        with pytest.raises(SystemExit) as exc_info:
            _parse_args([modo_posicional])
        assert exc_info.value.code == 2

    def test_mode_security_acepta_su_comando_posicional(self) -> None:
        """``--mode security "scan <ruta>"`` es el uso documentado (DEPLOYMENT.md, docs/user/cli.md)."""
        args = _parse_args(["--mode", "security", "scan ."])
        assert args.mode == "security"
        assert args.command == "scan ."

    def test_el_error_sugiere_oneshot_para_un_comando_corriente(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["install Requiem"])
        err = capsys.readouterr().err
        assert "ejecutarlo con `--mode oneshot`" in err
        assert "quisiste pasar" not in err

    def test_el_error_sugiere_el_modo_cuando_el_posicional_es_un_nombre_de_modo(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["vfs-health"])
        err = capsys.readouterr().err
        assert "quisiste pasar `--mode vfs-health`" in err
        assert "ejecutarlo" not in err

    def test_los_modos_clasificados_son_exactamente_los_choices_del_parser(self) -> None:
        accion_mode = next(a for a in _build_parser()._actions if "--mode" in a.option_strings)
        assert set(accion_mode.choices) == set(_MODOS_Y_SU_POSICIONAL)

    @pytest.mark.parametrize(("modo", "consume_el_posicional"), sorted(_MODOS_Y_SU_POSICIONAL.items()))
    def test_el_posicional_solo_se_admite_en_los_modos_que_lo_consumen(
        self, modo: str, consume_el_posicional: bool
    ) -> None:
        if consume_el_posicional:
            assert _parse_args(["--mode", modo, "x"]).command == "x"
        else:
            with pytest.raises(SystemExit) as exc_info:
                _parse_args(["--mode", modo, "x"])
            assert exc_info.value.code == 2


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

        with (
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "test-key",
                    "NEXUS_API_KEY": "",
                    "TELEGRAM_BOT_TOKEN": "",
                },
                clear=False,
            ),
            patch.object(Config, "DEFAULT_CONFIG_FILE", clean_config),
        ):
            ctx = AppContext(args)
            await ctx.start_minimal()
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


class TestSecurity:
    """El modo ``security`` recibe su comando como posicional y lo despacha a ``_run_security``."""

    @pytest.mark.asyncio
    async def test_security_despacha_el_comando_posicional_a_run_security(self) -> None:
        with (
            patch("sky_claw.__main__.AppContext") as mock_app_context,
            patch("sky_claw.__main__._run_security", new_callable=AsyncMock) as run_security,
        ):
            mock_instance = mock_app_context.return_value
            mock_instance.start = AsyncMock()
            mock_instance.stop = AsyncMock()

            await _main(["--mode", "security", "scan ."])

            run_security.assert_awaited_once_with(mock_instance, "scan .")
            mock_instance.stop.assert_awaited_once()


class TestInstallVfsBridge:
    @pytest.mark.asyncio
    async def test_instala_sin_arrancar_app_context(self, tmp_path: pathlib.Path) -> None:
        with (
            patch("sky_claw.__main__._install_vfs_bridge", new_callable=AsyncMock) as install,
            patch("sky_claw.__main__.AppContext") as app_context,
        ):
            await _main(["--mode", "install-vfs-bridge", "--mo2-root", str(tmp_path)])

        install.assert_awaited_once()
        assert install.await_args.args[0].mo2_root == tmp_path
        app_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_rechaza_instalacion_desde_wsl_antes_de_escribir_config(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.mo2.vfs_broker import VfsBrokerError

        args = MagicMock(mo2_root=tmp_path)
        with (
            patch("sky_claw.__main__.sys.platform", "linux"),
            patch("sky_claw.local.mo2.bridge_installer.MO2BridgeInstaller.install") as install,
            pytest.raises(VfsBrokerError, match="Windows"),
        ):
            await _install_vfs_bridge(args)

        install.assert_not_called()

    async def test_rechaza_worker_de_venv_no_congelado_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """Un intérprete de venv (trampoline) no puede ser inyectado por USVFS (Error 5).
        En Windows y sin el flag de dev, install-vfs-bridge falla cerrado y no escribe
        config (guard derivado del smoke real del PR #350)."""
        from sky_claw.local.mo2.vfs_broker import VfsBrokerError

        args = MagicMock(mo2_root=tmp_path)
        with (
            patch("sky_claw.__main__.sys.platform", "win32"),
            patch.dict("sky_claw.__main__.os.environ", {"SKYCLAW_ALLOW_DEV_WORKER": ""}, clear=False),
            patch("sky_claw.local.mo2.bridge_installer.MO2BridgeInstaller.install") as install,
            pytest.raises(VfsBrokerError, match="congelado"),
        ):
            await _install_vfs_bridge(args)

        install.assert_not_called()

    async def test_allow_dev_worker_habilita_worker_no_congelado(self, tmp_path: pathlib.Path) -> None:
        """El escape hatch SKYCLAW_ALLOW_DEV_WORKER=1 permite un worker no congelado
        (solo pruebas locales) sin fallar cerrado."""
        args = MagicMock(mo2_root=tmp_path)
        with (
            patch("sky_claw.__main__.sys.platform", "win32"),
            patch.dict("sky_claw.__main__.os.environ", {"SKYCLAW_ALLOW_DEV_WORKER": "1"}, clear=False),
            patch(
                "sky_claw.local.mo2.bridge_installer.MO2BridgeInstaller.install",
                return_value=tmp_path,
            ) as install,
        ):
            result = await _install_vfs_bridge(args)

        install.assert_called_once()
        assert install.call_args.kwargs["worker_prefix"] == (
            "-m",
            "sky_claw.local.mo2.vfs_worker",
        )
        assert result == tmp_path


class TestVfsHealth:
    @pytest.mark.asyncio
    async def test_probe_no_arranca_app_context(self, tmp_path: pathlib.Path) -> None:
        with (
            patch("sky_claw.__main__._run_vfs_health", new_callable=AsyncMock) as health,
            patch("sky_claw.__main__.AppContext") as app_context,
        ):
            await _main(
                [
                    "--mode",
                    "vfs-health",
                    "--mo2-root",
                    str(tmp_path / "MO2"),
                    "--skyrim-path",
                    str(tmp_path / "Skyrim"),
                ]
            )

        health.assert_awaited_once()
        app_context.assert_not_called()


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


def test_main_configura_logging_antes_del_loop_y_lo_cierra() -> None:
    from sky_claw import __main__ as main_module

    orden: list[str] = []
    parse = main_module._parse_args

    def _parse(argv):  # noqa: ANN001, ANN202
        orden.append("parse")
        return parse(argv)

    def _run(coro) -> None:  # noqa: ANN001
        orden.append("run")
        coro.close()

    with (
        patch.object(
            main_module,
            "setup_logging",
            side_effect=lambda **_kwargs: orden.append("setup"),
        ),
        patch.object(main_module, "_parse_args", side_effect=_parse),
        patch.object(
            main_module,
            "shutdown_logging",
            side_effect=lambda: orden.append("shutdown") or True,
        ),
        patch.object(main_module.asyncio, "run", side_effect=_run),
    ):
        main_module.main(["--mode", "cli"])

    assert orden == ["setup", "parse", "run", "shutdown"]


def test_main_help_no_contamina_argparse_con_evento_de_logging(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sky_claw import __main__ as main_module
    from sky_claw.logging_config import setup_logging as real_setup

    config = MagicMock(
        telegram_chat_id="",
        llm_provider="deepseek",
        mo2_root=str(tmp_path / "MO2Portable"),
        skyrim_path="",
        loot_exe="loot.exe",
    )

    def _setup(*, level: int):
        return real_setup(level=level, log_dir=tmp_path)

    with (
        patch.object(main_module, "setup_logging", side_effect=_setup),
        patch.object(main_module, "Config", return_value=config),
        pytest.raises(SystemExit) as exit_info,
    ):
        main_module.main(["--help"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert "usage: sky_claw" in captured.out
    assert "Logging async-safe inicializado" not in captured.out
    assert captured.err == ""


def test_main_gui_drena_evento_watchdog_antes_de_cerrar_listener(
    tmp_path: pathlib.Path,
) -> None:
    from sky_claw import __main__ as main_module
    from sky_claw.logging_config import setup_logging as real_setup

    def _setup(*, level: int):
        return real_setup(
            level=level,
            log_dir=tmp_path,
            console_stream=None,
        )

    def _run_gui(_args) -> None:  # noqa: ANN001
        logging.getLogger("SkyClaw.GUI.Watchdog").info(
            "cierre limpio por watchdog",
            extra={"event": "gui_watchdog_shutdown"},
        )

    with (
        patch.object(main_module, "setup_logging", side_effect=_setup),
        patch("sky_claw.app.modes.gui_mode.run_gui_mode", side_effect=_run_gui),
        patch(
            "sky_claw.logging_config._resolve_telegram_chat_id",
            return_value="",
        ),
    ):
        main_module.main(["--mode", "gui"])

    records = [json.loads(line) for line in (tmp_path / "sky_claw.log").read_text(encoding="utf-8").splitlines()]
    assert any(
        record["event"] == "gui_watchdog_shutdown" and record["message"] == "cierre limpio por watchdog"
        for record in records
    )


def test_main_loggea_crash_superior_y_preserva_excepcion(caplog) -> None:
    from sky_claw import __main__ as main_module

    def _run(coro) -> None:  # noqa: ANN001
        coro.close()
        raise RuntimeError("boom superior")

    with (
        patch.object(main_module, "setup_logging"),
        patch.object(main_module, "shutdown_logging", return_value=True) as shutdown,
        patch.object(main_module.asyncio, "run", side_effect=_run),
        caplog.at_level(logging.CRITICAL, logger="sky_claw"),
        pytest.raises(RuntimeError, match="boom superior"),
    ):
        main_module.main(["--mode", "cli"])

    assert "Fallo no manejado en el proceso principal" in caplog.text
    shutdown.assert_called_once_with()


@pytest.mark.asyncio
async def test_main_async_no_configura_handlers_dentro_del_loop() -> None:
    from sky_claw import __main__ as main_module

    with (
        patch.object(main_module, "setup_logging") as setup,
        pytest.raises(SystemExit),
    ):
        await main_module._main(["--mode", "oneshot"])

    setup.assert_not_called()
