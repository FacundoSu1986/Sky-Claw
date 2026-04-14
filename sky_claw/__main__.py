"""Sky-Claw CLI entry point.

Usage::

    python -m sky_claw --mode cli          # interactive REPL
    python -m sky_claw --mode telegram     # Telegram webhook server
    python -m sky_claw --mode oneshot "install Requiem"
    python -m sky_claw --mode web --port 8888  # local web UI
    python -m sky_claw --mode gui         # local desktop UI (NiceGUI)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import pathlib
import sys

from sky_claw.app_context import AppContext
from sky_claw.config import SystemPaths
from sky_claw.logging_config import setup_logging
from sky_claw.modes.cli_mode import _run_cli, _run_oneshot
from sky_claw.modes.gui_mode import run_gui_mode
from sky_claw.modes.security_mode import _run_security
from sky_claw.modes.telegram_mode import _run_telegram
from sky_claw.modes.web_mode import _run_web

logger = logging.getLogger("sky_claw")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sky_claw",
        description="Sky-Claw — Autonomous Skyrim mod management agent",
    )
    parser.add_argument(
        "--mode",
        choices=["cli", "telegram", "oneshot", "web", "gui", "security"],
        default="cli",
        help="Operation mode (default: cli)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SKY_CLAW_WEB_PORT", "8888")),
        help="Port for the web UI server (default: 8888)",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "deepseek", "ollama"],
        default=os.environ.get("LLM_PROVIDER"),
        help="LLM provider (auto-detected if not specified)",
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
        default=pathlib.Path(
            os.environ.get(
                "SKY_CLAW_MO2_ROOT", str(SystemPaths.get_base_drive() / "MO2Portable")
            )
        ),
        help="Path to the MO2 portable instance",
    )
    parser.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("SKY_CLAW_DB_PATH", "mod_registry.db")),
        help="Path to the mod registry database",
    )
    parser.add_argument(
        "--loot-exe",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("SKY_CLAW_LOOT_EXE", "loot.exe")),
        help="Path to the LOOT CLI executable",
    )
    parser.add_argument(
        "--webhook-host",
        default=os.environ.get("SKY_CLAW_WEBHOOK_HOST", "127.0.0.1"),  # nosec
        help="Host for the Telegram webhook server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--webhook-port",
        type=int,
        default=int(os.environ.get("SKY_CLAW_WEBHOOK_PORT", "8080")),
        help="Port for the Telegram webhook server (default: 8080)",
    )
    parser.add_argument(
        "--operator-chat-id",
        type=int,
        default=int(_env)
        if (_env := os.environ.get("SKY_CLAW_OPERATOR_CHAT_ID", ""))
        else None,
        help="Telegram chat ID for HITL operator notifications (env: SKY_CLAW_OPERATOR_CHAT_ID)",
    )
    parser.add_argument(
        "--staging-dir",
        type=pathlib.Path,
        default=pathlib.Path(
            os.environ.get(
                "SKY_CLAW_STAGING_DIR",
                str(SystemPaths.get_base_drive() / "MO2Portable/downloads"),
            )
        ),
        help="MO2 staging directory for mod downloads (env: SKY_CLAW_STAGING_DIR)",
    )
    parser.add_argument(
        "--xedit-exe",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("SKY_CLAW_XEDIT_EXE", ""))
        if os.environ.get("SKY_CLAW_XEDIT_EXE")
        else None,
        help="Path to the SSEEdit executable (env: SKY_CLAW_XEDIT_EXE)",
    )
    parser.add_argument(
        "--install-dir",
        type=pathlib.Path,
        default=pathlib.Path(
            os.environ.get("SKY_CLAW_INSTALL_DIR", str(SystemPaths.modding_root()))
        ),
        help="Directory for auto-installing tools like LOOT/SSEEdit (env: SKY_CLAW_INSTALL_DIR)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    if getattr(sys, "frozen", False):
        parser.set_defaults(mode="web")

    return parser.parse_args(argv)


async def _main(argv_or_args: list[str] | argparse.Namespace | None = None) -> None:
    """Asynchronous runner for CLI, Web, and Telegram modes.

    Accepts either raw argv strings (for testing) or a pre-parsed Namespace.
    """
    args = (
        argv_or_args
        if isinstance(argv_or_args, argparse.Namespace)
        else _parse_args(argv_or_args)
    )
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=log_level)

    logger.info("Sky-Claw starting in %s mode", args.mode)
    if args.mode == "oneshot" and not args.command:
        logger.error("Oneshot mode requires a command argument.")
        sys.exit(1)

    ctx = AppContext(args)
    if args.mode == "web":
        await ctx.start_minimal()
        try:
            await _run_web(ctx, args.port)
        finally:
            await ctx.stop()
    else:
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


def main(argv: list[str] | None = None) -> None:
    """Unified entry point controller."""
    args = _parse_args(argv)

    if args.mode == "gui":
        log_level = logging.DEBUG if args.verbose else logging.INFO
        setup_logging(level=log_level)
        run_gui_mode(args)
    else:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_main(args))


if __name__ == "__main__":
    main()
