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
import dataclasses
import json
import logging
import pathlib
import sys
import tomllib
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

import keyring
import keyring.errors


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
    # Nombres de los mods MO2 instalados por ensure_ngio (NGIO no tiene exe;
    # el orquestador los necesita para add_mod_to_modlist).
    ngio_mods: list[str] | None = None
    # Nombres de los mods MO2 instalados por ensure_community_shaders
    # (Address Library, SSE Engine Fixes, Community Shaders) — mismo contrato.
    community_shaders_mods: list[str] | None = None
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
                "Could not store API key in keyring (%s). Falling back to base64 encoding in config file.",
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
                "Could not store Nexus API key in keyring (%s). Falling back to base64 encoding in config file.",
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
                "Could not store Telegram token in keyring (%s). Falling back to base64 encoding in config file.",
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
            ngio_mods=data.get("ngio_mods"),
            community_shaders_mods=data.get("community_shaders_mods"),
            first_run=data.get("first_run", True),
        )
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load local config: %s", exc)
        return LocalConfig()


def save(config: LocalConfig, path: pathlib.Path = _DEFAULT_PATH) -> None:
    """Persist *config* to *path* as pretty-printed JSON.

    Escribe JSON **pase lo que pase**: sólo es correcto sobre el formato legacy
    (``sky_claw_config.json``). El código de aplicación no debe llamarlo
    directo — usa :func:`guardar_config`, que despacha por el formato real del
    archivo. `tests/test_local_config_persistencia.py` congela los importadores.
    """
    data = asdict(cast("DataclassInstance", config))
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Local config saved to %s", path)


# ----------------------------------------------------------------------------
# Persistencia agnóstica del formato (GUI y agente LLM comparten estas piezas)
# ----------------------------------------------------------------------------
# Hay DOS clases de config circulando y no son intercambiables: `Config`
# (`sky_claw/config.py`, TOML, estado en `_data`, es la que `AppContext` inyecta
# en el registry del agente y a la que apunta `AppContext.config_path`) y
# `LocalConfig` (dataclass, JSON, formato legacy que todavía migra
# `AppContext._migrate_legacy_json`). Ambos `save` reescriben el archivo entero
# sin backup ni escritura atómica, así que elegir mal el lector o el escritor no
# degrada: destruye la config del usuario en silencio.

#: Extensión del formato legacy. Todo lo demás se trata como el TOML canónico:
#: ante una extensión desconocida conviene equivocarse hacia el formato vigente,
#: no hacia el deprecado.
_SUFIJO_JSON_LEGACY = ".json"


def abrir_config_para_persistir(path: pathlib.Path) -> Any:
    """Devuelve el objeto de config que sabe leer *y* reescribir *path*.

    Fail-closed: si el archivo existe pero no parsea, se propaga el error del
    parser en vez de devolver un objeto con defaults. Los dos formatos tienen
    lectores tolerantes (``load`` atrapa ``JSONDecodeError``, ``Config`` atrapa
    ``TOMLDecodeError``) que se quedan con los defaults y siguen: combinado con
    un ``save`` que reescribe todo, eso convierte un archivo ilegible —o un
    ``OSError`` transitorio, que en Windows es un lock de otro proceso— en la
    pérdida completa de la configuración.

    Bloqueante (I/O + keyring): llamar desde un thread en contexto async.

    Raises:
        tomllib.TOMLDecodeError | json.JSONDecodeError: El archivo existe y no
            parsea en su formato.
        OSError: El archivo existe y no se pudo leer.
    """
    es_legacy = path.suffix.lower() == _SUFIJO_JSON_LEGACY
    if path.exists():
        # Parsear ANTES de construir: los constructores no distinguen "vacío"
        # de "ilegible", y esa diferencia acá vale toda la config del usuario.
        texto = path.read_text(encoding="utf-8")
        if es_legacy:
            json.loads(texto)
        else:
            tomllib.loads(texto)
    if es_legacy:
        return load(path)
    # Diferido: construir un `Config` lee keyring y no hace falta en el camino
    # legacy (y mantiene `sky_claw.local` sin dependencia dura del top-level).
    from sky_claw.config import Config

    return Config(path)


def escribir_campo(config: Any, campo: str, valor: Any) -> None:
    """Setea *campo* en *config* sin que el caller tenga que saber de qué clase es.

    `Config` guarda su estado en ``_data`` y su ``__getattr__`` sólo lee de ahí:
    un ``setattr`` crudo crearía un atributo de instancia que ningún ``save``
    mira nunca (el bug era exactamente ése, escrito dos veces en dos ramas).
    """
    data = getattr(config, "_data", None)
    if isinstance(data, dict):
        data[campo] = valor
    else:
        setattr(config, campo, valor)


def guardar_config(config: Any, path: pathlib.Path) -> None:
    """Persiste *config* por el mecanismo de su propia clase.

    Una dataclass (`LocalConfig`) va por el :func:`save` JSON de este módulo;
    cualquier otra config persiste con su propio ``save()`` (`Config` escribe
    TOML y mueve los secretos al keyring). Un objeto sin ninguno de los dos
    caminos aborta: escribir JSON "por las dudas" es lo que borraba el TOML.

    Bloqueante: llamar desde un thread en contexto async.

    Raises:
        TypeError: *config* no expone ninguna forma conocida de persistirse.
    """
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        save(cast("LocalConfig", config), path)
        return
    guardar = getattr(config, "save", None)
    if callable(guardar):
        guardar()
        return
    raise TypeError(f"La config {type(config).__name__!r} no sabe persistirse (ni dataclass ni .save()): abortado")


def persistir_campo(path: pathlib.Path, campo: str, valor: Any) -> None:
    """Ciclo leer-modificar-escribir de un solo campo sobre *path*.

    Para callers que tienen el path pero no el objeto vivo de config (la GUI).
    Quien ya tiene el objeto —el agente LLM, que lo recibe inyectado— debe usar
    :func:`escribir_campo` + :func:`guardar_config` sobre ESA instancia, para no
    pisar cambios en memoria que todavía no se guardaron.

    Bloqueante: llamar desde un thread en contexto async.
    """
    config = abrir_config_para_persistir(path)
    escribir_campo(config, campo, valor)
    guardar_config(config, path)
