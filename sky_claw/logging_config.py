import getpass
import logging
import logging.handlers
import os
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import json

from sky_claw.config import Config

# Correlation ID for tracking requests across components
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

# Get current configuration and user for redaction
_GLOBAL_CFG = Config()


_USERNAME_LOOKUP_ERRORS = (OSError, KeyError, ImportError)
_NO_TRACE_ID = "0" * 32

try:
    from opentelemetry import trace as _otel_trace
except ImportError:
    _otel_trace = None


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
    # Google API key (AIza prefix + 35 alphanumeric chars)
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{35}\b"), "[REDACTED]"),
    # Stripe secret/publishable keys (live and test environments)
    (re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    # AWS Secret Access Key — redact the value in key=value context
    (
        re.compile(r"(?i)\baws[_-]secret[_-]access[_-]key([\"'\s:=]+)([A-Za-z0-9/+=]{40})"),
        r"aws_secret_access_key\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(api[_-]?key|apikey|x-api-key|token|secret|password)([\"'\s:=]+)([^\s\"',;}{]{8,})"),
        r"\1\2[REDACTED]",
    ),
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

    def _redact(self, text: str) -> str:
        if not isinstance(text, str):
            return text

        # Mask Telegram Chat ID if configured
        chat_id = str(_GLOBAL_CFG.telegram_chat_id)
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
            return {
                self._redact(key) if isinstance(key, str) else key: self._redact_value(item, seen, depth)
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, seen, depth) for item in value)
        if isinstance(value, list):
            return [self._redact_value(item, seen, depth) for item in value]
        if isinstance(value, set):
            return {self._redact_value(item, seen, depth) for item in value}
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the main message
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)

        # Redact any string arguments passed to the logger
        if record.args:
            record.args = self._redact_value(record.args)

        for key, value in list(record.__dict__.items()):
            if key not in _LOG_RECORD_RESERVED_ATTRS:
                setattr(record, key, self._redact_value(value))

        return True


class CorrelationFilter(logging.Filter):
    """Filter that adds correlation_id and trace_id from context to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()  # type: ignore[attr-defined]
        trace_id = _NO_TRACE_ID
        if _otel_trace is not None:
            try:
                span = _otel_trace.get_current_span()
                ctx = span.get_span_context()
                if ctx.is_valid:
                    trace_id = format(ctx.trace_id, "032x")
            except AttributeError:
                pass
        record.trace_id = trace_id  # type: ignore[attr-defined]
        return True


def setup_logging(level: int = logging.INFO, log_file: str = "sky_claw.log"):
    """Set up structured logging with rotation and specialized handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplication during re-config
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    corr_filter = CorrelationFilter()
    redact_filter = SecurityRedactionFilter()

    # 10 MB per file, 5 backups
    max_bytes = 10 * 1024 * 1024
    backup_count = 5

    # --- Console Handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(correlation_id)s] %(name)s: %(message)s")
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(corr_filter)
    console_handler.addFilter(redact_filter)
    root_logger.addHandler(console_handler)

    # --- File Handlers (Rotating) ---
    os.makedirs("logs", exist_ok=True)

    json_formatter = json.JsonFormatter("%(asctime)s %(levelname)s %(correlation_id)s %(name)s %(message)s")

    def _add_rotating_handler(logger_obj, filename, propagate=True):
        file_path = os.path.join("logs", filename)
        handler = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(json_formatter)
        handler.addFilter(corr_filter)
        handler.addFilter(redact_filter)
        logger_obj.addHandler(handler)
        if not propagate:
            logger_obj.propagate = False

    # Main application log
    _add_rotating_handler(root_logger, log_file)

    # Specialized Watcher Log
    watcher_logger = logging.getLogger("SkyClaw.Watcher")
    _add_rotating_handler(watcher_logger, "watcher.log", propagate=False)

    # Specialized Security Log
    security_logger = logging.getLogger("SkyClaw.Security")
    _add_rotating_handler(security_logger, "watcher_security.log", propagate=False)

    logging.info("Logging initialized (Rotating Enabled) - Core and Specialized Watchers")
