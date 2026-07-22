"""LOOT deja de crear subprocesses standalone y delega al broker USVFS."""

from __future__ import annotations

import asyncio
import pathlib

import pytest

import sky_claw.local.mo2.brokered_loot as brokered_loot_module
from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner
from sky_claw.local.mo2.vfs_attestation import VfsAttestationChallenge
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJobResult


class _Broker:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def submit(self, job, **kwargs):
        self.calls.append((job, kwargs))
        return VfsJobResult.from_dict(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "job_id": job.job_id,
                "success": True,
                "message": "",
                "exit_code": 0,
                "stdout": "1. Skyrim.esm\n2. Update.esm\n",
                "stderr": "",
                "outputs": [str(path) for path in job.mutation_targets],
                "rollback_state": "not_required",
                "attestation": {
                    "profile": "Default",
                    "profile_fingerprint": job.expected_fingerprint,
                },
                "tool_result": {
                    "sorted_plugins": ["Skyrim.esm", "Update.esm"],
                    "warnings": [],
                    "errors": [],
                    "missing_patches": [],
                },
            }
        )


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
    target = profile / "plugins.txt"
    target.write_text("*Update.esm\n*Skyrim.esm\n", encoding="utf-8")
    return mo2, data, loot, target


async def test_runner_reutiliza_preview_y_envia_tool_id_allowlisted(tmp_path: pathlib.Path) -> None:
    mo2, data, loot, target = _entorno(tmp_path)
    broker = _Broker()
    runner = BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=loot,
        timeout=120,
        mutation_targets=lambda: (target,),
    )
    challenge = await runner.prepare_attestation()

    result = await runner.sort(update_masterlist=False)

    job, kwargs = broker.calls[0]
    assert job.tool_id == "loot_sort"
    assert job.expected_fingerprint == challenge.profile_fingerprint
    assert job.payload["loot_exe"] == str(loot.resolve())
    assert job.mutation_targets == (target.resolve(),)
    assert kwargs["challenge"] == challenge
    assert result.success is True
    assert result.sorted_plugins == ["Skyrim.esm", "Update.esm"]
    assert runner.last_vfs_result is not None


async def test_runner_sin_preview_construye_attestation_just_in_time(tmp_path: pathlib.Path) -> None:
    mo2, data, loot, target = _entorno(tmp_path)
    broker = _Broker()
    runner = BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=loot,
        timeout=120,
        mutation_targets=lambda: (target,),
    )

    result = await runner.sort(update_masterlist=True)

    job, _kwargs = broker.calls[0]
    assert job.payload["update_masterlist"] is True
    assert result.return_code == 0


async def test_for_profile_crea_runner_aislado_con_targets_del_perfil(tmp_path: pathlib.Path) -> None:
    mo2, data, loot, _target = _entorno(tmp_path)
    alternate = mo2 / "profiles" / "Alternate"
    alternate.mkdir()
    (alternate / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    alternate_target = alternate / "plugins.txt"
    alternate_target.write_text("*Skyrim.esm\n", encoding="utf-8")
    broker = _Broker()
    base = BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=loot,
        timeout=120,
        mutation_targets=lambda: (),
    )

    runner = base.for_profile("Alternate")
    challenge = await runner.prepare_attestation()
    await runner.sort(update_masterlist=False)

    job, _kwargs = broker.calls[0]
    assert runner is not base
    assert challenge.profile == "Alternate"
    assert job.profile == "Alternate"
    assert job.mutation_targets == (alternate_target.resolve(),)


async def test_previews_concurrentes_quedan_ligados_a_su_propia_ejecucion(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mo2, data, loot, target = _entorno(tmp_path)
    broker = _Broker()
    runner = BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=loot,
        timeout=120,
        mutation_targets=lambda: (target,),
    )
    challenges = iter(
        VfsAttestationChallenge(
            profile="Default",
            source_mod="CanaryMod",
            relative_path=pathlib.PurePosixPath("canary.txt"),
            sha256="f" * 64,
            profile_fingerprint=character * 64,
        )
        for character in ("1", "2", "3", "4")
    )
    monkeypatch.setattr(brokered_loot_module, "build_attestation_challenge", lambda **_kwargs: next(challenges))
    both_prepared = asyncio.Event()
    prepared_count = 0

    async def _flujo(update_masterlist: bool):
        nonlocal prepared_count
        challenge = await runner.prepare_attestation()
        prepared_count += 1
        if prepared_count == 2:
            both_prepared.set()
        await both_prepared.wait()
        await runner.sort(update_masterlist=update_masterlist)
        return challenge

    first, second = await asyncio.gather(_flujo(False), _flujo(True))
    fingerprints = {bool(job.payload["update_masterlist"]): job.expected_fingerprint for job, _ in broker.calls}

    assert fingerprints[False] == first.profile_fingerprint
    assert fingerprints[True] == second.profile_fingerprint
