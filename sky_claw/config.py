from __future__ import annotations

import contextlib
import logging
import math
import os
import pathlib
import sys
import tempfile
import tomllib
from typing import Any

import keyring
import keyring.errors

logger = logging.getLogger(__name__)


class SystemPaths:
    """Dynamic path resolution for Windows and WSL2 environments."""

    @staticmethod
    def get_base_drive() -> pathlib.Path:
        """Returns the base drive (C:/ or /mnt/c/) based on environment."""
        if sys.platform != "win32":
            # Check for WSL
            if pathlib.Path("/mnt/c").exists():
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

    def __init__(self, config_path: pathlib.Path | None = None) -> None:
        self._config_path = config_path or self.DEFAULT_CONFIG_FILE
        self._data: dict[str, Any] = self._load_defaults()
        #: Secretos cuyo ÚNICO ejemplar es el archivo: estaban en plaintext en el
        #: TOML y no se pudieron mover al keyring. `save()` los conserva en vez de
        #: borrarlos (ver el bloque de secretos ahí). Se vacía en cuanto el
        #: keyring acepta el secreto: a partir de ahí el archivo ya no es el único
        #: ejemplar y preservar el plaintext sería deshacer la migración.
        self._secretos_solo_en_archivo: dict[str, str] = {}
        self._load_from_file()
        self._migrate_llm_model()
        self._load_from_keyring()

    def _migrate_llm_model(self) -> None:
        """One-time, in-memory migration of the legacy global ``llm_model``.

        Models are now provider-scoped (``{provider}_model``) so switching
        providers never carries a stale, incompatible model. If a legacy global
        ``llm_model`` is set and the active provider has no model of its own
        yet, copy it into that provider's slot. Non-destructive (the TOML is not
        rewritten) and idempotent across repeated ``Config`` construction.
        """
        legacy = self._data.get("llm_model")
        if not legacy:
            return
        provider = str(self._data.get("llm_provider") or "").lower()
        slot = f"{provider}_model"
        if slot in self._data and not self._data.get(slot):
            self._data[slot] = legacy

    def _load_from_keyring(self) -> None:
        sensitive_keys = [
            "llm_api_key",
            "openai_api_key",
            "anthropic_api_key",
            "deepseek_api_key",
            "nexus_api_key",
            "search_api_key",
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
            except (keyring.errors.KeyringError, OSError):
                stored = None

            if plaintext and not stored:
                try:
                    keyring.set_password("sky_claw", key, plaintext)
                    migrated = True
                except (keyring.errors.KeyringError, OSError):
                    # El keyring no lo aceptó: el archivo sigue siendo el único
                    # ejemplar de este secreto. Registrarlo para que `save()` no
                    # lo borre al reintentar la migración y volver a fallar.
                    self._secretos_solo_en_archivo[key] = str(plaintext)

        # S3-FIX: If we migrated secrets to keyring, scrub them from the TOML on disk.
        # Verify the save succeeds; if it fails, log a critical warning so the
        # operator is aware that plaintext secrets remain on disk.
        if migrated:
            try:
                self.save()
                logger.info("Migrated sensitive keys to keyring and scrubbed plaintext from TOML.")
            except OSError as exc:
                logger.critical(
                    "Failed to scrub plaintext secrets from TOML after keyring migration: %s. "
                    "Sensitive data may remain on disk at %s.",
                    exc,
                    self._config_path,
                )

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
            "llm_model": "",  # legacy global model — migrated to {provider}_model on load
            "anthropic_model": "",
            "deepseek_model": "",
            "openai_model": "",
            "ollama_model": "",
            "llm_api_key": "",
            "openai_api_key": "",
            "anthropic_api_key": "",
            "deepseek_api_key": "",
            "nexus_api_key": "",
            "search_api_key": "",
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "first_run": True,
        }

    def _load_from_file(self) -> None:
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

                    # Audit S-3: drop the nested sections we already extracted
                    # to flat keys so update() doesn't re-introduce them as raw
                    # dicts (which would shadow the canonical flat form and
                    # confuse downstream precedence).  The remaining update()
                    # only carries genuinely top-level keys; if a top-level
                    # explicit override is present (e.g. ``telegram_bot_token =
                    # "Y"``) it wins over the nested-extracted value, which is
                    # the documented precedence for the dual-format support.
                    for nested_key in ("telegram", "nexus", "paths"):
                        file_data.pop(nested_key, None)
                    self._data.update(file_data)
            except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
                logger.warning("Failed to load config.toml: %s", exc)

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"'Config' object has no attribute '{name}'")

    def save(self) -> None:
        """Persist current configuration to TOML."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        import tomli_w

        sensitive_keys = [
            "llm_api_key",
            "openai_api_key",
            "anthropic_api_key",
            "deepseek_api_key",
            "nexus_api_key",
            "search_api_key",
            "telegram_bot_token",
        ]

        save_data = dict(self._data)
        for key in sensitive_keys:
            val = save_data.pop(key, None)
            if val:
                try:
                    keyring.set_password("sky_claw", key, str(val))
                except (keyring.errors.KeyringError, OSError) as exc:
                    logger.warning(
                        "Could not store '%s' in keyring: %s. "
                        "Secret will NOT be persisted — configure a keyring backend.",
                        key,
                        type(exc).__name__,
                    )
                    # Fail-closed en las DOS direcciones. No se escribe plaintext
                    # NUEVO en el archivo (la política de secretos no se degrada
                    # porque el keyring falle), pero tampoco se BORRA el que el
                    # usuario ya tenía ahí: el `pop` de arriba es incondicional, así
                    # que sin esto un keyring caído destruía el único ejemplar del
                    # secreto en el mismo save() que decía estar protegiéndolo — y
                    # no hay backup ni escritura atómica para recuperarlo. Se
                    # restaura el valor QUE ESTABA EN EL ARCHIVO, nunca el de
                    # memoria: un secreto recién tipeado no baja a disco en claro.
                    anterior = self._secretos_solo_en_archivo.get(key)
                    if anterior:
                        save_data[key] = anterior
                else:
                    # El keyring lo tiene: el archivo deja de ser el único ejemplar
                    # y el scrub del plaintext procede de forma definitiva.
                    self._secretos_solo_en_archivo.pop(key, None)

        # Escritura atómica: `open(path, "wb")` trunca el archivo ANTES de
        # escribir el contenido nuevo. Un fallo a mitad de la serialización
        # (disco lleno, OSError transitorio en Windows, el proceso matado)
        # dejaba el TOML en 0 bytes — la config anterior, completa y válida,
        # desaparecía por un fallo que no tenía nada que ver con ella. El
        # temporal vive en el mismo directorio que el destino (mismo
        # filesystem, condición de `os.replace` para ser atómico en
        # POSIX/NTFS) y se limpia si algo falla antes del replace.
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=self._config_path.parent, prefix=".sky_claw_config_", suffix=".toml.tmp"
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                tomli_w.dump(save_data, f)
            os.replace(tmp_name, self._config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

        # La actualización es solo en memoria: evita releer TOML/keyring desde
        # el filtro que corre en productores asyncio.
        from sky_claw.logging_config import update_logging_redaction_context

        update_logging_redaction_context(
            telegram_chat_id=str(self._data.get("telegram_chat_id") or ""),
        )

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
    return pathlib.Path(cfg.mo2_root) / "mod_registry.db" if cfg.mo2_root else pathlib.Path("mod_registry.db")


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
        # H-02: "github.com" removed — it was also in OUT_OF_SCOPE_HOSTS,
        # creating a semantic contradiction.  api.github.com covers API needs.
        "raw.githubusercontent.com",
        # Brave Search API — powers the search_nexus tool's web discovery
        # (scoped to nexusmods.com via the query, enriched via the official
        # Nexus API). GET only; see ALLOWED_METHODS below.
        "api.search.brave.com",
        "api.anthropic.com",
        "www.reddit.com",
        # SKSE — sitio oficial de Silverlock, único origen de los binarios del
        # script extender (no hay mirror en GitHub). GET only; ver ALLOWED_METHODS.
        # Sin esta entrada `ensure_skse` muere en el hop 0 del gateway DESPUÉS de
        # consumir la aprobación HITL del operador.
        "skse.silverlock.org",
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

# GitHub release asset API calls redirect to signed CDN URLs. These hosts are
# intentionally not part of ALLOWED_HOSTS; NetworkGateway may only use them as
# validated redirect targets for /repos/.../releases/assets/{id} downloads.
GITHUB_RELEASE_ASSET_REDIRECT_HOSTS = frozenset(
    [
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
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
    # F-01: "github.com" entry removed — host is not in ALLOWED_HOSTS (see H-02).
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
    "www.reddit.com": frozenset(["GET"]),
    "api.search.brave.com": frozenset(["GET"]),
    "skse.silverlock.org": frozenset(["GET"]),
}
TELEGRAM_PATH_PREFIX = "/bot"
NEXUS_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
NEXUS_DOWNLOAD_TIMEOUT_SECONDS = 600

# ── Centralized Search Paths (DRY — A4) ─────────────────────────────
STEAM_DEFAULT_PATHS: tuple[str, ...] = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"D:\Steam",
    r"D:\SteamLibrary",
    r"E:\Steam",
    r"E:\SteamLibrary",
    r"F:\Steam",
    r"F:\SteamLibrary",
)

MO2_COMMON_PATHS: tuple[str, ...] = (
    r"C:\Modding\MO2",
    r"D:\Modding\MO2",
    r"E:\Modding\MO2",
    r"C:\MO2Portable",
    r"D:\MO2Portable",
    r"C:\Games\MO2",
    r"D:\Games\MO2",
)

SKYRIM_COMMON_PATHS: tuple[str, ...] = (
    r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition",
    r"C:\Program Files\Steam\steamapps\common\Skyrim Special Edition",
    r"D:\SteamLibrary\steamapps\common\Skyrim Special Edition",
    r"D:\Steam\steamapps\common\Skyrim Special Edition",
    r"E:\SteamLibrary\steamapps\common\Skyrim Special Edition",
    r"D:\Games\Skyrim Special Edition",
    r"E:\Games\Skyrim Special Edition",
    # LE se instala en `common\Skyrim` con `Skyrim.exe`. Sin estas entradas, la rama
    # de rutas comunes de los dos detectores es inalcanzable para LE por más que
    # acepte `Skyrim.exe` — y LE es una de las tres ediciones que soporta SKSE_CONFIG.
    r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim",
    r"C:\Program Files\Steam\steamapps\common\Skyrim",
    r"D:\SteamLibrary\steamapps\common\Skyrim",
    r"D:\Steam\steamapps\common\Skyrim",
    r"E:\SteamLibrary\steamapps\common\Skyrim",
    r"D:\Games\Skyrim",
    r"E:\Games\Skyrim",
)

LOOT_SEARCH_PATHS: tuple[str, ...] = (
    r"C:\Modding\LOOT",
    r"D:\Modding\LOOT",
    r"C:\Program Files\LOOT",
    r"C:\Program Files (x86)\LOOT",
)

XEDIT_SEARCH_PATHS: tuple[str, ...] = (
    r"C:\Modding\SSEEdit",
    r"D:\Modding\SSEEdit",
    r"C:\Modding\xEdit",
    r"D:\Modding\xEdit",
    r"C:\Program Files\SSEEdit",
)

COMMON_TOOL_ROOTS: tuple[str, ...] = (
    r"C:\Modding",
    r"D:\Modding",
    r"E:\Modding",
    r"C:\Games",
    r"D:\Games",
)

# ── Magic Number Constants (A5) ──────────────────────────────────────
SKYRIM_SE_APPID: str = "489830"
SEARCH_TIMEOUT_SECONDS: float = 5.0
AE_MIN_SIZE_MB: int = 60
AE_MIN_MINOR_VERSION: int = 6
PROCESS_KILL_TIMEOUT_SECONDS: float = 3.0
CREATE_NO_WINDOW: int = 0x08000000


def _umbral_env(nombre: str, defecto: float) -> float:
    """Lee un umbral de entorno, cayendo al default si no es un número válido."""
    crudo = os.getenv(nombre)
    if not crudo:
        return defecto
    try:
        valor = float(crudo)
    except ValueError:
        logger.warning("%s='%s' no es un número; se usa el default %.1f", nombre, crudo, defecto)
        return defecto
    # ``isfinite`` y no solo ``<= 0``: NaN e infinito ESQUIVAN esa comparación
    # (con NaN toda comparación es False, e inf sí es > 0) y llegan intactos a
    # ``asyncio.sleep``, que con cualquiera de los dos NUNCA retorna. Un
    # ``=nan`` desactivaría el umbral en silencio en vez de fallar ruidoso.
    # También cubre el desborde: "1e400" parsea a inf sin parecerse a "inf".
    if not math.isfinite(valor) or valor <= 0:
        logger.warning(
            "%s='%s' debe ser un número finito > 0; se usa el default %.1f",
            nombre,
            crudo,
            defecto,
        )
        return defecto
    return valor


# ── Watchdog de cierre de la GUI (post-mortem WinError 10048) ────────
# Ajustables por entorno: un reconnect legítimo del navegador puede pasarse de
# la gracia tras una suspensión del equipo, carga alta o antivirus agresivo, y
# el operador necesita poder subirlos sin recompilar.
GUI_EXIT_WATCHDOG_GRACE_SECONDS: float = _umbral_env("SKY_CLAW_GUI_EXIT_GRACE_SECONDS", 15.0)
# Plazo para la PRIMERA conexión: cubre el arranque en frío del .exe + el
# escaneo del antivirus + el lanzamiento del navegador, así que es generoso.
GUI_EXIT_WATCHDOG_FIRST_CONNECT_SECONDS: float = _umbral_env("SKY_CLAW_GUI_EXIT_FIRST_CONNECT_SECONDS", 90.0)
