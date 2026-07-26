"""Runtime interno para logging no bloqueante y fail-safe.

Los productores solo encolan ``LogRecord`` con ``put_nowait``. El listener
dedicado concentra cualquier I/O de consola o archivo fuera del event loop.
"""

from __future__ import annotations

import copy
import io
import logging
import logging.handlers
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LoggingHealthSnapshot:
    """Vista inmutable del estado operativo del pipeline de logging."""

    degraded: bool
    dropped_records: int
    emergency_records: int
    file_errors: int
    listener_alive: bool


class LoggingHealth:
    """Estado compartido que nunca genera logging recursivo."""

    def __init__(self, *, emergency_capacity: int = 256) -> None:
        self._lock = threading.Lock()
        self._degraded = False
        self._dropped_records = 0
        self._file_errors = 0
        self._emergency: deque[logging.LogRecord] = deque(maxlen=emergency_capacity)

    def record_drop(self, record: logging.LogRecord) -> None:
        with self._lock:
            self._degraded = True
            self._dropped_records += 1
            if record.levelno >= logging.ERROR:
                self._emergency.append(record)

    def pop_emergency(self) -> logging.LogRecord | None:
        with self._lock:
            return self._emergency.popleft() if self._emergency else None

    def restore_emergency(self, record: logging.LogRecord) -> None:
        with self._lock:
            self._emergency.appendleft(record)

    def record_file_error(self) -> None:
        with self._lock:
            self._degraded = True
            self._file_errors += 1

    def record_listener_error(self) -> None:
        with self._lock:
            self._degraded = True

    def snapshot(self, *, listener_alive: bool) -> LoggingHealthSnapshot:
        with self._lock:
            return LoggingHealthSnapshot(
                degraded=self._degraded,
                dropped_records=self._dropped_records,
                emergency_records=len(self._emergency),
                file_errors=self._file_errors,
                listener_alive=listener_alive,
            )


class NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler acotado que jamás espera espacio ni escribe en disco."""

    def __init__(
        self,
        records: queue.Queue[logging.LogRecord | None],
        health: LoggingHealth,
    ) -> None:
        super().__init__(records)
        self._health = health

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # QueueHandler.prepare() formatea en el productor y borra exc_info. Este
        # pipeline es intra-proceso: basta una copia superficial. El filtro de
        # seguridad ya renderizó el traceback redacted en exc_text; se retira la
        # tupla cruda para que python-json-logger no vuelva a renderizar secretos.
        prepared = copy.copy(record)
        if prepared.exc_text:
            prepared.exc_info = None
        return prepared

    def handle(self, record: logging.LogRecord) -> bool:
        """Impide que un fallo del pipeline productor alcance la aplicación."""
        try:
            return super().handle(record)
        except Exception:  # noqa: BLE001 - boundary fail-safe: logging nunca alcanza al caller
            self._health.record_listener_error()
            return False

    def enqueue(self, record: logging.LogRecord) -> None:
        while True:
            emergency = self._health.pop_emergency()
            if emergency is None:
                break
            try:
                self.queue.put_nowait(emergency)
            except queue.Full:
                self._health.restore_emergency(emergency)
                break

        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._health.record_drop(record)


class FailSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler que degrada sin propagar errores al listener."""

    _RETRY_SECONDS = 1.0

    def __init__(
        self,
        filename: Path,
        *,
        max_bytes: int,
        backup_count: int,
        health: LoggingHealth,
    ) -> None:
        self._health = health
        self._write_retry_at = 0.0
        self._rollover_retry_at = 0.0
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )

    def emit(self, record: logging.LogRecord) -> None:
        if time.monotonic() < self._write_retry_at:
            return
        super().emit(record)

    def doRollover(self) -> None:  # noqa: N802
        """Difiere un rename bloqueado y conserva la escritura en el archivo base."""
        if time.monotonic() < self._rollover_retry_at:
            return
        try:
            super().doRollover()
        except OSError:
            self._rollover_retry_at = time.monotonic() + self._RETRY_SECONDS
            self._health.record_file_error()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        del record
        self._write_retry_at = time.monotonic() + self._RETRY_SECONDS
        self._health.record_file_error()


class _SafeQueueListener(logging.handlers.QueueListener):
    """QueueListener con cierre acotado para no colgar tests ni procesos."""

    def __init__(
        self,
        records: queue.Queue[logging.LogRecord | None],
        *handlers: logging.Handler,
        health: LoggingHealth,
    ) -> None:
        super().__init__(records, *handlers, respect_handler_level=True)
        self._health = health

    def stop_with_timeout(self, timeout_s: float) -> bool:
        thread = self._thread
        if thread is None:
            return True

        deadline = time.monotonic() + max(timeout_s, 0.0)
        while True:
            emergency = self._health.pop_emergency()
            if emergency is None:
                break
            try:
                self.queue.put_nowait(emergency)
            except queue.Full:
                self._health.restore_emergency(emergency)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._health.record_listener_error()
                    return False
                time.sleep(min(0.01, remaining))

        while True:
            try:
                self.enqueue_sentinel()
                break
            except queue.Full:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._health.record_listener_error()
                    return False
                time.sleep(min(0.01, remaining))

        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            self._health.record_listener_error()
            return False
        self._thread = None
        return True


class LoggingRuntime:
    """Ownership del handler productor, listener y sinks físicos."""

    def __init__(
        self,
        *,
        records: queue.Queue[logging.LogRecord | None],
        queue_handler: NonBlockingQueueHandler,
        handlers: tuple[logging.Handler, ...],
        health: LoggingHealth,
        log_dir: Path,
    ) -> None:
        self.records = records
        self.queue_handler = queue_handler
        self.handlers = handlers
        self.health = health
        self.log_dir = log_dir
        self._listener = _SafeQueueListener(records, *handlers, health=health)
        self._shutdown_lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        try:
            self._listener.start()
        except RuntimeError:
            self.health.record_listener_error()

    def health_snapshot(self) -> LoggingHealthSnapshot:
        thread = self._listener._thread
        return self.health.snapshot(listener_alive=bool(thread and thread.is_alive()))

    def shutdown(self, *, timeout_s: float = 2.0) -> bool:
        with self._shutdown_lock:
            if self._closed:
                return True
            root_logger = logging.getLogger()
            root_logger.removeHandler(self.queue_handler)
            if not root_logger.handlers:
                root_logger.addHandler(logging.NullHandler())
            self.queue_handler.close()
            stopped = self._listener.stop_with_timeout(timeout_s)
            if stopped:
                for handler in self.handlers:
                    handler.close()
                self._closed = True
            return stopped


class LoggerStream(io.TextIOBase):
    """Adaptador line-buffered para stdout/stderr ausentes en PyInstaller."""

    encoding: str = "utf-8"
    errors: str = "replace"

    def __init__(self, target: logging.Logger, level: int) -> None:
        super().__init__()
        self._target = target
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed logging stream")
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                self._target.log(self._level, "%s", line, extra={"event": "stdio"})
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._target.log(
                self._level,
                "%s",
                self._buffer,
                extra={"event": "stdio"},
            )
            self._buffer = ""

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")
