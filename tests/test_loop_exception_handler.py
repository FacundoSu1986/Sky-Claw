"""M-5: unhandled event-loop exceptions (e.g. from fire-and-forget tasks) must be
routed to the structured logger, not asyncio's default stderr handler, so they are
captured by the JSON log + redaction pipeline for root-cause analysis.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from sky_claw.__main__ import _install_loop_exception_handler


async def test_loop_exception_handler_logs_via_logger(caplog):
    _install_loop_exception_handler()
    loop = asyncio.get_running_loop()
    with caplog.at_level(logging.ERROR, logger="sky_claw"):
        loop.call_exception_handler({"message": "boom", "exception": ValueError("kaboom")})
    assert "Unhandled event-loop exception" in caplog.text
    assert "kaboom" in caplog.text  # exception rendered via exc_info
    assert any(record.name == "sky_claw" for record in caplog.records)


async def test_install_handler_desde_logging_config(caplog):
    """Ítem 4 sub-3: el helper vive en logging_config (compartido por _main y la
    GUI). Debe rutear una excepción de loop al logger desde su nueva ubicación."""
    from sky_claw.logging_config import install_loop_exception_handler

    install_loop_exception_handler()
    loop = asyncio.get_running_loop()
    with caplog.at_level(logging.ERROR, logger="sky_claw"):
        loop.call_exception_handler({"message": "boom-gui", "exception": ValueError("kaboom-gui")})
    assert "Unhandled event-loop exception" in caplog.text
    assert "kaboom-gui" in caplog.text
    assert any(record.name == "sky_claw" for record in caplog.records)


def test_main_alias_apunta_al_helper_compartido():
    """El alias de compat en __main__ debe ser el mismo objeto del módulo compartido."""
    from sky_claw.__main__ import _install_loop_exception_handler as alias
    from sky_claw.logging_config import install_loop_exception_handler

    assert alias is install_loop_exception_handler


def test_gui_bootstrap_instala_el_handler():
    """Ítem 4 sub-3: el bootstrap de la GUI (que corre dentro del loop de NiceGUI)
    debe instalar el loop-exception-handler — la ruta GUI no pasa por _main."""
    from sky_claw.antigravity.gui import _bootloader

    src = inspect.getsource(_bootloader.run_nicegui)
    assert "install_loop_exception_handler" in src


def test_entry_points_propagan_correlation_id():
    """Ítem 4 sub-2 (ancla de regresión): cada entry point debe setear
    correlation_id_var para que los flujos por-request sean trazables end-to-end.
    Previene que un nuevo entry point olvide propagar el correlation_id."""
    from sky_claw.antigravity.comms import telegram
    from sky_claw.antigravity.gui import _bootloader
    from sky_claw.antigravity.modes import cli_mode
    from sky_claw.antigravity.web import app

    for modulo in (app, telegram, cli_mode, _bootloader):
        src = inspect.getsource(modulo)
        assert "correlation_id_var.set" in src, f"{modulo.__name__} no propaga correlation_id"
