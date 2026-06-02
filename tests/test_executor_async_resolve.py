"""Audit #152 (A-1) — ManagedToolExecutor must not block the event loop on resolve().

``pathlib.Path.resolve()`` touches the filesystem (Windows reparse points,
network mounts) and can stall the event loop.  The audit asks for the
two ``resolve(strict=False)`` calls inside ``execute()`` (lines 78-79)
to dispatch through ``asyncio.to_thread`` so other coroutines keep
running while we wait for the FS.

This file fixes the path-jail behavior too, so the contracts here are:

1. Offload contract: the new ``_resolve_strict_false`` helper dispatches
   the actual ``.resolve()`` call through ``asyncio.to_thread`` (so a
   slow SMB/NFS resolve does not freeze the event loop).
2. Same result as the legacy sync call (regression sanity).
3. Path-jail still rejects ``..\\..\\Windows\\System32``-style traversals
   when invoked via the new async path — security regression guard.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import patch

import pytest

from sky_claw.antigravity.agent.executor import ManagedToolExecutor


class TestResolveStrictFalseAsync:
    @pytest.mark.asyncio
    async def test_offload_uses_asyncio_to_thread(self, tmp_path: pathlib.Path) -> None:
        """The helper must route ``.resolve(strict=False)`` through ``asyncio.to_thread``.

        We patch ``asyncio.to_thread`` with a spy that records the callable
        being dispatched and then delegates to the real implementation, so
        behavior is preserved while we get a deterministic assertion.
        """
        dispatched: list[object] = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
            dispatched.append(func)
            return await real_to_thread(func, *args, **kwargs)

        with patch("asyncio.to_thread", side_effect=spy_to_thread):
            result = await ManagedToolExecutor._resolve_strict_false(str(tmp_path))

        assert dispatched, "asyncio.to_thread was never called — helper still synchronous"
        assert isinstance(result, pathlib.Path)
        assert result == pathlib.Path(str(tmp_path)).resolve(strict=False), (
            "Async helper must return the same Path the sync resolve(strict=False) would"
        )

    @pytest.mark.asyncio
    async def test_resolves_non_existent_path_without_raising(self, tmp_path: pathlib.Path) -> None:
        """``strict=False`` must be honored: a non-existent path resolves cleanly."""
        target = tmp_path / "does-not-exist" / "subdir"
        result = await ManagedToolExecutor._resolve_strict_false(str(target))
        # On Windows / POSIX both, strict=False returns the lexically-resolved path
        # without raising even if the leaf does not exist.
        assert isinstance(result, pathlib.Path)


class TestPathJailRegressionViaAsyncResolve:
    """Security regression: the path-jail rejection still fires when the
    ``.resolve()`` call is offloaded.

    We invoke ``execute()`` with a Windows-absolute arg that resolves
    OUTSIDE the modding root.  The expected behavior is unchanged from
    before the fix: return -1 and never reach the subprocess.  Because
    rejection happens before ``create_subprocess_exec`` is invoked, we
    do not need to mock it.
    """

    @pytest.mark.asyncio
    async def test_traversal_outside_modding_root_is_rejected(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sky_claw.config import SystemPaths

        # Confine the jail to tmp_path / "jail" so anything else is "outside".
        jail = tmp_path / "jail"
        jail.mkdir()
        attacker_target = tmp_path / "attacker.txt"
        attacker_target.write_text("p0wn", encoding="utf-8")

        monkeypatch.setattr(SystemPaths, "modding_root", staticmethod(lambda: jail))

        from sky_claw.antigravity.security.path_validator import PathValidator

        validator = PathValidator([jail])
        executor = ManagedToolExecutor(timeout=1.0, path_validator=validator)

        rc = await executor.execute(
            binary_path="/bin/true",
            args=[str(attacker_target)],
        )

        assert rc == -1, (
            "Absolute path outside modding_root must be rejected by the jail "
            "regardless of whether resolve() is sync or async."
        )
