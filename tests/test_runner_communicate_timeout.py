"""P1: xEdit / Synthesis runners must not hang after killing a timed-out process.

On timeout the runners did ``proc.kill()`` then ``await proc.communicate()``
WITHOUT a timeout. If a grandchild process inherited the pipe, that second
``communicate()`` blocks forever and hangs the whole orchestration. The reap
must be bounded (``wait_for(proc.wait(), ...)``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisRunner,
    SynthesisTimeoutError,
)
from sky_claw.local.xedit.runner import XEditRunner, XEditTimeoutError


def _proc_stuck_in_communicate() -> AsyncMock:
    """Process whose ``communicate()`` never returns (timeout + stuck pipe),
    but whose ``wait()`` returns promptly — i.e. the bounded reap path works,
    the unbounded second ``communicate()`` would hang forever."""
    proc = AsyncMock()

    async def _hang(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return (b"", b"")

    proc.communicate = AsyncMock(side_effect=_hang)
    proc.wait = AsyncMock(return_value=-9)
    proc.kill = MagicMock()
    proc.returncode = -9
    return proc


async def test_xedit_does_not_hang_on_timeout(tmp_path):
    proc = _proc_stuck_in_communicate()
    (tmp_path / "xEdit.exe").touch()
    runner = XEditRunner(
        xedit_path=tmp_path / "xEdit.exe",
        game_path=tmp_path,
        output_dir=tmp_path / "out",
        timeout=1,
    )
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(XEditTimeoutError):
        await asyncio.wait_for(runner._execute_process(["dummy"]), timeout=3.0)
    proc.kill.assert_called_once()


async def test_synthesis_does_not_hang_on_timeout(tmp_path):
    proc = _proc_stuck_in_communicate()
    (tmp_path / "Synthesis.exe").touch()
    config = SynthesisConfig(
        game_path=tmp_path,
        mo2_path=tmp_path,
        output_path=tmp_path,
        synthesis_exe=tmp_path / "Synthesis.exe",
        timeout_seconds=1,
    )
    runner = SynthesisRunner(config)
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(SynthesisTimeoutError):
        await asyncio.wait_for(runner._execute_process(["dummy"]), timeout=3.0)
    proc.kill.assert_called_once()
