from __future__ import annotations

import contextlib
import copy
import logging
import math
import os
import pathlib
import sys
import tempfile
import threading
import tomllib
from typing import Any

import keyring
import keyring.errors

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Lock de PROCESO para el ciclo leer-mergear-escribir de `Config.save()`
# ----------------------------------------------------------------------------
# `save()` es sync y hay 6 llamadores que la usan directo sin pasar por el lock
# asyncio de `local_config.py` (`frontend_bridge`, `setup_wizard`,
# `sky_claw_gui`, dos en `app_context` y uno dentro del propio `__init__` vía
# `_load_from_keyring`): un `asyncio.Lock` no se puede tomar desde código sync,
# y desde F1 `save()` LEE el disco antes de escribir (merge-on-save), así que
# sin lock acá esos 6 caminos quedan con un TOCTOU leer/escribir. GUI y daemon
# corren en el MISMO proceso (la task `supervisor-daemon` se crea con
# `create_task`, no hay Popen) y el árbol no tiene filelock/portalocker/msvcrt,
# así que el alcance correcto es un `threading.Lock` por path normalizado —
# mismo criterio de identidad que `_install_lock_resource_id`. El orden de
# adquisición con el lock asyncio de los wrappers bloqueantes es siempre el
# mismo (asyncio afuera → threading adentro), así que no hay deadlock.
_LOCKS_DE_CONFIG: dict[str, threading.Lock] = {}
_GUARDIA_LOCKS_DE_CONFIG = threading.Lock()


def _lock_de_config(path: pathlib.Path) -> threading.Lock:
    """Lock compartido por path real del archivo, cualquiera sea el `Config`
    que lo guarde: dos objetos sobre el mismo config.toml serializan su
    leer-mergear-escribir completo."""
    clave = os.path.normcase(str(path.resolve())).casefold()
    with _GUARDIA_LOCKS_DE_CONFIG:
        lock = _LOCKS_DE_CONFIG.get(clave)
        if lock is None:
            lock = threading.Lock()
            _LOCKS_DE_CONFIG[clave] = lock
        return lock


def _parse_toml_de_config_a_plano(texto: str) -> dict[str, Any]:
    """Parsea el TOML de config y devuelve la vista PLANA.

    Extrae las secciones legacy ``[telegram]``/``[nexus]``/``[paths]`` a sus
    claves planas y las DESCARTA de la vista, igual que hace la carga: la
    forma canónica es plana, y si el merge-on-save de `save()` dejara vivas
    las secciones como dicts, shadowearían esa forma — y un ``[nexus]
    api_key`` en plaintext reviviría justo el secreto que el scrub quiere
    afuera (ver `tests/test_config_secretos_sin_keyring.py`).

    Precedencia (Audit #153 / S-3, anclada en
    `tests/test_config_nested_merge.py`): un override explícito top-level
    GANA sobre el valor extraído del nested. Por eso la extracción va a un
    dict aparte y las claves top-level se aplican ENCIMA — si la extracción
    escribiera sobre ``file_data`` directo, el nested taparía al override.
    """
    file_data: dict[str, Any] = tomllib.loads(texto)
    extraido: dict[str, Any] = {}
    telegram = file_data.get("telegram")
    if isinstance(telegram, dict):
        if "token" in telegram:
            extraido["telegram_bot_token"] = telegram["token"]
        if "chat_id" in telegram:
            extraido["telegram_chat_id"] = telegram["chat_id"]
    nexus = file_data.get("nexus")
    if isinstance(nexus, dict) and "api_key" in nexus:
        extraido["nexus_api_key"] = nexus["api_key"]
    paths = file_data.get("paths")
    if isinstance(paths, dict):
        if "mo2_path" in paths:
            extraido["mo2_root"] = paths["mo2_path"]
        if "skyrim_path" in paths:
            extraido["skyrim_path"] = paths["skyrim_path"]
    for nested_key in ("telegram", "nexus", "paths"):
        file_data.pop(nested_key, None)
    return {**extraido, **file_data}


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


class _DatosConfig(dict[str, Any]):
    """El estado de ``Config``, con registro de escrituras explícitas.

    El diff de ``Config.save()`` no puede depender SOLO de comparar valores:
    una asignación explícita del valor que ya estaba en el baseline
    (``setup_tools`` que re-descubre el mismo exe path que el objeto
    long-lived ya tenía) sigue siendo una declaración de intención y debe
    persistirse — la comparación no distingue "sin tocar" de "reasigné lo que
    ya estaba" (review de qodo, P2). ``escribir_campo`` y las asignaciones
    directas ``_data[k] = v`` (``app_context``, ``frontend_bridge``) pasan en
    masa por ``__setitem__``: ningún caller cambia nada.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Claves escritas explícitamente desde la construcción. El diff de
        #: save() las suma a la comparación de valores; el deepcopy del
        #: snapshot arrastra su propia copia (comparada contra la del baseline
        #: para saber qué se escribió DESDE el último save).
        self.dirty: set[str] = set()

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.dirty.add(key)

    def __deepcopy__(self, memo: dict[int, Any]) -> _DatosConfig:
        # Sin esto, `copy.deepcopy` reconstruye la subclase vía protocolo de
        # pickle usando `__setitem__` por clave: la copia arrancaría con TODO
        # marcado dirty, el diff `snapshot.dirty - baseline.dirty` se anulaba
        # entre copias y la señal de escrituras explícitas se perdía (el test
        # de reasignación explícita fallaba exactamente así).
        nuevo = _DatosConfig()
        memo[id(self)] = nuevo
        for clave, valor in self.items():
            nuevo[clave] = copy.deepcopy(valor, memo)
        nuevo.dirty = copy.deepcopy(self.dirty, memo)
        return nuevo


class Config:
    """Central configuration management for Sky-Claw.

    Loads from ~/.sky_claw/config.toml, allowing overrides via environment
    variables prefixed with SKY_CLAW_.
    """

    DEFAULT_CONFIG_DIR = pathlib.Path.home() / ".sky_claw"
    DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.toml"

    def __init__(self, config_path: pathlib.Path | None = None) -> None:
        self._config_path = config_path or self.DEFAULT_CONFIG_FILE
        self._data: _DatosConfig = _DatosConfig(self._load_defaults())
        #: Baseline para el merge-on-save de `save()`: snapshot de `_data` al
        #: terminar la construcción y refrescado tras cada `save()` exitoso.
        #: El diff `_data` vs baseline en save() es lo que ESTE objeto cambió —
        #: solo eso se aplica sobre la lectura fresca del disco, así que un
        #: objeto long-lived no puede pisar campos que otro camino escribió
        #: después de su construcción. Durante la construcción es `None`
        #: (`_load_from_keyring` puede llamar `save()` antes de que exista):
        #: save() lo interpreta como "volcar el estado entero", el
        #: comportamiento histórico, correcto para ese único save de arranque
        #: que migra/scrubea secretos recién leídos.
        self._baseline: _DatosConfig | None = None
        #: Secretos cuyo ÚNICO ejemplar es el archivo: estaban en plaintext en el
        #: TOML y no se pudieron mover al keyring. `save()` los conserva en vez de
        #: borrarlos (ver el bloque de secretos ahí). Se vacía en cuanto el
        #: keyring acepta el secreto: a partir de ahí el archivo ya no es el único
        #: ejemplar y preservar el plaintext sería deshacer la migración.
        self._secretos_solo_en_archivo: dict[str, str] = {}
        self._load_from_file()
        self._migrate_llm_model()
        self._load_from_keyring()
        # El snapshot va AL FINAL: `_load_from_keyring` muta `_data` (secretos
        # desde el keyring) y puede llamar `save()`; todo eso es estado "visto al
        # construir", no un cambio del objeto.
        self._baseline = copy.deepcopy(self._data)

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
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                # Desde F1 `save()` LEE el archivo (merge-on-save), así que puede
                # abortar fail-closed con el error del parser (`TOMLDecodeError`
                # es subclase de `ValueError`) o de lectura: ese fallo no puede
                # tumbar la construcción del objeto — se degrada a log crítico,
                # igual que antes se degradaba el fallo de escritura (review de
                # CodeRabbit sobre el contrato de error).
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
                texto = self._config_path.read_text(encoding="utf-8")
                # El parseo compartido (extracción de secciones legacy + drop) es
                # el MISMO que usa el merge-on-save de `save()`: carga y merge
                # no pueden divergir en qué es la vista plana del archivo.
                self._data.update(_parse_toml_de_config_a_plano(texto))
            except (tomllib.TOMLDecodeError, OSError, ValueError, UnicodeDecodeError) as exc:
                logger.warning("Failed to load config.toml: %s", exc)

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"'Config' object has no attribute '{name}'")

    def save(self) -> None:
        """Persiste la configuración actual a TOML con merge-on-save (F1).

        El modelo viejo —reemplazar el archivo entero con ``_data``— perdía
        campos: un ``Config`` long-lived (``AppContext`` construye uno por
        sesión) pisaba en su próximo ``save()`` TODO lo que otra superficie
        hubiera escrito con lectura fresca después de su construcción. Desde
        F1 ``save()``:

        1. relee el disco bajo el lock por path (:func:`_lock_de_config`) —
           leer y escribir son una unidad indivisible: sin lock, los 6
           llamadores directos de ``save()`` (que no pasan por el lock asyncio
           de ``local_config.py``) tendrían TOCTOU;
        2. aplica al estado fresco SOLO los campos que ESTE objeto cambió
           (snapshot de `_data` ANTES del diff, reutilizado como nuevo
           baseline; dirty-keys + comparación de valores contra el baseline
           anterior) — un objeto desactualizado ya no puede borrar lo que
           nunca vio;
        3. hace los borrados DESPUÉS del merge: el scrub de secretos y el drop
           de secciones legacy no pueden resucitar vía la lectura fresca (ver
           ``tests/test_config_secretos_sin_keyring.py``, la segunda pasada);
        4. escribe atómicamente (temporal + ``os.replace``) y recién tras el
           replace exitoso refresca el baseline.

        Todo escritor de Config participa por construcción: el merge vive acá,
        no en un wrapper. Anclas en ``tests/test_local_config_persistencia.py``.

        Raises:
            tomllib.TOMLDecodeError | UnicodeDecodeError | OSError: El archivo
                existe y no se pudo leer/parsear. Fail-closed declarado: no se
                mergea —y por tanto no se escribe— sobre lo que no se puede
                leer; el archivo queda intacto y el llamador decide (misma
                doctrina que ``local_config.abrir_config_para_persistir``).
        """
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        import tomli_w

        with _lock_de_config(self._config_path):
            fresco: dict[str, Any] = {}
            if self._config_path.exists():
                # Fail-closed, misma doctrina que
                # `local_config.abrir_config_para_persistir`: no se mergea —y
                # por tanto no se escribe— sobre lo que no se pudo leer.
                fresco = _parse_toml_de_config_a_plano(self._config_path.read_text(encoding="utf-8"))

            # Snapshot ANTES del diff: mientras save() corre en un thread del
            # pool (vía `guardar_config_bloqueante`), otra invocación
            # concurrente puede mutar el objeto compartido (dos `setup_tools`
            # sobre el mismo `local_cfg`). Si el baseline se copiara del
            # `_data` VIVO al final, esa mutación entraría al baseline pero no
            # al diff guardado, y los saves siguientes la tratarían como ya
            # persistida — pérdida silenciosa (review de qodo, P1). El mismo
            # snapshot se reutiliza como nuevo baseline: lo persistido y la
            # base del próximo diff son exactamente el mismo estado.
            snapshot = copy.deepcopy(self._data)

            baseline = self._baseline
            if baseline is None:
                # El save DENTRO de la construcción (`_load_from_keyring` migra
                # secretos y llama `save()` antes de que exista el snapshot):
                # vuelca el estado entero, el comportamiento histórico.
                diff: dict[str, Any] = dict(snapshot)
                borrados: set[str] = set()
            else:
                # Claves escritas explícitamente desde el último baseline: la
                # comparación de valores sola no detecta la reasignación
                # explícita de un valor igual al baseline (`setup_tools` que
                # re-descubre el mismo path), y el merge conservaría lo que
                # otra superficie escribió encima (review de qodo, P2).
                dirty_desde_baseline = snapshot.dirty - baseline.dirty
                diff = {
                    k: v
                    for k, v in snapshot.items()
                    if k in dirty_desde_baseline or k not in baseline or baseline[k] != v
                }
                borrados = set(baseline) - set(snapshot)

            save_data = {**fresco, **diff}
            for key in borrados:
                save_data.pop(key, None)

            sensitive_keys = [
                "llm_api_key",
                "openai_api_key",
                "anthropic_api_key",
                "deepseek_api_key",
                "nexus_api_key",
                "search_api_key",
                "telegram_bot_token",
            ]

            for key in sensitive_keys:
                # Autoridad del keyring (review de qodo, P1): el candidato a
                # persistir en el keyring es el valor del OBJETO —resuelto
                # contra el keyring en la construcción, o recién tipeado—
                # cuando existe; el plaintext del archivo rige SOLO si el
                # objeto no conoce el secreto. Pasar sin más el valor de la
                # lectura fresca sobrescribía una credencial del keyring MÁS
                # NUEVA con el plaintext obsoleto del TOML cuando el secreto
                # no formaba parte del diff: se rompía la autenticación y se
                # destruía la única credencial vigente.
                val_objeto = snapshot.get(key) or ""
                val_archivo = fresco.get(key) or ""
                save_data.pop(key, None)
                candidato = val_objeto or val_archivo
                if candidato:
                    try:
                        keyring.set_password("sky_claw", key, str(candidato))
                    except (keyring.errors.KeyringError, OSError) as exc:
                        logger.warning(
                            "Could not store '%s' in keyring: %s. "
                            "Secret will NOT be persisted — configure a keyring backend.",
                            key,
                            type(exc).__name__,
                        )
                        # Fail-closed en las DOS direcciones. No se escribe
                        # plaintext NUEVO en el archivo (la política de
                        # secretos no se degrada porque el keyring falle): si
                        # el candidato era un secreto recién tipeado que no
                        # estaba en disco, `val_archivo` es vacío y no se
                        # restaura nada. Pero tampoco se DESTRUYE el único
                        # ejemplar: si el secreto SÍ estaba en el archivo, se
                        # conserva exactamente lo que hay en disco (review de
                        # CodeRabbit: el snapshot de construcción no alcanza,
                        # otra superficie puede haberlo escrito después).
                        if val_archivo:
                            save_data[key] = val_archivo
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

            # Recién tras el replace exitoso: si la escritura falló, el próximo
            # save reintenta el MISMO diff en vez de dárselo por aplicado. EL
            # MISMO snapshot del diff (no un deepcopy fresco del `_data` vivo):
            # una mutación concurrente durante la escritura no puede quedar
            # tratada como ya persistida (review de qodo, P1).
            self._baseline = snapshot

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
