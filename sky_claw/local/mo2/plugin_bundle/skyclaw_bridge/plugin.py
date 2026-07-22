"""Plugin Python mínimo de MO2 que actúa como broker de launch/cancel."""

from __future__ import annotations

import base64
import contextlib
import json
import pathlib
import queue
import select
import socket
import threading
import time
from collections.abc import Iterator

import mobase
from PyQt6.QtCore import QCoreApplication, QTimer, qInfo, qWarning

from .protocol import (
    VfsFrameError,
    recv_authenticated_message,
    send_authenticated_message,
)
from .runtime import (
    PROTOCOL_VERSION,
    BridgeCommandError,
    BridgeLaunchController,
    flush_pending_events,
)

_MAX_DESCRIPTOR_BYTES = 64 * 1024


class _BridgeClient:
    """Socket en thread propio; nunca llama APIs de MO2 fuera del hilo Qt."""

    def __init__(
        self,
        *,
        descriptor_path: pathlib.Path,
        instance_id: str,
        commands: queue.Queue[dict[str, object]],
        outgoing: queue.Queue[dict[str, object]],
    ) -> None:
        self._descriptor_path = descriptor_path
        self._instance_id = instance_id
        self._commands = commands
        self._outgoing = outgoing
        self._stop = threading.Event()
        self._connection_lock = threading.Lock()
        self._connection: socket.socket | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="skyclaw-mo2-bridge",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, *, flush_timeout: float = 2.0) -> None:
        # Drenaje acotado ANTES de señalizar el cierre: worker_exit puede estar
        # encolado sin enviar y sin él el broker nunca libera el fence terminal
        # (lock de LOOT / rollback). Con el daemon inalcanzable se corta igual
        # al vencer el timeout — MO2 está saliendo y no puede quedar colgado.
        flush_pending_events(
            self._outgoing,
            timeout=flush_timeout,
            still_running=self._thread.is_alive,
        )
        self._stop.set()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                connection.close()
        self._thread.join(timeout=2)

    @contextlib.contextmanager
    def _track_connection(self, connection: socket.socket) -> Iterator[None]:
        with self._connection_lock:
            self._connection = connection
        try:
            yield
        finally:
            with self._connection_lock:
                if self._connection is connection:
                    self._connection = None

    def _load_descriptor(self) -> tuple[str, int, bytes, str, pathlib.Path]:
        path = self._descriptor_path
        if path.is_symlink() or path.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise BridgeCommandError("descriptor inseguro o demasiado grande")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("protocol_version") != PROTOCOL_VERSION:
            raise BridgeCommandError("descriptor incompatible")
        if raw.get("host") != "127.0.0.1" or raw.get("instance_id") != self._instance_id:
            raise BridgeCommandError("descriptor de host/instancia incorrecto")
        port = raw.get("port")
        token = raw.get("token")
        session_id = raw.get("session_id")
        jobs_root = raw.get("jobs_root")
        expires_at = raw.get("expires_at")
        if type(port) is not int or not 1 <= port <= 65_535:
            raise BridgeCommandError("puerto inválido en descriptor")
        if not all(isinstance(value, str) and value for value in (token, session_id, jobs_root)):
            raise BridgeCommandError("descriptor incompleto")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise BridgeCommandError("expires_at inválido")
        if float(expires_at) < time.time():
            raise BridgeCommandError("descriptor vencido")
        try:
            secret = base64.b64decode(token, altchars=b"-_", validate=True)
        except (TypeError, ValueError) as exc:
            raise BridgeCommandError("token inválido") from exc
        if len(secret) < 32:
            raise BridgeCommandError("token demasiado corto")
        jobs = pathlib.Path(jobs_root)
        if not jobs.is_absolute() or jobs.is_symlink():
            raise BridgeCommandError("jobs_root inseguro")
        assert isinstance(session_id, str)
        return "127.0.0.1", port, secret, session_id, jobs.resolve()

    def _run(self) -> None:
        delay = 0.25
        while not self._stop.is_set():
            try:
                host, port, secret, session_id, jobs_root = self._load_descriptor()
                with (
                    socket.create_connection((host, port), timeout=3) as connection,
                    self._track_connection(connection),
                ):
                    connection.settimeout(5)
                    send_authenticated_message(
                        connection,
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "type": "hello",
                            "role": "bridge",
                            "instance_id": self._instance_id,
                            "session_id": session_id,
                        },
                        secret,
                    )
                    ack = recv_authenticated_message(connection, secret)
                    if ack.get("type") != "hello_ack":
                        raise BridgeCommandError("broker rechazó hello")
                    delay = 0.25
                    self._connected_loop(connection, secret, jobs_root)
            except (OSError, ValueError, json.JSONDecodeError, VfsFrameError, BridgeCommandError) as exc:
                if not self._stop.wait(delay):
                    qWarning(f"Sky-Claw bridge desconectado: {exc}")
                delay = min(delay * 2, 5.0)

    def _connected_loop(
        self,
        connection: socket.socket,
        secret: bytes,
        jobs_root: pathlib.Path,
    ) -> None:
        while not self._stop.is_set():
            while True:
                try:
                    outgoing = self._outgoing.get_nowait()
                except queue.Empty:
                    break
                try:
                    send_authenticated_message(connection, outgoing, secret)
                except (OSError, VfsFrameError):
                    # El evento terminal no puede perderse: tras reconectar el
                    # broker lo necesita para habilitar rollback/liberar locks.
                    # El re-put va antes del task_done para que unfinished_tasks
                    # nunca toque cero con el evento sin entregar.
                    self._outgoing.put(outgoing)
                    self._outgoing.task_done()
                    raise
                self._outgoing.task_done()
            readable, _, _ = select.select([connection], [], [], 0.1)
            if not readable:
                continue
            message = recv_authenticated_message(connection, secret)
            message["_jobs_root"] = str(jobs_root)
            self._commands.put(message)


class SkyClawBridgePlugin(mobase.IPlugin):
    """Bridge delgado: socket/validación + ``startApplication`` explícito."""

    def __init__(self) -> None:
        super().__init__()
        self._organizer = None
        self._timer: QTimer | None = None
        self._client: _BridgeClient | None = None
        self._controller: BridgeLaunchController | None = None
        self._commands: queue.Queue[dict[str, object]] = queue.Queue()
        self._outgoing: queue.Queue[dict[str, object]] = queue.Queue()

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        try:
            config_path = pathlib.Path(__file__).with_name("bridge_config.json")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or config.get("protocol_version") != PROTOCOL_VERSION:
                raise BridgeCommandError("bridge_config tiene una version incompatible")
            raw_descriptor = pathlib.Path(config["descriptor_path"])
            raw_worker = pathlib.Path(config["worker_executable"])
            instance_id = config["instance_id"]
            raw_prefix = config.get("worker_prefix", ["--vfs-worker"])
            if not isinstance(instance_id, str) or not instance_id:
                raise BridgeCommandError("instance_id invalido")
            if instance_id in (".", "..") or any(char in instance_id for char in ("/", "\\", "\x00")):
                raise BridgeCommandError("instance_id inseguro")
            if not isinstance(raw_prefix, list):
                raise BridgeCommandError("worker_prefix debe ser una lista")
            worker_prefix = tuple(raw_prefix)
            if raw_descriptor.is_symlink() or raw_worker.is_symlink():
                raise BridgeCommandError("la configuracion fija no admite symlinks")
            descriptor = raw_descriptor.resolve()
            worker = raw_worker.resolve()
            if not descriptor.is_absolute() or not worker.is_file():
                raise BridgeCommandError("configuración de rutas inválida")
            if not worker_prefix or any(not isinstance(arg, str) or not arg for arg in worker_prefix):
                raise BridgeCommandError("worker_prefix inválido")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, BridgeCommandError) as exc:
            qWarning(f"Sky-Claw VFS Bridge no pudo cargar bridge_config.json: {exc}")
            return False

        # jobs_root llega en el descriptor de sesión. Se actualiza en el primer
        # launch antes de crear el controller, siempre en el hilo Qt.
        self._bridge_config = (descriptor, worker, instance_id, worker_prefix)
        self._client = _BridgeClient(
            descriptor_path=descriptor,
            instance_id=instance_id,
            commands=self._commands,
            outgoing=self._outgoing,
        )
        self._timer = QTimer()
        self._timer.timeout.connect(self._drain_commands)
        self._timer.start(50)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)
        self._client.start()
        qInfo("Sky-Claw VFS Bridge iniciado")
        return True

    def _drain_commands(self) -> None:
        for _ in range(16):
            try:
                message = self._commands.get_nowait()
            except queue.Empty:
                return
            jobs_root = pathlib.Path(str(message.pop("_jobs_root", "")))
            try:
                if self._controller is None:
                    descriptor, worker, _instance_id, worker_prefix = self._bridge_config
                    self._controller = BridgeLaunchController(
                        organizer=self._organizer,
                        worker_executable=worker,
                        worker_prefix=worker_prefix,
                        descriptor_path=descriptor,
                        jobs_root=jobs_root,
                        send_event=self._outgoing.put,
                    )
                message_type = message.get("type")
                if message_type == "launch_worker":
                    self._controller.launch(message)
                elif message_type == "cancel":
                    self._controller.cancel(message)
                elif message_type == "health":
                    self._outgoing.put(
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "type": "event",
                            "event": "bridge_health",
                        }
                    )
                else:
                    raise BridgeCommandError(f"operación no permitida: {message_type!r}")
            except (OSError, RuntimeError, ValueError) as exc:
                qWarning(f"Sky-Claw bridge rechazó comando: {exc}")
                self._outgoing.put(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "type": "event",
                        "event": "bridge_error",
                        "command": message.get("type"),
                        "job_id": message.get("job_id"),
                        "message": str(exc),
                    }
                )

    def _shutdown(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        if self._controller is not None:
            # Terminar el Job Object y esperar (acotado) a que cada monitor
            # encole su worker_exit; recién entonces el cliente puede drenar y
            # cortar. Si el socket muere antes, el broker del daemon queda
            # esperando el fence terminal indefinidamente.
            self._controller.stop()
            self._controller.wait_for_monitors(timeout=2.0)
        if self._client is not None:
            self._client.stop()

    def name(self) -> str:
        return "Sky-Claw VFS Bridge"

    def author(self) -> str:
        return "Sky-Claw"

    def description(self) -> str:
        return "Lanza workers allowlisted de Sky-Claw bajo el perfil USVFS solicitado."

    def version(self):
        return mobase.VersionInfo(1, 0, 0)

    def settings(self) -> list[mobase.PluginSetting]:
        return []

    def enabledByDefault(self) -> bool:  # noqa: N802 - override de la API MO2
        return True
