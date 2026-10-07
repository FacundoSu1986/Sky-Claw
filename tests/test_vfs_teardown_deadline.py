"""Tests del deadline interno de teardown en el broker VFS y sesión (issue #623).

Verifica que el teardown brokered:
1. No cuelga indefinidamente cuando el bridge permanece conectado pero el worker no sale.
2. Falla con VfsTeardownDeadlineError y marca terminality_unknown (sin falso verde de éxito).
3. Despierta de inmediato ante bridge_error de terminación con VfsBridgeTerminationError.
4. Impide que un rollback que requiere terminalidad demostrada proceda como seguro.
5. Preserva el camino feliz cuando worker_exit real llega en tiempo.
6. Preserva el contrato de desconexión del bridge (kill-on-close tras gracia).
7. Resiste cancelaciones externas repetidas sin reiniciar el deadline absoluto ni perder la excepción.
8. Tolera worker_exit tardío sin corromper el estado del broker ni reactivar el job.
9. B1: Clasifica carrera benigna pop(job)->worker_exit en bridge sin falso VfsBridgeTerminationError.
10. B2: close() ejecuta la limpieza completa de recursos locales aunque cancel falle con teardown error.
11. B3: submit() y BrokeredLootRunner preservan el timeout tipado con atributos de teardown.
12. Cuarentena: terminalidad desconocida persiste un marcador y bloquea nuevos jobs hasta resolución.
13. Completed driver: cancel() en sesión con driver completado propaga error de teardown y marca unknown.
14. Protocolo: bridge_error con job_id no válido falla cerrado con error de protocolo.
15. Concurrencia: _cancel_and_join no suprime BaseException indiscriminadamente.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import pathlib
import threading
from typing import Any

import pytest

from sky_claw.local.mo2 import vfs_broker
from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODProcess
from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner, LOOTTimeoutError
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    BridgeCommandError,
    BridgeLaunchController,
)
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import (
    VfsBridgeTerminationError,
    VfsBrokerError,
    VfsExecutionBroker,
    VfsJobTimeoutError,
    VfsTeardownDeadlineError,
    VfsTeardownError,
)
from sky_claw.local.mo2.vfs_contracts import (
    VFS_PROTOCOL_VERSION,
    VfsJob,
)
from sky_claw.local.mo2.vfs_ipc import (
    read_authenticated_message,
    write_authenticated_message,
)
from sky_claw.local.tools._process import kill_and_reap


def _kw_broker(tmp_path: pathlib.Path, **kwargs) -> VfsExecutionBroker:
    sig = inspect.signature(VfsExecutionBroker.__init__)
    deadline = kwargs.pop("deadline", 0.2)
    if "connected_worker_exit_deadline_seconds" in sig.parameters:
        kwargs["connected_worker_exit_deadline_seconds"] = deadline
    else:
        kwargs["fence_grace_seconds"] = deadline
    instance_id = kwargs.pop("instance_id", "portable-main")
    state_dir = kwargs.pop("state_dir", tmp_path / "state")
    secret = kwargs.pop("secret", b"x" * 32)
    hardener = kwargs.pop("descriptor_hardener", lambda _path: None)
    return VfsExecutionBroker(
        instance_id=instance_id,
        state_dir=state_dir,
        secret=secret,
        descriptor_hardener=hardener,
        **{k: v for k, v in kwargs.items() if k in sig.parameters},
    )


def _entorno(tmp_path: pathlib.Path, *, timeout: float = 10.0):
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
        timeout_seconds=timeout,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )
    return mo2, data, challenge, job


class _BridgeFalso:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, secret: bytes) -> None:
        self.reader = reader
        self.writer = writer
        self.secret = secret

    @classmethod
    async def conectar(cls, broker: VfsExecutionBroker) -> _BridgeFalso:
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
        return cls(reader, writer, secret)

    async def recv(self, *, timeout: float = 3.0) -> dict[str, object]:
        return await asyncio.wait_for(read_authenticated_message(self.reader, self.secret), timeout=timeout)

    async def worker_exit(self, job_id: str, *, exit_code: int = 0) -> None:
        await write_authenticated_message(
            self.writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "worker_exit",
                "job_id": job_id,
                "wait_ok": True,
                "exit_code": exit_code,
            },
            self.secret,
        )

    async def bridge_error(
        self,
        job_id: str,
        *,
        command: str = "cancel",
        message: str = "error",
        kind: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "event",
            "event": "bridge_error",
            "command": command,
            "job_id": job_id,
            "message": message,
        }
        if kind is not None:
            payload["kind"] = kind
        await write_authenticated_message(self.writer, payload, self.secret)

    async def cerrar(self) -> None:
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self.writer.wait_closed()


class _WorkerFalso:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, secret: bytes, job_id: str) -> None:
        self.reader = reader
        self.writer = writer
        self.secret = secret
        self.job_id = job_id

    @classmethod
    async def conectar(cls, broker: VfsExecutionBroker, job_id: str) -> _WorkerFalso:
        descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
        secret = base64.urlsafe_b64decode(descriptor["token"])
        reader, writer = await asyncio.open_connection(descriptor["host"], descriptor["port"])
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
        return cls(reader, writer, secret, job_id)

    async def started(self, pid: int) -> None:
        await write_authenticated_message(
            self.writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "tool_started",
                "job_id": self.job_id,
                "tool_pid": pid,
            },
            self.secret,
        )

    async def report_result(self, *, success: bool = True, exit_code: int = 0, challenge=None) -> None:
        attestation = None
        if challenge is not None:
            attestation = {
                "profile": "Default",
                "source_mod": challenge.source_mod,
                "relative_path": challenge.relative_path.as_posix(),
                "visible_sha256": challenge.sha256,
                "profile_fingerprint": challenge.profile_fingerprint,
                "grandchild_sha256": challenge.sha256,
            }
        await write_authenticated_message(
            self.writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "job_result",
                "result": {
                    "protocol_version": VFS_PROTOCOL_VERSION,
                    "job_id": self.job_id,
                    "success": success,
                    "message": "",
                    "exit_code": exit_code,
                    "stdout": "ok",
                    "stderr": "",
                    "outputs": [],
                    "rollback_state": "not_required",
                    "tool_result": {},
                    "attestation": attestation,
                },
            },
            self.secret,
        )

    async def cerrar(self) -> None:
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self.writer.wait_closed()


async def _abrir_sesion(
    broker: VfsExecutionBroker, bridge: _BridgeFalso, job: VfsJob, challenge, mo2, data, *, pid: int = 4242
):
    apertura = asyncio.create_task(broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
    launch = await bridge.recv()
    assert launch["type"] == "launch_worker"
    worker = await _WorkerFalso.conectar(broker, job.job_id)
    await worker.started(pid)
    sesion = await asyncio.wait_for(apertura, timeout=3)
    return sesion, worker


@pytest.mark.asyncio
async def test_teardown_con_bridge_conectado_y_sin_worker_exit_falla_con_deadline(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=1234)

        with pytest.raises(VfsTeardownDeadlineError):
            await asyncio.wait_for(sesion.cancel(), timeout=1.0)

        assert not sesion.confirmed_terminal
        assert sesion.terminality_unknown
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_bridge_error_durante_terminacion_despierta_inmediatamente_con_error_tipado(
    tmp_path: pathlib.Path,
) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=5.0)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=5555)

        cancelacion = asyncio.create_task(sesion.cancel())
        cancel_msg = await bridge.recv()
        assert cancel_msg["type"] == "cancel"

        # Error genuino de terminación
        await bridge.bridge_error(
            job.job_id,
            command="cancel",
            message="Job object termination failed",
            kind="termination_failed",
        )

        with pytest.raises(VfsBridgeTerminationError) as exc_info:
            await asyncio.wait_for(cancelacion, timeout=1.0)

        assert "Job object termination failed" in str(exc_info.value)
        assert not sesion.confirmed_terminal
        assert sesion.terminality_unknown
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_rollback_safety_impide_rollback_si_terminalidad_es_indeterminada(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=8888)
        proc = BrokeredDynDOLODProcess(sesion)

        rollback_ejecutado = False

        async def operacion_con_cleanup_y_rollback():
            nonlocal rollback_ejecutado
            try:
                raise RuntimeError("falla en runner")
            except Exception:
                await kill_and_reap(proc)
                if proc.confirmed_terminal:
                    rollback_ejecutado = True

        with pytest.raises(VfsTeardownDeadlineError):
            await operacion_con_cleanup_y_rollback()

        assert not rollback_ejecutado
        assert not proc.confirmed_terminal
        assert proc.terminality_unknown
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_teardown_con_worker_exit_real_confirma_terminalidad_exitosamente(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=1.0)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=9999)
        proc = BrokeredDynDOLODProcess(sesion)

        cancelacion = asyncio.create_task(sesion.cancel())
        cancel_msg = await bridge.recv()
        assert cancel_msg["type"] == "cancel"

        await bridge.worker_exit(job.job_id, exit_code=0)
        await asyncio.wait_for(cancelacion, timeout=2.0)

        assert sesion.confirmed_terminal
        assert not sesion.terminality_unknown
        assert proc.confirmed_terminal
        assert not proc.terminality_unknown
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_bridge_desconectado_preserva_contrato_existente_kill_on_close(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, fence_grace_seconds=0.2, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=1111)

        await bridge.cerrar()
        await asyncio.sleep(0.05)

        await asyncio.wait_for(sesion.cancel(), timeout=1.5)

        assert sesion.confirmed_terminal
        assert not sesion.terminality_unknown
    finally:
        if worker is not None:
            await worker.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_cancelacion_repetida_no_reinicia_deadline_ni_pierde_excepcion(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.3)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=2222)

        tarea_cancel = asyncio.create_task(sesion.cancel())
        await bridge.recv()

        await asyncio.sleep(0.05)
        tarea_cancel.cancel()
        await asyncio.sleep(0.05)
        tarea_cancel.cancel()

        # Debe lanzar CancelledError (la cancelación del caller) con la causa vinculada
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await asyncio.wait_for(tarea_cancel, timeout=1.5)

        assert getattr(exc_info.value, "terminality_unknown", False) is True

        # Llamar cancel() subsecuentemente re-lanza el error de teardown exacto
        with pytest.raises(VfsTeardownDeadlineError):
            await sesion.cancel()

        assert not sesion.confirmed_terminal
        assert sesion.terminality_unknown
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_worker_exit_tardio_no_sobrescribe_ni_corrompe_estado(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=3333)

        with pytest.raises(VfsTeardownDeadlineError):
            await asyncio.wait_for(sesion.cancel(), timeout=1.0)

        await bridge.worker_exit(job.job_id, exit_code=0)
        await asyncio.sleep(0.05)

        assert not sesion.confirmed_terminal
        assert sesion.terminality_unknown
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ==============================================================================
# R1: Carrera benigna pop(job) -> worker_exit con BridgeLaunchController real
# ==============================================================================


class _OrganizerFake:
    def __init__(self) -> None:
        self.started_apps: list[object] = []

    def startApplication(self, *args: object) -> int:  # noqa: N802
        self.started_apps.append(args)
        return 9999

    def waitForApplication(self, handle: int, refresh: bool) -> tuple[bool, int]:  # noqa: N802
        return True, 0


class _JobObjectFake:
    def __init__(self) -> None:
        self.assigned: list[int] = []
        self.terminated = False
        self.closed = False

    def assign(self, handle: int) -> None:
        self.assigned.append(handle)

    def terminate(self) -> None:
        self.terminated = True

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_benign_cancel_race_with_real_bridge_launch_controller(tmp_path: pathlib.Path) -> None:
    """B1: Cuando el worker ya terminó en el bridge (pop(job) ejecutado) pero

    worker_exit está en tránsito, un cancel genera BridgeJobUnknownError /
    'job desconocido'. El broker NO debe convertirlo en VfsBridgeTerminationError,
    debe esperar worker_exit y preservar el resultado válido.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=2.0)
    await broker.start()

    worker_binary = tmp_path / "mock_worker.exe"
    worker_binary.write_bytes(b"binary")

    organizer = _OrganizerFake()
    process_unblock = threading.Event()
    worker_exit_gate = threading.Event()

    def _waiter(handle: int) -> tuple[bool, int]:
        process_unblock.wait(timeout=5.0)
        return True, 0

    bridge_events_out: list[dict[str, object]] = []

    def _send_bridge_event(evt: dict[str, object]) -> None:
        if evt.get("event") == "worker_exit":
            worker_exit_gate.wait(timeout=5.0)
        bridge_events_out.append(evt)

    controller = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker_binary,
        worker_prefix=["--dummy"],
        descriptor_path=broker.descriptor_path,
        jobs_root=broker._jobs_dir,
        send_event=_send_bridge_event,
        job_factory=_JobObjectFake,
        process_waiter=_waiter,
    )

    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=7777)

        # Simulamos que el controller del bridge lanza y monitorea el job
        controller.launch(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "launch_worker",
                "job_id": job.job_id,
                "profile": job.profile,
                "manifest_path": str((broker._jobs_dir / f"{job.job_id}.json").resolve()),
                "overwrite_mod": None,
            }
        )

        # El proceso termina en el bridge: monitor ejecuta pop() y queda frenado en worker_exit_gate
        process_unblock.set()
        deadline_pop = asyncio.get_running_loop().time() + 1.0
        while job.job_id in controller._jobs and asyncio.get_running_loop().time() < deadline_pop:
            await asyncio.sleep(0.005)
        assert job.job_id not in controller._jobs, "El monitor del bridge no realizó pop(job) dentro del plazo"

        # Ahora llega un cancel desde el broker/sesión:
        cancel_task = asyncio.create_task(sesion.cancel())
        cancel_req = await bridge.recv()
        assert cancel_req["type"] == "cancel"

        # El plugin del bridge ejecuta controller.cancel(cancel_req)
        # Como pop() ya ocurrió en el controller, cancel() lanza "job desconocido"
        with pytest.raises(BridgeCommandError) as cmd_exc:
            controller.cancel(cancel_req)
        assert "desconocido" in str(cmd_exc.value)

        # El bridge reporta el error con clasificación estructurada kind="job_unknown"
        # (o el mensaje tradicional)
        await bridge.bridge_error(
            job.job_id,
            command="cancel",
            message=str(cmd_exc.value),
            kind="job_unknown",
        )

        # Damos un instante: en HEAD, el broker convierte esto inmediatamente en VfsBridgeTerminationError!
        await asyncio.sleep(0.05)

        # Liberamos worker_exit_gate para que llegue el worker_exit real
        worker_exit_gate.set()
        await bridge.worker_exit(job.job_id, exit_code=0)

        # El cancel debe completar SIN VfsBridgeTerminationError falso
        await asyncio.wait_for(cancel_task, timeout=2.0)

        assert sesion.confirmed_terminal is True
        assert sesion.terminality_unknown is False
        assert getattr(broker, "quarantine_reason", None) is None
    finally:
        worker_exit_gate.set()
        process_unblock.set()
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ==============================================================================
# R2: close() limpia recursos locales aunque active job falle teardown
# ==============================================================================


@pytest.mark.asyncio
async def test_close_limpia_recursos_locales_aunque_cancel_falle_con_teardown_error(
    tmp_path: pathlib.Path,
) -> None:
    """B2: broker.close() debe cerrar sockets, servidor y descriptor local

    aun si un job activo en vuelo falla teardown con VfsTeardownDeadlineError.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=6666)

        # No enviamos worker_exit: el teardown durante close() expirará por deadline
        with pytest.raises(VfsTeardownError):
            await broker.close()

        # RECURSOS LOCALES DEBEN ESTAR CERRADOS Y LIMPIOS:
        assert broker._server is None
        assert not broker.descriptor_path.exists()
        assert len(broker._client_tasks) == 0
        assert len(broker._worker_writers) == 0
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        # close idempotente
        with contextlib.suppress(Exception):
            await broker.close()


# ==============================================================================
# R3: Preservación de timeout tipado con atributos de teardown
# ==============================================================================


@pytest.mark.asyncio
async def test_submit_preserva_vfs_job_timeout_error_con_atributos_de_teardown(
    tmp_path: pathlib.Path,
) -> None:
    """B3: submit() ante timeout del job debe levantar VfsJobTimeoutError

    conservando terminality_unknown=True y teardown_error en lugar de ser
    enmascarado por VfsTeardownDeadlineError.
    """
    mo2, data, challenge, job = _entorno(tmp_path, timeout=0.2)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    try:
        submit_task = asyncio.create_task(
            broker.submit(
                job,
                challenge=challenge,
                mo2_root=mo2,
                virtual_data_dir=data,
            )
        )
        launch = await bridge.recv()
        assert launch["type"] == "launch_worker"

        # No enviamos resultado ni worker_exit: expira timeout(0.2) y luego deadline(0.2)
        with pytest.raises(VfsJobTimeoutError) as exc_info:
            await asyncio.wait_for(submit_task, timeout=2.0)

        err = exc_info.value
        assert getattr(err, "terminality_unknown", False) is True
        assert isinstance(getattr(err, "teardown_error", None), VfsTeardownDeadlineError)
    finally:
        await bridge.cerrar()
        with contextlib.suppress(Exception):
            await broker.close()


@pytest.mark.asyncio
async def test_brokered_loot_runner_sort_propaga_atributos_de_teardown_en_loot_timeout(
    tmp_path: pathlib.Path,
) -> None:
    """B3: BrokeredLootRunner.sort traduce VfsJobTimeoutError a LOOTTimeoutError

    copiando terminality_unknown=True y teardown_error.
    """
    mo2, data, challenge, job = _entorno(tmp_path, timeout=0.2)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)

    fake_loot_exe = tmp_path / "Loot.exe"
    fake_loot_exe.write_bytes(b"loot")

    runner = BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        data_root=mo2,
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=fake_loot_exe,
        timeout=1,
        mutation_targets=lambda: (),
        loot_data_path=tmp_path / "AppData" / "Local" / "LOOT",
        loot_data_base=tmp_path / "AppData" / "Local" / "LOOT",
    )

    try:
        sort_task = asyncio.create_task(runner.sort())
        launch = await bridge.recv()
        assert launch["type"] == "launch_worker"

        with pytest.raises(LOOTTimeoutError) as exc_info:
            await asyncio.wait_for(sort_task, timeout=2.0)

        err = exc_info.value
        assert getattr(err, "terminality_unknown", False) is True
        assert isinstance(getattr(err, "teardown_error", None), VfsTeardownDeadlineError)
    finally:
        await bridge.cerrar()
        with contextlib.suppress(Exception):
            await broker.close()


# ==============================================================================
# R4: Cuarentena de instancia ante terminalidad desconocida
# ==============================================================================


@pytest.mark.asyncio
async def test_quarantine_marca_instancia_y_rechaza_nuevos_jobs(tmp_path: pathlib.Path) -> None:
    """Quarantine: Si un job termina con terminalidad desconocida, la instancia

    queda en cuarentena:
    1. Se crea el marcador .{instance_id}.quarantine en disco.
    2. broker.quarantine_reason queda poblado.
    3. Nuevos jobs en la misma instancia fallan con VfsInstanceQuarantinedError.
    4. Un segundo broker en el mismo state_dir rechaza admisiones.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    state_dir = tmp_path / "state"
    broker = _kw_broker(tmp_path, state_dir=state_dir, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    vfs_instance_quarantined_error = getattr(vfs_broker, "VfsInstanceQuarantinedError", None)
    assert vfs_instance_quarantined_error is not None, "VfsInstanceQuarantinedError debe existir"

    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=4444)

        with pytest.raises(VfsTeardownDeadlineError):
            await asyncio.wait_for(sesion.cancel(), timeout=1.0)

        # 1. Marcador persistente en disco
        quarantine_file = state_dir / f".{broker._instance_id}.quarantine"
        assert quarantine_file.exists()
        payload = json.loads(quarantine_file.read_text(encoding="utf-8"))
        assert payload["job_id"] == job.job_id

        # 2. En memoria
        assert getattr(broker, "quarantine_reason", None) is not None

        # 3. Admisión rechazada
        job2 = VfsJob.create(
            instance_id="portable-main",
            profile="Default",
            tool_id="health",
            payload={},
            timeout_seconds=5.0,
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(),
        )
        with pytest.raises(vfs_instance_quarantined_error):
            await broker.open_session(job2, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)

        with pytest.raises(vfs_instance_quarantined_error):
            await broker.submit(job2, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)

        # 4. Segundo broker creado tras restart en el mismo state_dir
        await bridge.cerrar()
        await broker.close()

        broker2 = _kw_broker(tmp_path, state_dir=state_dir, deadline=0.2)
        await broker2.start()
        try:
            assert getattr(broker2, "quarantine_reason", None) is not None
            with pytest.raises(vfs_instance_quarantined_error):
                await broker2.open_session(job2, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)
        finally:
            await broker2.close()

    finally:
        if worker is not None:
            await worker.cerrar()


@pytest.mark.asyncio
async def test_quarantine_liberada_por_operador_con_evidencia(tmp_path: pathlib.Path) -> None:
    """Quarantine: release_quarantine(*, evidence='...') remueve el marcador

    y permite la admisión de nuevos jobs.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    state_dir = tmp_path / "state"
    broker = _kw_broker(tmp_path, state_dir=state_dir, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=4445)
        with pytest.raises(VfsTeardownDeadlineError):
            await sesion.cancel()

        quarantine_file = state_dir / f".{broker._instance_id}.quarantine"
        assert quarantine_file.exists()

        # Operador libera con evidencia
        assert hasattr(broker, "release_quarantine"), "broker debe implementar release_quarantine"
        await broker.release_quarantine(evidence="verificado via Task Manager que el proceso no existe")

        assert not quarantine_file.exists()
        assert getattr(broker, "quarantine_reason", None) is None
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_quarantine_liberada_por_worker_exit_tardio(tmp_path: pathlib.Path) -> None:
    """Quarantine: Si un worker_exit llega tardío para el job en cuarentena,

    la terminalidad queda demostrada y la cuarentena se levanta automáticamente.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    state_dir = tmp_path / "state"
    broker = _kw_broker(tmp_path, state_dir=state_dir, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=4446)
        with pytest.raises(VfsTeardownDeadlineError):
            await sesion.cancel()

        quarantine_file = state_dir / f".{broker._instance_id}.quarantine"
        assert quarantine_file.exists()

        # worker_exit tardío
        await bridge.worker_exit(job.job_id, exit_code=0)
        await asyncio.sleep(0.05)

        assert not quarantine_file.exists()
        assert getattr(broker, "quarantine_reason", None) is None
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ==============================================================================
# R6: Driver completado con fallo en teardown propaga error en cancel()
# ==============================================================================


@pytest.mark.asyncio
async def test_completed_driver_cancel_propaga_teardown_error_y_marca_unknown(
    tmp_path: pathlib.Path,
) -> None:
    """Completed driver: Si el driver de la sesión completó pero el teardown falló

    por deadline, cancel() no debe retornar silenciosamente; debe propagar
    el error y reflejar terminality_unknown=True.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=4447)

        # Worker entrega resultado
        await worker.report_result(success=True, challenge=challenge)

        # Esperamos a que el driver complete el job_result pero el fence de exit_future falle por deadline
        with pytest.raises(VfsTeardownDeadlineError):
            await sesion.result()

        # Ahora el driver terminó con excepción de teardown. Llamar cancel() debe propagar y marcar unknown
        with pytest.raises(VfsTeardownDeadlineError):
            await sesion.cancel()

        assert sesion.terminality_unknown is True
        assert sesion.confirmed_terminal is False
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ==============================================================================
# R7: Validación estricta de bridge_error
# ==============================================================================


@pytest.mark.asyncio
async def test_bridge_error_validacion_estricta(tmp_path: pathlib.Path) -> None:
    """R7: bridge_error con job_id no str o ausente debe levantar VfsBrokerError

    (error de protocolo) en lugar de proceder con None o int.
    """
    broker = _kw_broker(tmp_path)
    with pytest.raises(VfsBrokerError, match="job_id"):
        broker._handle_bridge_message(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "bridge_error",
                "command": "cancel",
                "job_id": 12345,
                "message": "error",
            }
        )


# ==============================================================================
# R8: _cancel_and_join no suprime BaseException indiscriminadamente
# ==============================================================================


@pytest.mark.asyncio
async def test_cancel_and_join_no_traga_base_exception() -> None:
    """R8: _cancel_and_join no debe usar suppress(BaseException).

    Si la tarea tiene una falla como RuntimeError o SystemExit, no debe enmascararse
    silenciosamente.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    fut.set_exception(RuntimeError("error inesperado en join"))

    # Con la versión arreglada, no debe suprimir RuntimeError
    # (o la función solo suprime CancelledError)
    with pytest.raises(RuntimeError):
        await vfs_broker._cancel_and_join(fut)


# ==============================================================================
# F1: _await_worker_exit propaga TypeError interno sin reintento con firma vieja
# ==============================================================================


@pytest.mark.asyncio
async def test_await_worker_exit_propaga_type_error_interno_sin_reintento(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: Si ocurre un TypeError DENTRO de _await_exit_or_bridge_loss, debe propagarse.

    Pre-fix: la llamada estaba envuelta en `except TypeError:` que reintentaba con 2 argumentos,
    confundiendo un error de lógica interno con drift de firma.
    """
    broker = VfsExecutionBroker(instance_id="inst-f1", state_dir=tmp_path)
    loop = asyncio.get_running_loop()
    exit_future: asyncio.Future[int | None] = loop.create_future()

    llamadas = 0

    async def _failing_exit(*args: Any, **kwargs: Any) -> None:
        nonlocal llamadas
        llamadas += 1
        raise TypeError("type error interno de logica")

    monkeypatch.setattr(broker, "_await_exit_or_bridge_loss", _failing_exit)

    with pytest.raises(TypeError, match="type error interno de logica"):
        await broker._await_worker_exit(exit_future, job_id="job-1", deadline=loop.time() + 5.0)

    assert llamadas == 1, f"Se esperaba exactamente 1 llamada, pero hubo {llamadas} (indica reintento)"


# ==============================================================================
# F2: Completed driver con error tipado de timeout y terminalidad desconocida
# ==============================================================================


@pytest.mark.asyncio
async def test_session_cancel_con_driver_terminado_con_timeout_marcado_unknown_falla_tipado() -> None:
    """F2: Si el driver terminó con VfsJobTimeoutError marcado terminality_unknown,

    session.cancel() debe propagar el error tipado y marcar la sesión con terminalidad desconocida,
    en lugar de retornar silenciosamente si _resultado_futuro está done.
    """
    from sky_claw.app.db.rollback_veto import mark_unknown_terminality
    from sky_claw.local.mo2.vfs_contracts import VfsJobResult
    from sky_claw.local.mo2.vfs_session import VfsProcessSession

    loop = asyncio.get_running_loop()
    resultado_fut: asyncio.Future[VfsJobResult] = loop.create_future()
    # Simular que el resultado ya fue recibido antes de la cancelación
    res_fake = VfsJobResult.from_dict(
        {
            "protocol_version": 1,
            "job_id": "job-timeout",
            "success": True,
            "message": "",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "outputs": [],
            "rollback_state": "not_required",
            "attestation": None,
            "tool_result": {},
        }
    )
    resultado_fut.set_result(res_fake)

    event_q: asyncio.Queue[Any] = asyncio.Queue()

    async def _dummy_cancel() -> None:
        pass

    sesion = VfsProcessSession(
        job_id="job-timeout",
        result_future=resultado_fut,
        event_queue=event_q,
        cancelar=_dummy_cancel,
    )

    # Crear driver que falló con VfsJobTimeoutError marcado con terminality_unknown
    exc_timeout = VfsJobTimeoutError("timeout de espera del worker")
    mark_unknown_terminality(exc_timeout)

    driver_task: asyncio.Task[VfsJobResult] = loop.create_task(asyncio.sleep(0))
    await driver_task
    # Convertir driver en done con la excepción
    driver_fut: asyncio.Future[VfsJobResult] = loop.create_future()
    driver_fut.set_exception(exc_timeout)
    sesion._vincular_driver(driver_fut)  # type: ignore[arg-type]

    # Pre-fix: cancel() retorna None silenciosamente porque VfsJobTimeoutError no es VfsTeardownError
    # Post-fix: debe levantar VfsJobTimeoutError, marcar terminality_unknown=True y confirmed_terminal=False
    with pytest.raises(VfsJobTimeoutError, match="timeout de espera del worker"):
        await sesion.cancel()

    assert sesion.terminality_unknown is True
    assert sesion.confirmed_terminal is False


# ==============================================================================
# F3: Clasificación estructurada sin inferencia por substring matching
# ==============================================================================


def test_bridge_error_classification_tipada_sin_substring_matching() -> None:
    """F3: bridge_error_event no debe hacer inferencia por strings en exc.

    - Un error ordinario con la palabra 'termination' NO debe clasificarse como termination_failed.
    - Un BridgeTerminationError tipado SIN la palabra 'termination' SÍ debe clasificarse como termination_failed.
    """
    from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
        BridgeCommandError,
        BridgeJobUnknownError,
        BridgeTerminationError,
        bridge_error_event,
    )

    # 1. Error ordinario que contiene 'termination' en el mensaje
    err_ordinario = BridgeCommandError("argumento termination_mode no soportado")
    event_ord = bridge_error_event(command="cancel", job_id="job-1", exc=err_ordinario)
    assert event_ord["kind"] == "rejected", f"Se esperaba 'rejected', pero dio {event_ord['kind']}"

    # 2. Error tipado de terminación sin la palabra termination en el mensaje
    err_term = BridgeTerminationError("el proceso no cerro en Win32")
    event_term = bridge_error_event(command="cancel", job_id="job-1", exc=err_term)
    assert event_term["kind"] == "termination_failed", (
        f"Se esperaba 'termination_failed', pero dio {event_term['kind']}"
    )

    # 3. Error tipado de job desconocido
    err_unknown = BridgeJobUnknownError("el identificador expiro")
    event_unk = bridge_error_event(command="cancel", job_id="job-1", exc=err_unknown)
    assert event_unk["kind"] == "job_unknown", f"Se esperaba 'job_unknown', pero dio {event_unk['kind']}"


# ==============================================================================
# F4: Carrera bridge_ready / bridge_lost ante deadline
# ==============================================================================


@pytest.mark.asyncio
async def test_race_bridge_ready_desconexion_aplica_contrato_bridge_loss_no_deadline_error(
    tmp_path: pathlib.Path,
) -> None:
    """F4: Si el bridge estaba ready, pero se desconecta antes/durante el deadline,

    debe aplicarse el contrato de pérdida de bridge (bridge-loss contract) y NO
    emitirse incorrectamente VfsTeardownDeadlineError.
    """
    broker = VfsExecutionBroker(instance_id="inst-f4", state_dir=tmp_path, fence_grace_seconds=0.1)
    loop = asyncio.get_running_loop()
    exit_future: asyncio.Future[int | None] = loop.create_future()

    # Bridge inicialmente ready
    broker._bridge_ready.set()
    broker._bridge_lost.clear()

    # En el checkpoint, el bridge se cae
    now = loop.time()
    deadline = now + 0.05

    async def _desconectar_en_checkpoint() -> None:
        await asyncio.sleep(0.01)
        broker._bridge_ready.clear()
        broker._bridge_lost.set()

    task_desc = asyncio.create_task(_desconectar_en_checkpoint())
    try:
        # Ejecutar _await_worker_exit
        await broker._await_worker_exit(
            exit_future,
            job_id="job-race",
            deadline=deadline,
        )
        # Si sigue el contrato de pérdida de bridge: al expirar la gracia sin reconexión,
        # exit_future debe completarse con None (asume muerto por kill-on-close)
        assert exit_future.done()
        assert exit_future.result() is None
    finally:
        task_desc.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task_desc


# ==============================================================================
# Cross-Process Quarantine & Persistence Failure Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_quarantine_cross_process_file_lock_serializa_brokers(tmp_path: pathlib.Path) -> None:
    """Verifica que el file lock interproceso serialice Broker A y Broker B.

    Demuestra que Broker B no puede pasar start() ni leer estado inconsistente
    mientras Broker A retiene el lock. Al liberar A, B adquiere el lock y
    observa la cuarentena de forma determinista.
    """
    state_dir = tmp_path / "vfs_state"

    broker_a = VfsExecutionBroker(instance_id="inst-cross", state_dir=state_dir)
    broker_b = VfsExecutionBroker(instance_id="inst-cross", state_dir=state_dir)

    await broker_a.start()

    # Broker B intenta start() mientras A tiene el lock
    with pytest.raises(VfsBrokerError, match="ya esta poseida por otro broker"):
        await broker_b.start()

    # Broker A pone la instancia en cuarentena
    broker_a._quarantine_instance("job-123", "terminalidad desconocida demostrada")

    # Broker A se cierra
    await broker_a.close()

    # Ahora Broker B sí puede iniciar, y DEBE cargar la cuarentena
    await broker_b.start()
    try:
        assert broker_b.quarantine_reason is not None
        assert "terminalidad desconocida demostrada" in broker_b.quarantine_reason
    finally:
        await broker_b.close()


@pytest.mark.asyncio
async def test_quarantine_persistence_failure_mantiene_instancia_fail_closed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si la persistencia del marcador de cuarentena falla en disco,

    el broker NO debe liberar el lock limpiamente permitiendo que otro broker
    inicie y mutee sin saber que hubo terminalidad desconocida. Debe ser fail-closed.
    """
    state_dir = tmp_path / "vfs_state"

    broker_a = VfsExecutionBroker(instance_id="inst-fail-io", state_dir=state_dir)
    broker_b = VfsExecutionBroker(instance_id="inst-fail-io", state_dir=state_dir)

    await broker_a.start()

    import os

    real_replace = os.replace

    # Inyectar fallo al escribir el archivo de cuarentena
    def _failing_replace(src: Any, dst: Any) -> None:
        if "quarantine" in pathlib.Path(dst).name:
            raise OSError("Fallo simulado de E/S en disco al reemplazar cuarentena")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing_replace)

    broker_a._quarantine_instance("job-failed-io", "terminalidad indeterminada con error de disco")

    # Cerrar broker A
    await broker_a.close()

    from sky_claw.local.mo2.vfs_broker import VfsInstanceQuarantinedError

    # Broker B intenta iniciar: debe fallar o detectar cuarentena fail-closed,
    # NUNCA iniciar como si nada hubiera pasado con quarantine_reason == None
    with pytest.raises((VfsBrokerError, VfsInstanceQuarantinedError)):
        await broker_b.start()
        if broker_b.quarantine_reason is None:
            # Si inició sin razón de cuarentena, violó fail-closed
            raise AssertionError("Broker B inició sin detectar cuarentena tras falla de persistencia!")


@pytest.mark.asyncio
async def test_release_quarantine_tras_falla_de_persistencia_reconcilia_lock_y_permite_rearranque(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker A: Si la cuarentena entró por falla de persistencia (poison lock),

    un release_quarantine autorizado con evidencia debe:
    1. Reconciliar el payload del file lock en disco (quitar quarantined=True).
    2. Limpiar _quarantine_persistence_failed y _quarantine_reason.
    3. Permitir que close() libere el file lock.
    4. Permitir que un segundo broker pueda iniciar limpiamente sin cuarentena.
    """
    state_dir = tmp_path / "vfs_state"
    broker_a = VfsExecutionBroker(instance_id="inst-poison-rel", state_dir=state_dir)
    await broker_a.start()

    import os

    real_replace = os.replace

    def _failing_replace(src: Any, dst: Any) -> None:
        if "quarantine" in pathlib.Path(dst).name:
            raise OSError("Fallo simulado de E/S en disco al reemplazar cuarentena")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing_replace)
    broker_a._quarantine_instance("job-p1", "falla de persistencia")
    assert broker_a._quarantine_persistence_failed is True
    assert broker_a.quarantine_reason is not None

    # Restaurar replace normal
    monkeypatch.setattr(os, "replace", real_replace)

    # Operador autoriza levantamiento con evidencia
    await broker_a.release_quarantine(evidence="verificado por operador que job-p1 murio")

    assert broker_a.quarantine_reason is None
    assert broker_a._quarantine_persistence_failed is False
    assert broker_a._quarantined_job_id is None

    await broker_a.close()

    # Segundo broker debe poder iniciar y NO estar en cuarentena
    broker_b = VfsExecutionBroker(instance_id="inst-poison-rel", state_dir=state_dir)
    await broker_b.start()
    try:
        assert broker_b.quarantine_reason is None
    finally:
        await broker_b.close()


@pytest.mark.asyncio
async def test_release_quarantine_falla_closed_si_cleanup_de_disco_falla(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker A (variante fail-closed): Si durante release_quarantine la

    reconciliación en disco falla (p.ej. OSError al escribir el lock reconciliado),
    release_quarantine DEBE propagar el error y mantener la instancia fail-closed
    (_quarantine_persistence_failed sigue True, razón sigue activa).
    """
    state_dir = tmp_path / "vfs_state"
    broker = VfsExecutionBroker(instance_id="inst-poison-fail-cleanup", state_dir=state_dir)
    await broker.start()

    import os

    real_replace = os.replace

    def _failing_replace(src: Any, dst: Any) -> None:
        if "quarantine" in pathlib.Path(dst).name:
            raise OSError("Fallo simulado de E/S")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing_replace)
    broker._quarantine_instance("job-p2", "falla de persistencia")
    assert broker._quarantine_persistence_failed is True

    # Restaurar replace normal pero provocar error al reconciliar el file lock en disco
    monkeypatch.setattr(os, "replace", real_replace)

    real_write_bytes = pathlib.Path.write_bytes

    def _failing_write_bytes(path_obj: pathlib.Path, data: bytes) -> int:
        if path_obj.name == broker._instance_lock_path.name:
            raise OSError("Fallo de E/S al escribir el lock reconciliado")
        return real_write_bytes(path_obj, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _failing_write_bytes)

    with pytest.raises(OSError, match="Fallo de E/S al escribir el lock"):
        await broker.release_quarantine(evidence="evidencia valida pero disco falla")

    # Debe mantenerse fail-closed
    assert broker._quarantine_persistence_failed is True
    assert broker.quarantine_reason is not None

    # Al cerrar, el lock debe retenerse fail-closed
    monkeypatch.undo()
    await broker.close()
    assert broker._instance_lock_path.exists()


@pytest.mark.asyncio
async def test_worker_exit_de_otro_job_no_libera_cuarentena_sin_marcador(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker B: Si la persistencia del marcador falló (poison lock activo),

    un worker_exit para un job distinto (job-B) NO debe liberar la cuarentena
    del job original (job-A). Solo el worker_exit del job-A puede liberarla,
    y al hacerlo debe reconciliar el lock en disco.
    """
    state_dir = tmp_path / "vfs_state"
    broker = VfsExecutionBroker(instance_id="inst-wrong-job", state_dir=state_dir)
    await broker.start()

    import os

    real_replace = os.replace

    def _failing_replace(src: Any, dst: Any) -> None:
        if "quarantine" in pathlib.Path(dst).name:
            raise OSError("Fallo simulado de E/S")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing_replace)
    broker._quarantine_instance("job-A", "terminalidad indeterminada job-A")
    assert broker.quarantine_reason is not None
    assert broker._quarantine_persistence_failed is True

    # 1. Llega worker_exit para un job-B ajeno:
    broker._liberar_cuarentena_por_worker_exit("job-B")
    # DEBE seguir en cuarentena!
    assert broker.quarantine_reason is not None
    assert broker._quarantined_job_id == "job-A"
    assert broker._quarantine_persistence_failed is True

    # 2. Llega worker_exit para el job-A correcto:
    broker._liberar_cuarentena_por_worker_exit("job-A")
    assert broker.quarantine_reason is None
    assert broker._quarantined_job_id is None
    assert broker._quarantine_persistence_failed is False

    await broker.close()

    # Y tras close, broker_b puede iniciar limpiamente porque job-A reconcilió el lock:
    broker_b = VfsExecutionBroker(instance_id="inst-wrong-job", state_dir=state_dir)
    await broker_b.start()
    try:
        assert broker_b.quarantine_reason is None
    finally:
        await broker_b.close()


@pytest.mark.asyncio
async def test_result_cancel_caller_preserva_cancelled_error_y_anota_teardown_deadline(
    tmp_path: pathlib.Path,
) -> None:
    """Blocker A: Si el caller cancela result() mientras espera el driver, y luego el

    teardown fence falla con VfsTeardownDeadlineError (terminalidad desconocida),
    result() DEBE propagar CancelledError (la cancelación del caller gana),
    pero anotado con terminality_unknown=True, teardown_error y __cause__ preservada.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=8888)

        # El caller espera result()
        result_task = asyncio.create_task(sesion.result())
        await asyncio.sleep(0.05)

        # El caller cancela su tarea
        result_task.cancel()

        # Teardown fence arranca; el bridge recibe cancel pero worker_exit NO llega
        cancel_req = await bridge.recv()
        assert cancel_req["type"] == "cancel"

        # Esperamos a que la tarea termine
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await result_task

        err = exc_info.value
        assert getattr(err, "terminality_unknown", False) is True
        assert isinstance(getattr(err, "teardown_error", None), VfsTeardownDeadlineError)
        assert isinstance(err.__cause__, VfsTeardownDeadlineError)
        assert not sesion.confirmed_terminal
        assert sesion.terminality_unknown is True
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        with contextlib.suppress(Exception):
            await broker.close()


@pytest.mark.asyncio
async def test_result_cancel_caller_con_fence_limpio_propaga_cancelled_error_puro(
    tmp_path: pathlib.Path,
) -> None:
    """Blocker A (variante limpia): Si el caller cancela result() y el teardown

    confirma salida con worker_exit, se propaga CancelledError puro sin
    atributos de terminalidad desconocida ni cause.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=2.0)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=8889)

        result_task = asyncio.create_task(sesion.result())
        await asyncio.sleep(0.05)

        result_task.cancel()

        cancel_req = await bridge.recv()
        assert cancel_req["type"] == "cancel"

        # Worker sale normalmente
        await bridge.worker_exit(job.job_id, exit_code=0)

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await result_task

        err = exc_info.value
        assert getattr(err, "terminality_unknown", False) is False
        assert getattr(err, "teardown_error", None) is None
        assert err.__cause__ is None
        assert sesion.confirmed_terminal is True
        assert sesion.terminality_unknown is False
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        with contextlib.suppress(Exception):
            await broker.close()


@pytest.mark.asyncio
async def test_late_worker_exit_reconciliation_failure_retiene_cuarentena_y_emite_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hardening: Si llega un worker_exit tardío pero la reconciliación en disco

    falla (p.ej. unlink del marcador o reescritura del lock lanza OSError),
    la cuarentena DEBE permanecer activa (fail-closed) y emitirse un log de warning.
    """
    state_dir = tmp_path / "vfs_state"
    broker = VfsExecutionBroker(instance_id="inst-late-fail", state_dir=state_dir)
    await broker.start()

    broker._quarantine_instance("job-late-1", "terminalidad desconocida previa")
    assert broker.quarantine_reason is not None
    assert broker._quarantined_job_id == "job-late-1"

    # Provocar fallo en el unlink del archivo de cuarentena
    real_unlink = pathlib.Path.unlink

    def _failing_unlink(path_obj: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if path_obj.name == broker._quarantine_path.name:
            raise OSError("Fallo simulado de disco al hacer unlink del marcador")
        real_unlink(path_obj, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _failing_unlink)

    import logging

    with caplog.at_level(logging.WARNING):
        broker._liberar_cuarentena_por_worker_exit("job-late-1")

    # Cuarentena DEBE permanecer activa
    assert broker.quarantine_reason is not None
    assert broker._quarantined_job_id == "job-late-1"
    # Log visible emitido
    assert any(
        "la cuarentena permanece activa" in rec.message or "Fallo de filesystem" in rec.message
        for rec in caplog.records
    )

    await broker.close()


@pytest.mark.asyncio
async def test_doble_falla_de_persistencia_de_cuarentena_impide_admision_tras_rearranque(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker B: Si falla TANTO la persistencia del marcador de cuarentena (.quarantine)

    COMO la persistencia del envenenamiento del lock de instancia (.lock),
    un nuevo broker que inicie sobre el mismo state_dir tras la terminación del anterior
    NO debe admitir silenciosamente mutaciones ni nuevas sesiones.
    """
    state_dir = tmp_path / "vfs_state"
    broker_a = VfsExecutionBroker(instance_id="portable-main", state_dir=state_dir)
    await broker_a.start()

    import os

    real_replace = os.replace

    # 1. Inyectar fallo al escribir marcador de cuarentena
    def _failing_replace(src: Any, dst: Any) -> None:
        if "quarantine" in pathlib.Path(dst).name:
            raise OSError("Fallo simulado al reemplazar marcador de cuarentena")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing_replace)

    real_write_bytes = pathlib.Path.write_bytes

    # 2. Inyectar fallo al envenenar el lock de instancia
    def _failing_write_bytes(path_obj: pathlib.Path, data: bytes) -> int:
        if path_obj.name == broker_a._instance_lock_path.name:
            raise OSError("Fallo simulado al escribir lock de instancia")
        return real_write_bytes(path_obj, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _failing_write_bytes)

    # broker_a entra en cuarentena por terminalidad indeterminada
    broker_a._quarantine_instance("job-double-fail", "terminalidad indeterminada con doble falla de disco")
    assert broker_a._quarantine_persistence_failed is True
    assert broker_a.quarantine_reason is not None

    # Cerrar broker_a (retiene el lock en disco fail-closed)
    await broker_a.close()

    # Restaurar operaciones normales de disco para el nuevo broker
    monkeypatch.undo()

    # Simular que el PID del broker_a pertenecía a un proceso previo que murió
    raw_lock = json.loads(broker_a._instance_lock_path.read_text(encoding="utf-8"))
    raw_lock["pid"] = os.getpid() + 10000
    broker_a._instance_lock_path.write_text(json.dumps(raw_lock), encoding="utf-8")

    # Simular que el PID del broker_a ya no existe (stale PID)
    import psutil

    real_process = psutil.Process

    class _DeadProcessMock:
        def __init__(self, pid: int) -> None:
            if pid == os.getpid():
                self._real = real_process(pid)
            else:
                raise psutil.NoSuchProcess(pid)

        def create_time(self) -> float:
            return self._real.create_time()

    monkeypatch.setattr(psutil, "Process", _DeadProcessMock)

    # Iniciar broker_b sobre el mismo state_dir
    broker_b = VfsExecutionBroker(instance_id="portable-main", state_dir=state_dir)
    from sky_claw.local.mo2.vfs_broker import VfsInstanceQuarantinedError

    # El arranque o la admisión DEBE detectar que la instancia no es segura para mutar:
    # No puede admitir silenciosamente mutaciones con quarantine_reason == None
    mo2, data, challenge, job = _entorno(tmp_path)

    try:
        await broker_b.start()
        # Si start() no levantó excepción, la admisión DEBE fallar con VfsInstanceQuarantinedError
        with pytest.raises(VfsInstanceQuarantinedError):
            await broker_b.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)
    except VfsInstanceQuarantinedError:
        pass  # VfsInstanceQuarantinedError en start() es igualmente fail-closed válido
    finally:
        with contextlib.suppress(Exception):
            await broker_b.close()
