"""Unit tests for the unified external-process helper (M-1).

`sky_claw/local/tools/_process.py` consolidates the subprocess dance that was
duplicated across every ``*_runner.py`` (exec -> communicate w/ timeout ->
kill+reap, plus CancelledError handling). The contract:

- ``kill_and_reap`` is None-safe, kills the process, and reaps it with a bounded
  wait that suppresses ONLY ``TimeoutError`` — a shutdown cancellation during the
  reap must propagate (matches ``windows_interop._kill_and_reap``).
- ``run_capture`` returns ``(stdout, stderr, returncode)`` on success; on timeout
  it kills+reaps and raises ``TimeoutError``; ``FileNotFoundError`` propagates;
  on cancellation it kills+reaps and re-raises ``CancelledError`` (no orphan).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools._process import kill_and_reap, run_capture


def _proc(*, communicate=None, returncode=0, wait_rc=-9) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = communicate or AsyncMock(return_value=(b"out", b"err"))
    proc.wait = AsyncMock(return_value=wait_rc)
    proc.kill = MagicMock()  # asyncio subprocess .kill() is synchronous
    proc.returncode = returncode
    return proc


# --- kill_and_reap ----------------------------------------------------------


async def test_kill_and_reap_none_is_noop():
    await kill_and_reap(None)  # must not raise


async def test_kill_and_reap_kills_and_reaps():
    proc = _proc()
    await kill_and_reap(proc)
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


async def test_kill_and_reap_suppresses_only_reap_timeout():
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc()
    proc.wait = AsyncMock(side_effect=_hang)
    # Bounded reap must not hang nor raise on timeout.
    await asyncio.wait_for(kill_and_reap(proc, timeout=0.05), timeout=2.0)
    proc.kill.assert_called_once()


async def test_kill_and_reap_propagates_cancellation():
    proc = _proc()
    proc.wait = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await kill_and_reap(proc)
    proc.kill.assert_called_once()  # killed before the cancel propagated


# --- run_capture ------------------------------------------------------------


async def test_run_capture_returns_streams_and_code_on_success():
    proc = _proc(communicate=AsyncMock(return_value=(b"hello", b"")), returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        stdout, stderr, rc = await run_capture(["tool.exe", "--flag"], timeout=5.0)
    assert (stdout, stderr, rc) == (b"hello", b"", 0)


async def test_run_capture_kills_and_raises_on_timeout():
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc(communicate=AsyncMock(side_effect=_hang))
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(TimeoutError):
        await asyncio.wait_for(run_capture(["tool.exe"], timeout=0.05), timeout=2.0)
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()  # killed AND reaped


async def test_run_capture_propagates_file_not_found():
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError), pytest.raises(FileNotFoundError):
        await run_capture(["missing.exe"], timeout=5.0)


async def test_run_capture_kills_on_unexpected_error():
    # A non-timeout/non-cancel failure after spawn (e.g. a pipe/I-O error) must
    # still kill+reap the child — otherwise runners that delegate cleanup here
    # would orphan the external tool.
    proc = _proc(communicate=AsyncMock(side_effect=OSError("pipe broke")))
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(OSError, match="pipe broke"):
        await run_capture(["tool.exe"], timeout=5.0)
    proc.kill.assert_called_once()


async def test_run_capture_kills_and_reraises_on_cancel():
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc(communicate=AsyncMock(side_effect=_hang))
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        task = asyncio.create_task(run_capture(["tool.exe"], timeout=30.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    proc.kill.assert_called_once()
