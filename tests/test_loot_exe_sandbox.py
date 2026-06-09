"""Tests for LOOT exe sandboxing in the lazy agent-tool path (PR #171 follow-up).

Same Codex P1 vector closed for pandora/bodyslide in #171: ``loot_exe`` is
config-controlled (local_cfg / CLI args), so the lazily built LOOTRunner must
receive the PathValidator — ``LOOTRunner.sort()`` validates the executable
before any subprocess launch, exactly like the dry-run preview path already
does. Also covers the call-time ``_resolve_loot_exe`` resolution that replaces
the dead ``loot_exe_ref`` plumbing.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.agent.tools import AsyncToolRegistry


def _make_registry(
    *,
    tmp_path: pathlib.Path,
    loot_exe: pathlib.Path | None = None,
    loot_runner: object | None = None,
    local_cfg: object | None = None,
    path_validator: object | None = None,
) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
        loot_exe=loot_exe,
        loot_runner=loot_runner,
        local_cfg=local_cfg,
        path_validator=path_validator,
    )


def _sort_result() -> MagicMock:
    return MagicMock(success=True, return_code=0, sorted_plugins=[], warnings=[], errors=[])


# ---------------------------------------------------------------------------
# path_validator threaded into the lazily built LOOTRunner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_runner_receives_path_validator(tmp_path: pathlib.Path) -> None:
    """The lazy init must build LOOTRunner with the registry's validator."""
    exe = tmp_path / "loot.exe"
    exe.touch()
    validator = MagicMock()
    reg = _make_registry(tmp_path=tmp_path, loot_exe=exe, path_validator=validator)

    with patch("sky_claw.local.loot.cli.LOOTRunner") as runner_cls:
        runner_cls.return_value.sort = AsyncMock(return_value=_sort_result())
        await reg.execute("run_loot_sort", {"profile": "Default"})

    runner_cls.assert_called_once()
    assert runner_cls.call_args.kwargs["path_validator"] is validator


@pytest.mark.asyncio
async def test_sandbox_rejection_surfaces_as_error_json(tmp_path: pathlib.Path) -> None:
    """A validator rejection inside LOOTRunner.sort() keeps the JSON contract."""
    exe = tmp_path / "loot.exe"
    exe.touch()
    validator = MagicMock()
    validator.validate = MagicMock(side_effect=ValueError("outside sandbox"))
    reg = _make_registry(tmp_path=tmp_path, loot_exe=exe, path_validator=validator)

    # Real LOOTRunner: sort() calls validator.validate(loot_exe) before any
    # subprocess launch and the rejection propagates as the error payload.
    result = json.loads(await reg.execute("run_loot_sort", {"profile": "Default"}))

    assert "outside sandbox" in result["error"]
    validator.validate.assert_called_once_with(exe)


@pytest.mark.asyncio
async def test_injected_runner_bypasses_lazy_init(tmp_path: pathlib.Path) -> None:
    """A constructor-injected runner (code-controlled DI) is used as-is."""
    injected = MagicMock()
    injected.sort = AsyncMock(return_value=_sort_result())
    reg = _make_registry(tmp_path=tmp_path, loot_runner=injected, path_validator=MagicMock())

    with patch("sky_claw.local.loot.cli.LOOTRunner") as runner_cls:
        result = json.loads(await reg.execute("run_loot_sort", {"profile": "Default"}))

    runner_cls.assert_not_called()
    injected.sort.assert_awaited_once()
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Call-time exe resolution (replaces the dead loot_exe_ref plumbing)
# ---------------------------------------------------------------------------


def test_resolve_loot_exe_prefers_local_cfg(tmp_path: pathlib.Path) -> None:
    """Same-session installs persist into local_cfg; call-time read picks them up."""
    constructor_exe = tmp_path / "old-loot.exe"
    installed_exe = tmp_path / "installed" / "loot.exe"
    installed_exe.parent.mkdir()
    installed_exe.touch()
    cfg = SimpleNamespace(loot_exe=None)
    reg = _make_registry(tmp_path=tmp_path, loot_exe=constructor_exe, local_cfg=cfg)

    assert reg._resolve_loot_exe() == constructor_exe  # nothing installed yet

    cfg.loot_exe = str(installed_exe)  # what setup_tools persists post-install
    assert reg._resolve_loot_exe() == installed_exe


def test_resolve_loot_exe_ignores_missing_cfg_path(tmp_path: pathlib.Path) -> None:
    """A configured-but-missing path falls back to the constructor value."""
    constructor_exe = tmp_path / "loot.exe"
    cfg = SimpleNamespace(loot_exe=str(tmp_path / "gone.exe"))
    reg = _make_registry(tmp_path=tmp_path, loot_exe=constructor_exe, local_cfg=cfg)

    assert reg._resolve_loot_exe() == constructor_exe
