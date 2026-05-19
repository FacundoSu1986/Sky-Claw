"""Web mode entry point — headless NiceGUI server with configurable port."""

from __future__ import annotations

from sky_claw.antigravity.gui._bootloader import run_nicegui

__all__ = ["run_web_mode"]


def run_web_mode(args) -> None:
    run_nicegui(args, port=getattr(args, "port", 8081), title="Sky-Claw Web", show=False)
