"""Tests for the BodySlide/Pandora tool consolidation (obs #187).

The legacy AnimationHub-backed pair (``run_pandora``/``run_bodyslide``) and the
runner-backed pair (``run_pandora_behavior``/``run_bodyslide_batch``) were
consolidated: the canonical tool names now delegate to the M-02/M-03 runners
(unified ``_process`` subprocess handling), resolved lazily from ``local_cfg``
at call time so a same-session ``setup_tools`` install is picked up without
mutable-ref plumbing.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.agent.tools import AsyncToolRegistry
from sky_claw.local.tools.bodyslide_runner import BodySlideRunner
from sky_claw.local.tools.pandora_runner import PandoraRunner


def _make_registry(
    *,
    tmp_path: pathlib.Path,
    local_cfg: object | None = None,
    pandora_runner: object | None = None,
    bodyslide_runner: object | None = None,
) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
        loot_exe=None,
        local_cfg=local_cfg,
        pandora_runner=pandora_runner,
        bodyslide_runner=bodyslide_runner,
    )


def _runner_result(**overrides: object) -> MagicMock:
    defaults: dict[str, object] = {
        "success": True,
        "return_code": 0,
        "stdout": "ok",
        "stderr": "",
        "duration_seconds": 1.0,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


# ---------------------------------------------------------------------------
# Tool surface: 4 -> 2
# ---------------------------------------------------------------------------


def test_duplicate_tool_names_removed(tmp_path: pathlib.Path) -> None:
    """Only the canonical names survive; the never-wired duplicates are gone."""
    reg = _make_registry(tmp_path=tmp_path)
    assert "run_pandora" in reg.tools
    assert "run_bodyslide" in reg.tools
    assert "run_pandora_behavior" not in reg.tools
    assert "run_bodyslide_batch" not in reg.tools


def test_run_bodyslide_advertises_group_params(tmp_path: pathlib.Path) -> None:
    """run_bodyslide exposes the BodySlideBatchParams schema (configurable preset
    group) instead of the legacy hardcoded-preset zero-arg contract."""
    reg = _make_registry(tmp_path=tmp_path)
    schema = reg.tools["run_bodyslide"].input_schema
    assert "group" in schema["properties"]
    assert "output_path" in schema["properties"]


# ---------------------------------------------------------------------------
# Lazy resolution from local_cfg (mirrors the _run_loot_sort pattern)
# ---------------------------------------------------------------------------


def test_resolver_prefers_injected_runner(tmp_path: pathlib.Path) -> None:
    injected = MagicMock(spec=PandoraRunner)
    reg = _make_registry(tmp_path=tmp_path, pandora_runner=injected)
    assert reg._resolve_pandora_runner() is injected


def test_resolver_returns_none_without_config(tmp_path: pathlib.Path) -> None:
    reg = _make_registry(tmp_path=tmp_path, local_cfg=None)
    assert reg._resolve_pandora_runner() is None
    assert reg._resolve_bodyslide_runner() is None


def test_resolver_returns_none_when_exe_missing(tmp_path: pathlib.Path) -> None:
    cfg = SimpleNamespace(pandora_exe=str(tmp_path / "nope.exe"), bodyslide_exe=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)
    assert reg._resolve_pandora_runner() is None


def test_resolver_builds_runner_from_local_cfg(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "Pandora.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)

    runner = reg._resolve_pandora_runner()

    assert isinstance(runner, PandoraRunner)
    assert runner.config.pandora_exe == exe
    assert runner.config.game_path == tmp_path  # mo2.root


def test_resolver_picks_up_same_session_install(tmp_path: pathlib.Path) -> None:
    """setup_tools persists the exe path into local_cfg; because resolution
    happens at call time, the very next run_bodyslide call must see it."""
    cfg = SimpleNamespace(pandora_exe=None, bodyslide_exe=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)
    assert reg._resolve_bodyslide_runner() is None

    exe = tmp_path / "BodySlide.exe"
    exe.touch()
    cfg.bodyslide_exe = str(exe)  # what setup_tools does post-install

    runner = reg._resolve_bodyslide_runner()
    assert isinstance(runner, BodySlideRunner)
    assert runner.config.bodyslide_exe == exe


# ---------------------------------------------------------------------------
# End-to-end through the tool descriptors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pandora_tool_uses_injected_runner(tmp_path: pathlib.Path) -> None:
    injected = MagicMock(spec=PandoraRunner)
    injected.run_pandora = AsyncMock(return_value=_runner_result())
    reg = _make_registry(tmp_path=tmp_path, pandora_runner=injected)

    result = json.loads(await reg.tools["run_pandora"].fn())

    assert result["success"] is True
    injected.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_bodyslide_tool_forwards_group_and_output(tmp_path: pathlib.Path) -> None:
    injected = MagicMock(spec=BodySlideRunner)
    injected.run_batch = AsyncMock(return_value=_runner_result())
    reg = _make_registry(tmp_path=tmp_path, bodyslide_runner=injected)

    result = json.loads(await reg.tools["run_bodyslide"].fn(group="3BA", output_path="out"))

    assert result["success"] is True
    injected.run_batch.assert_awaited_once_with("3BA", "out")


@pytest.mark.asyncio
async def test_unconfigured_tools_return_structured_error(tmp_path: pathlib.Path) -> None:
    reg = _make_registry(tmp_path=tmp_path, local_cfg=None)

    pandora = json.loads(await reg.tools["run_pandora"].fn())
    bodyslide = json.loads(await reg.tools["run_bodyslide"].fn())

    assert "not configured" in pandora["error"]
    assert "not configured" in bodyslide["error"]
