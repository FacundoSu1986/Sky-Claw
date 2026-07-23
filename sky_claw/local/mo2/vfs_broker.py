"""Broker asíncrono entre el daemon Sky-Claw y el bridge cargado por MO2."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import psutil

from sky_claw.antigravity.security.file_permissions import restrict_to_owner
from sky_claw.antigravity.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.mo2.vfs_attestation import VfsAttestationChallenge
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob, VfsJobResult
from sky_claw.local.mo2.vfs_ipc import (
    VfsFrameError,
    read_authenticated_message,
    write_authenticated_message,
)
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest, write_worker_manifest

logger = logging.getLogger(__name__)

_BRIDGE_CONNECT_TIMEOUT = 10.0
_DESCRIPTOR_TTL_SECONDS = 24 * 60 * 60
_COOPERATIVE_CANCEL_GRACE_SECONDS = 0.5
# Si el bridge (MO2) se desconecta con un job en vuelo, el fence terminal espera
# su reconexión para recibir el worker_exit; pasada esta ventana sin reconectar
# se asume el worker muerto (el Job Object es kill-on-close: un MO2 caído mata al
# worker) para que el fence NUNCA cuelgue indefinidamente reteniendo el lock.
_BRIDGE_LOSS_FENCE_GRACE_SECONDS = 30.0
# La cola de eventos de lifecycle no tiene consumidor obligatorio; se acota para
# que no crezca sin límite durante la vida del daemon (drop-oldest).
_MAX_BUFFERED_EVENTS = 256


async def _cancel_and_join(task: asyncio.Future[Any]) -> None:
    """Cancela una future auxiliar del fence y absorbe su ``CancelledError``."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def vfs_instance_id(mo2_root: pathlib.Path) -> str:
    """Deriva un identificador estable sin filtrar la ruta local por IPC/logs."""
    normalized = os.path.normcase(str(mo2_root.resolve())).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"mo2-{digest}"


class VfsBrokerError(RuntimeError):
    """Error de lifecycle o protocolo del broker VFS."""


class VfsBridgeDisconnectedError(VfsBrokerError):
    """El bridge MO2 se desconectó con trabajos pendientes."""


class VfsBridgeLaunchError(VfsBrokerError):
    """MO2 rechazó el launch antes de iniciar un worker utilizable."""


class VfsJobTimeoutError(VfsBrokerError):
    """El worker no reportó resultado antes del timeout del job."""


class VfsWorkerDisconnectedError(VfsBrokerError):
    """El worker termino sin entregar un VfsJobResult canonico."""


class VfsResultValidationError(VfsBrokerError):
    """El resultado no corresponde al job y attestation que autorizó el daemon."""


class VfsExecutionBroker:
    """Servidor loopback autenticado y serializado por instancia de MO2."""

    def __init__(
        self,
        *,
        instance_id: str,
        state_dir: pathlib.Path,
        secret: bytes | None = None,
        descriptor_hardener: Callable[[pathlib.Path], None] = restrict_to_owner,
        fence_grace_seconds: float = _BRIDGE_LOSS_FENCE_GRACE_SECONDS,
    ) -> None:
        try:
            self._instance_id = assert_safe_component(instance_id, field="instance_id")
        except PathViolationError as exc:
            raise VfsBrokerError(str(exc)) from exc
        self._state_dir = state_dir.resolve()
        self._jobs_dir = self._state_dir / "jobs"
        self._descriptor_path = self._state_dir / f"{self._instance_id}.json"
        self._instance_lock_path = self._state_dir / f".{self._instance_id}.lock"
        self._secret = secret or secrets.token_bytes(32)
        if len(self._secret) < 32:
            raise VfsBrokerError("el secreto del broker debe tener al menos 32 bytes")
        self._hardener = descriptor_hardener
        self._fence_grace = fence_grace_seconds
        self._session_id = str(uuid.uuid4())
        self._server: asyncio.AbstractServer | None = None
        self._bridge_writer: asyncio.StreamWriter | None = None
        self._bridge_task: asyncio.Task[None] | None = None
        self._worker_writers: set[asyncio.StreamWriter] = set()
        self._worker_by_job: dict[str, asyncio.StreamWriter] = {}
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._bridge_ready = asyncio.Event()
        # Se activa cuando el bridge se desconecta y se limpia al (re)conectar;
        # el fence terminal lo usa para acotar la espera de worker_exit.
        self._bridge_lost = asyncio.Event()
        self._instance_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[VfsJobResult]] = {}
        self._pending_context: dict[str, tuple[VfsJob, VfsAttestationChallenge]] = {}
        self._worker_exit: dict[str, asyncio.Future[int | None]] = {}
        self._termination_tasks: dict[str, asyncio.Task[None]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_BUFFERED_EVENTS)
        self._closing = False
        self._owns_instance_file_lock = False

    @property
    def descriptor_path(self) -> pathlib.Path:
        return self._descriptor_path

    async def start(self) -> None:
        """Publica una sesión nueva; es idempotente mientras siga activa."""
        if self._server is not None:
            return
        self._closing = False
        await asyncio.to_thread(self._acquire_instance_file_lock)
        try:
            server = await asyncio.start_server(self._handle_connection, "127.0.0.1", 0)
            self._server = server
            socket = server.sockets[0]
            port = int(socket.getsockname()[1])
            await asyncio.to_thread(self._write_descriptor, port)
        except BaseException:
            active_server = self._server
            if active_server is not None:
                active_server.close()
                await active_server.wait_closed()
            self._server = None
            await asyncio.to_thread(self._release_instance_file_lock)
            raise

    def _acquire_instance_file_lock(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self._instance_lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError as exc:
                if self._instance_lock_owner_alive():
                    raise VfsBrokerError(f"la instancia {self._instance_id} ya esta poseida por otro broker") from exc
                with contextlib.suppress(FileNotFoundError):
                    self._instance_lock_path.unlink()
                continue
            try:
                # create_time del dueño: identidad estable contra reuso de PID del SO,
                # verificada en _instance_lock_owner_alive (mismo criterio que vfs.py #302).
                try:
                    own_create_time: float | None = psutil.Process(os.getpid()).create_time()
                except psutil.Error:
                    own_create_time = None
                payload = json.dumps(
                    {
                        "pid": os.getpid(),
                        "session_id": self._session_id,
                        "create_time": own_create_time,
                    },
                    sort_keys=True,
                ).encode("utf-8")
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
            try:
                self._hardener(self._instance_lock_path)
            except BaseException:
                self._instance_lock_path.unlink(missing_ok=True)
                raise
            self._owns_instance_file_lock = True
            return
        raise VfsBrokerError(f"no se pudo reclamar el lock de la instancia {self._instance_id}")

    def _instance_lock_owner_alive(self) -> bool:
        try:
            raw = json.loads(self._instance_lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return True
        pid = raw.get("pid") if isinstance(raw, dict) else None
        if type(pid) is not int or pid <= 0:
            return True
        if pid == os.getpid():
            return True
        expected_create_time = raw.get("create_time") if isinstance(raw, dict) else None
        # Liveness vía psutil + create_time, NO os.kill(pid, 0): en Windows os.kill
        # sobre un PID reciclado por un proceso protegido del SO lanza un SystemError
        # irrecuperable (OSError con excepción C sin traducir), y un PID simplemente
        # reusado se leería como "vivo" y el lock jamás se reclamaría (deadlock de
        # arranque tras un crash del broker). create_time da la identidad estable del
        # proceso, mismo patrón que vfs.MO2Controller._kill_process_tree (review #302).
        try:
            create_time = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            return False  # el dueño murió: PID libre → lock reclamable
        except psutil.Error:
            return True  # no verificable (AccessDenied/…): conservador, no robar el lock
        # Vivo salvo que el create_time registrado no coincida (PID reusado por otro
        # proceso ⇒ el dueño original ya murió). Un lock antiguo sin create_time
        # (None) no dispara la reclamación: se trata como vivo (conservador).
        return not (isinstance(expected_create_time, (int, float)) and create_time != expected_create_time)

    def _release_instance_file_lock(self) -> None:
        if not self._owns_instance_file_lock:
            return
        try:
            raw = json.loads(self._instance_lock_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("session_id") != self._session_id:
                logger.warning("El lock de instancia cambio de owner; no se elimina")
                return
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("No se pudo verificar el owner del lock de instancia", exc_info=True)
            return
        finally:
            self._owns_instance_file_lock = False
        self._instance_lock_path.unlink(missing_ok=True)

    def _write_descriptor(self, port: int) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._hardener(self._state_dir)
        self._hardener(self._jobs_dir)
        descriptor = {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "host": "127.0.0.1",
            "port": port,
            "token": base64.urlsafe_b64encode(self._secret).decode("ascii"),
            "instance_id": self._instance_id,
            "session_id": self._session_id,
            "jobs_root": str(self._jobs_dir),
            "expires_at": time.time() + _DESCRIPTOR_TTL_SECONDS,
        }
        tmp = self._descriptor_path.with_name(f".{self._descriptor_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(descriptor, sort_keys=True), encoding="utf-8")
            self._hardener(tmp)
            os.replace(tmp, self._descriptor_path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    async def wait_until_ready(self, *, timeout: float = _BRIDGE_CONNECT_TIMEOUT) -> None:
        try:
            await asyncio.wait_for(self._bridge_ready.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise VfsBrokerError("MO2 bridge no se conectó al broker") from exc

    async def submit(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        mo2_root: pathlib.Path,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> VfsJobResult:
        """Serializa, lanza y espera un único job para esta instancia."""
        if self._server is None:
            raise VfsBrokerError("el broker no está iniciado")
        if job.instance_id != self._instance_id:
            raise VfsBrokerError("el job apunta a otra instancia MO2")
        if job.profile != challenge.profile or job.expected_fingerprint != challenge.profile_fingerprint:
            raise VfsBrokerError("job y attestation no comparten perfil/fingerprint")

        async with self._instance_lock:
            await self.wait_until_ready()
            manifest_path = self._jobs_dir / f"{job.job_id}.json"
            manifest = VfsWorkerManifest(
                protocol_version=VFS_PROTOCOL_VERSION,
                job=job,
                challenge=challenge,
                mo2_root=mo2_root.resolve(),
                virtual_data_dir=virtual_data_dir.resolve(),
                descriptor_path=self._descriptor_path,
            )
            await asyncio.to_thread(
                write_worker_manifest,
                manifest_path,
                manifest,
                secret=self._secret,
                hardener=self._hardener,
            )
            loop = asyncio.get_running_loop()
            result_future: asyncio.Future[VfsJobResult] = loop.create_future()
            exit_future: asyncio.Future[int | None] = loop.create_future()
            self._pending[job.job_id] = result_future
            self._pending_context[job.job_id] = (job, challenge)
            self._worker_exit[job.job_id] = exit_future
            try:
                await self._send_bridge(
                    {
                        "protocol_version": VFS_PROTOCOL_VERSION,
                        "type": "launch_worker",
                        "job_id": job.job_id,
                        "profile": job.profile,
                        "manifest_path": str(manifest_path),
                        "overwrite_mod": overwrite_mod,
                    }
                )
                try:
                    return await self._await_job_completion(
                        result_future,
                        exit_future,
                        timeout=job.timeout_seconds,
                    )
                except TimeoutError as exc:
                    await self._send_cancel(job.job_id)
                    raise VfsJobTimeoutError(f"job {job.job_id} excedió {job.timeout_seconds:g}s") from exc
                except asyncio.CancelledError:
                    await self._send_cancel(job.job_id)
                    raise
                except Exception:
                    # Un resultado inválido o un fallo de lifecycle tampoco
                    # habilita rollback mientras el árbol siga ejecutándose.
                    await self._await_worker_exit(exit_future)
                    raise
            finally:
                self._pending.pop(job.job_id, None)
                self._pending_context.pop(job.job_id, None)
                self._worker_exit.pop(job.job_id, None)
                self._termination_tasks.pop(job.job_id, None)
                if not result_future.done():
                    result_future.cancel()
                await asyncio.to_thread(manifest_path.unlink, missing_ok=True)

    async def _send_cancel(self, job_id: str) -> None:
        termination = self._termination_tasks.get(job_id)
        if termination is None:
            termination = asyncio.create_task(
                self._request_termination_and_wait(job_id),
                name=f"vfs-terminate-{job_id}",
            )
            self._termination_tasks[job_id] = termination
        cancelled_again = False
        while not termination.done():
            try:
                await asyncio.shield(termination)
            except asyncio.CancelledError:
                # La cancelación externa no puede interrumpir el fence que
                # protege rollback. Se propaga únicamente después de worker_exit.
                cancelled_again = True
        termination.result()
        if cancelled_again:
            raise asyncio.CancelledError

    async def _await_job_completion(
        self,
        result_future: asyncio.Future[VfsJobResult],
        exit_future: asyncio.Future[int | None],
        *,
        timeout: float,
    ) -> VfsJobResult:
        result = await asyncio.wait_for(asyncio.shield(result_future), timeout=timeout)
        # Fence acotado que SÍ propaga la cancelación: si el caller cancela acá,
        # submit la enruta por _send_cancel (que corre su propio fence protegido
        # y avisa al bridge). La gracia de bridge muerto igual acota la espera.
        deadline = asyncio.get_running_loop().time() + self._fence_grace
        while not exit_future.done():
            await self._await_exit_or_bridge_loss(exit_future, deadline)
        exit_future.result()
        return result

    async def _await_worker_exit(self, exit_future: asyncio.Future[int | None]) -> None:
        """Espera el ``worker_exit`` del bridge sin poder colgar para siempre.

        Resiste la cancelación externa —rollback no puede empezar antes de la
        confirmación terminal— pero si el bridge se desconecta y no reconecta
        dentro de ``_fence_grace`` segundos, resuelve el fence asumiendo el
        worker muerto. El Job Object es kill-on-close: si MO2 (el bridge) murió,
        el worker murió con él, así que romper el fence acá evita la inanición
        indefinida del lock ``load-order`` que sostiene el caller.

        La ventana de gracia es un deadline ABSOLUTO fijado antes del loop: una
        ``CancelledError`` absorbida no lo reinicia, así que ni siquiera
        cancelaciones repetidas extienden el fence más allá de ``_fence_grace``
        (review CodeRabbit PR #352).
        """
        cancelled = False
        deadline = asyncio.get_running_loop().time() + self._fence_grace
        while not exit_future.done():
            try:
                await self._await_exit_or_bridge_loss(exit_future, deadline)
            except asyncio.CancelledError:
                cancelled = True
        exit_future.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _await_exit_or_bridge_loss(self, exit_future: asyncio.Future[int | None], deadline: float) -> None:
        """Una espera acotada: worker_exit, o pérdida terminal del bridge.

        ``deadline`` es el instante absoluto (loop clock) tras el cual, si el
        bridge sigue caído, se asume el worker muerto. Sólo acota la espera
        mientras el bridge está desconectado; con el bridge vivo se espera el
        ``worker_exit`` sin tope.
        """
        exit_wait: asyncio.Future[Any] = asyncio.ensure_future(asyncio.shield(exit_future))
        try:
            if self._bridge_ready.is_set():
                # Bridge vivo: despertar ante worker_exit o ante su desconexión.
                lost_wait: asyncio.Future[Any] = asyncio.ensure_future(self._bridge_lost.wait())
                try:
                    await asyncio.wait({exit_wait, lost_wait}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    await _cancel_and_join(lost_wait)
                return
            # Bridge caído: sólo el tiempo que reste hasta el deadline absoluto.
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if not exit_future.done():
                    logger.warning(
                        "El bridge MO2 no reconectó en %.1fs; se asume el worker muerto para liberar el fence",
                        self._fence_grace,
                    )
                    exit_future.set_result(None)
                return
            ready_wait: asyncio.Future[Any] = asyncio.ensure_future(self._bridge_ready.wait())
            grace: asyncio.Future[Any] = asyncio.ensure_future(asyncio.sleep(remaining))
            try:
                await asyncio.wait({exit_wait, ready_wait, grace}, return_when=asyncio.FIRST_COMPLETED)
                if grace.done() and not ready_wait.done() and not exit_future.done():
                    logger.warning(
                        "El bridge MO2 no reconectó en %.1fs; se asume el worker muerto para liberar el fence",
                        self._fence_grace,
                    )
                    exit_future.set_result(None)
            finally:
                await _cancel_and_join(ready_wait)
                await _cancel_and_join(grace)
        finally:
            await _cancel_and_join(exit_wait)

    async def _request_termination_and_wait(self, job_id: str) -> None:
        message = {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "cancel",
            "job_id": job_id,
        }
        worker = self._worker_by_job.get(job_id)
        if worker is not None and not worker.is_closing():
            try:
                async with self._write_lock:
                    await write_authenticated_message(worker, message, self._secret)
                deadline = asyncio.get_running_loop().time() + _COOPERATIVE_CANCEL_GRACE_SECONDS
                while job_id in self._worker_by_job and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.025)
            except (ConnectionError, OSError, VfsFrameError):
                logger.warning("No se pudo entregar cancel al worker %s", job_id, exc_info=True)
        try:
            # El bridge termina el Job Object despues de la ventana cooperativa.
            await self._send_bridge(message)
        except (VfsBrokerError, ConnectionError, OSError):
            logger.warning("No se pudo entregar cancel para job %s", job_id, exc_info=True)
        exit_future = self._worker_exit.get(job_id)
        if exit_future is None:
            raise VfsBrokerError(f"no existe tracking terminal para job {job_id}")
        # No se permite rollback ni liberación del lock hasta que el monitor del
        # bridge confirme que el Job Object completo dejó de ejecutar — o hasta
        # que se agote la gracia de reconexión si el bridge murió (fence acotado).
        await self._await_worker_exit(exit_future)

    async def _send_bridge(self, message: Mapping[str, object]) -> None:
        writer = self._bridge_writer
        if writer is None or writer.is_closing():
            raise VfsBridgeDisconnectedError("MO2 bridge no está conectado")
        async with self._write_lock:
            try:
                await write_authenticated_message(writer, message, self._secret)
            except (ConnectionError, OSError, VfsFrameError) as exc:
                raise VfsBridgeDisconnectedError("falló el envío al MO2 bridge") from exc

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._client_tasks.add(current)
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or peer[0] not in ("127.0.0.1", "::1"):
            writer.close()
            await writer.wait_closed()
            return
        worker_job_id: str | None = None
        try:
            hello = await asyncio.wait_for(
                read_authenticated_message(reader, self._secret),
                timeout=5,
            )
            role, worker_job_id = self._validate_hello(hello)
            if role == "bridge":
                if self._bridge_writer is not None and not self._bridge_writer.is_closing():
                    raise VfsBrokerError("ya existe un bridge conectado para la instancia")
                self._bridge_writer = writer
                self._bridge_task = current
            else:
                assert worker_job_id is not None
                self._worker_writers.add(writer)
                self._worker_by_job[worker_job_id] = writer
            await write_authenticated_message(
                writer,
                {
                    "protocol_version": VFS_PROTOCOL_VERSION,
                    "type": "hello_ack",
                    "session_id": self._session_id,
                },
                self._secret,
            )
            if role == "bridge":
                self._bridge_ready.set()
                self._bridge_lost.clear()
                while not self._closing:
                    message = await read_authenticated_message(reader, self._secret)
                    self._handle_bridge_message(message)
            else:
                assert worker_job_id is not None
                while not self._closing:
                    message = await read_authenticated_message(reader, self._secret)
                    if self._handle_worker_message(message, expected_job_id=worker_job_id):
                        async with self._write_lock:
                            await write_authenticated_message(
                                writer,
                                {
                                    "protocol_version": VFS_PROTOCOL_VERSION,
                                    "type": "job_result_ack",
                                    "job_id": worker_job_id,
                                },
                                self._secret,
                            )
                        break
        except (TimeoutError, VfsFrameError, VfsBrokerError, ConnectionError, OSError) as exc:
            if not self._closing:
                logger.warning("Conexión del MO2 bridge terminada: %s", exc)
        finally:
            if self._bridge_writer is writer:
                self._bridge_writer = None
                self._bridge_task = None
                self._bridge_ready.clear()
                self._bridge_lost.set()
                # Una desconexión transitoria NO invalida jobs en vuelo: el
                # resultado llega por el socket del worker (independiente) y el
                # bridge reconecta reenviando el worker_exit. Sólo el cierre real
                # del broker (_closing) falla los pendientes; el fence acotado
                # (_await_worker_exit) cubre el caso de bridge muerto sin retorno.
                if self._closing:
                    self._fail_pending(VfsBridgeDisconnectedError("MO2 bridge desconectado"))
            self._worker_writers.discard(writer)
            if worker_job_id is not None and self._worker_by_job.get(worker_job_id) is writer:
                self._worker_by_job.pop(worker_job_id, None)
            if current is not None:
                self._client_tasks.discard(current)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _validate_hello(self, message: Mapping[str, object]) -> tuple[str, str | None]:
        if message.get("protocol_version") != VFS_PROTOCOL_VERSION:
            raise VfsBrokerError("versión incompatible en hello")
        if message.get("type") != "hello" or message.get("role") not in ("bridge", "worker"):
            raise VfsBrokerError("rol no permitido en hello")
        if message.get("instance_id") != self._instance_id:
            raise VfsBrokerError("bridge conectado para otra instancia")
        if message.get("session_id") != self._session_id:
            raise VfsBrokerError("session_id del bridge no coincide")
        role = str(message["role"])
        if role == "bridge":
            return role, None
        job_id = message.get("job_id")
        if not isinstance(job_id, str) or job_id not in self._pending:
            raise VfsBrokerError("worker conectado para un job desconocido")
        return role, job_id

    def _handle_bridge_message(self, message: Mapping[str, object]) -> None:
        if message.get("protocol_version") != VFS_PROTOCOL_VERSION:
            raise VfsBrokerError("versión incompatible en mensaje del bridge")
        message_type = message.get("type")
        if message_type in ("event", "launch_ack"):
            event = message.get("event") if message_type == "event" else None
            if event == "bridge_error" and message.get("command") == "launch_worker":
                job_id = message.get("job_id")
                future = self._pending.get(job_id) if isinstance(job_id, str) else None
                exit_future = self._worker_exit.get(job_id) if isinstance(job_id, str) else None
                detail = message.get("message")
                if future is None or future.done() or exit_future is None or not isinstance(detail, str) or not detail:
                    raise VfsBrokerError("bridge_error de launch no corresponde a un job pendiente")
                future.set_exception(VfsBridgeLaunchError(detail))
                if not exit_future.done():
                    exit_future.set_result(None)
            elif event == "worker_exit":
                job_id = message.get("job_id")
                exit_future = self._worker_exit.get(job_id) if isinstance(job_id, str) else None
                exit_code = message.get("exit_code")
                parsed_exit_code = exit_code if type(exit_code) is int else None
                if exit_future is not None and not exit_future.done():
                    exit_future.set_result(parsed_exit_code)
                future = self._pending.get(job_id) if isinstance(job_id, str) else None
                if future is not None and not future.done() and job_id not in self._termination_tasks:
                    future.set_exception(
                        VfsWorkerDisconnectedError(
                            f"worker {job_id} termino sin resultado (exit_code={parsed_exit_code!r})"
                        )
                    )
            self._emit_event(message)
            return
        raise VfsBrokerError(f"tipo de mensaje del bridge no permitido: {message_type!r}")

    def _handle_worker_message(
        self,
        message: Mapping[str, object],
        *,
        expected_job_id: str,
    ) -> bool:
        if message.get("protocol_version") != VFS_PROTOCOL_VERSION:
            raise VfsBrokerError("versión incompatible en mensaje del worker")
        message_type = message.get("type")
        if message_type == "event":
            if message.get("job_id") != expected_job_id:
                raise VfsBrokerError("evento del worker atribuido a otro job")
            self._emit_event(message)
            return False
        if message_type != "job_result":
            raise VfsBrokerError(f"tipo de mensaje del worker no permitido: {message_type!r}")
        raw_result = message.get("result")
        if not isinstance(raw_result, Mapping):
            raise VfsBrokerError("job_result del worker sin resultado válido")
        result = VfsJobResult.from_dict(raw_result)
        if result.job_id != expected_job_id:
            raise VfsBrokerError("resultado del worker atribuido a otro job")
        future = self._pending.get(expected_job_id)
        if future is None or future.done():
            raise VfsBrokerError(f"resultado para job desconocido: {expected_job_id}")
        context = self._pending_context.get(expected_job_id)
        if context is None:
            raise VfsBrokerError(f"contexto para job desconocido: {expected_job_id}")
        try:
            self._validate_worker_result(result, job=context[0], challenge=context[1])
        except VfsResultValidationError as exc:
            future.set_exception(exc)
            raise
        future.set_result(result)
        return True

    @staticmethod
    def _validate_worker_result(
        result: VfsJobResult,
        *,
        job: VfsJob,
        challenge: VfsAttestationChallenge,
    ) -> None:
        undeclared = tuple(path for path in result.outputs if path not in job.mutation_targets)
        if undeclared:
            raise VfsResultValidationError("resultado declara outputs fuera de mutation_targets")

        proof = result.attestation
        if proof is None:
            if result.success:
                raise VfsResultValidationError("resultado exitoso sin attestation")
            return
        expected = {
            "profile": job.profile,
            "source_mod": challenge.source_mod,
            "relative_path": challenge.relative_path.as_posix(),
            "visible_sha256": challenge.sha256,
            "profile_fingerprint": job.expected_fingerprint,
        }
        if any(proof.get(field) != value for field, value in expected.items()):
            raise VfsResultValidationError("attestation del resultado no corresponde al job aprobado")
        if result.success and proof.get("grandchild_sha256") != challenge.sha256:
            raise VfsResultValidationError("resultado exitoso sin attestation válida del proceso nieto")

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    def _emit_event(self, message: Mapping[str, object]) -> None:
        """Encola un evento de lifecycle sin crecer sin límite (drop-oldest)."""
        if self._events.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._events.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._events.put_nowait(dict(message))

    async def next_event(self) -> dict[str, Any]:
        """Devuelve el siguiente evento de lifecycle reportado por el bridge."""
        return await self._events.get()

    async def close(self) -> None:
        """Termina jobs activos y luego cierra sesión, sockets y descriptor."""
        async with self._close_lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        if self._server is None:
            await asyncio.to_thread(self._release_instance_file_lock)
            return
        active_jobs = tuple(job_id for job_id, future in self._worker_exit.items() if not future.done())
        for job_id in active_jobs:
            await self._send_cancel(job_id)
        self._closing = True
        server = self._server
        server.close()
        writer = self._bridge_writer
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
        worker_writers = tuple(self._worker_writers)
        for worker_writer in worker_writers:
            worker_writer.close()
        for worker_writer in worker_writers:
            with contextlib.suppress(ConnectionError, OSError):
                await worker_writer.wait_closed()
        task = self._bridge_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._bridge_writer = None
        self._bridge_task = None
        current = asyncio.current_task()
        client_tasks = tuple(task for task in self._client_tasks if task is not current and not task.done())
        for client_task in client_tasks:
            client_task.cancel()
        for client_task in client_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await client_task
        await server.wait_closed()
        self._server = None
        self._worker_writers.clear()
        self._worker_by_job.clear()
        self._client_tasks.clear()
        self._bridge_ready.clear()
        self._fail_pending(VfsBridgeDisconnectedError("broker cerrado"))
        try:
            await asyncio.to_thread(self._descriptor_path.unlink, missing_ok=True)
        finally:
            await asyncio.to_thread(self._release_instance_file_lock)
