"""Contratos y attestation del broker de ejecución bajo USVFS (F8)."""

from __future__ import annotations

import pathlib

import pytest

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
    VfsProtocolError,
)


def _crear_perfil(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    mo2_root = tmp_path / "MO2"
    profile = mo2_root / "profiles" / "Default"
    mod = mo2_root / "mods" / "CanaryMod"
    data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n-DisabledMod\n", encoding="utf-8-sig")
    (mod / "SKSE" / "Plugins").mkdir(parents=True)
    (mod / "SKSE" / "Plugins" / "skyclaw-canary.txt").write_bytes(b"canary-v1")
    return mo2_root, profile, data


def test_vfs_job_roundtrip_con_allowlist_y_contrato_canonico(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "profiles" / "Default" / "plugins.txt"
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="loot_sort",
        payload={"update_masterlist": False},
        timeout_seconds=120.0,
        expected_fingerprint="a" * 64,
        mutation_targets=(target,),
    )

    restored = VfsJob.from_dict(job.to_dict())

    assert restored == job
    assert restored.protocol_version == VFS_PROTOCOL_VERSION
    assert restored.job_id
    assert restored.mutation_targets == (target.resolve(),)


def test_vfs_job_rechaza_tool_arbitraria() -> None:
    with pytest.raises(VfsProtocolError, match="tool_id no permitido"):
        VfsJob.create(
            instance_id="portable-main",
            profile="Default",
            tool_id="powershell.exe",
            payload={},
            timeout_seconds=30.0,
            expected_fingerprint="a" * 64,
            mutation_targets=(),
        )


def test_vfs_job_rechaza_version_de_protocolo_incompatible(tmp_path: pathlib.Path) -> None:
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=30.0,
        expected_fingerprint="a" * 64,
        mutation_targets=(tmp_path / "target",),
    ).to_dict()
    job["protocol_version"] = VFS_PROTOCOL_VERSION + 1

    with pytest.raises(VfsProtocolError, match="versión de protocolo"):
        VfsJob.from_dict(job)


def test_vfs_job_rechaza_job_id_con_traversal(tmp_path: pathlib.Path) -> None:
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=30.0,
        expected_fingerprint="a" * 64,
        mutation_targets=(tmp_path / "target",),
    ).to_dict()
    job["job_id"] = "../fuera"

    with pytest.raises(VfsProtocolError, match="job_id"):
        VfsJob.from_dict(job)


def test_vfs_job_result_exige_success_y_message_canonicos() -> None:
    result = VfsJobResult.from_dict(
        {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "job_id": "job-1",
            "success": False,
            "message": "perfil incorrecto",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "outputs": [],
            "rollback_state": "not_started",
            "attestation": None,
        }
    )

    assert result.success is False
    assert result.message == "perfil incorrecto"
    assert VfsJobResult.from_dict(result.to_dict()) == result


def test_attestation_elige_canary_ausente_de_data_fisico(tmp_path: pathlib.Path) -> None:
    mo2_root, _profile, physical_data = _crear_perfil(tmp_path)

    challenge = build_attestation_challenge(
        mo2_root=mo2_root,
        profile="Default",
        physical_data_dir=physical_data,
    )

    assert challenge.relative_path == pathlib.PurePosixPath("SKSE/Plugins/skyclaw-canary.txt")
    assert challenge.source_mod == "CanaryMod"
    assert challenge.sha256
    assert not (physical_data / pathlib.Path(*challenge.relative_path.parts)).exists()

    assert VfsAttestationChallenge.from_dict(challenge.to_dict()) == challenge


def test_attestation_challenge_rechaza_ruta_relativa_con_traversal() -> None:
    with pytest.raises(VfsAttestationError, match="relative_path"):
        VfsAttestationChallenge.from_dict(
            {
                "profile": "Default",
                "source_mod": "CanaryMod",
                "relative_path": "../fuera.txt",
                "sha256": "a" * 64,
                "profile_fingerprint": "b" * 64,
            }
        )


def test_attestation_rechaza_worker_fuera_de_usvfs(tmp_path: pathlib.Path) -> None:
    mo2_root, _profile, physical_data = _crear_perfil(tmp_path)
    challenge = build_attestation_challenge(
        mo2_root=mo2_root,
        profile="Default",
        physical_data_dir=physical_data,
    )

    with pytest.raises(VfsAttestationError, match="canary no visible"):
        verify_vfs_attestation(
            challenge=challenge,
            mo2_root=mo2_root,
            profile="Default",
            virtual_data_dir=physical_data,
        )


def test_attestation_acepta_vista_virtual_y_detecta_drift(tmp_path: pathlib.Path) -> None:
    mo2_root, profile, physical_data = _crear_perfil(tmp_path)
    challenge = build_attestation_challenge(
        mo2_root=mo2_root,
        profile="Default",
        physical_data_dir=physical_data,
    )
    virtual_data = tmp_path / "VistaVirtual" / "Data"
    virtual_canary = virtual_data / pathlib.Path(*challenge.relative_path.parts)
    virtual_canary.parent.mkdir(parents=True)
    virtual_canary.write_bytes(b"canary-v1")

    proof = verify_vfs_attestation(
        challenge=challenge,
        mo2_root=mo2_root,
        profile="Default",
        virtual_data_dir=virtual_data,
    )

    assert proof.profile_fingerprint == challenge.profile_fingerprint
    assert proof.visible_sha256 == challenge.sha256

    (profile / "modlist.txt").write_text("-CanaryMod\n", encoding="utf-8-sig")
    with pytest.raises(VfsAttestationError, match="fingerprint"):
        verify_vfs_attestation(
            challenge=challenge,
            mo2_root=mo2_root,
            profile="Default",
            virtual_data_dir=virtual_data,
        )


def test_attestation_falla_cerrado_sin_canary_elegible(tmp_path: pathlib.Path) -> None:
    mo2_root, _profile, physical_data = _crear_perfil(tmp_path)
    physical_canary = physical_data / "SKSE" / "Plugins" / "skyclaw-canary.txt"
    physical_canary.parent.mkdir(parents=True)
    physical_canary.write_bytes(b"vanilla-copy")

    with pytest.raises(VfsAttestationError, match="canary elegible"):
        build_attestation_challenge(
            mo2_root=mo2_root,
            profile="Default",
            physical_data_dir=physical_data,
        )
