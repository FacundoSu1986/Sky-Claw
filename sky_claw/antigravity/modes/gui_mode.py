"""GUI mode entry point — boots the NiceGUI Forge interface."""

from __future__ import annotations

from sky_claw.antigravity.gui._bootloader import (
    _gui_logic_loop,
    _gui_mod_update_loop,
    run_nicegui,
)

__all__ = ["_gui_logic_loop", "_gui_mod_update_loop", "run_gui_mode"]


def run_gui_mode(args) -> None:
    run_nicegui(args, port=8080, title="Sky-Claw", show=True)
