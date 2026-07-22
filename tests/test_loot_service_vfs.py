"""Wiring de LOOT al broker: preview pre-HITL y cero fallback standalone."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from sky_claw.antigravity.core.models import LootExecutionParams
from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner
from sky_claw.local.tools.loot_service import LootSortingService


def _entorno(tmp_path: pathlib.Path):
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    loot = tmp_path / "LOOT" / "loot.exe"
    loot.parent.mkdir()
    loot.write_bytes(b"loot")
    resolver = SimpleNamespace(
        get_skyrim_path=lambda: data.parent,
        get_mo2_path=lambda: mo2,
        get_loot_exe=lambda: loot,
        get_active_profile=lambda: "Default",
    )
    return mo2, data, loot, resolver


async def test_prepare_vfs_attestation_construye_runner_brokered(tmp_path: pathlib.Path) -> None:
    _mo2, _data, _loot, resolver = _entorno(tmp_path)
    service = LootSortingService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=resolver,
        vfs_broker=MagicMock(),
        vfs_instance_id="portable-main",
        require_vfs=True,
    )

    challenge = await service.prepare_vfs_attestation(LootExecutionParams(profile_name="Default"))

    assert challenge is not None
    assert challenge.profile == "Default"
    assert isinstance(service._ensure_loot_runner("Default"), BrokeredLootRunner)


async def test_require_vfs_sin_broker_falla_cerrado_antes_de_subprocess() -> None:
    service = LootSortingService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        require_vfs=True,
    )

    result = await service.sort_load_order(override_preflight=True)

    assert result["success"] is False
    assert "USVFS" in result["message"]


async def test_runner_inyectado_reutiliza_la_misma_instancia_perfilada() -> None:
    class _Runner:
        def __init__(self, profile: str = "Default") -> None:
            self.profile = profile
            self.created: list[_Runner] = []

        def for_profile(self, profile: str):
            child = _Runner(profile)
            self.created.append(child)
            return child

        async def prepare_attestation(self):
            return SimpleNamespace(profile=self.profile)

    root = _Runner()
    service = LootSortingService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        loot_runner=root,
        require_vfs=True,
    )

    challenge = await service.prepare_vfs_attestation(LootExecutionParams(profile_name="Alternate"))
    first = service._ensure_loot_runner("Alternate")
    second = service._ensure_loot_runner("Alternate")

    assert challenge.profile == "Alternate"
    assert first is second
    assert len(root.created) == 1


async def test_targets_de_rollback_salen_del_runner_vfs_perfilado(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "profiles" / "Alternate" / "plugins.txt"

    class _Runner:
        def mutation_targets(self) -> tuple[pathlib.Path, ...]:
            return (target,)

    service = LootSortingService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
    )

    paths = await service._resolve_load_order_for_runner(_Runner())

    assert paths.files == (target.resolve(),)
    assert paths.sources == ("vfs_profile",)
