"""Sesión de proceso brokered: PID vivo, liveness, resultado y cancelación (PR-586A).

Estos tests prueban la primitive —no la política de locking— con el harness real
de sockets del broker: un bridge falso y workers falsos que hablan el protocolo
autenticado. El caso end-to-end usa un worker REAL (``run_worker_session``) y un
proceso REAL benigno.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import pathlib
import sys
import time

import psutil
import pytest

from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import (
    VfsBrokerError,
    VfsExecutionBroker,
    VfsJobTimeoutError,
    VfsWorkerDisconnectedError,
)
from sky_claw.local.mo2.vfs_contracts import (
    VFS_PROTOCOL_VERSION,
    VfsJob,
    VfsToolExitEvent,
    VfsToolStartedEvent,
)
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_session import (
    VfsJobCancelledError,
    VfsProcessSession,
    VfsSessionProtocolError,
)
from sky_claw.local.mo2.vfs_worker import (
    VfsProcessSpec,
    VfsToolExecution,
    run_brokered_process,
    run_worker_session,
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


def _entorno_end_to_end(tmp_path: pathlib.Path):
    """Entorno con vista virtual separada del Data físico (el canary vive ahí)."""
    mo2, data, challenge, job = _entorno(tmp_path)
    virtual = tmp_path / "virtual" / "Data"
    virtual.mkdir(parents=True)
    (virtual / "canary.txt").write_bytes(b"canary")
    return mo2, data, virtual, challenge, job


async def _broker(tmp_path: pathlib.Path, **kwargs: object) -> VfsExecutionBroker:
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _path: None,
        **kwargs,  # type: ignore[arg-type]
    )
    await broker.start()
    return broker


def _resultado(
    job_id: str,
    challenge,
    *,
    success: bool = True,
    message: str = "",
    exit_code: int | None = 0,
) -> dict[str, object]:
    attestation = None
    if success:
        attestation = {
            "profile": "Default",
            "source_mod": challenge.source_mod,
            "relative_path": challenge.relative_path.as_posix(),
            "visible_sha256": challenge.sha256,
            "profile_fingerprint": challenge.profile_fingerprint,
            "grandchild_sha256": challenge.sha256,
        }
    return {
        "protocol_version": VFS_PROTOCOL_VERSION,
        "job_id": job_id,
        "success": success,
        "message": message,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": "",
        "outputs": [],
        "rollback_state": "not_required" if success else "pending",
        "attestation": attestation,
        "tool_result": {},
    }


class _BridgeFalso:
    """Bridge autenticado mínimo: recibe launches/cancels y emite worker_exit."""

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

    async def cerrar(self) -> None:
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self.writer.wait_closed()


class _WorkerFalso:
    """Worker autenticado mínimo: emite eventos de sesión y el job_result."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        secret: bytes,
        job_id: str,
    ) -> None:
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

    async def evento(self, nombre: str, **campos: object) -> None:
        job_id = campos.pop("job_id", self.job_id)
        await write_authenticated_message(
            self.writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": nombre,
                "job_id": job_id,
                **campos,
            },
            self.secret,
        )

    async def started(self, pid: int) -> None:
        await self.evento("tool_started", tool_pid=pid)

    async def exited(self, exit_code: int) -> None:
        await self.evento("tool_exit", exit_code=exit_code)

    async def resultado(self, resultado: dict[str, object]) -> None:
        await write_authenticated_message(
            self.writer,
            {"protocol_version": VFS_PROTOCOL_VERSION, "type": "job_result", "result": resultado},
            self.secret,
        )
        ack = await asyncio.wait_for(read_authenticated_message(self.reader, self.secret), timeout=2)
        assert ack["type"] == "job_result_ack"

    async def esperar_cierre(self, *, timeout: float = 3.0) -> None:
        """Espera el cierre del socket por rechazo del broker (EOF del lado server)."""
        try:
            data = await asyncio.wait_for(self.reader.read(1), timeout=timeout)
        except (ConnectionError, OSError):
            return
        if data == b"":
            return
        raise AssertionError("el broker no cerró la conexión del worker")

    async def cerrar(self) -> None:
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self.writer.wait_closed()


async def _abrir_sesion(
    broker: VfsExecutionBroker,
    bridge: _BridgeFalso,
    job: VfsJob,
    challenge,
    mo2: pathlib.Path,
    data: pathlib.Path,
    *,
    pid: int = 4242,
) -> tuple[VfsProcessSession, _WorkerFalso]:
    """Abre una sesión y la deja con ``tool_started`` ya emitido."""
    apertura = asyncio.create_task(broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
    launch = await bridge.recv()
    assert launch["type"] == "launch_worker"
    worker = await _WorkerFalso.conectar(broker, job.job_id)
    await worker.started(pid)
    sesion = await asyncio.wait_for(apertura, timeout=3)
    return sesion, worker


# ---------------------------------------------------------------------------
# PID antes del resultado
# ---------------------------------------------------------------------------


async def test_open_session_entrega_pid_antes_del_resultado(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=7777)

        assert isinstance(sesion, VfsProcessSession)
        assert sesion.job_id == job.job_id
        assert sesion.pid == 7777
        assert sesion.returncode is None, "el job todavía no terminó"
        assert not sesion._driver.done()  # type: ignore[union-attr]

        await worker.exited(0)
        await worker.resultado(_resultado(job.job_id, challenge))
        await bridge.worker_exit(job.job_id)

        resultado = await asyncio.wait_for(sesion.result(), timeout=3)
        assert resultado.success is True
        assert await sesion.result() is resultado, "result() es idempotente"
        assert sesion.returncode == 0
        assert await sesion.wait() == 0
        assert await sesion.wait() == 0, "wait() es idempotente"

        assert job.job_id not in broker._job_event_queues, "la suscripción no se filtra"
        assert job.job_id not in broker._session_drivers
        assert not broker._instance_lock.locked(), "el lock de instancia se libera al terminar"
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ---------------------------------------------------------------------------
# Coherencia tool_exit ↔ resultado
# ---------------------------------------------------------------------------


async def test_tool_exit_incoherente_con_el_resultado_falla_cerrado(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=1001)

        await worker.exited(9)
        await worker.resultado(_resultado(job.job_id, challenge, exit_code=7))
        await bridge.worker_exit(job.job_id)

        with pytest.raises(VfsSessionProtocolError, match="tool_exit"):
            await asyncio.wait_for(sesion.result(), timeout=3)
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


async def test_tool_exit_sin_exit_code_en_el_resultado_no_es_incoherente(tmp_path: pathlib.Path) -> None:
    """La comparación sólo aplica cuando AMBAS señales declaran código."""
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=1002)

        await worker.exited(5)
        await worker.resultado(_resultado(job.job_id, challenge, success=False, message="post-check", exit_code=None))
        await bridge.worker_exit(job.job_id)

        resultado = await asyncio.wait_for(sesion.result(), timeout=3)
        assert resultado.success is False
        assert sesion.returncode is None
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ---------------------------------------------------------------------------
# Event routing por job
# ---------------------------------------------------------------------------


async def test_eventos_se_enrutan_por_job_y_no_se_mezclan(tmp_path: pathlib.Path) -> None:
    broker = await _broker(tmp_path)
    try:
        cola_a: asyncio.Queue[object] = asyncio.Queue(maxsize=8)
        cola_b: asyncio.Queue[object] = asyncio.Queue(maxsize=8)
        broker._job_event_queues["job-a"] = cola_a  # type: ignore[arg-type]
        broker._job_event_queues["job-b"] = cola_b  # type: ignore[arg-type]

        broker._publicar_evento_de_sesion(VfsToolStartedEvent(job_id="job-a", tool_pid=1))
        broker._publicar_evento_de_sesion(VfsToolExitEvent(job_id="job-b", exit_code=0))

        assert cola_a.get_nowait() == VfsToolStartedEvent(job_id="job-a", tool_pid=1)
        assert cola_b.get_nowait() == VfsToolExitEvent(job_id="job-b", exit_code=0)
        assert cola_a.empty() and cola_b.empty()
    finally:
        await broker.close()


async def test_evento_para_un_job_sin_sesion_registrada_es_rechazado(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    pending = None
    try:
        pending = asyncio.create_task(broker.submit(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data))
        launch = await bridge.recv()
        assert launch["type"] == "launch_worker"
        worker = await _WorkerFalso.conectar(broker, job.job_id)

        # Un submit() no tiene sesión: los eventos de sesión no tienen a quién ir.
        await worker.started(99)
        await worker.esperar_cierre()
        await bridge.worker_exit(job.job_id)

        with pytest.raises(VfsWorkerDisconnectedError):
            await asyncio.wait_for(pending, timeout=3)
    finally:
        if worker is not None:
            await worker.cerrar()
        if pending is not None and not pending.done():
            with contextlib.suppress(ConnectionError, OSError):
                await bridge.worker_exit(job.job_id, exit_code=1)
        await bridge.cerrar()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        await broker.close()


async def test_worker_no_puede_inyectar_eventos_atribuidos_a_otro_job(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=55)

        await worker.evento("tool_started", tool_pid=99, job_id="otro-job")
        await worker.esperar_cierre()

        await bridge.worker_exit(job.job_id)
        with pytest.raises(VfsWorkerDisconnectedError):
            await asyncio.wait_for(sesion.result(), timeout=3)
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ---------------------------------------------------------------------------
# Falla antes de tool_started
# ---------------------------------------------------------------------------


async def test_open_session_falla_con_la_causa_si_el_worker_muere_antes_de_tool_started(
    tmp_path: pathlib.Path,
) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        apertura = asyncio.create_task(
            broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)
        )
        launch = await bridge.recv()
        assert launch["type"] == "launch_worker"
        worker = await _WorkerFalso.conectar(broker, job.job_id)
        await worker.resultado(
            _resultado(
                job.job_id, challenge, success=False, message="attestation VFS falló: canary no visible", exit_code=None
            )
        )
        await bridge.worker_exit(job.job_id)

        with pytest.raises(VfsSessionProtocolError, match="attestation VFS falló"):
            await asyncio.wait_for(apertura, timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


async def test_apertura_fallida_tras_el_manifiesto_no_deja_artefactos(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un fallo entre el manifiesto y el driver no puede dejar tracking ni manifiesto.

    Sin driver no hay ``finally`` que limpie: esa ruta tiene que descartar el
    archivo firmado y los registros por su cuenta (hallazgo de review).
    """
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    try:
        original = broker._escribir_manifiesto

        async def fallar_despues_de_escribir(*args: object, **kwargs: object) -> pathlib.Path:
            await original(*args, **kwargs)  # type: ignore[arg-type]
            raise VfsBrokerError("fallo simulado post-manifiesto")

        monkeypatch.setattr(broker, "_escribir_manifiesto", fallar_despues_de_escribir)
        with pytest.raises(VfsBrokerError, match="post-manifiesto"):
            await broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)

        assert not broker._instance_lock.locked()
        assert job.job_id not in broker._pending
        assert job.job_id not in broker._worker_exit
        assert job.job_id not in broker._job_event_queues
        assert not (broker._jobs_dir / f"{job.job_id}.json").exists()
    finally:
        await bridge.cerrar()
        await broker.close()


async def test_apertura_fallida_sin_bridge_libera_el_lock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falla antes de registrar nada: el lock de instancia no queda tomado."""
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)

    async def sin_bridge(*_args: object, **_kwargs: object) -> None:
        raise VfsBrokerError("MO2 bridge no se conectó al broker")

    monkeypatch.setattr(broker, "wait_until_ready", sin_bridge)
    try:
        with pytest.raises(VfsBrokerError, match="no se conectó"):
            await broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)

        assert not broker._instance_lock.locked()
        assert job.job_id not in broker._pending
        assert job.job_id not in broker._worker_exit
        assert not (broker._jobs_dir / f"{job.job_id}.json").exists()
    finally:
        await broker.close()


async def test_open_session_falla_cerrado_si_el_worker_muere_sin_resultado(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    try:
        apertura = asyncio.create_task(
            broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)
        )
        launch = await bridge.recv()
        assert launch["type"] == "launch_worker"
        await bridge.worker_exit(job.job_id, exit_code=70)

        with pytest.raises(VfsWorkerDisconnectedError):
            await asyncio.wait_for(apertura, timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        await bridge.cerrar()
        await broker.close()


# ---------------------------------------------------------------------------
# Cancelación
# ---------------------------------------------------------------------------


async def test_cancel_detiene_el_job_y_espera_el_fence_de_worker_exit(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=6060)

        cancelacion = asyncio.create_task(sesion.cancel())
        cancel = await bridge.recv(timeout=4)
        assert cancel == {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "cancel",
            "job_id": job.job_id,
        }
        await asyncio.sleep(0.05)
        assert not cancelacion.done(), "cancel no puede resolverse antes del worker_exit causal"

        await bridge.worker_exit(job.job_id, exit_code=1)
        await asyncio.wait_for(cancelacion, timeout=3)

        with pytest.raises(VfsJobCancelledError):
            await asyncio.wait_for(sesion.result(), timeout=3)
        assert sesion.returncode is None
        await asyncio.wait_for(sesion.cancel(), timeout=3)  # cancel repetido es idempotente
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


async def test_cancelar_la_apertura_antes_de_tool_started_teardown_sin_huerfanos(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    try:
        apertura = asyncio.create_task(
            broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)
        )
        launch = await bridge.recv()
        assert launch["type"] == "launch_worker"

        apertura.cancel()
        cancel = await bridge.recv(timeout=4)
        assert cancel["type"] == "cancel"
        await asyncio.sleep(0.05)
        assert not apertura.done()

        await bridge.worker_exit(job.job_id, exit_code=1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(apertura, timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        await bridge.cerrar()
        await broker.close()


async def test_cancelar_al_que_espera_el_resultado_teardown_sin_huerfanos(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=9090)

        espera = asyncio.create_task(sesion.result())
        await asyncio.sleep(0.05)
        espera.cancel()

        cancel = await bridge.recv(timeout=4)
        assert cancel["type"] == "cancel"
        await bridge.worker_exit(job.job_id, exit_code=1)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(espera, timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ---------------------------------------------------------------------------
# Timeout y cierre del broker
# ---------------------------------------------------------------------------


async def test_timeout_de_la_sesion_cancela_y_resuelve_con_causa(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path, timeout=0.2)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=3030)

        cancel = await bridge.recv(timeout=4)
        assert cancel["type"] == "cancel"
        await asyncio.sleep(0.05)

        await bridge.worker_exit(job.job_id, exit_code=1)
        with pytest.raises(VfsJobTimeoutError):
            await asyncio.wait_for(sesion.result(), timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


async def test_close_durante_la_sesion_resuelve_la_sesion_y_libera_el_lock(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=7070)

        cierre = asyncio.create_task(broker.close())
        cancel = await bridge.recv(timeout=4)
        assert cancel["type"] == "cancel"
        await bridge.worker_exit(job.job_id, exit_code=1)
        await asyncio.wait_for(cierre, timeout=4)

        with pytest.raises(VfsBrokerError):
            await asyncio.wait_for(sesion.result(), timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        if broker._server is not None:
            await broker.close()


async def test_worker_exit_sin_resultado_durante_la_sesion_falla_cerrado(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=8080)

        await bridge.worker_exit(job.job_id, exit_code=70)
        with pytest.raises(VfsWorkerDisconnectedError):
            await asyncio.wait_for(sesion.result(), timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ---------------------------------------------------------------------------
# Secuencias incoherentes de eventos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "eventos_extra",
    [
        pytest.param([("tool_started", {"tool_pid": 2})], id="segundo-started-otro-pid"),
        pytest.param([("tool_started", {"tool_pid": 4242})], id="segundo-started-mismo-pid"),
        pytest.param([("tool_exit", {"exit_code": 0}), ("tool_exit", {"exit_code": 0})], id="exit-duplicado"),
    ],
)
async def test_secuencias_incoherentes_de_eventos_fallan_cerrado(
    tmp_path: pathlib.Path,
    eventos_extra: list[tuple[str, dict[str, object]]],
) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=4242)

        for nombre, campos in eventos_extra:
            await worker.evento(nombre, **campos)
        await bridge.worker_exit(job.job_id)

        with pytest.raises(VfsSessionProtocolError):
            await asyncio.wait_for(sesion.result(), timeout=3)
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


async def test_tool_exit_antes_de_tool_started_falla_cerrado(tmp_path: pathlib.Path) -> None:
    mo2, data, challenge, job = _entorno(tmp_path)
    broker = await _broker(tmp_path)
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        apertura = asyncio.create_task(
            broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=data)
        )
        launch = await bridge.recv()
        assert launch["type"] == "launch_worker"
        worker = await _WorkerFalso.conectar(broker, job.job_id)

        await worker.exited(3)
        await bridge.worker_exit(job.job_id)

        with pytest.raises(VfsSessionProtocolError, match="tool_exit"):
            await asyncio.wait_for(apertura, timeout=3)
        assert not broker._instance_lock.locked()
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


# ---------------------------------------------------------------------------
# End-to-end: worker real + proceso real bajo el protocolo completo
# ---------------------------------------------------------------------------


async def _lanzar_worker_end_to_end(
    broker: VfsExecutionBroker,
    *,
    manifest_path: pathlib.Path,
    job_id: str,
    comando: str,
) -> asyncio.Task:
    """Arranca un worker REAL con un handler de sesión que corre un proceso real."""

    async def handler_sesion(manifest, event_sink) -> VfsToolExecution:
        resultado = await run_brokered_process(
            VfsProcessSpec(executable=pathlib.Path(sys.executable), arguments=("-c", comando)),
            event_sink=event_sink,
        )
        return VfsToolExecution(
            success=True,
            message="",
            exit_code=resultado.exit_code,
            stdout=resultado.stdout,
            stderr=resultado.stderr,
            outputs=(),
        )

    async def probar(path: pathlib.Path, sha256: str, _timeout: float) -> str:
        assert path.is_file()
        return sha256

    return asyncio.create_task(
        run_worker_session(
            manifest_path=manifest_path,
            descriptor_path=broker.descriptor_path,
            expected_job_id=job_id,
            session_handlers={"health": handler_sesion},  # type: ignore[dict-item]
            grandchild_probe=probar,
        )
    )


async def test_sesion_end_to_end_con_worker_real_y_proceso_real(tmp_path: pathlib.Path) -> None:
    mo2, _data, virtual, challenge, job = _entorno_end_to_end(tmp_path)
    broker = await _broker(tmp_path, fence_grace_seconds=0.3)
    bridge = await _BridgeFalso.conectar(broker)
    worker_task = None
    try:
        apertura = asyncio.create_task(
            broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=virtual)
        )
        launch = await bridge.recv()
        worker_task = await _lanzar_worker_end_to_end(
            broker,
            manifest_path=pathlib.Path(str(launch["manifest_path"])),
            job_id=job.job_id,
            comando="print('lods')",
        )

        sesion = await asyncio.wait_for(apertura, timeout=5)
        assert sesion.pid > 0

        await asyncio.wait_for(worker_task, timeout=5)
        await bridge.worker_exit(job.job_id)

        resultado = await asyncio.wait_for(sesion.result(), timeout=5)
        assert resultado.success is True
        assert resultado.exit_code == 0
        assert resultado.stdout.strip() == "lods"
        assert sesion.returncode == 0
    finally:
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await bridge.cerrar()
        await broker.close()


async def test_sesion_end_to_end_cancel_mata_el_proceso_real(tmp_path: pathlib.Path) -> None:
    mo2, _data, virtual, challenge, job = _entorno_end_to_end(tmp_path)
    broker = await _broker(tmp_path, fence_grace_seconds=0.3)
    bridge = await _BridgeFalso.conectar(broker)
    worker_task = None
    try:
        apertura = asyncio.create_task(
            broker.open_session(job, challenge=challenge, mo2_root=mo2, virtual_data_dir=virtual)
        )
        launch = await bridge.recv()
        worker_task = await _lanzar_worker_end_to_end(
            broker,
            manifest_path=pathlib.Path(str(launch["manifest_path"])),
            job_id=job.job_id,
            comando="import time; time.sleep(60)",
        )

        sesion = await asyncio.wait_for(apertura, timeout=5)
        pid = sesion.pid
        assert psutil.pid_exists(pid)

        cancelacion = asyncio.create_task(sesion.cancel())
        # El worker real cierra su socket al cancelarse; el bridge falso confirma
        # el worker_exit cuando el proceso del worker terminó.
        await asyncio.wait_for(worker_task, timeout=5)
        await bridge.worker_exit(job.job_id, exit_code=2)
        await asyncio.wait_for(cancelacion, timeout=5)

        with pytest.raises(VfsJobCancelledError):
            await asyncio.wait_for(sesion.result(), timeout=3)

        inicio = time.monotonic()
        while psutil.pid_exists(pid) and time.monotonic() - inicio < 5:
            await asyncio.sleep(0.05)
        assert not psutil.pid_exists(pid), "el proceso del tool debe morir con la cancelación"
    finally:
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await bridge.cerrar()
        await broker.close()
