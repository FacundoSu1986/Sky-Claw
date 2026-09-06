"""Pruebas del protocolo VFS v2 con separación de raices y rechazo de v1."""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    VfsAttestationError,
    build_attestation_challenge,
    verify_vfs_attestation,
)
from sky_claw.local.mo2.vfs_contracts import (
    VFS_PROTOCOL_VERSION,
    VfsJob,
    VfsJobResult,
)
from sky_claw.local.mo2.vfs_manifest import (
    VfsManifestError,
    VfsWorkerManifest,
)
from sky_claw.local.mo2.vfs_worker import (
    VfsToolExecution,
    execute_worker_manifest,
)


def test_vfs_protocol_version_es_2() -> None:
    """El protocolo VFS debe estar en version 2 para soportar raices separadas."""
    assert VFS_PROTOCOL_VERSION == 2


def test_vfs_manifest_split_roots_roundtrip(tmp_path: pathlib.Path) -> None:
    """VfsWorkerManifest v2 preserva data_root, mods_dir e install_root separados."""
    install_root = tmp_path / "MO2_Install"
    data_root = tmp_path / "MO2_Data"
    mods_dir = tmp_path / "Custom_Mods"
    virtual_data = tmp_path / "Virtual" / "Data"
    descriptor = tmp_path / "descriptor.json"

    for path in (install_root, data_root, mods_dir, virtual_data, descriptor.parent):
        path.mkdir(parents=True, exist_ok=True)

    challenge = VfsAttestationChallenge(
        profile="Default",
        source_mod="ModA",
        relative_path=pathlib.PurePosixPath("canary.txt"),
        sha256="a" * 64,
        profile_fingerprint="b" * 64,
    )
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10.0,
        expected_fingerprint="b" * 64,
        mutation_targets=(),
    )

    manifest = VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        data_root=data_root,
        mods_dir=mods_dir,
        install_root=install_root,
        virtual_data_dir=virtual_data,
        descriptor_path=descriptor,
    )

    payload = manifest.to_dict()
    assert payload["protocol_version"] == 2
    assert payload["data_root"] == str(data_root.resolve())
    assert payload["mods_dir"] == str(mods_dir.resolve())
    assert payload["install_root"] == str(install_root.resolve())
    assert payload["mo2_root"] == str(install_root.resolve())

    restored = VfsWorkerManifest.from_dict(payload)
    assert restored.protocol_version == 2
    assert restored.data_root == data_root.resolve()
    assert restored.mods_dir == mods_dir.resolve()
    assert restored.install_root == install_root.resolve()
    assert restored.mo2_root == install_root.resolve()


def test_vfs_manifest_rechaza_version_de_protocolo_1(tmp_path: pathlib.Path) -> None:
    """VfsWorkerManifest debe rechazar un manifiesto v1 con VfsManifestError."""
    challenge = VfsAttestationChallenge(
        profile="Default",
        source_mod="ModA",
        relative_path=pathlib.PurePosixPath("canary.txt"),
        sha256="a" * 64,
        profile_fingerprint="b" * 64,
    )
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10.0,
        expected_fingerprint="b" * 64,
        mutation_targets=(),
    )
    raw = {
        "protocol_version": 1,
        "job": job.to_dict(),
        "challenge": challenge.to_dict(),
        "mo2_root": str(tmp_path),
        "virtual_data_dir": str(tmp_path / "virtual"),
        "descriptor_path": str(tmp_path / "descriptor.json"),
    }

    with pytest.raises(VfsManifestError, match="incompatible"):
        VfsWorkerManifest.from_dict(raw)


def test_vfs_manifest_v2_exige_data_root_y_mods_dir(tmp_path: pathlib.Path) -> None:
    """VfsWorkerManifest v2 debe rechazar manifiestos que no declaren data_root y mods_dir."""
    challenge = VfsAttestationChallenge(
        profile="Default",
        source_mod="ModA",
        relative_path=pathlib.PurePosixPath("canary.txt"),
        sha256="a" * 64,
        profile_fingerprint="b" * 64,
    )
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10.0,
        expected_fingerprint="b" * 64,
        mutation_targets=(),
    )
    raw = {
        "protocol_version": 2,
        "job": job.to_dict(),
        "challenge": challenge.to_dict(),
        "virtual_data_dir": str(tmp_path / "virtual"),
        "descriptor_path": str(tmp_path / "descriptor.json"),
    }

    with pytest.raises(VfsManifestError, match="data_root"):
        VfsWorkerManifest.from_dict(raw)


def test_attestation_con_raices_divididas_y_trampa_en_data_mods(tmp_path: pathlib.Path) -> None:
    """Attestation busca canaries en mods_dir e ignora la trampa en <data_root>/mods."""
    data_root = tmp_path / "MO2_Data"
    mods_dir = tmp_path / "Custom_Mods"
    trap_mods = data_root / "mods"
    game_data = tmp_path / "Game" / "Data"

    profile_dir = data_root / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")

    real_mod = mods_dir / "CanaryMod" / "SKSE" / "Plugins"
    real_mod.mkdir(parents=True)
    real_canary = real_mod / "skyclaw-canary.txt"
    real_canary.write_bytes(b"canary-v2-from-custom-mods")

    trap_mod = trap_mods / "CanaryMod" / "SKSE" / "Plugins"
    trap_mod.mkdir(parents=True)
    trap_canary = trap_mod / "skyclaw-canary.txt"
    trap_canary.write_bytes(b"TRAP-CANARY-DATA-MODS")

    game_data.mkdir(parents=True)

    challenge = build_attestation_challenge(
        data_root=data_root,
        mods_dir=mods_dir,
        profile="Default",
        physical_data_dir=game_data,
    )

    expected_sha = hashlib.sha256(b"canary-v2-from-custom-mods").hexdigest()
    assert challenge.sha256 == expected_sha
    assert challenge.source_mod == "CanaryMod"

    virtual_data = tmp_path / "Virtual" / "Data"
    virtual_canary = virtual_data / "SKSE" / "Plugins" / "skyclaw-canary.txt"
    virtual_canary.parent.mkdir(parents=True)
    virtual_canary.write_bytes(b"canary-v2-from-custom-mods")

    proof = verify_vfs_attestation(
        challenge=challenge,
        data_root=data_root,
        mods_dir=mods_dir,
        profile="Default",
        virtual_data_dir=virtual_data,
    )

    assert proof.visible_sha256 == expected_sha
    assert proof.profile_fingerprint == challenge.profile_fingerprint

    # Trampa: si la vista virtual expone el contenido de data_root/mods, falla
    virtual_canary.write_bytes(b"TRAP-CANARY-DATA-MODS")
    with pytest.raises(VfsAttestationError, match="no coincide"):
        verify_vfs_attestation(
            challenge=challenge,
            data_root=data_root,
            mods_dir=mods_dir,
            profile="Default",
            virtual_data_dir=virtual_data,
        )


async def test_worker_despacha_con_raices_divididas(tmp_path: pathlib.Path) -> None:
    """El worker attesta y despacha usando data_root y mods_dir del manifiesto."""
    install_root = tmp_path / "MO2_Install"
    data_root = tmp_path / "MO2_Data"
    mods_dir = tmp_path / "Custom_Mods"
    trap_mods = data_root / "mods"
    game_data = tmp_path / "Game" / "Data"
    virtual_data = tmp_path / "Virtual" / "Data"

    profile_dir = data_root / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")

    real_mod = mods_dir / "CanaryMod"
    real_mod.mkdir(parents=True)
    (real_mod / "canary.txt").write_bytes(b"worker-canary")

    trap_mod = trap_mods / "CanaryMod"
    trap_mod.mkdir(parents=True)
    (trap_mod / "canary.txt").write_bytes(b"TRAP")

    game_data.mkdir(parents=True)
    virtual_data.mkdir(parents=True)
    (virtual_data / "canary.txt").write_bytes(b"worker-canary")

    challenge = build_attestation_challenge(
        data_root=data_root,
        mods_dir=mods_dir,
        profile="Default",
        physical_data_dir=game_data,
    )

    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10.0,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )

    manifest = VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        data_root=data_root,
        mods_dir=mods_dir,
        install_root=install_root,
        virtual_data_dir=virtual_data,
        descriptor_path=tmp_path / "descriptor.json",
    )

    async def _mock_probe(_path: pathlib.Path, sha: str, _timeout: float) -> str:
        return sha

    async def _health_handler(_manifest: VfsWorkerManifest) -> VfsToolExecution:
        return VfsToolExecution(
            success=True,
            message="",
            exit_code=0,
            stdout="health ok v2",
            stderr="",
            outputs=(),
        )

    result = await execute_worker_manifest(
        manifest,
        handlers={"health": _health_handler},
        grandchild_probe=_mock_probe,
    )

    assert result.success is True
    assert result.stdout == "health ok v2"
    assert result.attestation is not None
    assert result.attestation["visible_sha256"] == challenge.sha256


async def test_brokered_loot_runner_con_raices_divididas(tmp_path: pathlib.Path) -> None:
    """BrokeredLootRunner usa data_root y mods_dir para attestation y submit."""
    install_root = tmp_path / "MO2_Install"
    data_root = tmp_path / "MO2_Data"
    mods_dir = tmp_path / "Custom_Mods"
    trap_mods = data_root / "mods"
    game_data = tmp_path / "Game" / "Data"

    profile_dir = data_root / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    target = profile_dir / "plugins.txt"
    target.write_text("*Skyrim.esm\n", encoding="utf-8")

    real_mod = mods_dir / "CanaryMod"
    real_mod.mkdir(parents=True)
    (real_mod / "canary.txt").write_bytes(b"loot-canary")

    trap_mod = trap_mods / "CanaryMod"
    trap_mod.mkdir(parents=True)
    (trap_mod / "canary.txt").write_bytes(b"TRAP")

    game_data.mkdir(parents=True)
    loot = tmp_path / "LOOT" / "loot.exe"
    loot.parent.mkdir()
    loot.write_bytes(b"loot")

    calls: list[dict[str, object]] = []

    class _MockBroker:
        async def submit(self, job: VfsJob, **kwargs: object) -> VfsJobResult:
            calls.append({"job": job, "kwargs": kwargs})
            return VfsJobResult.from_dict(
                {
                    "protocol_version": VFS_PROTOCOL_VERSION,
                    "job_id": job.job_id,
                    "success": True,
                    "message": "",
                    "exit_code": 0,
                    "stdout": "sorted",
                    "stderr": "",
                    "outputs": [str(path) for path in job.mutation_targets],
                    "rollback_state": "not_required",
                    "attestation": {
                        "profile": "Default",
                        "profile_fingerprint": job.expected_fingerprint,
                    },
                    "tool_result": {
                        "sorted_plugins": ["Skyrim.esm"],
                        "warnings": [],
                        "errors": [],
                        "missing_patches": [],
                    },
                }
            )

    runner = BrokeredLootRunner(
        broker=_MockBroker(),
        instance_id="portable-main",
        data_root=data_root,
        mods_dir=mods_dir,
        install_root=install_root,
        profile="Default",
        game_data_dir=game_data,
        loot_exe=loot,
        timeout=60,
        mutation_targets=lambda: (target,),
    )

    challenge = await runner.prepare_attestation()
    expected_sha = hashlib.sha256(b"loot-canary").hexdigest()
    assert challenge.sha256 == expected_sha

    result = await runner.sort(update_masterlist=False)
    assert result.success is True
    assert len(calls) == 1
    call_kwargs = calls[0]["kwargs"]
    assert call_kwargs["data_root"] == data_root.resolve()
    assert call_kwargs["mods_dir"] == mods_dir.resolve()
    assert call_kwargs["install_root"] == install_root.resolve()
