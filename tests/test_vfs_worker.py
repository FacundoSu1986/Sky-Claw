"""Worker desechable: attestation antes de dispatch y prueba de proceso nieto."""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sys
import time

import psutil
import pytest

from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker
from sky_claw.local.mo2.vfs_contracts import (
    ALLOWED_VFS_TOOL_IDS,
    VFS_PROTOCOL_VERSION,
    VfsJob,
    VfsToolExitEvent,
    VfsToolStartedEvent,
)
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import (
    VfsProcessSpec,
    VfsToolExecution,
    VfsWorkerBootstrapError,
    _default_handlers,
    _default_session_handlers,
    execute_worker_manifest,
    load_broker_descriptor,
    run_brokered_process,
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
                "protocol_version": VFS_PROTOCOL_VERSION,
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
            "protocol_version": VFS_PROTOCOL_VERSION,
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
                "protocol_version": VFS_PROTOCOL_VERSION,
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


# ---------------------------------------------------------------------------
# Primitive de sesión de proceso (PR-586A)
# ---------------------------------------------------------------------------


class _SinkGrabador:
    """Sink de eventos de test: registra y prueba liveness del PID al emitir."""

    def __init__(self, job_id: str = "job-1") -> None:
        self._job_id = job_id
        self.eventos: list[object] = []
        self.started = asyncio.Event()
        self.pid_vivo_al_emitir: bool | None = None

    @property
    def job_id(self) -> str:
        return self._job_id

    async def emit(self, evento: object) -> None:
        if isinstance(evento, VfsToolStartedEvent):
            self.pid_vivo_al_emitir = psutil.pid_exists(evento.tool_pid)
            self.started.set()
        self.eventos.append(evento)


def _esperar_pid_muerto(pid: int, *, timeout: float = 5.0) -> None:
    inicio = time.monotonic()
    while time.monotonic() - inicio < timeout:
        if not psutil.pid_exists(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"el pid {pid} sobrevivió al cancel (proceso huérfano)")


async def test_proceso_brokered_emite_started_con_pid_vivo_y_exit_con_codigo() -> None:
    sink = _SinkGrabador()
    spec = VfsProcessSpec(
        executable=pathlib.Path(sys.executable),
        arguments=("-c", "print('hola')"),
    )

    resultado = await run_brokered_process(spec, event_sink=sink)

    assert [type(evento) for evento in sink.eventos] == [VfsToolStartedEvent, VfsToolExitEvent]
    started = sink.eventos[0]
    salida = sink.eventos[1]
    assert isinstance(started, VfsToolStartedEvent)
    assert isinstance(salida, VfsToolExitEvent)
    assert started.tool_pid > 0
    assert sink.pid_vivo_al_emitir is True, "tool_started debe emitirse con el proceso todavía vivo"
    assert salida.exit_code == resultado.exit_code == 0
    assert resultado.stdout.strip() == "hola"
    assert resultado.stdout_truncated is False
    assert resultado.stderr_truncated is False


async def test_proceso_brokered_acota_la_captura_por_la_cola() -> None:
    sink = _SinkGrabador()
    programa = "import sys; sys.stdout.write('x' * 5000 + 'TAIL'); sys.stderr.write('y' * 5000)"
    spec = VfsProcessSpec(executable=pathlib.Path(sys.executable), arguments=("-c", programa))

    resultado = await run_brokered_process(spec, event_sink=sink, limite_captura_bytes=1024)

    assert len(resultado.stdout) == 1024
    assert resultado.stdout.endswith("TAIL")
    assert resultado.stdout_truncated is True
    assert len(resultado.stderr) == 1024
    assert resultado.stderr_truncated is True


async def test_proceso_brokered_cancelado_mata_y_recolecta_el_proceso() -> None:
    sink = _SinkGrabador()
    spec = VfsProcessSpec(
        executable=pathlib.Path(sys.executable),
        arguments=("-c", "import time; time.sleep(60)"),
    )
    tarea = asyncio.create_task(run_brokered_process(spec, event_sink=sink))
    try:
        await asyncio.wait_for(sink.started.wait(), timeout=5)
        pid = sink.eventos[0].tool_pid  # type: ignore[union-attr]
        assert psutil.pid_exists(pid)

        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea

        _esperar_pid_muerto(pid)
    finally:
        if not tarea.done():
            tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea


async def test_proceso_brokered_exige_ejecutable_absoluto() -> None:
    spec = VfsProcessSpec(executable=pathlib.Path("python"), arguments=("-c", "pass"))

    with pytest.raises(ValueError, match="absoluta"):
        await run_brokered_process(spec, event_sink=_SinkGrabador())


async def test_dispatch_de_handler_de_sesion_recibe_el_sink(tmp_path: pathlib.Path) -> None:
    manifest, _canary = _manifest(tmp_path, virtual=True)
    sink = _SinkGrabador(manifest.job.job_id)
    visto: list[object] = []

    async def handler_sesion(_manifest: VfsWorkerManifest, event_sink: object) -> VfsToolExecution:
        visto.append(event_sink)
        return VfsToolExecution.ok()

    resultado = await execute_worker_manifest(
        manifest,
        session_handlers={"health": handler_sesion},  # type: ignore[dict-item]
        grandchild_probe=lambda _path, sha, _timeout: _return_sha(sha),
        event_sink=sink,
    )

    assert resultado.success is True
    assert visto == [sink]


async def test_handler_de_sesion_sin_sink_inyectado_no_recibe_none(tmp_path: pathlib.Path) -> None:
    """Sin sink cableado el handler recibe un sink no-op: nunca ``None``."""
    manifest, _canary = _manifest(tmp_path, virtual=True)
    visto: list[object] = []

    async def handler_sesion(_manifest: VfsWorkerManifest, event_sink: object) -> VfsToolExecution:
        visto.append(event_sink)
        await event_sink.emit(VfsToolExitEvent(job_id=_manifest.job.job_id, exit_code=0))  # type: ignore[attr-defined]
        return VfsToolExecution.ok()

    resultado = await execute_worker_manifest(
        manifest,
        session_handlers={"health": handler_sesion},  # type: ignore[dict-item]
        grandchild_probe=lambda _path, sha, _timeout: _return_sha(sha),
    )

    assert resultado.success is True
    assert visto and visto[0] is not None


def test_pr_586a_no_registra_tool_ids_de_sesion_productivos() -> None:
    """Ancla del alcance: la primitive no migra ningún ritual.

    El día que 586B agregue handlers de sesión productivos, este test se rompe
    a propósito y obliga a decidir qué tool_ids entran a la allowlist.
    """
    assert _default_session_handlers() == {}
    assert set(_default_handlers()) == ALLOWED_VFS_TOOL_IDS
