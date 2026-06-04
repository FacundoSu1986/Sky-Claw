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

import pytest

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
