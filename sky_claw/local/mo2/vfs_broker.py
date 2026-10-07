"""Broker asíncrono entre el daemon Sky-Claw y el bridge cargado por MO2."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import functools
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

from sky_claw.app.security.file_permissions import restrict_to_owner
from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.mo2.vfs_attestation import VfsAttestationChallenge
from sky_claw.local.mo2.vfs_contracts import (
    VFS_MANIFEST_PROTOCOL_VERSION,
    VFS_PROTOCOL_VERSION,
    VfsJob,
    VfsJobResult,
    VfsProtocolError,
    VfsSessionEvent,
    parse_worker_event,
)
from sky_claw.local.mo2.vfs_ipc import (
    VfsFrameError,
    read_authenticated_message,
    write_authenticated_message,
)
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest, write_worker_manifest
from sky_claw.local.mo2.vfs_session import VfsProcessSession

logger = logging.getLogger(__name__)

_BRIDGE_CONNECT_TIMEOUT = 10.0
_DESCRIPTOR_TTL_SECONDS = 24 * 60 * 60
_COOPERATIVE_CANCEL_GRACE_SECONDS = 0.5
# Si el bridge (MO2) se desconecta con un job en vuelo, el fence terminal espera
# su reconexión para recibir el worker_exit; pasada esta ventana sin reconectar
# se asume el worker muerto (el Job Object es kill-on-close: un MO2 caído mata al
# worker) para que el fence NUNCA cuelgue indefinidamente reteniendo el lock.
_BRIDGE_LOSS_FENCE_GRACE_SECONDS = 30.0
# Deadline hard para la confirmación de worker_exit cuando el bridge MO2 permanece
# conectado tras solicitar terminación.
_CONNECTED_WORKER_EXIT_DEADLINE_SECONDS = 30.0
# La cola de eventos de lifecycle no tiene consumidor obligatorio; se acota para
# que no crezca sin límite durante la vida del daemon (drop-oldest).
_MAX_BUFFERED_EVENTS = 256
# Cola por job de una sesión: el worker sólo emite dos eventos por corrida, así
# que un tope chico alcanza; si se llena, es un worker desbocado y se falla
# cerrado (nada de drop silencioso en el camino que alimenta al daemon).
_MAX_EVENTOS_DE_SESION = 64


async def _cancel_and_join(task: asyncio.Future[Any]) -> None:
    """Cancela una future auxiliar del fence y absorbe su cancelación esperada."""
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


class VfsInstanceQuarantinedError(VfsBrokerError):
    """La instancia MO2 está en cuarentena por un trabajo previo con terminalidad desconocida."""


class VfsTeardownError(VfsBrokerError):
    """Fallo en el teardown del worker; la terminalidad del proceso es indeterminada."""


class VfsTeardownDeadlineError(VfsTeardownError):
    """El teardown del worker no concluyó antes del deadline con el bridge conectado."""


class VfsBridgeTerminationError(VfsTeardownError):
    """El bridge reportó un error al intentar terminar el Job Object del worker."""


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


def _recoger_deslenlace_del_driver(driver: asyncio.Task[VfsJobResult]) -> None:
    """Marca como observada la excepción de un driver que el caller pudo abandonar.

    ``Task.exception()`` no consume ni transforma el desenlace: si el caller
    llama después ``session.result()``, sigue levantando la misma causa. Sin
    este ownership, una sesión abandonada deja la task con excepción no
    recuperada y asyncio reporta "Task exception was never retrieved" al
    recolectarla — ruido que tapa la causa real del job.
    """
    if driver.cancelled():
        return
    error = driver.exception()
    if error is not None:
        logger.debug("desenlace de sesión todavía no consumido: %s", error, extra={"vfs_error": type(error).__name__})


@dataclasses.dataclass(frozen=True, slots=True)
class _RaicesEfectivas:
    """Raíces resueltas que viajan al manifiesto firmado del worker."""

    data_root: pathlib.Path
    mods_dir: pathlib.Path
    install_root: pathlib.Path


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
        connected_worker_exit_deadline_seconds: float = _CONNECTED_WORKER_EXIT_DEADLINE_SECONDS,
    ) -> None:
        try:
            self._instance_id = assert_safe_component(instance_id, field="instance_id")
        except PathViolationError as exc:
            raise VfsBrokerError(str(exc)) from exc
        self._state_dir = state_dir.resolve()
        self._jobs_dir = self._state_dir / "jobs"
        self._descriptor_path = self._state_dir / f"{self._instance_id}.json"
        self._instance_lock_path = self._state_dir / f".{self._instance_id}.lock"
        self._quarantine_path = self._state_dir / f".{self._instance_id}.quarantine"
        self._secret = secret or secrets.token_bytes(32)
        if len(self._secret) < 32:
            raise VfsBrokerError("el secreto del broker debe tener al menos 32 bytes")
        self._hardener = descriptor_hardener
        self._fence_grace = fence_grace_seconds
        self._connected_worker_exit_deadline = connected_worker_exit_deadline_seconds
        self._quarantine_reason: str | None = None
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
        # Routing de eventos mid-job por job: una sesión se suscribe ANTES de
        # enviar launch_worker, así que un tool_started jamás puede caer en una
        # ventana sin suscriptor ni despertar a la sesión de otro job.
        self._job_event_queues: dict[str, asyncio.Queue[VfsSessionEvent]] = {}
        self._session_drivers: dict[str, asyncio.Task[VfsJobResult]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_BUFFERED_EVENTS)
        self._closing = False
        self._owns_instance_file_lock = False

    @property
    def descriptor_path(self) -> pathlib.Path:
        return self._descriptor_path

    @property
    def quarantine_reason(self) -> str | None:
        """Razón de la cuarentena activa si la instancia está aislada por terminalidad desconocida."""
        return self._quarantine_reason

    def _load_quarantine(self) -> None:
        if not self._quarantine_path.exists():
            self._quarantine_reason = None
            return
        try:
            raw = json.loads(self._quarantine_path.read_text(encoding="utf-8"))
            reason = raw.get("reason", "terminalidad indeterminada previa")
            self._quarantine_reason = str(reason)
        except Exception:
            self._quarantine_reason = "marcador de cuarentena presente pero ilegible (fail-closed)"

    def _quarantine_instance(
        self,
        job_id: str,
        reason: str,
        *,
        exc: BaseException | None = None,
    ) -> None:
        self._quarantine_reason = reason
        payload = {
            "job_id": job_id,
            "session_id": self._session_id,
            "pid": os.getpid(),
            "reason": reason,
            "error_type": type(exc).__name__ if exc is not None else None,
            "detail": str(exc) if exc is not None else None,
            "timestamp": time.time(),
        }
        tmp = self._quarantine_path.with_name(f".{self._quarantine_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            self._hardener(tmp)
            os.replace(tmp, self._quarantine_path)
            logger.critical(
                "Instancia %s puesta en CUARENTENA por terminalidad desconocida del job %s: %s",
                self._instance_id,
                job_id,
                reason,
                extra={"instance_id": self._instance_id, "job_id": job_id, "quarantine_reason": reason},
            )
        except Exception:
            tmp.unlink(missing_ok=True)
            logger.critical("No se pudo persistir el marcador de cuarentena para %s", self._instance_id, exc_info=True)

    def _liberar_cuarentena_por_worker_exit(self, job_id: str) -> None:
        if self._quarantine_path.exists():
            try:
                raw = json.loads(self._quarantine_path.read_text(encoding="utf-8"))
                if raw.get("job_id") == job_id:
                    logger.info("Worker exit tardío confirmó salida para job %s; liberando cuarentena", job_id)
                    self._quarantine_reason = None
                    self._quarantine_path.unlink(missing_ok=True)
            except Exception:
                pass
        elif self._quarantine_reason is not None:
            self._quarantine_reason = None

    async def release_quarantine(self, *, evidence: str) -> None:
        """Libera la cuarentena de la instancia tras constatación del operador."""
        if not isinstance(evidence, str) or not evidence.strip():
            raise VfsBrokerError("se requiere evidencia explícita para levantar la cuarentena")
        logger.critical(
            "Cuarentena de la instancia %s liberada por operador: %s",
            self._instance_id,
            evidence,
            extra={"instance_id": self._instance_id, "evidence": evidence},
        )
        self._quarantine_reason = None
        await asyncio.to_thread(self._quarantine_path.unlink, missing_ok=True)

    async def start(self) -> None:
        """Publica una sesión nueva; es idempotente mientras siga activa."""
        if self._server is not None:
            return
        self._closing = False
        await asyncio.to_thread(self._acquire_instance_file_lock)
        await asyncio.to_thread(self._load_quarantine)
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

    def _precondiciones(self, job: VfsJob, challenge: VfsAttestationChallenge) -> None:
        """Validaciones comunes de ``submit``/``open_session`` (una sola copia)."""
        if self._server is None:
            raise VfsBrokerError("el broker no está iniciado")
        if self._quarantine_reason is not None:
            raise VfsInstanceQuarantinedError(
                f"la instancia {self._instance_id} está en cuarentena por terminalidad indeterminada: {self._quarantine_reason}"
            )
        if job.instance_id != self._instance_id:
            raise VfsBrokerError("el job apunta a otra instancia MO2")
        if job.profile != challenge.profile or job.expected_fingerprint != challenge.profile_fingerprint:
            raise VfsBrokerError("job y attestation no comparten perfil/fingerprint")

    @staticmethod
    def _raices_efectivas(
        *,
        mo2_root: pathlib.Path | None,
        data_root: pathlib.Path | None,
        mods_dir: pathlib.Path | None,
        install_root: pathlib.Path | None,
    ) -> _RaicesEfectivas:
        effective_data = data_root or mo2_root
        if effective_data is None:
            raise VfsBrokerError("se requiere data_root o mo2_root")
        effective_mods = mods_dir or (effective_data / "mods")
        effective_install = install_root or mo2_root or effective_data
        return _RaicesEfectivas(
            data_root=effective_data.resolve(),
            mods_dir=effective_mods.resolve(),
            install_root=effective_install.resolve(),
        )

    async def _escribir_manifiesto(
        self,
        job: VfsJob,
        challenge: VfsAttestationChallenge,
        raices: _RaicesEfectivas,
        virtual_data_dir: pathlib.Path,
    ) -> pathlib.Path:
        manifest_path = self._jobs_dir / f"{job.job_id}.json"
        manifest = VfsWorkerManifest(
            protocol_version=VFS_MANIFEST_PROTOCOL_VERSION,
            job=job,
            challenge=challenge,
            data_root=raices.data_root,
            mods_dir=raices.mods_dir,
            install_root=raices.install_root,
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
        return manifest_path

    def _registrar_job(
        self,
        job: VfsJob,
        challenge: VfsAttestationChallenge,
    ) -> tuple[asyncio.Future[VfsJobResult], asyncio.Future[int | None]]:
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[VfsJobResult] = loop.create_future()
        exit_future: asyncio.Future[int | None] = loop.create_future()
        self._pending[job.job_id] = result_future
        self._pending_context[job.job_id] = (job, challenge)
        self._worker_exit[job.job_id] = exit_future
        return result_future, exit_future

    async def _limpiar_registro(
        self,
        job_id: str,
        manifest_path: pathlib.Path,
        result_future: asyncio.Future[VfsJobResult],
        session: VfsProcessSession | None = None,
    ) -> None:
        """Limpieza idempotente del job: mapas, suscripción, futuro y manifiesto.

        El futuro se marca como recuperado cuando ya tiene excepción: nadie más lo
        va a esperar y ``asyncio`` sólo avisa por futures con excepción nunca
        recuperada (ruido de crash sin causa accionable).
        """
        self._pending.pop(job_id, None)
        self._pending_context.pop(job_id, None)
        self._worker_exit.pop(job_id, None)
        self._termination_tasks.pop(job_id, None)
        if session is not None:
            await session._detener_recolector()
            self._job_event_queues.pop(job_id, None)
            self._session_drivers.pop(job_id, None)
        if not result_future.done():
            result_future.cancel()
        else:
            with contextlib.suppress(BaseException):
                result_future.exception()
        await asyncio.to_thread(manifest_path.unlink, missing_ok=True)

    def _mensaje_de_launch(
        self, job: VfsJob, manifest_path: pathlib.Path, overwrite_mod: str | None
    ) -> dict[str, object]:
        return {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "launch_worker",
            "job_id": job.job_id,
            "profile": job.profile,
            "manifest_path": str(manifest_path),
            "overwrite_mod": overwrite_mod,
        }

    async def submit(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        mo2_root: pathlib.Path | None = None,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> VfsJobResult:
        """Serializa, lanza y espera un único job para esta instancia."""
        self._precondiciones(job, challenge)
        raices = self._raices_efectivas(
            mo2_root=mo2_root,
            data_root=data_root,
            mods_dir=mods_dir,
            install_root=install_root,
        )

        async with self._instance_lock:
            if self._quarantine_reason is not None:
                raise VfsInstanceQuarantinedError(
                    f"la instancia {self._instance_id} está en cuarentena por terminalidad indeterminada: {self._quarantine_reason}"
                )
            await self.wait_until_ready()
            manifest_path = await self._escribir_manifiesto(job, challenge, raices, virtual_data_dir)
            result_future, exit_future = self._registrar_job(job, challenge)
            try:
                await self._send_bridge(self._mensaje_de_launch(job, manifest_path, overwrite_mod))
                try:
                    return await self._await_job_completion(
                        result_future,
                        exit_future,
                        job_id=job.job_id,
                        timeout=job.timeout_seconds,
                    )
                except TimeoutError as exc:
                    td_err: VfsTeardownError | None = None
                    try:
                        await self._send_cancel(job.job_id)
                    except VfsTeardownError as t_exc:
                        td_err = t_exc
                    err = VfsJobTimeoutError(f"job {job.job_id} excedió {job.timeout_seconds:g}s")
                    err.__cause__ = exc
                    if td_err is not None:
                        from sky_claw.app.db.rollback_veto import mark_unknown_terminality

                        mark_unknown_terminality(err, teardown_error=td_err)
                    raise err from exc
                except asyncio.CancelledError:
                    td_err = None
                    try:
                        await self._send_cancel(job.job_id)
                    except VfsTeardownError as t_exc:
                        td_err = t_exc
                    if td_err is not None:
                        from sky_claw.app.db.rollback_veto import mark_unknown_terminality

                        canc = asyncio.CancelledError()
                        mark_unknown_terminality(canc, teardown_error=td_err)
                        canc.__cause__ = td_err
                        raise canc from td_err
                    raise
                except Exception as exc:
                    # Un resultado inválido o un fallo de lifecycle tampoco
                    # habilita rollback mientras el árbol siga ejecutándose.
                    td_err = None
                    try:
                        await self._await_worker_exit(exit_future, job_id=job.job_id)
                    except VfsTeardownError as t_exc:
                        td_err = t_exc
                    if td_err is not None:
                        from sky_claw.app.db.rollback_veto import mark_unknown_terminality

                        mark_unknown_terminality(exc, teardown_error=td_err)
                    raise
            finally:
                await self._limpiar_registro(job.job_id, manifest_path, result_future)

    async def open_session(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        mo2_root: pathlib.Path | None = None,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> VfsProcessSession:
        """Lanza el job y retorna con un ``tool_started`` válido ya observado.

        Comparte con ``submit`` la serialización por instancia, el manifiesto
        firmado, la validación del resultado y el fence ``worker_exit``; la
        diferencia es que NO espera el desenlace: devuelve un handle del proceso
        vivo. Si el job falla antes de crear el tool (manifest, attestation,
        bootstrap del worker, bridge caído), levanta la excepción causal y jamás
        entrega una sesión con PID placeholder.

        **Lock de instancia (ADR 0007).** La sesión retiene ``_instance_lock``
        durante TODA su vida: un ``submit``/``open_session`` concurrente para la
        misma instancia espera al terminal (no se libera mientras el tool vive).
        Lo libera el ``finally`` del driver, que también limpia el tracking.
        """
        self._precondiciones(job, challenge)
        raices = self._raices_efectivas(
            mo2_root=mo2_root,
            data_root=data_root,
            mods_dir=mods_dir,
            install_root=install_root,
        )
        # El lock se adquiere ACÁ y lo libera el finally del driver (o el except
        # de abajo si ni siquiera llegó a existir): una sesión viva serializa la
        # instancia tanto como un submit en vuelo.
        await self._instance_lock.acquire()
        driver_creado = False
        try:
            if self._quarantine_reason is not None:
                raise VfsInstanceQuarantinedError(
                    f"la instancia {self._instance_id} está en cuarentena por terminalidad indeterminada: {self._quarantine_reason}"
                )
            await self.wait_until_ready()
            manifest_path = await self._escribir_manifiesto(job, challenge, raices, virtual_data_dir)
            result_future, exit_future = self._registrar_job(job, challenge)
            # La suscripción existe ANTES de que el launch pueda crear un worker:
            # no hay ventana en la que un tool_started se pierda por llegar antes
            # que el suscriptor.
            cola: asyncio.Queue[VfsSessionEvent] = asyncio.Queue(maxsize=_MAX_EVENTOS_DE_SESION)
            self._job_event_queues[job.job_id] = cola
            sesion = VfsProcessSession(
                job_id=job.job_id,
                result_future=result_future,
                event_queue=cola,
                cancelar=functools.partial(self._cancelar_job_de_sesion, job.job_id),
            )
            sesion._iniciar_recolector()
            driver = asyncio.create_task(
                self._conducir_sesion(
                    sesion,
                    mensaje_launch=self._mensaje_de_launch(job, manifest_path, overwrite_mod),
                    result_future=result_future,
                    exit_future=exit_future,
                    manifest_path=manifest_path,
                    timeout=job.timeout_seconds,
                ),
                name=f"vfs-session-{job.job_id}",
            )
            # Ownership del desenlace: si el caller abandona la sesión sin
            # consumir result()/wait()/cancel(), la excepción del driver queda
            # observada y no ensucia el event loop (la causa sigue disponible).
            driver.add_done_callback(_recoger_deslenlace_del_driver)
            sesion._vincular_driver(driver)
            self._session_drivers[job.job_id] = driver
            driver_creado = True
            try:
                await sesion._esperar_tool_started()
            except BaseException:
                with contextlib.suppress(asyncio.CancelledError):
                    await sesion._fence_de_teardown()
                raise
            return sesion
        except BaseException:
            # Sin driver (fallo antes de crearlo) no hay finally que limpie ni
            # libere el lock: se descarta lo que se haya alcanzado a escribir o
            # registrar y recién ahí se libera. Con driver, su finally es el
            # único responsable.
            if not driver_creado:
                await self._descartar_apertura_sin_driver(job.job_id)
                self._instance_lock.release()
            raise

    async def _conducir_sesion(
        self,
        sesion: VfsProcessSession,
        *,
        mensaje_launch: Mapping[str, object],
        result_future: asyncio.Future[VfsJobResult],
        exit_future: asyncio.Future[int | None],
        manifest_path: pathlib.Path,
        timeout: float,
    ) -> VfsJobResult:
        """Task dueña del desenlace de una sesión (espeja el lifecycle de submit)."""
        try:
            lanzado = False
            try:
                await self._send_bridge(mensaje_launch)
                lanzado = True
                return await self._await_job_completion(
                    result_future,
                    exit_future,
                    job_id=sesion.job_id,
                    timeout=timeout,
                )
            except TimeoutError as exc:
                td_err: VfsTeardownError | None = None
                try:
                    await self._send_cancel(sesion.job_id)
                except VfsTeardownError as t_exc:
                    td_err = t_exc
                err = VfsJobTimeoutError(f"job {sesion.job_id} excedió {timeout:g}s")
                err.__cause__ = exc
                if td_err is not None:
                    from sky_claw.app.db.rollback_veto import mark_unknown_terminality

                    mark_unknown_terminality(err, teardown_error=td_err)
                raise err from exc
            except asyncio.CancelledError:
                td_err = None
                try:
                    await self._send_cancel(sesion.job_id)
                except VfsTeardownError as t_exc:
                    td_err = t_exc
                if td_err is not None:
                    from sky_claw.app.db.rollback_veto import mark_unknown_terminality

                    canc = asyncio.CancelledError()
                    mark_unknown_terminality(canc, teardown_error=td_err)
                    canc.__cause__ = td_err
                    raise canc from td_err
                raise
            except Exception as exc:
                # Con el launch emitido, un resultado inválido o un fallo de
                # lifecycle tampoco habilita rollback mientras el árbol siga
                # ejecutándose. Sin launch no hay worker que esperar.
                if lanzado:
                    td_err = None
                    try:
                        await self._await_worker_exit(exit_future, job_id=sesion.job_id)
                    except VfsTeardownError as t_exc:
                        td_err = t_exc
                    if td_err is not None:
                        from sky_claw.app.db.rollback_veto import mark_unknown_terminality

                        mark_unknown_terminality(exc, teardown_error=td_err)
                raise
        finally:
            try:
                await self._limpiar_registro(sesion.job_id, manifest_path, result_future, sesion)
            finally:
                self._instance_lock.release()

    async def _descartar_apertura_sin_driver(self, job_id: str) -> None:
        """Limpia una apertura fallida ANTES de que exista driver o sesión.

        Cuando el driver ya existe, su ``finally`` es el único dueño de la
        limpieza; esta ruta cubre el hueco previo: si el fallo llega después de
        escribir el manifiesto firmado (o de registrar futuros), nadie más lo
        borraría y el artefacto quedaría en ``state_dir``. El path se
        reconstruye —es determinista por job— en vez de depender de una variable
        que puede no haberse asignado si el fallo vino del propio manifiesto.
        """
        self._pending.pop(job_id, None)
        self._pending_context.pop(job_id, None)
        self._worker_exit.pop(job_id, None)
        self._termination_tasks.pop(job_id, None)
        self._job_event_queues.pop(job_id, None)
        self._session_drivers.pop(job_id, None)
        await asyncio.to_thread((self._jobs_dir / f"{job_id}.json").unlink, missing_ok=True)

    async def _cancelar_job_de_sesion(self, job_id: str) -> None:
        """Cancelación pedida por una sesión: idempotente y tolerante a terminal.

        ``_send_cancel`` sólo falla si el job ya no está en tracking; para una
        sesión eso significa "ya terminó", no un error de cancelación.
        """
        if job_id not in self._worker_exit:
            return
        try:
            await self._send_cancel(job_id)
        except VfsTeardownError:
            raise
        except VfsBrokerError:
            if job_id in self._worker_exit:
                raise

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
        job_id: str | None = None,
        timeout: float,
    ) -> VfsJobResult:
        result = await asyncio.wait_for(asyncio.shield(result_future), timeout=timeout)
        deadline = asyncio.get_running_loop().time() + self._connected_worker_exit_deadline
        while not exit_future.done():
            try:
                await self._await_exit_or_bridge_loss(exit_future, deadline, job_id=job_id)
            except TypeError:
                await self._await_exit_or_bridge_loss(exit_future, deadline)
        exit_future.result()
        return result

    async def _await_worker_exit(
        self,
        exit_future: asyncio.Future[int | None],
        *,
        job_id: str | None = None,
        deadline: float | None = None,
    ) -> None:
        """Espera el ``worker_exit`` del bridge sin poder colgar para siempre.

        Resiste la cancelación externa —rollback no puede empezar antes de la
        confirmación terminal— pero si el deadline vence o si el bridge se
        desconecta y no reconecta dentro de ``_fence_grace`` segundos, resuelve
        el fence. Si el bridge estaba conectado, vence con error tipado
        (terminalidad indeterminada); si el bridge murió y no reconectó, se
        asume el worker muerto por kill-on-close.

        La ventana de gracia es un deadline ABSOLUTO fijado antes del loop: una
        ``CancelledError`` absorbida no lo reinicia, así que ni siquiera
        cancelaciones repetidas extienden el fence más allá del límite absoluto
        (review CodeRabbit PR #352).
        """
        cancelled = False
        loop = asyncio.get_running_loop()
        now = loop.time()
        connected_limite = deadline if deadline is not None else (now + self._connected_worker_exit_deadline)
        bridge_loss_limite = now + self._fence_grace
        resolved_job_id = job_id
        if resolved_job_id is None:
            for j_id, f in self._worker_exit.items():
                if f is exit_future:
                    resolved_job_id = j_id
                    break
        while not exit_future.done():
            try:
                try:
                    await self._await_exit_or_bridge_loss(
                        exit_future,
                        connected_limite,
                        job_id=resolved_job_id,
                        bridge_loss_deadline=bridge_loss_limite,
                    )
                except TypeError:
                    await self._await_exit_or_bridge_loss(exit_future, connected_limite)
            except asyncio.CancelledError:
                cancelled = True
        exit_future.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _await_exit_or_bridge_loss(
        self,
        exit_future: asyncio.Future[int | None],
        deadline: float,
        *,
        job_id: str | None = None,
        bridge_loss_deadline: float | None = None,
    ) -> None:
        """Una espera acotada: worker_exit, error de terminación o pérdida terminal del bridge.

        ``deadline`` es el instante absoluto (loop clock) tras el cual, si el
        worker no confirmó salida, el fence vence. Si el bridge está vivo,
        vence con :class:`VfsTeardownDeadlineError` (sin fingir terminalidad);
        si el bridge está desconectado y expira la gracia, se asume el worker
        muerto por kill-on-close.
        """
        exit_wait: asyncio.Task[Any] = asyncio.create_task(asyncio.wait({exit_future}))
        try:
            resolved_job_id = job_id
            if resolved_job_id is None:
                for j_id, f in self._worker_exit.items():
                    if f is exit_future:
                        resolved_job_id = j_id
                        break
            remaining = deadline - asyncio.get_running_loop().time()
            if self._bridge_ready.is_set():
                # Bridge vivo: acotado al deadline absoluto. Si expira sin worker_exit,
                # la terminalidad es indeterminada (NUNCA set_result(None)).
                if remaining <= 0:
                    if not exit_future.done():
                        logger.warning(
                            "El worker VFS %s no confirmó worker_exit en %.1fs con el bridge MO2 conectado; "
                            "la terminalidad del proceso es indeterminada",
                            resolved_job_id or "desconocido",
                            self._connected_worker_exit_deadline,
                        )
                        if resolved_job_id is not None:
                            self._quarantine_instance(
                                resolved_job_id,
                                f"teardown sin confirmación de worker_exit tras {self._connected_worker_exit_deadline:.1f}s con bridge conectado",
                            )
                        exit_future.set_exception(
                            VfsTeardownDeadlineError(
                                f"el teardown del worker no confirmó worker_exit en {self._connected_worker_exit_deadline:.1f}s con el bridge MO2 conectado"
                            )
                        )
                    return
                lost_wait: asyncio.Task[Any] = asyncio.create_task(self._bridge_lost.wait())
                grace: asyncio.Task[Any] = asyncio.create_task(asyncio.sleep(remaining))
                try:
                    await asyncio.wait({exit_wait, lost_wait, grace}, return_when=asyncio.FIRST_COMPLETED)
                    if grace.done() and not lost_wait.done() and not exit_future.done():
                        logger.warning(
                            "El worker VFS %s no confirmó worker_exit en %.1fs con el bridge MO2 conectado; "
                            "la terminalidad del proceso es indeterminada",
                            resolved_job_id or "desconocido",
                            self._connected_worker_exit_deadline,
                        )
                        if resolved_job_id is not None:
                            self._quarantine_instance(
                                resolved_job_id,
                                f"teardown sin confirmación de worker_exit tras {self._connected_worker_exit_deadline:.1f}s con bridge conectado",
                            )
                        exit_future.set_exception(
                            VfsTeardownDeadlineError(
                                f"el teardown del worker no confirmó worker_exit en {self._connected_worker_exit_deadline:.1f}s con el bridge MO2 conectado"
                            )
                        )
                finally:
                    await _cancel_and_join(lost_wait)
                    await _cancel_and_join(grace)
                return
            # Bridge caído: sólo el tiempo que reste hasta el deadline de pérdida de bridge.
            effective_loss_deadline = (
                bridge_loss_deadline
                if bridge_loss_deadline is not None
                else (asyncio.get_running_loop().time() + self._fence_grace)
            )
            remaining_loss = effective_loss_deadline - asyncio.get_running_loop().time()
            if remaining_loss <= 0:
                if not exit_future.done():
                    logger.warning(
                        "El bridge MO2 no reconectó en %.1fs; se asume el worker muerto para liberar el fence",
                        self._fence_grace,
                    )
                    exit_future.set_result(None)
                return
            ready_wait: asyncio.Task[Any] = asyncio.create_task(self._bridge_ready.wait())
            grace = asyncio.create_task(asyncio.sleep(remaining_loss))
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
                coop_deadline = asyncio.get_running_loop().time() + _COOPERATIVE_CANCEL_GRACE_SECONDS
                while job_id in self._worker_by_job and asyncio.get_running_loop().time() < coop_deadline:
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
        # que expire el deadline con fallo tipado o reconexión de bridge caído.
        await self._await_worker_exit(exit_future, job_id=job_id)

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
            if event == "bridge_error":
                job_id = message.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    raise VfsBrokerError(f"bridge_error con job_id no válido: {job_id!r}")
                if message.get("command") == "launch_worker":
                    future = self._pending.get(job_id)
                    exit_future = self._worker_exit.get(job_id)
                    detail = message.get("message")
                    if (
                        future is None
                        or future.done()
                        or exit_future is None
                        or not isinstance(detail, str)
                        or not detail
                    ):
                        raise VfsBrokerError("bridge_error de launch no corresponde a un job pendiente")
                    future.set_exception(VfsBridgeLaunchError(detail))
                    if not exit_future.done():
                        exit_future.set_result(None)
                elif message.get("command") in ("cancel", "terminate") or job_id in self._termination_tasks:
                    detail = message.get("message")
                    detail_str = detail if isinstance(detail, str) and detail else "error no especificado del bridge"
                    kind = message.get("kind")
                    if kind == "job_unknown":
                        logger.info(
                            "Bridge reportó job desconocido para cancel/terminate de %s; posible carrera benigna con worker_exit en tránsito: %s",
                            job_id,
                            detail_str,
                        )
                        # No fallamos exit_future ni ponemos en cuarentena: dejamos que
                        # el worker_exit del monitor resuelva el fence normalmente.
                        return
                    logger.error(
                        "MO2 bridge reportó error durante terminación del job %s: %s",
                        job_id,
                        detail_str,
                        extra={"job_id": job_id, "command": message.get("command")},
                    )
                    err = VfsBridgeTerminationError(
                        f"error del bridge durante la terminación del job {job_id}: {detail_str}"
                    )
                    self._quarantine_instance(
                        job_id,
                        f"error del bridge durante terminación ({detail_str})",
                        exc=err,
                    )
                    exit_future = self._worker_exit.get(job_id)
                    if exit_future is not None and not exit_future.done():
                        exit_future.set_exception(err)
                    future = self._pending.get(job_id)
                    if future is not None and not future.done():
                        future.set_exception(err)
            elif event == "worker_exit":
                job_id = message.get("job_id")
                if isinstance(job_id, str):
                    self._liberar_cuarentena_por_worker_exit(job_id)
                exit_future = self._worker_exit.get(job_id) if isinstance(job_id, str) else None
                exit_code = message.get("exit_code")
                parsed_exit_code = exit_code if type(exit_code) is int else None
                if exit_future is not None and not exit_future.done():
                    exit_future.set_result(parsed_exit_code)
                future = self._pending.get(job_id) if isinstance(job_id, str) else None
                if (
                    isinstance(job_id, str)
                    and future is not None
                    and not future.done()
                    and job_id not in self._termination_tasks
                ):
                    pending_context = self._pending_context.get(job_id)
                    tool_id = pending_context[0].tool_id if pending_context is not None else "unknown"
                    logger.error(
                        "VFS worker %s termino sin resultado",
                        job_id,
                        extra={
                            "event": "worker_exit",
                            "operation": "vfs_worker",
                            "tool": tool_id,
                            "job_id": job_id,
                            "exit_code": parsed_exit_code,
                        },
                    )
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
            try:
                evento = parse_worker_event(message)
            except VfsProtocolError as exc:
                raise VfsBrokerError(f"evento de sesión inválido del worker: {exc}") from exc
            self._publicar_evento_de_sesion(evento)
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

    def _publicar_evento_de_sesion(self, evento: VfsSessionEvent) -> None:
        """Enruta un evento mid-job a la cola de SU job (nunca a la de otro).

        Sin sesión registrada, un evento de sesión es una violación de protocolo
        y no ruido: aceptarlo alimentaría una cola que nadie lee y escondería a
        un worker emitiendo fuera de contrato.
        """
        cola = self._job_event_queues.get(evento.job_id)
        if cola is None:
            raise VfsBrokerError(f"evento de sesión para un job sin sesión registrada: {evento.job_id}")
        try:
            cola.put_nowait(evento)
        except asyncio.QueueFull as exc:
            raise VfsBrokerError(f"la sesión {evento.job_id} desbordó su cola de eventos") from exc

    async def next_event(self) -> dict[str, Any]:
        """Devuelve el siguiente evento de lifecycle reportado por el bridge."""
        return await self._events.get()

    async def close(self) -> None:
        """Termina jobs activos y luego cierra sesión, sockets y descriptor."""
        async with self._close_lock:
            cleanup = asyncio.ensure_future(self._close_unlocked())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # El cierre es un fence: la cancelación se conserva, pero no
                    # puede abandonar sockets, descriptor o lock de instancia.
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    async def _close_unlocked(self) -> None:
        if self._server is None:
            await asyncio.to_thread(self._release_instance_file_lock)
            return
        teardown_error: VfsTeardownError | None = None
        cancelled_during_cancel = False
        active_jobs = tuple(job_id for job_id, future in self._worker_exit.items() if not future.done())
        for job_id in active_jobs:
            try:
                await self._send_cancel(job_id)
            except VfsTeardownError as exc:
                if teardown_error is None:
                    teardown_error = exc
            except asyncio.CancelledError:
                cancelled_during_cancel = True
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
        # El cierre no puede dejar sesiones en vuelo: al fallar los pendientes
        # los drivers despiertan y su finally limpia y libera el lock de
        # instancia. Se los espera para que "close() terminó" implique teardown.
        session_drivers = tuple(
            driver for driver in self._session_drivers.values() if driver is not current and not driver.done()
        )
        for driver in session_drivers:
            while not driver.done():
                try:
                    await asyncio.shield(driver)
                except asyncio.CancelledError:
                    # La cancelación externa se propaga al final de close(); no
                    # puede abandonar el teardown de una sesión a medio camino.
                    continue
                except BaseException:
                    # Desenlace de la sesión: lo lee result(), no close().
                    pass
            if not driver.cancelled():
                driver.exception()  # recuperada
        self._job_event_queues.clear()
        self._session_drivers.clear()
        try:
            await asyncio.to_thread(self._descriptor_path.unlink, missing_ok=True)
        finally:
            await asyncio.to_thread(self._release_instance_file_lock)
        if teardown_error is not None:
            raise teardown_error
        if cancelled_during_cancel:
            raise asyncio.CancelledError
