import logging
import os
import pathlib
import tomllib
import keyring
from typing import Any, Optional
import sys

logger = logging.getLogger(__name__)


class SystemPaths:
    """Dynamic path resolution for Windows and WSL2 environments."""

    @staticmethod
    def get_base_drive() -> pathlib.Path:
        """Returns the base drive (C:/ or /mnt/c/) based on environment."""
        if sys.platform != "win32":
            # Check for WSL
            if os.path.exists("/mnt/c"):
                return pathlib.Path("/mnt/c")
        return pathlib.Path("C:/")

    @classmethod
    def resolve(cls, path_str: str) -> pathlib.Path:
        """Converts a Windows-style path string to a dynamic pathlib.Path."""
        if not path_str:
            return pathlib.Path()

        # Standardize separators
        std_path = path_str.replace("\\", "/")

        # If it looks like a Windows absolute path, re-map it
        if len(std_path) > 1 and std_path[1] == ":":
            drive_letter = std_path[0].lower()
            relative = std_path[3:]  # Skip 'C:/'
            if sys.platform != "win32":
                return cls.get_base_drive().parent / drive_letter / relative
            return pathlib.Path(f"{drive_letter.upper()}:/") / relative

        return pathlib.Path(std_path)

    @classmethod
    def modding_root(cls) -> pathlib.Path:
        return cls.get_base_drive() / "Modding"


class Config:
    """Central configuration management for Sky-Claw.

    Loads from ~/.sky_claw/config.toml, allowing overrides via environment
    variables prefixed with SKY_CLAW_.
    """

    DEFAULT_CONFIG_DIR = pathlib.Path.home() / ".sky_claw"
    DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.toml"

    def __init__(self, config_path: Optional[pathlib.Path] = None):
        self._config_path = config_path or self.DEFAULT_CONFIG_FILE
        self._data: dict[str, Any] = self._load_defaults()
        self._load_from_file()
        self._load_from_keyring()
        self._load_from_env()

    def _load_from_keyring(self):
        sensitive_keys = [
            "llm_api_key",
            "openai_api_key",
            "anthropic_api_key",
            "deepseek_api_key",
            "nexus_api_key",
            "telegram_bot_token",
            "ws_auth_token",
        ]
        migrated = False
        for key in sensitive_keys:
            plaintext = self._data.get(key)
            try:
                stored = keyring.get_password("sky_claw", key)
                if stored:
                    self._data[key] = stored
                    if plaintext and plaintext == stored:
                        migrated = True
            except Exception:
                stored = None

            if plaintext and not stored:
                try:
                    keyring.set_password("sky_claw", key, plaintext)
                    migrated = True
                except Exception:
                    pass

        # If we found plain text keys and migrated them, scrub them from the TOML
        if migrated:
            self.save()

    def _load_defaults(self) -> dict[str, Any]:
        return {
            "mo2_root": "",
            "install_dir": str(SystemPaths.modding_root()),
            "loot_exe": "",
            "xedit_exe": "",
            "pandora_exe": "",
            "bodyslide_exe": "",
            "skyrim_path": "",
            "llm_provider": "deepseek",
            "llm_model": "",
            "llm_api_key": "",
            "openai_api_key": "",
            "anthropic_api_key": "",
            "deepseek_api_key": "",
            "nexus_api_key": "",
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "first_run": True,
        }

    def _load_from_file(self):
        if self._config_path.exists():
            try:
                with open(self._config_path, "rb") as f:
                    file_data = tomllib.load(f)

                    # Support for nested structure [telegram] token, [nexus] api_key, [paths] mo2_path
                    if "telegram" in file_data:
                        t = file_data["telegram"]
                        if "token" in t:
                            self._data["telegram_bot_token"] = t["token"]
                        if "chat_id" in t:
                            self._data["telegram_chat_id"] = t["chat_id"]

                    if "nexus" in file_data:
                        n = file_data["nexus"]
                        if "api_key" in n:
                            self._data["nexus_api_key"] = n["api_key"]

                    if "paths" in file_data:
                        p = file_data["paths"]
                        if "mo2_path" in p:
                            self._data["mo2_root"] = p["mo2_path"]
                        if "skyrim_path" in p:
                            self._data["skyrim_path"] = p["skyrim_path"]

                    # Also update flatly for any remaining top-level keys
                    # This might overwrite what we just set if both formats exist, but that's okay.
                    self._data.update(file_data)
            except Exception as exc:
                print(f"Warning: Failed to load config.toml: {exc}")
                pass

    def _load_from_env(self):
        for key in self._data.keys():
            env_key = f"SKY_CLAW_{key.upper()}"
            env_val = os.environ.get(env_key)
            if env_val:
                # Basic type conversion for boolean
                if isinstance(self._data[key], bool):
                    self._data[key] = env_val.lower() in ("true", "1", "yes")
                else:
                    self._data[key] = env_val

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"'Config' object has no attribute '{name}'")

    def save(self):
        """Persist current configuration to TOML."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        import tomli_w

        sensitive_keys = [
            "llm_api_key",
            "openai_api_key",
            "anthropic_api_key",
            "deepseek_api_key",
            "nexus_api_key",
            "telegram_bot_token",
        ]

        save_data = dict(self._data)
        for key in sensitive_keys:
            val = save_data.pop(key, None)
            if val:
                try:
                    keyring.set_password("sky_claw", key, str(val))
                except Exception as exc:
                    logger.warning(
                        "Could not store '%s' in keyring: %s. "
                        "Secret will NOT be persisted — configure a keyring backend.",
                        key,
                        type(exc).__name__,
                    )
                    # Fail-closed: do NOT fall back to plaintext in config file

        with open(self._config_path, "wb") as f:
            tomli_w.dump(save_data, f)

    @property
    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


# ── Backward Compatibility ──────────────────────────────────────────
# These constants are used by legacy modules and tests.
# In the future, they should move to the Config class or TOML.

_global_cfg: Config | None = None


def _get_config() -> Config:
    global _global_cfg
    if _global_cfg is None:
        _global_cfg = Config()
    return _global_cfg


def _get_db_path() -> pathlib.Path:
    cfg = _get_config()
    return (
        pathlib.Path(cfg.mo2_root) / "mod_registry.db"
        if cfg.mo2_root
        else pathlib.Path("mod_registry.db")
    )


DB_PATH = _get_db_path()
ALLOWED_HOSTS = frozenset(
    [
        "api.deepseek.com",
        "api.openai.com",
        "api.telegram.org",
        "www.nexusmods.com",
        "api.nexusmods.com",
        "premium-files.nexusmods.com",
        "cf-files.nexusmods.com",
        "staticdelivery.nexusmods.com",
        "api.github.com",
        "github.com",
        "raw.githubusercontent.com",
        "api.anthropic.com",
    ]
)
OUT_OF_SCOPE_HOSTS = frozenset(
    [
        "github.com",
        "discord.com",
        "dropbox.com",
        "mega.nz",
        "patreon.com",
    ]
)
HITL_TIMEOUT_SECONDS = 300

# Refactored common paths using SystemPaths abstraction
LOOT_COMMON_PATHS = [
    SystemPaths.get_base_drive() / "Program Files/LOOT/loot.exe",
    SystemPaths.get_base_drive() / "Program Files (x86)/LOOT/loot.exe",
]
XEDIT_COMMON_PATHS = [
    SystemPaths.get_base_drive() / "Program Files/SSEEdit/SSEEdit.exe",
    SystemPaths.modding_root() / "SSEEdit/SSEEdit.exe",
]

# Mapping of host patterns to allowed HTTP methods.
ALLOWED_METHODS = {
    "api.nexusmods.com": frozenset(["GET", "POST", "HEAD"]),
    "github.com": frozenset(["GET"]),
    "raw.githubusercontent.com": frozenset(["GET"]),
    "api.anthropic.com": frozenset(["POST"]),
    "api.deepseek.com": frozenset(["POST"]),
    "api.openai.com": frozenset(["POST"]),
    "api.telegram.org": frozenset(["GET", "POST"]),
    "api.github.com": frozenset(["GET"]),
    "www.nexusmods.com": frozenset(["GET"]),
    "premium-files.nexusmods.com": frozenset(["GET"]),
    "cf-files.nexusmods.com": frozenset(["GET"]),
    "staticdelivery.nexusmods.com": frozenset(["GET"]),
}
TELEGRAM_PATH_PREFIX = "/bot"
NEXUS_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
NEXUS_DOWNLOAD_TIMEOUT_SECONDS = 600
