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
        await asyncio.sleep(0.05)

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
