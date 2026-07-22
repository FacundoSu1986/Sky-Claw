"""Worker desechable: attestation antes de dispatch y prueba de proceso nieto."""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import time

import pytest

from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import (
    VfsToolExecution,
    VfsWorkerBootstrapError,
    execute_worker_manifest,
    load_broker_descriptor,
    run_worker_session,
)


def _manifest(tmp_path: pathlib.Path, *, virtual: bool) -> tuple[VfsWorkerManifest, pathlib.Path]:
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    physical_data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    physical_data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    challenge = build_attestation_challenge(
        mo2_root=mo2,
        profile="Default",
        physical_data_dir=physical_data,
    )
    virtual_data = tmp_path / "virtual" / "Data" if virtual else physical_data
    if virtual:
        virtual_data.mkdir(parents=True)
        (virtual_data / "canary.txt").write_bytes(b"canary")
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )
    return (
        VfsWorkerManifest(
            protocol_version=VFS_PROTOCOL_VERSION,
            job=job,
            challenge=challenge,
            mo2_root=mo2,
            virtual_data_dir=virtual_data,
            descriptor_path=tmp_path / "descriptor.json",
        ),
        virtual_data / "canary.txt",
    )


async def test_worker_fuera_de_usvfs_falla_antes_del_handler(tmp_path: pathlib.Path) -> None:
    manifest, _canary = _manifest(tmp_path, virtual=False)
    called = False

    async def handler(_manifest: VfsWorkerManifest) -> VfsToolExecution:
        nonlocal called
        called = True
        return VfsToolExecution.ok()

    result = await execute_worker_manifest(
        manifest,
        handlers={"health": handler},
        grandchild_probe=lambda _path, _sha, _timeout: _probe_ok(),
    )

    assert result.success is False
    assert "canary no visible" in result.message
    assert result.attestation is None
    assert called is False


async def _probe_ok() -> str:
    return "grandchild-sha"


async def test_worker_attesta_worker_y_nieto_antes_del_handler(tmp_path: pathlib.Path) -> None:
    manifest, canary = _manifest(tmp_path, virtual=True)
    observed: list[pathlib.Path] = []

    async def probe(path: pathlib.Path, sha256: str, timeout: float) -> str:
        assert sha256 == manifest.challenge.sha256
        assert timeout > 0
        observed.append(path)
        return sha256

    async def handler(_manifest: VfsWorkerManifest) -> VfsToolExecution:
        return VfsToolExecution(
            success=True,
            message="",
            exit_code=0,
            stdout="health ok",
            stderr="",
            outputs=(),
        )

    result = await execute_worker_manifest(
        manifest,
        handlers={"health": handler},
        grandchild_probe=probe,
    )

    assert result.success is True
    assert result.stdout == "health ok"
    assert result.attestation is not None
    assert result.attestation["grandchild_sha256"] == manifest.challenge.sha256
    assert observed == [canary]


async def test_worker_falla_cerrado_si_el_nieto_no_ve_el_canary(tmp_path: pathlib.Path) -> None:
    manifest, _canary = _manifest(tmp_path, virtual=True)

    async def failed_probe(_path: pathlib.Path, _sha256: str, _timeout: float) -> str:
        raise RuntimeError("nieto sin overlay")

    result = await execute_worker_manifest(manifest, grandchild_probe=failed_probe)

    assert result.success is False
    assert "proceso nieto" in result.message
    assert result.exit_code is None


def test_descriptor_rechaza_host_no_loopback(tmp_path: pathlib.Path) -> None:
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "host": "0.0.0.0",
                "port": 1234,
                "token": base64.urlsafe_b64encode(b"x" * 32).decode("ascii"),
                "instance_id": "portable-main",
                "session_id": "session",
                "expires_at": time.time() + 60,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(VfsWorkerBootstrapError, match="loopback"):
        load_broker_descriptor(descriptor)


async def test_worker_reporta_resultado_por_su_canal_autenticado(tmp_path: pathlib.Path) -> None:
    manifest, _canary = _manifest(tmp_path, virtual=True)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    secret = base64.urlsafe_b64decode(descriptor["token"])
    bridge_reader, bridge_writer = await asyncio.open_connection(descriptor["host"], descriptor["port"])
    await write_authenticated_message(
        bridge_writer,
        {
            "protocol_version": 1,
            "type": "hello",
            "role": "bridge",
            "instance_id": "portable-main",
            "session_id": descriptor["session_id"],
        },
        secret,
    )
    await read_authenticated_message(bridge_reader, secret)
    await broker.wait_until_ready(timeout=1)
    try:
        pending = asyncio.create_task(
            broker.submit(
                manifest.job,
                challenge=manifest.challenge,
                mo2_root=manifest.mo2_root,
                virtual_data_dir=manifest.virtual_data_dir,
            )
        )
        launch = await read_authenticated_message(bridge_reader, secret)
        worker = asyncio.create_task(
            run_worker_session(
                manifest_path=pathlib.Path(str(launch["manifest_path"])),
                descriptor_path=broker.descriptor_path,
                expected_job_id=manifest.job.job_id,
                grandchild_probe=lambda _path, sha, _timeout: _return_sha(sha),
            )
        )

        worker_result = await asyncio.wait_for(worker, timeout=1)
        await write_authenticated_message(
            bridge_writer,
            {
                "protocol_version": 1,
                "type": "event",
                "event": "worker_exit",
                "job_id": manifest.job.job_id,
                "wait_ok": True,
                "exit_code": 0,
            },
            secret,
        )
        result = await asyncio.wait_for(pending, timeout=1)
        assert result.success is True
        assert worker_result == result
    finally:
        bridge_writer.close()
        await bridge_writer.wait_closed()
        await broker.close()


async def _return_sha(sha256: str) -> str:
    return sha256
