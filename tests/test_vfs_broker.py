"""Broker asíncrono: lifecycle, serialización y cancelación por instancia."""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
from typing import Any

import pytest

from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import (
    VfsBrokerError,
    VfsExecutionBroker,
    VfsJobTimeoutError,
    VfsWorkerDisconnectedError,
    vfs_instance_id,
)
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_manifest import VfsManifestError, read_worker_manifest


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
    challenge = build_attestation_challenge(
        mo2_root=mo2,
        profile="Default",
        physical_data_dir=data,
    )
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )
    return mo2, data, challenge, job


def test_instance_id_es_estable_y_no_expone_la_ruta(tmp_path: pathlib.Path) -> None:
    mo2 = tmp_path / "MO2 Portable"

    first = vfs_instance_id(mo2)
    second = vfs_instance_id(mo2 / ".")

    assert first == second
    assert first.startswith("mo2-")
    assert "MO2" not in first


async def test_un_solo_broker_puede_poseer_una_instancia(tmp_path: pathlib.Path) -> None:
    state = tmp_path / "state"
    first = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=state,
        descriptor_hardener=lambda _path: None,
    )
    second = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=state,
        descriptor_hardener=lambda _path: None,
    )
    await first.start()
    try:
        with pytest.raises(VfsBrokerError, match="ya esta poseida"):
            await second.start()
    finally:
        await first.close()

    await second.start()
    await second.close()


async def test_broker_reclama_lock_de_pid_inexistente(tmp_path: pathlib.Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lock = state / ".portable-main.lock"
    lock.write_text(
        json.dumps({"pid": 2_147_483_647, "session_id": "crashed"}),
        encoding="utf-8",
    )
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=state,
        descriptor_hardener=lambda _path: None,
    )

    await broker.start()
    try:
        assert broker.descriptor_path.is_file()
    finally:
        await broker.close()

    assert not lock.exists()


async def _conectar_bridge(broker: VfsExecutionBroker):
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    secret = base64.urlsafe_b64decode(descriptor["token"])
    reader, writer = await asyncio.open_connection(descriptor["host"], descriptor["port"])
    await write_authenticated_message(
        writer,
        {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "hello",
            "role": "bridge",
            "instance_id": "portable-main",
            "session_id": descriptor["session_id"],
        },
        secret,
    )
    ack = await read_authenticated_message(reader, secret)
    assert ack["type"] == "hello_ack"
    await broker.wait_until_ready(timeout=1)
    return reader, writer, secret


async def _reportar_como_worker(
    broker: VfsExecutionBroker,
    *,
    job_id: str,
    result: dict[str, object],
    expect_ack: bool = True,
) -> None:
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    secret = base64.urlsafe_b64decode(descriptor["token"])
    reader, writer = await asyncio.open_connection(descriptor["host"], descriptor["port"])
    try:
        await write_authenticated_message(
            writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "hello",
                "role": "worker",
                "instance_id": "portable-main",
                "session_id": descriptor["session_id"],
                "job_id": job_id,
            },
            secret,
        )
        ack = await read_authenticated_message(reader, secret)
        assert ack["type"] == "hello_ack"
        await write_authenticated_message(
            writer,
            {"protocol_version": 1, "type": "job_result", "result": result},
            secret,
        )
        if expect_ack:
            result_ack = await asyncio.wait_for(
                read_authenticated_message(reader, secret),
                timeout=1,
            )
            assert result_ack == {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "job_result_ack",
                "job_id": job_id,
            }
    finally:
        writer.close()
        await writer.wait_closed()


async def _reportar_salida_bridge(
    writer: asyncio.StreamWriter,
    secret: bytes,
    *,
    job_id: str,
    exit_code: int = 0,
) -> None:
    await write_authenticated_message(
        writer,
        {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "event",
            "event": "worker_exit",
            "job_id": job_id,
            "wait_ok": True,
            "exit_code": exit_code,
        },
        secret,
    )


def _resultado(job_id: str, challenge) -> dict[str, object]:
    return {
        "protocol_version": VFS_PROTOCOL_VERSION,
        "job_id": job_id,
        "success": True,
        "message": "",
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "outputs": [],
        "rollback_state": "not_required",
        "attestation": {
            "profile": "Default",
            "source_mod": challenge.source_mod,
            "relative_path": challenge.relative_path.as_posix(),
            "visible_sha256": challenge.sha256,
            "profile_fingerprint": challenge.profile_fingerprint,
            "grandchild_sha256": challenge.sha256,
        },
        "tool_result": {},
    }


async def test_broker_envia_manifest_sin_executable_arbitrario(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(
            broker.submit(
                job,
                challenge=challenge,
                mo2_root=mo2,
                virtual_data_dir=data,
            )
        )
        launch = await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=1)
        assert launch["type"] == "launch_worker"
        assert "executable" not in launch
        assert launch["profile"] == "Default"

        manifest = read_worker_manifest(pathlib.Path(str(launch["manifest_path"])), secret=secret)
        assert manifest.job == job
        assert manifest.challenge == challenge

        await _reportar_como_worker(broker, job_id=job.job_id, result=_resultado(job.job_id, challenge))
        await _reportar_salida_bridge(writer, secret, job_id=job.job_id)
        result = await asyncio.wait_for(pending, timeout=1)
        assert result.success is True
        assert result.stdout == "ok"
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_broker_rechaza_resultado_con_attestation_ajena_al_job(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        await read_authenticated_message(reader, secret)
        wrong = _resultado(job.job_id, challenge)
        assert isinstance(wrong["attestation"], dict)
        wrong["attestation"]["profile_fingerprint"] = "a" * 64
        await _reportar_como_worker(
            broker,
            job_id=job.job_id,
            result=wrong,
            expect_ack=False,
        )
        await _reportar_salida_bridge(writer, secret, job_id=job.job_id)

        with pytest.raises(VfsBrokerError, match="attestation"):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_manifest_firma_detecta_manipulacion(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        launch = await read_authenticated_message(reader, secret)
        manifest_path = pathlib.Path(str(launch["manifest_path"]))
        raw: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["manifest"]["job"]["profile"] = "Otro"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(VfsManifestError, match="firma"):
            read_worker_manifest(manifest_path, secret=secret)

        await _reportar_como_worker(
            broker,
            job_id=job.job_id,
            result=_resultado(job.job_id, challenge),
        )
        await _reportar_salida_bridge(writer, secret, job_id=job.job_id)
        await pending
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_broker_serializa_dos_jobs_de_la_misma_instancia(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, first = _entorno(tmp_path)
    second = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        one = asyncio.create_task(broker.submit(first, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        two = asyncio.create_task(broker.submit(second, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        first_launch = await read_authenticated_message(reader, secret)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=0.05)
        await _reportar_como_worker(
            broker,
            job_id=str(first_launch["job_id"]),
            result=_resultado(str(first_launch["job_id"]), challenge),
        )
        await _reportar_salida_bridge(writer, secret, job_id=str(first_launch["job_id"]))
        await one
        second_launch = await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=1)
        await _reportar_como_worker(
            broker,
            job_id=str(second_launch["job_id"]),
            result=_resultado(str(second_launch["job_id"]), challenge),
        )
        await _reportar_salida_bridge(writer, secret, job_id=str(second_launch["job_id"]))
        await two
        assert first_launch["job_id"] != second_launch["job_id"]
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_cancelacion_envia_cancel_al_bridge(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        await read_authenticated_message(reader, secret)
        pending.cancel()
        cancel = await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=1)
        assert cancel == {
            "job_id": job.job_id,
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "cancel",
        }
        pending.cancel()
        await asyncio.sleep(0.1)
        assert not pending.done(), "rollback no puede empezar antes de confirmar worker_exit"

        await write_authenticated_message(
            writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "worker_exit",
                "job_id": job.job_id,
                "wait_ok": True,
                "exit_code": 1,
            },
            secret,
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_timeout_espera_worker_exit_antes_de_propagarse(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, _job = _entorno(tmp_path)
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=0.05,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        await read_authenticated_message(reader, secret)
        cancel = await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=1)
        assert cancel["type"] == "cancel"
        await asyncio.sleep(0.1)
        assert not pending.done()

        await _reportar_salida_bridge(writer, secret, job_id=job.job_id, exit_code=1)
        with pytest.raises(VfsJobTimeoutError):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_cancelacion_despues_del_resultado_preserva_confirmacion_terminal(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        await read_authenticated_message(reader, secret)
        await _reportar_como_worker(broker, job_id=job.job_id, result=_resultado(job.job_id, challenge))
        await asyncio.sleep(0.05)

        pending.cancel()
        cancel = await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=1)
        assert cancel["type"] == "cancel"
        await asyncio.sleep(0.1)
        assert not pending.done()

        await _reportar_salida_bridge(writer, secret, job_id=job.job_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_worker_exit_sin_resultado_falla_sin_esperar_timeout(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        await read_authenticated_message(reader, secret)
        await write_authenticated_message(
            writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "worker_exit",
                "job_id": job.job_id,
                "wait_ok": True,
                "exit_code": 70,
            },
            secret,
        )

        with pytest.raises(VfsWorkerDisconnectedError, match="70"):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_worker_exit_sin_ack_rechaza_resultado_tardio(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        await read_authenticated_message(reader, secret)
        await _reportar_salida_bridge(writer, secret, job_id=job.job_id)

        with pytest.raises(VfsWorkerDisconnectedError):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_bridge_error_de_launch_falla_sin_esperar_timeout(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        await read_authenticated_message(reader, secret)
        await write_authenticated_message(
            writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "bridge_error",
                "command": "launch_worker",
                "job_id": job.job_id,
                "message": "MO2 rechazó el launch",
            },
            secret,
        )

        with pytest.raises(VfsBrokerError, match="rechazó"):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        writer.close()
        await writer.wait_closed()
        await broker.close()


async def test_cancelacion_durante_fence_de_error_no_libera_antes_de_worker_exit(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, _secret = await _conectar_bridge(broker)
    pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
    await read_authenticated_message(reader, _secret)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    pending.cancel()
    pending.cancel()
    await asyncio.sleep(0.1)
    assert not pending.done()

    replacement_reader, replacement_writer, replacement_secret = await _conectar_bridge(broker)
    try:
        await _reportar_salida_bridge(replacement_writer, replacement_secret, job_id=job.job_id, exit_code=1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        replacement_writer.close()
        await replacement_writer.wait_closed()
        del replacement_reader
        await broker.close()


async def test_close_cierra_conexiones_antes_de_esperar_listener(tmp_path: pathlib.Path) -> None:
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    _reader, writer, _secret = await _conectar_bridge(broker)
    servidor_real = broker._server
    assert servidor_real is not None

    class ServidorQueVerificaOrden:
        def close(self) -> None:
            servidor_real.close()

        async def wait_closed(self) -> None:
            bridge_writer = broker._bridge_writer
            assert bridge_writer is None or bridge_writer.is_closing(), (
                "las conexiones deben cerrarse antes de esperar el listener"
            )
            await servidor_real.wait_closed()

    broker._server = ServidorQueVerificaOrden()
    try:
        await broker.close()
    finally:
        writer.close()
        await writer.wait_closed()
        if broker._server is not None:
            broker._server = servidor_real
            await broker.close()


async def test_close_con_job_activo_espera_terminacion_y_resuelve_submit(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()
    reader, writer, secret = await _conectar_bridge(broker)
    pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
    await read_authenticated_message(reader, secret)

    closing = asyncio.create_task(broker.close())
    cancel = await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=1)
    assert cancel["type"] == "cancel"
    await asyncio.sleep(0.1)
    assert not closing.done()
    assert not pending.done()

    await _reportar_salida_bridge(writer, secret, job_id=job.job_id, exit_code=1)
    await asyncio.wait_for(closing, timeout=1)
    with pytest.raises(VfsBrokerError):
        await asyncio.wait_for(pending, timeout=1)


async def test_close_concurrente_es_idempotente(tmp_path: pathlib.Path) -> None:
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
    )
    await broker.start()

    await asyncio.gather(broker.close(), broker.close())

    assert not broker.descriptor_path.exists()
