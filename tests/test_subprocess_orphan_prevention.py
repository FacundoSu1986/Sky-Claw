"""P0 regression: external-tool runners must never leak an orphaned OS process.

BodySlide / Pandora / Wrye Bash are long-running GUI tools that frequently hang.
When a run times out OR the orchestrator is shut down (task cancellation), the
runner must ``kill()`` + reap the child process. Otherwise the orphan keeps
holding handles on the MO2 VFS and the Skyrim ``Data`` directory, which breaks
every subsequent run in an unattended/autonomous session.

These tests intentionally fail against the pre-fix code (the ``except`` blocks
returned a failure result without killing the process).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner
from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraRunner
from sky_claw.local.tools.wrye_bash_runner import WryeBashConfig, WryeBashRunner


def _hanging_proc() -> AsyncMock:
    """A subprocess mock whose ``communicate()`` never returns (a hung GUI tool).

    ``kill()`` on a real :class:`asyncio.subprocess.Process` is synchronous, so
    it is a plain :class:`MagicMock`; ``wait()`` is awaited, so it is an
    :class:`AsyncMock`.
    """
    proc = AsyncMock()

    async def _never(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return (b"", b"")

    proc.communicate = AsyncMock(side_effect=_never)
    proc.wait = AsyncMock(return_value=-9)
    proc.kill = MagicMock()
    proc.returncode = None
    return proc


def _bodyslide(tmp_path, timeout: float) -> tuple[BodySlideRunner, tuple]:
    runner = BodySlideRunner(
        BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=tmp_path, timeout_seconds=timeout)
    )
    return runner, ("Build", str(tmp_path / "out"))


def _pandora(tmp_path, timeout: float) -> tuple[PandoraRunner, tuple]:
    runner = PandoraRunner(
        PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=tmp_path, timeout_seconds=timeout)
    )
    return runner, ()


def _wrye(tmp_path, timeout: float) -> tuple[WryeBashRunner, tuple]:
    runner = WryeBashRunner(
        WryeBashConfig(
            wrye_bash_path=tmp_path / "bash.exe",
            game_path=tmp_path,
            mo2_path=tmp_path,
            timeout_seconds=timeout,
        )
    )
    return runner, ()


_CALLS = {
    BodySlideRunner: lambda r, a: r.run_batch(*a),
    PandoraRunner: lambda r, a: r.run_pandora(*a),
    WryeBashRunner: lambda r, a: r.generate_bashed_patch(*a),
}


@pytest.mark.parametrize("factory", [_bodyslide, _pandora, _wrye])
async def test_runner_kills_process_on_timeout(tmp_path, factory):
    """On timeout the runner must kill + reap the child process."""
    proc = _hanging_proc()
    runner, args = factory(tmp_path, 0.05)
    call = _CALLS[type(runner)]

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await call(runner, args)

    assert result.success is False
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


@pytest.mark.parametrize("factory", [_bodyslide, _pandora, _wrye])
async def test_runner_kills_process_on_cancel(tmp_path, factory):
    """On task cancellation (graceful shutdown) the runner must kill the child
    and re-raise ``CancelledError`` (never swallow cancellation)."""
    proc = _hanging_proc()
    runner, args = factory(tmp_path, 30.0)
    call = _CALLS[type(runner)]

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        task = asyncio.create_task(call(runner, args))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    proc.kill.assert_called_once()
