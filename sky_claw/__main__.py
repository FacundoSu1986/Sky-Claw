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
import os
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
from sky_claw.logging_config import install_loop_exception_handler, setup_logging  # noqa: E402

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
        choices=[
            "cli",
            "telegram",
            "oneshot",
            "gui",
            "security",
            "install-vfs-bridge",
            "vfs-health",
        ],
        default="cli",
        help="Operation mode (default: cli)",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "deepseek", "openai", "ollama"],
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
        "--skyrim-path",
        type=pathlib.Path,
        default=pathlib.Path(config.skyrim_path) if config.skyrim_path else None,
        help="Path to the Skyrim installation (required by vfs-health)",
    )
    parser.add_argument(
        "--vfs-profile",
        default="Default",
        help="MO2 profile used by the vfs-health probe (default: Default)",
    )
    parser.add_argument(
        "--vfs-timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for the vfs-health worker (default: 30)",
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


# Compat: el helper vive ahora en logging_config (compartido con el bootstrap de
# la GUI, que instala su propio handler en el loop de NiceGUI). Se mantiene el
# nombre para call-sites y tests existentes.
_install_loop_exception_handler = install_loop_exception_handler


async def _install_vfs_bridge(args: argparse.Namespace) -> pathlib.Path:
    """Instala/actualiza el plugin MO2 sin iniciar el daemon completo."""
    from sky_claw.local.mo2.bridge_installer import MO2BridgeInstaller
    from sky_claw.local.mo2.vfs_broker import VfsBrokerError, vfs_instance_id

    if sys.platform != "win32":
        raise VfsBrokerError(
            "La instalación del bridge MO2/USVFS debe ejecutarse desde Windows; "
            "un intérprete Linux/WSL no puede ser lanzado por MO2."
        )

    root = pathlib.Path(args.mo2_root).resolve()
    instance_id = vfs_instance_id(root)
    descriptor = Config.DEFAULT_CONFIG_DIR / "vfs_bridge" / instance_id / f"{instance_id}.json"
    if getattr(sys, "frozen", False):
        worker_executable = pathlib.Path(sys.executable)
        worker_prefix = ("--vfs-worker",)
    else:
        worker_executable = pathlib.Path(sys.executable)
        worker_prefix = ("-m", "sky_claw.local.mo2.vfs_worker")
    installer = MO2BridgeInstaller()
    installed = await asyncio.to_thread(
        installer.install,
        mo2_root=root,
        worker_executable=worker_executable,
        worker_prefix=worker_prefix,
        descriptor_path=descriptor,
        instance_id=instance_id,
    )
    logger.info("Bridge MO2/USVFS instalado en %s", installed)
    return installed


async def _run_vfs_health(args: argparse.Namespace) -> None:
    """Ejecuta worker+nieto bajo USVFS sin arrancar el resto del daemon."""
    from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
    from sky_claw.local.mo2.vfs_broker import VfsBrokerError, VfsExecutionBroker, vfs_instance_id
    from sky_claw.local.mo2.vfs_contracts import VfsJob

    root = pathlib.Path(args.mo2_root).resolve()
    game = None if args.skyrim_path is None else pathlib.Path(args.skyrim_path).resolve()
    if not (root / "ModOrganizer.exe").is_file():
        raise VfsBrokerError(f"ModOrganizer.exe no existe bajo {root}")
    if game is None or not (game / "Data").is_dir():
        raise VfsBrokerError("--skyrim-path debe apuntar a una instalacion con Data")
    instance_id = vfs_instance_id(root)
    broker = VfsExecutionBroker(
        instance_id=instance_id,
        state_dir=Config.DEFAULT_CONFIG_DIR / "vfs_bridge" / instance_id,
    )
    await broker.start()
    try:
        challenge = await asyncio.to_thread(
            build_attestation_challenge,
            mo2_root=root,
            profile=args.vfs_profile,
            physical_data_dir=game / "Data",
        )
        job = VfsJob.create(
            instance_id=instance_id,
            profile=args.vfs_profile,
            tool_id="health",
            payload={},
            timeout_seconds=args.vfs_timeout,
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(),
        )
        result = await broker.submit(
            job,
            challenge=challenge,
            mo2_root=root,
            virtual_data_dir=game / "Data",
        )
        if not result.success:
            raise VfsBrokerError(result.message or "el probe VFS fallo")
        logger.info(
            "Probe VFS correcto para perfil %s; worker y nieto validaron el canary",
            args.vfs_profile,
        )
    finally:
        await broker.close()


async def _main(argv_or_args: list[str] | argparse.Namespace | None = None) -> None:
    """Asynchronous runner for CLI, Telegram, oneshot and security modes.

    Accepts either raw argv strings (for testing) or a pre-parsed Namespace.
    """
    args = argv_or_args if isinstance(argv_or_args, argparse.Namespace) else _parse_args(argv_or_args)
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=log_level)
    _install_loop_exception_handler()

    logger.info("Sky-Claw starting in %s mode", args.mode)
    if args.mode == "install-vfs-bridge":
        await _install_vfs_bridge(args)
        return
    if args.mode == "vfs-health":
        await _run_vfs_health(args)
        return
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


def _ensure_std_streams() -> None:
    """A PyInstaller ``--windowed`` build starts with ``sys.stdout`` and
    ``sys.stderr`` set to ``None`` (there is no console). Code that writes to
    them with a bare ``print`` — notably NiceGUI/uvicorn's startup banner —
    then raises ``AttributeError: 'NoneType' object has no attribute 'write'``
    and tears the GUI process down before the server can bind its port.

    Point the missing streams at a startup log next to the executable so the
    app survives (structured logs still go to ``logs/`` via ``setup_logging``).
    Best-effort: falls back to ``os.devnull`` if the log file can't be opened.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_path = pathlib.Path(sys.executable).parent / "sky_claw_startup.log"
        stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main(argv: list[str] | None = None) -> None:
    """Unified entry point controller."""
    _ensure_std_streams()
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv and effective_argv[0] in ("--vfs-worker", "--vfs-probe-child"):
        from sky_claw.local.mo2.vfs_worker import worker_main

        worker_argv = effective_argv[1:]
        if effective_argv[0] == "--vfs-probe-child":
            worker_argv = ["--probe-child", *worker_argv]
        raise SystemExit(worker_main(worker_argv))
    args = _parse_args(effective_argv)

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
