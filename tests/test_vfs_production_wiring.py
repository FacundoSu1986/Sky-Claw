"""Construccion fail-closed del runner VFS que usa AppContext."""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.loot.cli import LOOTNotFoundError
from sky_claw.local.mo2.brokered_loot import (
    BrokeredLootRunner,
    VfsRequiredLootRunner,
    build_vfs_loot_runner,
)


def _paths(tmp_path: pathlib.Path):
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    data = tmp_path / "Skyrim" / "Data"
    loot = tmp_path / "LOOT" / "loot.exe"
    profile.mkdir(parents=True)
    data.mkdir(parents=True)
    loot.parent.mkdir()
    loot.write_bytes(b"loot")
    (profile / "plugins.txt").write_text("*Skyrim.esm\n", encoding="utf-8")
    return mo2, data.parent, loot


def test_construye_runner_brokered_con_prerrequisitos(tmp_path: pathlib.Path) -> None:
    mo2, game, loot = _paths(tmp_path)
    broker = object()

    runner = build_vfs_loot_runner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        game_path=game,
        loot_exe=loot,
        profile="Default",
    )

    assert isinstance(runner, BrokeredLootRunner)


async def test_sin_broker_devuelve_guard_que_falla_cerrado(tmp_path: pathlib.Path) -> None:
    mo2, game, loot = _paths(tmp_path)

    runner = build_vfs_loot_runner(
        broker=None,
        instance_id=None,
        mo2_root=mo2,
        game_path=game,
        loot_exe=loot,
        profile="Default",
    )

    assert isinstance(runner, VfsRequiredLootRunner)
    with pytest.raises(LOOTNotFoundError, match="F8 guard"):
        await runner.sort()
