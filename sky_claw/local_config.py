"""Persistent local configuration for Sky-Claw.

Stores tool paths and user preferences in a JSON file so the user
doesn't have to pass ``--loot-exe`` / ``--xedit-exe`` every time.

API keys are stored as base64-obfuscated values (not plaintext).
This is *not* encryption — it simply prevents casual shoulder-surfing.

The file is created on first use at the path given to :func:`load`
(default: ``sky_claw_config.json`` in the current directory).
"""

from __future__ import annotations

import base64
import json
import logging
import pathlib
import sys
import keyring
import keyring.errors
from dataclasses import dataclass, asdict
from typing import Any, cast, Protocol


class DataclassInstance(Protocol):
    __dataclass_fields__: dict[str, Any]


logger = logging.getLogger(__name__)

_DEFAULT_PATH = pathlib.Path("sky_claw_config.json")


def get_exe_dir() -> pathlib.Path:
    """Return the directory where the .exe lives, or CWD for normal Python."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).parent
    return pathlib.Path.cwd()


@dataclass
class LocalConfig:
    """Runtime configuration persisted between sessions."""

    loot_exe: str | None = None
    xedit_exe: str | None = None
    mo2_root: str | None = None
    install_dir: str | None = None
    skyrim_path: str | None = None
    pandora_exe: str | None = None
    bodyslide_exe: str | None = None
    api_key_b64: str | None = None  # Legacy base64
    nexus_api_key_b64: str | None = None  # Legacy base64
    telegram_bot_token_b64: str | None = None  # Legacy base64
    telegram_chat_id: str | int | None = None
    first_run: bool = True

    def _migrate_and_get(self, legacy_val: str | None, service_name: str) -> str | None:
        """Helper to get from keyring, or migrate from legacy base64 if needed."""
        try:
            stored = keyring.get_password("sky_claw", service_name)
            if stored:
                return stored
        except (keyring.errors.KeyringError, OSError, Exception) as exc:
            logger.error("Failed to read from keyring: %s", exc)

        # Migration logic
        if legacy_val is None:
            return None
        try:
            decoded = base64.b64decode(legacy_val.encode()).decode()
            if decoded:
                try:
                    keyring.set_password("sky_claw", service_name, decoded)
                except (keyring.errors.KeyringError, OSError, Exception) as exc:
                    logger.error("Failed to migrate key to keyring: %s", exc)
            return decoded
        except (ValueError, UnicodeDecodeError) as exc:
            logger.error("Failed to decode legacy key: %s", exc)
            return None

    def get_api_key(self) -> str | None:
        """Return the API key from secure storage."""
        return self._migrate_and_get(self.api_key_b64, "api_key")

    def set_api_key(self, key: str) -> None:
        """Store an API key in secure storage."""
        try:
            keyring.set_password("sky_claw", "api_key", key)
            self.api_key_b64 = None  # Clear legacy
        except (keyring.errors.KeyringError, OSError, Exception) as exc:
            logger.warning(
                "Could not store API key in keyring (%s). "
                "Falling back to base64 encoding in config file.",
                type(exc).__name__,
            )
            self.api_key_b64 = base64.b64encode(key.encode()).decode()

    def get_nexus_api_key(self) -> str | None:
        """Return the Nexus Mods API key from secure storage."""
        return self._migrate_and_get(self.nexus_api_key_b64, "nexus_api_key")

    def set_nexus_api_key(self, key: str) -> None:
        """Store a Nexus Mods API key in secure storage."""
        try:
            keyring.set_password("sky_claw", "nexus_api_key", key)
            self.nexus_api_key_b64 = None
        except (keyring.errors.KeyringError, OSError, Exception) as exc:
            logger.warning(
                "Could not store Nexus API key in keyring (%s). "
                "Falling back to base64 encoding in config file.",
                type(exc).__name__,
            )
            self.nexus_api_key_b64 = base64.b64encode(key.encode()).decode()

    def get_telegram_bot_token(self) -> str | None:
        """Return the Telegram Bot Token from secure storage."""
        return self._migrate_and_get(self.telegram_bot_token_b64, "telegram_bot_token")

    def set_telegram_bot_token(self, token: str) -> None:
        """Store a Telegram Bot Token in secure storage."""
        try:
            keyring.set_password("sky_claw", "telegram_bot_token", token)
            self.telegram_bot_token_b64 = None
        except (keyring.errors.KeyringError, OSError, Exception) as exc:
            logger.warning(
                "Could not store Telegram token in keyring (%s). "
                "Falling back to base64 encoding in config file.",
                type(exc).__name__,
            )
            self.telegram_bot_token_b64 = base64.b64encode(token.encode()).decode()


def load(path: pathlib.Path = _DEFAULT_PATH) -> LocalConfig:
    """Load configuration from *path*, returning defaults if absent."""
    if not path.exists():
        logger.info("No local config at %s — using defaults", path)
        return LocalConfig()

    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return LocalConfig(
            loot_exe=data.get("loot_exe"),
            xedit_exe=data.get("xedit_exe"),
            mo2_root=data.get("mo2_root"),
            install_dir=data.get("install_dir"),
            skyrim_path=data.get("skyrim_path"),
            api_key_b64=data.get("api_key_b64"),
            nexus_api_key_b64=data.get("nexus_api_key_b64"),
            telegram_bot_token_b64=data.get("telegram_bot_token_b64"),
            telegram_chat_id=data.get("telegram_chat_id"),
            pandora_exe=data.get("pandora_exe"),
            bodyslide_exe=data.get("bodyslide_exe"),
            first_run=data.get("first_run", True),
        )
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load local config: %s", exc)
        return LocalConfig()


def save(config: LocalConfig, path: pathlib.Path = _DEFAULT_PATH) -> None:
    """Persist *config* to *path* as pretty-printed JSON."""
    data = asdict(cast(DataclassInstance, config))
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Local config saved to %s", path)
