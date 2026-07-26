"""GUI mode entry point — boots the NiceGUI Forge interface."""

from __future__ import annotations

from sky_claw.app.gui._bootloader import (
    _gui_logic_loop,
    _gui_mod_update_loop,
    run_nicegui,
)

__all__ = ["_gui_logic_loop", "_gui_mod_update_loop", "run_gui_mode"]


def run_gui_mode(args) -> None:
    # exit_on_last_client: el .exe se compila con console=False y ui.run abre el
    # navegador del sistema (no native=True), así que cerrar la ventana no le
    # manda NADA al proceso. Sin el watchdog, app.on_shutdown nunca corre y el
    # proceso queda vivo reteniendo 8080/8765 → WinError 10048 al reabrir.
    run_nicegui(args, port=8080, title="Sky-Claw", show=True, exit_on_last_client=True)
