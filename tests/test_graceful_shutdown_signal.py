"""P0: SIGTERM must unwind like Ctrl+C.

On Unix/WSL2 ``asyncio.run`` does not install a SIGTERM handler, so a plain
``kill <pid>`` terminates the process WITHOUT running ``AppContext.stop()`` or
the runners' ``CancelledError`` cleanup — leaking orphaned heavy tools
(DynDOLOD/xEdit/BodySlide) that hold handles on the MO2 VFS / Skyrim Data dir.

Translating SIGTERM into ``KeyboardInterrupt`` reuses asyncio.run's graceful
cancellation path.
"""

from __future__ import annotations

import signal
import sys
import types

import pytest

import sky_claw.__main__ as main_mod
from sky_claw.__main__ import _install_sigterm_handler


def test_sigterm_handler_translates_to_keyboardinterrupt():
    original = signal.getsignal(signal.SIGTERM)
    try:
        _install_sigterm_handler()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(int(signal.SIGTERM), None)
    finally:
        signal.signal(signal.SIGTERM, original)


def test_sigterm_handler_installed_for_gui_mode(monkeypatch):
    """GUI mode must also install the SIGTERM handler — not only the asyncio modes.

    A fake ``gui_mode`` module avoids importing NiceGUI; ``setup_logging`` is
    stubbed so the test does not reconfigure logging handlers.
    """
    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # reset so we detect the install
    launched: dict[str, bool] = {}

    fake_gui = types.ModuleType("sky_claw.antigravity.modes.gui_mode")
    fake_gui.run_gui_mode = lambda _args: launched.setdefault("gui", True)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sky_claw.antigravity.modes.gui_mode", fake_gui)
    monkeypatch.setattr(main_mod, "setup_logging", lambda **_kw: None)

    try:
        main_mod.main(["--mode", "gui"])
        assert launched.get("gui") is True
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(int(signal.SIGTERM), None)
    finally:
        signal.signal(signal.SIGTERM, original)
