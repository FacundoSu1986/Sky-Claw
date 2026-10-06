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
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import pathlib
import time

import pytest

from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODProcess
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import (
    VfsBridgeTerminationError,
    VfsExecutionBroker,
    VfsTeardownDeadlineError,
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

    async def bridge_error(self, job_id: str, *, command: str = "cancel", message: str = "error") -> None:
        await write_authenticated_message(
            self.writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "bridge_error",
                "command": command,
                "job_id": job_id,
                "message": message,
            },
            self.secret,
        )

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
    """Verifica que si el bridge sigue vivo pero worker_exit nunca llega, el teardown

    termina de forma acotada por su deadline interno y levanta VfsTeardownDeadlineError,
    dejando confirmed_terminal en False y terminality_unknown en True.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        fence_grace_seconds=0.2,
    )
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=1234)

        inicio = time.monotonic()
        with pytest.raises(VfsTeardownDeadlineError):
            await asyncio.wait_for(sesion.cancel(), timeout=1.0)
        duracion = time.monotonic() - inicio

        assert duracion < 0.8
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
    """Verifica que si el bridge reporta bridge_error durante cancel, el fence

    despierta de inmediato sin esperar el timeout y levanta VfsBridgeTerminationError,
    demostrando que un error de terminación NO se convierte en worker_exit.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        fence_grace_seconds=5.0,  # Grace largo para demostrar que NO espera el timeout
    )
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=5555)

        cancelacion = asyncio.create_task(sesion.cancel())
        cancel_msg = await bridge.recv()
        assert cancel_msg["type"] == "cancel"

        inicio = time.monotonic()
        # El bridge responde con bridge_error en vez de worker_exit
        await bridge.bridge_error(job.job_id, command="cancel", message="Job object termination failed")

        with pytest.raises(VfsBridgeTerminationError) as exc_info:
            await asyncio.wait_for(cancelacion, timeout=1.0)
        duracion = time.monotonic() - inicio

        assert "Job object termination failed" in str(exc_info.value)
        assert duracion < 0.5  # Despertó casi instantáneo
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
    """Verifica que si la terminalidad es indeterminada, kill_and_reap propaga

    la excepción de teardown y el rollback que exige terminalidad NO arranca.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        fence_grace_seconds=0.2,
    )
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
                # Simula fallo en ejecución que dispara teardown
                raise RuntimeError("falla en runner")
            except Exception:
                # El runner invoca kill_and_reap antes de cualquier rollback
                await kill_and_reap(proc)
                # Si kill_and_reap no lanzó (falso verde), el rollback iniciaría aquí:
                if proc.confirmed_terminal:
                    rollback_ejecutado = True

        with pytest.raises(VfsTeardownDeadlineError):
            await operacion_con_cleanup_y_rollback()

        # El rollback NUNCA debió ejecutarse porque la terminalidad no fue confirmada
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
    """Camino feliz: cancel solicitado y worker_exit recibido a tiempo confirma

    confirmed_terminal == True y terminality_unknown == False.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        fence_grace_seconds=1.0,
    )
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
    """Verifica que si el bridge muere y se desconecta, al agotar la gracia de

    reconexión se aplica el contrato de kill-on-close (asumiendo worker muerto)
    sin levantar VfsTeardownDeadlineError.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        fence_grace_seconds=0.2,
    )
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=1111)

        # Cerramos el bridge (desconexión)
        await bridge.cerrar()
        await asyncio.sleep(0.05)

        # Cancelamos la sesión con el bridge ya desconectado
        # Debe completar normalmente cuando expira la gracia de reconexión
        await asyncio.wait_for(sesion.cancel(), timeout=1.5)

        assert sesion.confirmed_terminal
        assert not sesion.terminality_unknown
    finally:
        if worker is not None:
            await worker.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_cancelacion_repetida_no_reinicia_deadline_ni_pierde_excepcion(tmp_path: pathlib.Path) -> None:
    """Verifica que cancelaciones externas repetidas (cancel #1, cancel #2) no

    reinician el deadline absoluto y preservan la excepción final de teardown.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        fence_grace_seconds=0.3,
    )
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=2222)

        # Iniciamos cancel
        tarea_cancel = asyncio.create_task(sesion.cancel())
        await bridge.recv()

        # Enviamos cancelaciones externas mientras espera
        await asyncio.sleep(0.05)
        tarea_cancel.cancel()
        await asyncio.sleep(0.05)
        tarea_cancel.cancel()

        inicio = time.monotonic()
        with pytest.raises((VfsTeardownDeadlineError, asyncio.CancelledError)):
            await asyncio.wait_for(tarea_cancel, timeout=1.0)
        duracion = time.monotonic() - inicio

        # El deadline absoluto no debió extenderse
        assert duracion < 0.6

        # Llamar cancel() nuevamente después del fallo re-lanza el error de teardown
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
    """Verifica que si un worker_exit llega tarde (después de que el teardown ya

    falló por deadline), se ignora pacíficamente sin corromper el broker.
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        fence_grace_seconds=0.2,
    )
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=3333)

        with pytest.raises(VfsTeardownDeadlineError):
            await asyncio.wait_for(sesion.cancel(), timeout=1.0)

        # El teardown falló por deadline. Ahora el bridge envía worker_exit tardío:
        await bridge.worker_exit(job.job_id, exit_code=0)
        await asyncio.sleep(0.05)

        # El estado de la sesión permanece como no-confirmado y fallido
        assert not sesion.confirmed_terminal
        assert sesion.terminality_unknown

        # El lock de instancia quedó liberado y el broker no quedó bloqueado
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()
