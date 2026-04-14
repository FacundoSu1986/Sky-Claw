import logging
import logging.handlers
import os
import re
import sys
from contextvars import ContextVar

from pythonjsonlogger import json

from sky_claw.config import Config

# Correlation ID for tracking requests across components
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

# Get current configuration and user for redaction
_GLOBAL_CFG = Config()
_CURRENT_USER = os.environ.get("USERNAME", os.environ.get("USER", "User"))

_REDACTION_PATTERNS = [
    re.compile(r"[0-9]{8,10}:[a-zA-Z0-9_\-]{35}"),  # Telegram bot token
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # LLM API Keys (DeepSeek/OpenAI)
    re.compile(rf"(?i)(Users[\\/]){re.escape(_CURRENT_USER)}"),  # Windows User Path
    re.compile(
        r'(?i)(?:api_key|apikey|token)["\s:=]+([A-Za-z0-9_\-]{16,})'
    ),  # General Generic Keys
]


class SecurityRedactionFilter(logging.Filter):
    """Filter that redacts sensitive credentials and PII from log messages."""

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
        for pattern in _REDACTION_PATTERNS:
            text = pattern.sub("[REDACTED]", text)

        return text

    def filter(self, record):
        # Redact the main message
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)

        # Redact any string arguments passed to the logger
        if record.args:
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    new_args.append(self._redact(arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)

        return True


class CorrelationFilter(logging.Filter):
    """Filter that adds correlation_id from ContextVar to each record."""

    def filter(self, record):
        record.correlation_id = correlation_id_var.get()
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
    MAX_BYTES = 10 * 1024 * 1024
    BACKUP_COUNT = 5

    # --- Console Handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(correlation_id)s] %(name)s: %(message)s"
    )
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(corr_filter)
    console_handler.addFilter(redact_filter)
    root_logger.addHandler(console_handler)

    # --- File Handlers (Rotating) ---
    os.makedirs("logs", exist_ok=True)

    json_formatter = json.JsonFormatter(
        "%(asctime)s %(levelname)s %(correlation_id)s %(name)s %(message)s"
    )

    def _add_rotating_handler(logger_obj, filename, propagate=True):
        file_path = os.path.join("logs", filename)
        handler = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
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

    logging.info(
        "Logging initialized (Rotating Enabled) - Core and Specialized Watchers"
    )
