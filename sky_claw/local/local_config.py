"""Persistent local configuration for Sky-Claw.

Stores tool paths and user preferences in a JSON file so the user
doesn't have to pass ``--loot-exe`` / ``--xedit-exe`` every time.

API keys are stored as base64-obfuscated values (not plaintext).
This is *not* encryption — it simply prevents casual shoulder-surfing.

The file is created on first use at the path given to :func:`load`
(default: ``sky_claw_config.json`` in the current directory).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import sys
import tempfile
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

    Escritura atómica (temporal + ``os.replace``, mismo directorio que el
    destino): un ``write_text`` directo trunca el archivo antes de escribir el
    contenido nuevo, así que un fallo a mitad de camino dejaba 0 bytes donde
    antes había una config completa y válida.
    """
    data = asdict(cast("DataclassInstance", config))
    contenido = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".sky_claw_config_", suffix=".json.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(contenido)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
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

    Fail-closed: si el archivo existe y tiene contenido pero no parsea, se
    propaga el error del parser en vez de devolver un objeto con defaults. Los
    dos formatos tienen lectores tolerantes (``load`` atrapa
    ``JSONDecodeError``, ``Config`` atrapa ``TOMLDecodeError``) que se quedan
    con los defaults y siguen: combinado con un ``save`` que reescribe todo,
    eso convierte un archivo ilegible —o un ``OSError`` transitorio, que en
    Windows es un lock de otro proceso— en la pérdida completa de la
    configuración.

    Un archivo de 0 bytes es "sin datos todavía" (arranque interrumpido antes
    de la primera escritura, un ``touch`` manual), no "corrupto" — un TOML
    vacío YA es válido (``tomllib.loads("") == {}``) y se trata igual acá para
    el JSON legacy, donde ``json.loads("")`` sí lanza: sin este caso especial,
    un ``sky_claw_config.json`` vacío bloqueaba la persistencia con el mismo
    fail-closed que un archivo con basura adentro, que es una situación
    distinta y sí debe abortar (ver el test de contenido inválido).

    Bloqueante (I/O + keyring): llamar desde un thread en contexto async.

    Raises:
        tomllib.TOMLDecodeError | json.JSONDecodeError: El archivo existe, NO
            está vacío, y no parsea en su formato.
        OSError: El archivo existe y no se pudo leer.
    """
    es_legacy = path.suffix.lower() == _SUFIJO_JSON_LEGACY
    if path.exists():
        # Parsear ANTES de construir: los constructores no distinguen "vacío"
        # de "ilegible", y esa diferencia acá vale toda la config del usuario.
        texto = path.read_text(encoding="utf-8")
        if es_legacy and texto.strip():
            json.loads(texto)
        elif not es_legacy:
            tomllib.loads(texto)  # ya tolera "" -> {}; no hace falta el guard
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


# ----------------------------------------------------------------------------
# Serialización entre GUI y agente LLM (mismo proceso, mismo config.toml)
# ----------------------------------------------------------------------------
# GUI (`run_ritual_install` → `persistir_campo`) y agente LLM (`setup_tools` →
# `guardar_config`) corren en el mismo proceso y el mismo event loop —
# comparten `AppContext` igual que el lock ya existente para los sorts de LOOT
# (`app_context.py`, comentario "Audit #190: shared lock-DB staging dir... para
# que el mundo agente-tools y la GUI serialicen"). Sin un lock análogo acá, dos
# `to_thread` concurrentes pueden intercalar sus `os.replace` sobre el MISMO
# `config.toml` — no hay garantía de que uno complete antes de que el otro
# empiece. El lock compartido (:func:`_obtener_lock_de_escritura`) serializa
# el ciclo COMPLETO (lectura incluida, no sólo la escritura final): si sólo se
# serializara el `save()`, el lector de `persistir_campo` podría partir de un
# archivo que el otro escritor está a punto de sobreescribir con datos más
# viejos que los que ya están en disco.
#
# LO QUE GARANTIZA EL CONJUNTO: el archivo nunca queda corrupto ni a medio
# escribir — un `save()` completo siempre corre antes de que el otro empiece —
# y DESDE F1 TAMPOCO se pierde contenido: `Config.save()` hace merge-on-save
# (relee el disco bajo su threading.Lock por path y aplica solo el diff del
# objeto contra su baseline — ver `sky_claw/config.py`), así que un `Config`
# long-lived (el `local_cfg` que `AppContext` construye una vez por sesión) ya
# no puede pisar con su próximo `save()` un campo que otro camino escribió con
# lectura fresca. La frontera está anclada en
# `tests/test_local_config_persistencia.py` (sección "Carrera GUI ↔ agente
# LLM", cerrada por F1) y en `tests/test_config_secretos_sin_keyring.py`
# (los borrados de secretos van DESPUÉS del merge y no pueden resucitar).
#
# Es además un lock de PROCESO (vale para GUI+agente conviviendo en la misma
# instancia de Sky-Claw), no de archivo: no protege contra otro proceso
# tocando el mismo config.toml (p.ej. dos instancias corriendo a la vez), que
# es un escenario no soportado en otras partes del código tampoco. Los dos
# locks no son redundantes: el threading.Lock por path de `Config.save()` es
# el coordinador del ARCHIVO — toda mutación del TOML pasa por él con lectura
# fresca, incluido el ciclo de `persistir_campo`, cuya escritura final también
# es un merge-on-save (los llamadores directos de `.save()` que se saltean
# este lock asyncio quedan cubiertos igual); el de este módulo además
# serializa los ciclos de los wrappers. Ancla del interleaving "GUI lee →
# agente escribe → GUI reemplaza": `tests/test_local_config_persistencia.py`.
_lock_de_escritura: asyncio.Lock | None = None
_loop_del_lock: asyncio.AbstractEventLoop | None = None


def _obtener_lock_de_escritura() -> asyncio.Lock:
    """Devuelve el lock compartido, recreándolo si cambió el event loop.

    Cada otro `asyncio.Lock` del árbol vive como atributo de una instancia de
    vida larga (``self._lock = asyncio.Lock()`` dentro de un ``__init__``), así
    que se recrea junto con esa instancia — en producción, una vez por
    proceso; en tests, una vez por instancia de test. Este lock no tiene una
    instancia dueña (GUI y agente no comparten un objeto, sólo el módulo), así
    que un `asyncio.Lock()` a nivel de módulo se ataría al PRIMER event loop
    que lo toque y quedaría inservible para cualquier loop posterior — en
    producción da igual (un solo loop para toda la vida del proceso), pero
    bajo un runner que crea un loop nuevo por test (pytest-asyncio,
    `asyncio_mode=auto`) el segundo test que lo usa revienta con "Lock is
    bound to a different event loop". Se recrea perezosamente cuando el loop
    que lo pide no es el que lo creó.
    """
    global _lock_de_escritura, _loop_del_lock
    loop_actual = asyncio.get_running_loop()
    if _lock_de_escritura is None or _loop_del_lock is not loop_actual:
        _lock_de_escritura = asyncio.Lock()
        _loop_del_lock = loop_actual
    return _lock_de_escritura


async def guardar_config_bloqueante(config: Any, path: pathlib.Path) -> None:
    """Envoltorio async de :func:`guardar_config`: mismo I/O, serializado con
    :func:`persistir_campo_bloqueante` bajo el mismo lock (ver
    :func:`_obtener_lock_de_escritura`).

    El lock se sostiene mientras el ``to_thread`` está en vuelo: no bloquea el
    event loop (el I/O real corre en el thread pool), pero sí impide que el
    otro escritor arranque su propio ciclo hasta que éste termine.
    """
    async with _obtener_lock_de_escritura():
        await asyncio.to_thread(guardar_config, config, path)


async def persistir_campo_bloqueante(path: pathlib.Path, campo: str, valor: Any) -> None:
    """Envoltorio async de :func:`persistir_campo`, con el mismo lock que
    :func:`guardar_config_bloqueante`. Ver el comentario de módulo sobre por
    qué el lock cubre la lectura y no sólo la escritura.
    """
    async with _obtener_lock_de_escritura():
        await asyncio.to_thread(persistir_campo, path, campo, valor)
