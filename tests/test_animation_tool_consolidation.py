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
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner
from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraRunner


@pytest.fixture
async def proteccion_pandora(
    tmp_path: pathlib.Path,
) -> AsyncIterator[tuple[DistributedLockManager, FileSnapshotManager]]:
    lock_manager = DistributedLockManager(tmp_path / "pandora_locks.db")
    await lock_manager.initialize()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    try:
        yield lock_manager, FileSnapshotManager(snapshot_dir=snapshots)
    finally:
        await lock_manager.close()


def _make_registry(
    *,
    tmp_path: pathlib.Path,
    local_cfg: object | None = None,
    pandora_runner: object | None = None,
    bodyslide_runner: object | None = None,
    path_validator: object | None = None,
    lock_manager: object | None = None,
    snapshot_manager: object | None = None,
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
        path_validator=path_validator,
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
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


# ---------------------------------------------------------------------------
# Sandbox validation of config-supplied exe paths (PR #171 review: Codex P1)
# ---------------------------------------------------------------------------


def test_resolver_validates_exe_against_sandbox(tmp_path: pathlib.Path) -> None:
    """The config-derived exe must pass PathValidator before a runner is built."""
    exe = tmp_path / "Pandora.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=None)
    validator = MagicMock()
    validator.validate = MagicMock(return_value=exe)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg, path_validator=validator)

    runner = reg._resolve_pandora_runner()

    assert isinstance(runner, PandoraRunner)
    validator.validate.assert_called_once_with(exe)


def test_resolver_rejects_exe_outside_sandbox(tmp_path: pathlib.Path) -> None:
    """A validator rejection (e.g. symlink escape) must block the launch."""
    exe = tmp_path / "evil.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=str(exe))
    validator = MagicMock()
    validator.validate = MagicMock(side_effect=ValueError("outside sandbox"))
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg, path_validator=validator)

    assert reg._resolve_pandora_runner() is None
    assert reg._resolve_bodyslide_runner() is None


@pytest.mark.asyncio
async def test_rejected_exe_surfaces_as_not_configured(tmp_path: pathlib.Path) -> None:
    """End-to-end: a sandbox-rejected exe yields the structured error JSON."""
    exe = tmp_path / "evil.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=None)
    validator = MagicMock()
    validator.validate = MagicMock(side_effect=ValueError("outside sandbox"))
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg, path_validator=validator)

    result = json.loads(await reg.tools["run_pandora"].fn())

    assert "not configured" in result["error"]


def test_injected_runner_skips_config_validation(tmp_path: pathlib.Path) -> None:
    """Constructor-injected runners are code-controlled DI — trusted as-is."""
    injected = MagicMock(spec=PandoraRunner)
    validator = MagicMock()
    reg = _make_registry(tmp_path=tmp_path, pandora_runner=injected, path_validator=validator)

    assert reg._resolve_pandora_runner() is injected
    validator.validate.assert_not_called()


# ---------------------------------------------------------------------------
# game_path resolution (PR #171 review: Codex P2)
# ---------------------------------------------------------------------------


def test_game_path_prefers_skyrim_path(tmp_path: pathlib.Path) -> None:
    """Portable-MO2 setups: cwd must be the game dir, not mo2.root."""
    skyrim = tmp_path / "skyrim"
    skyrim.mkdir()
    exe = tmp_path / "BodySlide.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=None, bodyslide_exe=str(exe), skyrim_path=str(skyrim))
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)

    runner = reg._resolve_bodyslide_runner()

    assert isinstance(runner, BodySlideRunner)
    assert runner.config.game_path == skyrim


def test_game_path_falls_back_to_mo2_root(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "BodySlide.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=None, bodyslide_exe=str(exe), skyrim_path=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)

    runner = reg._resolve_bodyslide_runner()

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
async def test_run_pandora_tool_uses_injected_runner(
    tmp_path: pathlib.Path,
    proteccion_pandora: tuple[DistributedLockManager, FileSnapshotManager],
) -> None:
    injected = MagicMock(spec=PandoraRunner)
    game = tmp_path / "game"
    game.mkdir(parents=True)
    injected.config = PandoraConfig(
        pandora_exe=tmp_path / "Pandora" / "Pandora.exe",
        game_path=game,
    )

    async def _run_exitoso() -> MagicMock:
        output = game.resolve() / "Pandora_Output"
        output.mkdir()
        (output / "generado.hkx").write_bytes(b"pandora")
        return _runner_result()

    injected.run_pandora = AsyncMock(side_effect=_run_exitoso)
    lock_manager, snapshot_manager = proteccion_pandora
    reg = _make_registry(
        tmp_path=tmp_path,
        pandora_runner=injected,
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
    )

    result = json.loads(await reg.tools["run_pandora"].fn())

    assert result["success"] is True
    injected.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_bodyslide_tool_forwards_group_and_output(tmp_path: pathlib.Path) -> None:
    """U-04: el destino físico ya no es el ``output_path`` que manda el LLM —
    es el subárbol administrado por grupo (``bodyslide_output_target``). El
    parámetro ``output_path`` se conserva en el contrato de la tool (no rompe
    a callers existentes) pero deja de determinar dónde escribe BodySlide."""
    from sky_claw.local.tools.output_targets import bodyslide_output_target

    game = tmp_path / "game"
    injected = MagicMock(spec=BodySlideRunner)
    injected.config = BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=game)
    injected.run_batch = AsyncMock(return_value=_runner_result())
    reg = _make_registry(
        tmp_path=tmp_path,
        bodyslide_runner=injected,
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
    )

    transaction = MagicMock()
    transaction.lease_lost = False
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "sky_claw.app.db.locks.SnapshotTransactionLock",
        return_value=transaction,
    ):
        result = json.loads(await reg.tools["run_bodyslide"].fn(group="3BA", output_path="out"))

    esperado = bodyslide_output_target(game=game, group="3BA")
    assert result["success"] is True
    injected.run_batch.assert_awaited_once_with("3BA", str(esperado))


@pytest.mark.asyncio
async def test_unconfigured_tools_return_structured_error(tmp_path: pathlib.Path) -> None:
    reg = _make_registry(tmp_path=tmp_path, local_cfg=None)

    pandora = json.loads(await reg.tools["run_pandora"].fn())
    bodyslide = json.loads(await reg.tools["run_bodyslide"].fn())

    assert "not configured" in pandora["error"]
    assert "not configured" in bodyslide["error"]


# ---------------------------------------------------------------------------
# output_path sandboxing in the schema (PR #171 review: Codex P1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside",
        "..\\outside",
        "out/../../outside",
        "C:\\evil",
        "C:evil",  # drive-relative
        "\\\\srv\\share",  # UNC
        "/abs/posix",
        "\\abs\\rootless",
    ],
)
def test_bodyslide_output_path_rejects_escapes(bad_path: str) -> None:
    """output_path feeds BodySlide.exe -o; absolute/traversal forms must fail
    central validation (TASK-011: execute() validates via params_model)."""
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    with pytest.raises(ValueError):
        BodySlideBatchParams(output_path=bad_path)


@pytest.mark.parametrize("good_path", ["meshes", "out/meshes", "calientetools\\output"])
def test_bodyslide_output_path_accepts_relative(good_path: str) -> None:
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    params = BodySlideBatchParams(output_path=good_path)
    assert params.output_path == good_path


# ---------------------------------------------------------------------------
# group sandboxing en el schema (U-04): ahora se usa como componente de ruta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_group", [".", ".."])
def test_bodyslide_group_rejects_bare_dot_segments(bad_group: str) -> None:
    """``group`` pasa a ser el componente único de ruta bajo
    ``BodySlide_Output/<group>`` (U-04, ``output_targets.bodyslide_output_target``).
    ``_SAFE_NAME_PATTERN`` permite '.', así que un valor de solo puntos pasaría el
    patrón de caracteres intacto y, resuelto contra el filesystem, ``..`` subiría
    un nivel — el mismo componente especial que ``output_path`` ya rechaza
    explícitamente (PR #171), acá para un campo que antes no tocaba una ruta."""
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    with pytest.raises(ValueError):
        BodySlideBatchParams(group=bad_group)


@pytest.mark.parametrize("bad_group", ["a/b", "a\\b", "../evil", "CBBE/../../evil"])
def test_bodyslide_group_rejects_path_separators(bad_group: str) -> None:
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    with pytest.raises(ValueError):
        BodySlideBatchParams(group=bad_group)


@pytest.mark.parametrize("good_group", ["CBBE", "3BA", "CBBE Body Physics", "Preset-1.0"])
def test_bodyslide_group_accepts_safe_names(good_group: str) -> None:
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    params = BodySlideBatchParams(group=good_group)
    assert params.group == good_group
