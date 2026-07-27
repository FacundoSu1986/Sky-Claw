import asyncio
import contextlib
import getpass
import hashlib
import logging
import pathlib
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol, TextIO, cast

from pythonjsonlogger import json

if TYPE_CHECKING:
    from opentelemetry.context import Context as _OtelContext

from sky_claw._logging_runtime import (
    FailSafeRotatingFileHandler,
    LoggerStream,
    LoggingHealth,
    LoggingRuntime,
    NonBlockingQueueHandler,
)

logger = logging.getLogger("sky_claw")

# Correlation ID for tracking requests across components
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

#: Campos del log JSON. ``trace_id`` es explícito (paridad con
#: ``correlation_id``): ambos los setea CorrelationFilter, pero sin declararlo
#: aquí quedaba a merced del extra-merge de pythonjsonlogger (que solo lo emite
#: si el record ya trae el atributo). Como campo requerido, siempre aparece.
_JSON_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(correlation_id)s %(trace_id)s "
    "%(process_role)s %(process)d %(threadName)s %(task_name)s "
    "%(name)s %(message)s %(exc_text)s"
)


_USERNAME_LOOKUP_ERRORS = (OSError, KeyError, ImportError)
_NO_TRACE_ID = "0" * 32


class _SpanContext(Protocol):
    @property
    def is_valid(self) -> bool: ...

    @property
    def trace_id(self) -> int: ...


class _Span(Protocol):
    def get_span_context(self) -> _SpanContext: ...


class _GetCurrentSpan(Protocol):
    def __call__(self, context: "_OtelContext | None" = None) -> _Span: ...


_get_current_span: _GetCurrentSpan | None
try:
    from opentelemetry.trace import get_current_span as _imported_get_current_span
except ImportError:
    _get_current_span = None
else:
    _get_current_span = _imported_get_current_span


def _resolve_current_user() -> str:
    try:
        return getpass.getuser()
    except _USERNAME_LOOKUP_ERRORS:
        return "User"


_CURRENT_USER = _resolve_current_user()

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9]{6,12}:[a-zA-Z0-9_\-]{30,90}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-(?:proj|ant|live|test)?-?[a-zA-Z0-9_\-]{20,}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b(Bearer\s+)[^\s\"',;}{]{8,}"), r"\1[REDACTED]"),
    # GitHub tokens (classic ghp_/gho_/ghu_/ghs_/ghr_ are 36 chars; cap at 255 for future-compat)
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"), "[REDACTED]"),
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    # Slack tokens
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    # GitLab personal/project/group tokens
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"), "[REDACTED]"),
    # Raw JWT (3-segment eyJ… header.payload.signature)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "[REDACTED]"),
    # Google API key (AIza prefix + 35 alphanumeric/dash/underscore chars).
    # Lookahead instead of \b: \b fails when the key ends in '-' (non-word char).
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])"), "[REDACTED]"),
    # Stripe secret/publishable keys (live and test environments)
    (re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    # AWS Secret Access Key — redact the value in key=value context;
    # capture the original key token (group 1) to preserve its casing/separators.
    (
        re.compile(r"(?i)(\baws[_-]secret[_-]access[_-]key)([\"'\s:=]+)([A-Za-z0-9/+=]{40})"),
        r"\1\2[REDACTED]",
    ),
    # Credentials in URL query strings (access_token=, key=, sig=, …) — names
    # the generic key=value pattern below misses: \b fails after '_'
    # (access_token), and key/sig/session/auth are not in its alternation.
    # The value class excludes '&' so neighbouring query params survive.
    (
        re.compile(
            r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|id[_-]?token"
            r"|client[_-]?secret|signature|session|auth|key|sig)=)[^\s&\"',;}{]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(api[_-]?key|apikey|x-api-key|token|secret|password)([\"'\s:=]+)([^\s\"',;}{]{8,})"),
        r"\1\2[REDACTED]",
    ),
)

# Keys whose *values* must be redacted unconditionally regardless of value shape.
# Used by _redact_container to handle structured log extras such as
# {"aws_secret_access_key": "<value>"} where the value has no recognisable prefix.
# Each alternative is a WHOLE secret key name (anchored by \b on both sides): a
# bare 'token' is deliberately excluded so token-budget telemetry keys
# (token_count, max_tokens, prompt_tokens, token_budget) keep their values.
_SENSITIVE_KEY_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(?:aws[_-]secret[_-]access[_-]key|client[_-]secret|(?:bot|access|refresh)[_-]token)\b"
)

_LOG_RECORD_RESERVED_ATTRS = frozenset(
    logging.LogRecord(
        name="",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"asctime", "correlation_id", "message"}


class SecurityRedactionFilter(logging.Filter):
    """Filter that redacts sensitive credentials and PII from log messages."""

    _MAX_DEPTH: int = 64  # Guard against pathologically deep (non-cyclic) structures.

    def __init__(
        self,
        *,
        telegram_chat_id: str = "",
        chat_id_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._telegram_chat_id = telegram_chat_id
        self._chat_id_provider = chat_id_provider

    def _redact(self, text: str) -> str:
        if not isinstance(text, str):
            return text

        # Mask Telegram Chat ID if configured
        chat_id = self._chat_id_provider() if self._chat_id_provider is not None else self._telegram_chat_id
        if chat_id and len(chat_id) > 5:
            text = text.replace(chat_id, "[REDACTED]")

        # Mask Windows User Paths (C:\Users\Admin -> C:\Users\***)
        text = re.sub(rf"(?i)(Users[\\/]){re.escape(_CURRENT_USER)}", r"\1***", text)

        # Mask API Keys and Tokens
        for pattern, replacement in _REDACTION_PATTERNS:
            text = pattern.sub(replacement, text)

        return text

    def _redact_value(self, value: Any, seen: set[int] | None = None, depth: int = 0) -> Any:
        if depth >= self._MAX_DEPTH:
            return "[REDACTED:DEPTH]"
        if isinstance(value, str):
            return self._redact(value)
        if not isinstance(value, (Mapping, tuple, list, set)):
            return value
        if seen is None:
            seen = set()

        value_id = id(value)
        if value_id in seen:
            return "[REDACTED:CYCLE]"

        seen.add(value_id)
        try:
            return self._redact_container(value, seen, depth + 1)
        finally:
            seen.remove(value_id)

    def _redact_container(self, value: Any, seen: set[int], depth: int) -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                redacted_key = self._redact(key) if isinstance(key, str) else key
                # Key-aware redaction: when the key identifies a credential type
                # whose value has no recognisable prefix (e.g. AWS Secret Access Key),
                # redact the value directly instead of relying on pattern matching.
                if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                    result[redacted_key] = "[REDACTED]"
                else:
                    result[redacted_key] = self._redact_value(item, seen, depth)
            return result
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, seen, depth) for item in value)
        if isinstance(value, list):
            return [self._redact_value(item, seen, depth) for item in value]
        if isinstance(value, set):
            return {self._redact_value(item, seen, depth) for item in value}
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the main message
        record.msg = self._redact_value(record.msg)

        # Redact any string arguments passed to the logger
        if record.args:
            record.args = self._redact_value(record.args)

        for key, value in list(record.__dict__.items()):
            if key in _LOG_RECORD_RESERVED_ATTRS:
                continue
            # `extra={"client_secret": v}` lands here as a top-level attribute,
            # not inside a Mapping — so apply the same key-aware redaction that
            # _redact_container does, otherwise shapeless secrets leak.
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                setattr(record, key, "[REDACTED]")
            else:
                setattr(record, key, self._redact_value(value))

        # Redact secrets that may surface in exception tracebacks. Render the
        # traceback to text once and scrub it, so each handler's formatter reuses
        # the redacted ``exc_text`` instead of re-rendering the raw frames (which
        # would bypass redaction and leak tokens/passwords from exception text).
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._redact(record.exc_text)

        # A diferencia de exc_info, stack_info (logger.warning(..., stack_info=True))
        # ya llega renderizado como texto por la stdlib: no hay paso de formateo
        # que interceptar, así que sin esta rama viaja sin redactar. Vector real:
        # NiceGUI llama logging.getLogger("nicegui").warning(..., stack_info=True)
        # en Client.check_existence(), y ese logger propaga al root sin
        # propagate=False.
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)

        return True


class CorrelationFilter(logging.Filter):
    """Filter that adds correlation_id and trace_id from context to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        trace_id = _NO_TRACE_ID
        if _get_current_span is not None:
            try:
                span = _get_current_span()
                ctx = span.get_span_context()
                if ctx.is_valid:
                    trace_id = format(ctx.trace_id, "032x")
            except AttributeError:
                pass
        record.trace_id = trace_id
        return True


class RuntimeContextFilter(logging.Filter):
    """Captura contexto del productor antes de cruzar al hilo listener."""

    def __init__(self, process_role: str) -> None:
        super().__init__()
        self._process_role = process_role

    def filter(self, record: logging.LogRecord) -> bool:
        task_name = ""
        with contextlib.suppress(RuntimeError):
            task = asyncio.current_task()
            if task is not None:
                task_name = task.get_name()
        record.process_role = self._process_role
        if not hasattr(record, "task_name"):
            record.task_name = task_name
        if not hasattr(record, "event"):
            record.event = "log"
        return True


def install_loop_exception_handler() -> None:
    """Route otherwise-unhandled event-loop exceptions (e.g. from fire-and-forget
    tasks) to the structured logger instead of asyncio's default stderr handler,
    so they flow through the JSON log + secret-redaction pipeline for root-cause
    analysis. Best-effort: no-ops when no loop is running.

    Vive acá (y no en ``__main__``) porque cada event loop necesita su propio
    handler: ``_main`` lo instala para las rutas CLI/Telegram/oneshot, y la GUI
    (loop propio de NiceGUI) lo instala en su bootstrap — ambos comparten esta
    única implementación.
    """

    def _handler(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        exc = context.get("exception")
        exc_info = (type(exc), exc, exc.__traceback__) if isinstance(exc, BaseException) else None
        task = context.get("task") or context.get("future")
        task_name = task.get_name() if isinstance(task, asyncio.Task) else ""
        logger.error(
            "Unhandled event-loop exception: %s",
            context.get("message", ""),
            exc_info=exc_info,
            extra={
                "event": "event_loop_exception",
                "task_name": task_name,
            },
        )

    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().set_exception_handler(_handler)


class _LoggerPrefixFilter(logging.Filter):
    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefix)


class _ExcludeEventFilter(logging.Filter):
    def __init__(self, event: str) -> None:
        super().__init__()
        self._event = event

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "event", "") != self._event


_QUEUE_CAPACITY = 8192
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5
_CONSOLE_DEFAULT = object()
_MAX_STDERR_BYTES = 64 * 1024
_runtime: LoggingRuntime | None = None
_runtime_lock = threading.Lock()
_telegram_chat_id_for_redaction = ""

#: Roles cuyo proceso nunca habla con Telegram. Resolver el chat id ahí implica
#: construir ``Config()`` —ocho lecturas del Credential Manager— en cada proceso
#: lanzado, y hacer transitar todos los secretos por su memoria, sin comprar
#: ninguna redacción a cambio. El worker VFS se lanza una vez por job.
_ROLES_SIN_REDACCION_TELEGRAM = frozenset({"vfs_worker"})


def default_log_dir() -> pathlib.Path:
    """Directorio persistente y estable para todos los modos de ejecución."""
    return pathlib.Path.home() / ".sky_claw" / "logs"


def subprocess_error_extra(
    *,
    operation: str,
    tool: str,
    exit_code: int | None,
    stderr: str,
    job_id: str | None = None,
    child_pid: int | None = None,
    pipeline_stage: int | None = None,
) -> dict[str, object]:
    """Normaliza evidencia terminal sin permitir logs de tamaño ilimitado."""
    stderr_bytes = stderr.encode("utf-8", errors="replace")
    truncated = len(stderr_bytes) > _MAX_STDERR_BYTES
    if truncated:
        rendered_stderr = stderr_bytes[-_MAX_STDERR_BYTES:].decode(
            "utf-8",
            errors="replace",
        )
        while len(rendered_stderr.encode("utf-8")) > _MAX_STDERR_BYTES:
            rendered_stderr = rendered_stderr[1:]
    else:
        rendered_stderr = stderr

    extra: dict[str, object] = {
        "event": "external_process_failed",
        "operation": operation,
        "tool": tool,
        "exit_code": exit_code,
        "stderr": rendered_stderr,
        "stderr_size": len(stderr_bytes),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "stderr_truncated": truncated,
    }
    if job_id is not None:
        extra["job_id"] = job_id
    if child_pid is not None:
        extra["child_pid"] = child_pid
    if pipeline_stage is not None:
        extra["pipeline_stage"] = pipeline_stage
    return extra


def _resolve_telegram_chat_id() -> str:
    """Carga lazy y fail-safe: importar logging nunca toca TOML ni keyring."""
    try:
        from sky_claw.config import Config

        return str(Config().telegram_chat_id or "")
    except Exception:
        return ""


def _current_telegram_chat_id() -> str:
    """Lectura en memoria usada por el filtro productor; nunca realiza I/O."""
    return _telegram_chat_id_for_redaction


def update_logging_redaction_context(*, telegram_chat_id: str) -> None:
    """Actualiza valores redactables tras guardar configuración, sin reiniciar."""
    global _telegram_chat_id_for_redaction
    _telegram_chat_id_for_redaction = str(telegram_chat_id or "")


def _json_formatter() -> json.JsonFormatter:
    formatter = json.JsonFormatter(
        _JSON_LOG_FORMAT,
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        rename_fields={
            "asctime": "timestamp",
            "levelname": "level",
            "name": "logger",
            "process": "pid",
            "threadName": "thread",
            "exc_text": "exc_info",
        },
    )
    formatter.converter = time.gmtime
    return formatter


def _file_handler(
    path: pathlib.Path,
    *,
    health: LoggingHealth,
    formatter: logging.Formatter,
) -> FailSafeRotatingFileHandler:
    handler = FailSafeRotatingFileHandler(
        path,
        max_bytes=_MAX_BYTES,
        backup_count=_BACKUP_COUNT,
        health=health,
    )
    handler.setFormatter(formatter)
    return handler


def setup_logging(
    level: int = logging.INFO,
    log_file: str = "sky_claw.log",
    *,
    log_dir: pathlib.Path | None = None,
    process_role: str = "app",
    console_stream: TextIO | None | object = _CONSOLE_DEFAULT,
) -> LoggingRuntime:
    """Configura un único pipeline no bloqueante para el proceso actual."""
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            logging.getLogger().setLevel(level)
            return _runtime

        target_dir = default_log_dir() if log_dir is None else pathlib.Path(log_dir)
        health = LoggingHealth()
        file_ready = True
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            health.record_file_error()
            file_ready = False

        records: queue.Queue[logging.LogRecord | None] = queue.Queue(maxsize=_QUEUE_CAPACITY)
        queue_handler = NonBlockingQueueHandler(records, health)
        queue_handler.addFilter(CorrelationFilter())
        queue_handler.addFilter(RuntimeContextFilter(process_role))
        if process_role not in _ROLES_SIN_REDACCION_TELEGRAM:
            update_logging_redaction_context(telegram_chat_id=_resolve_telegram_chat_id())
        queue_handler.addFilter(SecurityRedactionFilter(chat_id_provider=_current_telegram_chat_id))

        handlers: list[logging.Handler] = []
        stream = sys.stdout if console_stream is _CONSOLE_DEFAULT else cast(TextIO | None, console_stream)
        if stream is not None and not isinstance(stream, LoggerStream):
            # Una consola Windows con codepage limitado (cp1252, no UTF-8) no
            # puede codificar buena parte de los mensajes en español del repo
            # (tildes, flechas, emojis). Sin esto, TextIOWrapper.write falla
            # completo (no parcial) con UnicodeEncodeError, StreamHandler.emit
            # lo deriva a handleError, y la stdlib imprime "--- Logging error
            # ---" en vez del mensaje real: se pierde de la consola sin dejar
            # rastro legible. getattr en vez de isinstance(io.TextIOWrapper):
            # streams inyectados en tests (io.StringIO) no tienen reconfigure()
            # y no deben romper.
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                with contextlib.suppress(ValueError, OSError):
                    reconfigure(errors="backslashreplace")
            console_handler = logging.StreamHandler(stream)
            console_handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] [%(correlation_id)s] %(name)s: %(message)s")
            )
            console_handler.addFilter(_ExcludeEventFilter("logging_initialized"))
            handlers.append(console_handler)

        if file_ready:
            formatter = _json_formatter()
            main_handler = _file_handler(
                target_dir / log_file,
                health=health,
                formatter=formatter,
            )
            handlers.append(main_handler)

            if process_role != "vfs_worker":
                crash_handler = _file_handler(
                    target_dir / "crash.log",
                    health=health,
                    formatter=formatter,
                )
                crash_handler.setLevel(logging.ERROR)
                handlers.append(crash_handler)

            if process_role == "app":
                watcher_handler = _file_handler(
                    target_dir / "watcher.log",
                    health=health,
                    formatter=formatter,
                )
                watcher_handler.addFilter(_LoggerPrefixFilter("SkyClaw.Watcher"))
                handlers.append(watcher_handler)

                security_handler = _file_handler(
                    target_dir / "watcher_security.log",
                    health=health,
                    formatter=formatter,
                )
                security_handler.addFilter(_LoggerPrefixFilter("SkyClaw.Security"))
                handlers.append(security_handler)

        root_logger = logging.getLogger()
        root_logger.setLevel(level)
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        root_logger.addHandler(queue_handler)
        for name in ("SkyClaw.Watcher", "SkyClaw.Security"):
            specialized = logging.getLogger(name)
            specialized.handlers.clear()
            specialized.propagate = True

        runtime = LoggingRuntime(
            records=records,
            queue_handler=queue_handler,
            handlers=tuple(handlers),
            health=health,
            log_dir=target_dir,
        )
        runtime.start()
        _runtime = runtime

    logging.getLogger("sky_claw").info(
        "Logging async-safe inicializado en %s",
        target_dir,
        extra={"event": "logging_initialized"},
    )
    return runtime


def shutdown_logging(timeout_s: float = 2.0) -> bool:
    """Drena y detiene el listener fuera del event loop; es idempotente."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            return True
        try:
            stopped = _runtime.shutdown(timeout_s=timeout_s)
        except Exception:
            _runtime.health.record_listener_error()
            return False
        if stopped:
            _runtime = None
        return stopped


def flush_logging(timeout_s: float = 1.0) -> bool:
    """Espera el drenado de la cola; en async debe llamarse con ``to_thread``."""
    with _runtime_lock:
        runtime = _runtime
    if runtime is None:
        return True
    return runtime.wait_until_idle(timeout_s=timeout_s)


def install_missing_std_stream_adapters() -> None:
    """En builds windowed, convierte salida de terceros en records estructurados."""
    if sys.stdout is None:
        sys.stdout = LoggerStream(logging.getLogger("SkyClaw.Stdout"), logging.INFO)
    if sys.stderr is None:
        # stderr es el stream normal de Uvicorn incluso para mensajes INFO; el
        # stream físico no determina por sí solo la severidad del evento.
        sys.stderr = LoggerStream(logging.getLogger("SkyClaw.Stderr"), logging.INFO)
