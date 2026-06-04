"""Sky-Claw CLI entry point.

Usage::

    python -m sky_claw --mode cli          # interactive REPL
    python -m sky_claw --mode telegram     # Telegram webhook server
    python -m sky_claw --mode oneshot "install Requiem"
    python -m sky_claw --mode gui         # local desktop UI (NiceGUI)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import pathlib
import signal
import sys

# Apply Python 3.14+ polyfills before importing sky_claw submodules that could
# transitively pull NiceGUI / vbuild. Must run before the package imports below.
from sky_claw.compat import setup_python_compat

setup_python_compat()

from sky_claw.antigravity.modes.cli_mode import _run_cli, _run_oneshot  # noqa: E402
from sky_claw.antigravity.modes.security_mode import _run_security  # noqa: E402
from sky_claw.antigravity.modes.telegram_mode import _run_telegram  # noqa: E402
from sky_claw.app_context import AppContext  # noqa: E402
from sky_claw.config import Config, SystemPaths  # noqa: E402
from sky_claw.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("sky_claw")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = Config()
    _chat_id_str = config.telegram_chat_id or ""
    _default_chat_id = int(_chat_id_str) if _chat_id_str.isdigit() else None

    parser = argparse.ArgumentParser(
        prog="sky_claw",
        description="Sky-Claw — Autonomous Skyrim mod management agent",
    )
    parser.add_argument(
        "--mode",
        choices=["cli", "telegram", "oneshot", "gui", "security"],
        default="cli",
        help="Operation mode (default: cli)",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "deepseek", "ollama"],
        default=config.llm_provider or "deepseek",
        help="LLM provider (default: deepseek)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="Command to execute in oneshot mode",
    )
    parser.add_argument(
        "--mo2-root",
        type=pathlib.Path,
        default=pathlib.Path(config.mo2_root or str(SystemPaths.get_base_drive() / "MO2Portable")),
        help="Path to the MO2 portable instance",
    )
    parser.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=pathlib.Path("mod_registry.db"),
        help="Path to the mod registry database",
    )
    parser.add_argument(
        "--loot-exe",
        type=pathlib.Path,
        default=pathlib.Path(config.loot_exe or "loot.exe"),
        help="Path to the LOOT CLI executable",
    )
    parser.add_argument(
        "--webhook-host",
        default="127.0.0.1",  # nosec
        help="Host for the Telegram webhook server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=8080,
        help="Port for the Telegram webhook server (default: 8080)",
    )
    parser.add_argument(
        "--operator-chat-id",
        type=int,
        default=_default_chat_id,
        help="Telegram chat ID for HITL operator notifications",
    )
    parser.add_argument(
        "--staging-dir",
        type=pathlib.Path,
        default=pathlib.Path(str(SystemPaths.get_base_drive() / "MO2Portable/downloads")),
        help="MO2 staging directory for mod downloads",
    )
    parser.add_argument(
        "--xedit-exe",
        type=pathlib.Path,
        default=pathlib.Path(config.xedit_exe) if config.xedit_exe else None,
        help="Path to the SSEEdit executable",
    )
    parser.add_argument(
        "--install-dir",
        type=pathlib.Path,
        default=pathlib.Path(config.install_dir or str(SystemPaths.modding_root())),
        help="Directory for auto-installing tools like LOOT/SSEEdit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    if getattr(sys, "frozen", False):
        parser.set_defaults(mode="gui")

    return parser.parse_args(argv)


async def _main(argv_or_args: list[str] | argparse.Namespace | None = None) -> None:
    """Asynchronous runner for CLI, Telegram, oneshot and security modes.

    Accepts either raw argv strings (for testing) or a pre-parsed Namespace.
    """
    args = argv_or_args if isinstance(argv_or_args, argparse.Namespace) else _parse_args(argv_or_args)
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=log_level)

    logger.info("Sky-Claw starting in %s mode", args.mode)
    if args.mode == "oneshot" and not args.command:
        logger.error("Oneshot mode requires a command argument.")
        sys.exit(1)

    ctx = AppContext(args)
    await ctx.start()
    try:
        if args.mode == "cli":
            await _run_cli(ctx)
        elif args.mode == "oneshot":
            await _run_oneshot(ctx, args.command)
        elif args.mode == "telegram":
            await _run_telegram(ctx, args.webhook_host, args.webhook_port)
        elif args.mode == "security":
            await _run_security(ctx, args.command)
    finally:
        await ctx.stop()


def _install_sigterm_handler() -> None:
    """Translate SIGTERM into ``KeyboardInterrupt`` so ``asyncio.run`` unwinds
    gracefully — running ``AppContext.stop()`` and the runners' ``CancelledError``
    cleanup — instead of the process being killed outright and orphaning heavy
    external tools (DynDOLOD/xEdit/BodySlide) that hold VFS/Data handles.

    Best-effort: no-ops where signals are unavailable (e.g. non-main thread).
    On Windows this covers ``os.kill(pid, SIGTERM)``; the console-close event
    (CTRL_CLOSE_EVENT) is not delivered as a Python signal and is out of scope.
    """

    def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError, OSError, AttributeError):
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)


def main(argv: list[str] | None = None) -> None:
    """Unified entry point controller."""
    args = _parse_args(argv)

    # P0: SIGTERM (Unix/WSL2) must trigger graceful shutdown in EVERY mode so
    # in-flight external processes are killed, not orphaned. In GUI mode NiceGUI/
    # uvicorn install their own handlers once running; this covers the startup
    # window plus the non-GUI asyncio.run loop.
    _install_sigterm_handler()

    if args.mode == "gui":
        log_level = logging.DEBUG if args.verbose else logging.INFO
        setup_logging(level=log_level)
        from sky_claw.antigravity.modes.gui_mode import run_gui_mode  # lazy: pulls NiceGUI

        run_gui_mode(args)
    else:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_main(args))


if __name__ == "__main__":
    main()
