"""Broker asíncrono entre el daemon Sky-Claw y el bridge cargado por MO2."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
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
        self._session_id = str(uuid.uuid4())
        self._server: asyncio.AbstractServer | None = None
        self._bridge_writer: asyncio.StreamWriter | None = None
        self._bridge_task: asyncio.Task[None] | None = None
        self._worker_writers: set[asyncio.StreamWriter] = set()
        self._worker_by_job: dict[str, asyncio.StreamWriter] = {}
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._bridge_ready = asyncio.Event()
        self._instance_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[VfsJobResult]] = {}
        self._pending_context: dict[str, tuple[VfsJob, VfsAttestationChallenge]] = {}
        self._worker_exit: dict[str, asyncio.Future[int | None]] = {}
        self._termination_tasks: dict[str, asyncio.Task[None]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
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
                payload = json.dumps(
                    {"pid": os.getpid(), "session_id": self._session_id},
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
            pid = raw.get("pid") if isinstance(raw, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return True
        if type(pid) is not int or pid <= 0:
            return True
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, OverflowError) as exc:
            return not (getattr(exc, "errno", None) == errno.ESRCH or getattr(exc, "winerror", None) == 87)
        return True

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
                    await self._await_terminal_fence(exit_future)
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
        await asyncio.shield(exit_future)
        return result

    @staticmethod
    async def _await_terminal_fence(exit_future: asyncio.Future[int | None]) -> None:
        cancelled = False
        while not exit_future.done():
            try:
                await asyncio.shield(exit_future)
            except asyncio.CancelledError:
                cancelled = True
        exit_future.result()
        if cancelled:
            raise asyncio.CancelledError

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
        # bridge confirme que el Job Object completo dejó de ejecutar.
        await asyncio.shield(exit_future)

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
            self._events.put_nowait(dict(message))
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
            self._events.put_nowait(dict(message))
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
        self._server.close()
        await self._server.wait_closed()
        self._server = None
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
        self._worker_writers.clear()
        self._worker_by_job.clear()
        self._client_tasks.clear()
        self._bridge_ready.clear()
        self._fail_pending(VfsBridgeDisconnectedError("broker cerrado"))
        try:
            await asyncio.to_thread(self._descriptor_path.unlink, missing_ok=True)
        finally:
            await asyncio.to_thread(self._release_instance_file_lock)
