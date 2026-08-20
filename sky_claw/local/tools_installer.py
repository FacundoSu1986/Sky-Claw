"""Auto-installer for external tools (LOOT, SSEEdit).

Downloads official releases from GitHub when the tools are not found
locally.  Every download requires mandatory HITL operator approval and
passes through :class:`NetworkGateway` for egress control.
"""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import hashlib
import logging
import os
import pathlib
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import aiohttp

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.app.security.links import rmtree_link_aware
from sky_claw.app.security.network_gateway import (
    EgressViolationError,
    NetworkGatewayTimeoutError,
)
from sky_claw.app.security.path_validator import PathValidator, PathViolationError
from sky_claw.config import (
    GITHUB_RELEASE_ASSET_REDIRECT_HOSTS,
    SystemPaths,
)
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.discovery.scanner import (
    PANDORA_EXE_NAMES,
    detect_skyrim_edition,
    read_skyrim_version,
    skse_dll_game_version,
    skyrim_version_matches,
)
from sky_claw.local.thread_bridge import esperar_hilo_ininterrumpible as _esperar_hilo_ininterrumpible

if TYPE_CHECKING:
    from sky_claw.app.scraper.nexus_downloader import NexusDownloader
    from sky_claw.app.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

_ES_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Lock cross-process de instalación (T-31)
# ---------------------------------------------------------------------------
#
# La familia de mutadores de ``ToolsInstaller`` (ensure_loot/ensure_xedit/
# ensure_pandora/ensure_bodyslide/ensure_skse/ensure_ngio/_ensure_github_mod/
# _ensure_nexus_mod) intercala chequeo-de-existencia, descarga, extracción,
# verificación de payload y limpieza sobre el MISMO ``install_dir``/``mod_dir``.
# Dos instalaciones concurrentes —agente LLM y GUI, o dos ventanas— corrompen
# silenciosamente el directorio (review de CodeRabbit en #418, T-31).
#
# La propiedad del mecanismo: el lock cross-process solo protege si TODOS los
# mutadores participan (ancla en tests/test_tools_installer_lock_scoping.py) y
# cubre el ciclo COMPLETO, desde el chequeo de "ya instalado" hasta la limpieza
# final — no solo alrededor de la extracción, o el TOCTOU sigue vivo.
#
# Constantes de instalación. El TTL default de ``DistributedLockManager``
# (600 s) es para rituales xEdit/DynDOLOD; una instalación puede tardar hasta
# ``_SEVENZIP_TIMEOUT_SECONDS`` (300 s) por archive + descarga (600 s) +
# validación, y ``ensure_ngio`` instala hasta 3 componentes. Se usa un TTL de
# instalación y una espera total larga para que el segundo llamador vea
# ``already_existed=True`` en vez de ``LockAcquisitionError``.

#: Agente del instalador — distinto de cada ritual, espejo de
#: ``_RECONCILE_AGENT_ID`` en ``rollback_reconciler.py``.
_INSTALL_AGENT_ID = "tools-installer"

#: TTL del lock de instalación (1 h) — cubre el peor caso de 3 componentes.
_INSTALL_TTL_SECONDS = 3600.0

#: Retries/espera: backoff exponencial máx 10 s × 180 intentos ≈ 30 min de
#: espera total (15 s de backoff escalonado 1+2+4+8 + 176 × 10 s) antes de
#: fallar con ``LockAcquisitionError``. Un segundo llamador espera a que el
#: primero termine (y ve ``already_existed=True``).
_INSTALL_MAX_RETRIES = 180
_INSTALL_BACKOFF_BASE = 1.0
_INSTALL_BACKOFF_MAX = 10.0


def _install_lock_resource_id(path: pathlib.Path) -> str:
    """Clave canónica del lock de instalación para *path*.

    ``PathValidator.validate()`` solo resuelve la ruta, no normaliza
    mayúsculas. Derivamos la clave con ``os.path.normcase(...).casefold()`` —
    igual que ``vfs_instance_id()`` — para que ``C:\\Tools\\LOOT`` y
    ``c:\\tools\\loot`` tomen el MISMO lock sobre el mismo directorio.
    """
    normalized = os.path.normcase(str(path.resolve())).casefold()
    return f"tools-install:{normalized}"


#: Cadena de locks ya tomados por esta llamada (ContextVar). Permite la
#: reentrada: ``ensure_ngio`` delega en ``_ensure_github_mod``/``_ensure_nexus_mod``
#: y ninguno de ellos debe re-adquirir un lock que el caller ya tiene (eso
#: sería un deadlock o doble adquisición sobre el mismo ``resource_id``).
_cadena_de_locks: ContextVar[frozenset[str]] = ContextVar("_cadena_de_locks", default=frozenset())


@contextlib.asynccontextmanager
async def _bajo_lock_de_instalacion(
    lock_manager: DistributedLockManager,
    resource_id: str,
    ttl: float = _INSTALL_TTL_SECONDS,
) -> Any:
    """Adquiere el lock de instalación y lo libera SIEMPRE en ``finally``.

    Si el caller ya tiene este ``resource_id`` en su cadena (reentrada), no se
    re-adquiere: el lock ya está tomado por esta misma llamada.

    ``asyncio.CancelledError`` NO lo captura ``except Exception``, así que el
    ``finally`` es el único lugar seguro para liberar — espejo de
    ``SnapshotTransactionLock.__aexit__``.

    Mientras la zona crítica corre, un heartbeat renueva el lease en
    ``TTL/3``: ``DistributedLockManager.acquire_lock`` usa TTL FIJO sin
    reanudar, así que una instalación que exceda el TTL expiraría su propio
    lease y otro proceso podría reclamar el recurso con la primera todavía
    escribiendo (audit T-31, V7).

    El release es a prueba de cancelación: una segunda ``CancelledError``
    durante el ``await release_lock`` (que hace ``conn.commit``) abortaría el
    release y el lock quedaría retenido hasta el TTL. ``asyncio.shield``
    posterga esa cancelación hasta que el release terminó.

    Args:
        lock_manager: Instancia con la que se adquiere (el caller es dueño del
            ciclo de vida; acá solo se usa para acquire/release). NO puede ser
            ``None``: fail-closed es que sin lock no hay instalación.
        resource_id: Clave canónica del recurso a serializar.
        ttl: Duración del lease. Default a la constante de instalación.
    """
    # Fail-closed: sin lock NO se instala. Aceptar None acá (y hacer yield sin
    # adquirir) contradice el constructor, que exige lock_manager obligatorio —
    # dejaría un mutador silenciosamente sin serializar.
    if lock_manager is None:
        raise ToolInstallError("ToolsInstaller sin lock_manager: se rechaza la instalación (fail-closed).")
    if resource_id in _cadena_de_locks.get():
        yield
        return

    await lock_manager.acquire_lock(resource_id, _INSTALL_AGENT_ID, ttl=ttl)
    token = _cadena_de_locks.set(_cadena_de_locks.get() | {resource_id})
    heartbeat: asyncio.Task[None] | None = None
    if getattr(lock_manager, "renew_lock", None) is not None:
        heartbeat = asyncio.create_task(
            _mantener_lock_vivo(lock_manager, resource_id, ttl),
            name=f"tools-installer-heartbeat-{resource_id}",
        )
    try:
        yield
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — el heartbeat NO puede abortar el finally
                # `_mantener_lock_vivo` deja almacenada cualquier excepcion que
                # `renew_lock` no atrape (sqlite3.Error si esta cubierto, pero no
                # una conexion cerrada a mitad del shutdown), y `await heartbeat`
                # la re-lanza. Si escapara de aca se saltearian las DOS lineas
                # que siguen: el `reset` dejaria el resource_id colgado en la
                # cadena —y una reentrada posterior se saltearia el lock, que es
                # justo lo que este modulo existe para impedir— y el release no
                # correria, reteniendo el recurso hasta que venza el TTL de 1 h.
                logger.error("Heartbeat del lock de instalacion '%s' fallo: %s", resource_id, exc)
        _cadena_de_locks.reset(token)
        try:
            # El release también es cancelable (await + commit): una segunda
            # cancelación durante este await dejaría el lock retenido hasta TTL.
            # Shield posterga la cancelación hasta que el release terminó.
            await asyncio.shield(lock_manager.release_lock(resource_id, _INSTALL_AGENT_ID))
        except Exception as exc:  # noqa: BLE001 — nunca enmascarar el error original
            logger.error("Fallo al liberar el lock de instalación '%s': %s", resource_id, exc)


# ---------------------------------------------------------------------------
# API pública de la familia de instalación
# ---------------------------------------------------------------------------
# Otros mutadores del mismo recurso (agente LLM → install_mod_from_archive)
# consumen estos alias en vez de los helpers privados: un rename interno no
# puede romper la instalación en runtime sin romper el import time del paquete.
install_lock_resource_id = _install_lock_resource_id
bajo_lock_de_instalacion = _bajo_lock_de_instalacion


async def _mantener_lock_vivo(
    lock_manager: DistributedLockManager,
    resource_id: str,
    ttl: float,
) -> None:
    """Renueva el lease del lock de instalación mientras la zona crítica corre.

    ``DistributedLockManager.acquire_lock`` usa TTL fijo (sin heartbeat): una
    instalación que exceda el TTL expira su propio lease y un segundo proceso
    lo reclama con la primera todavía escribiendo (fail-open temporal). Este
    heartbeat renueva a ``TTL/3``; si un renewal falla (lease vencida o
    reclamada por otro), el loop termina y se loguea CRÍTICO — no se aborta la
    instalación (un hilo de extracción en vuelo no se puede matar), pero el
    evento queda en el registro para que el operador sepa que pudo haber
    concurrencia.

    Managers que no exponen ``renew_lock`` (mocks viejos de tests) degradan a
    "sin heartbeat" en vez de romper: el primer ``await renew_fn`` lanza
    ``TypeError`` y el loop retorna.
    """
    divisor = 3.0
    intervalo = max(ttl / divisor, 0.05)
    try:
        while True:
            await asyncio.sleep(intervalo)
            renew_fn = getattr(lock_manager, "renew_lock", None)
            if renew_fn is None:
                return
            try:
                renovado = await renew_fn(resource_id, _INSTALL_AGENT_ID, ttl=ttl)
            except TypeError:
                # renew_lock presente pero no awaitable (MagicMock de tests).
                return
            if not renovado:
                logger.critical(
                    "Lock de instalación '%s' PERDIDO durante la operación: otro "
                    "proceso pudo reclamar el recurso (lease vencida).",
                    resource_id,
                )
                return
    except asyncio.CancelledError:
        raise


# GitHub API endpoints for official releases.
_LOOT_RELEASES_URL = "https://api.github.com/repos/loot/loot/releases/latest"
_XEDIT_RELEASES_URL = "https://api.github.com/repos/TES5Edit/TES5Edit/releases/latest"
_PANDORA_RELEASES_URL = "https://api.github.com/repos/Monitor221hz/Pandora-Behaviour-Engine-Plus/releases/latest"
_NGIO_RELEASES_URL = "https://api.github.com/repos/DwemerEngineer/No-Grass-In-Objects-NG/releases/latest"

# ---------------------------------------------------------------------------
# Configuración de SKSE
# ---------------------------------------------------------------------------
#
# GOG queda FUERA a propósito, y no es un pendiente: es una decisión de producto
# (base de usuarios chica y catálogo de mods compatibles pobre). Silverlock publica
# un archive aparte para GOG (`skse64_*_gog.7z`), así que soportarlo sería agregar su
# entrada acá y pasarle el `store` del snapshot a la selección de payload — no está
# hecho porque no se quiere, no porque falte.
#
# El recorte es fail-closed sin costo: el gate de versión exacta de `ensure_skse` corre
# ANTES del HITL y de la descarga, y GOG AE (1.6.1179) no matchea el 1.6.1170 del
# payload de Steam, así que un usuario de GOG recibe el error sin que se le pida
# aprobación, sin egress y sin que se escriba nada en el directorio del juego. El
# mensaje lo manda a https://skse.silverlock.org/, que es donde está su build.
#
# Esa garantía vale ahora en las DOS ramas del gate. Antes dependía de que la versión
# se pudiera leer: con `read_skyrim_version` devolviendo "" (sin `pefile`, PE que no
# parsea, PE sin recurso de versión) el mismatch no se evaluaba, la edición la decidía
# una heurística de tamaño, y al usuario de GOG se le pedía aprobación y se le bajaba
# el payload de Steam. La rama ilegible también corta.

# `steam_loader` NO declara "este build lo trae": es el NOMBRE del componente para esa
# familia, y lo usan dos consumidores distintos. `_cleanup_orphaned_skse_dlls` lo
# protege del glob `skse*.dll` (borrarlo dejaría la instalación sin loader de Steam), y
# `_verify_skse_installed` lo usa para nombrar el archivo — pero condiciona el aviso a
# que el PAYLOAD lo haya traído, porque qué archivos incluye cada archive es propiedad
# del build, no de la edición.
SKSE_CONFIG: dict[str, dict[str, str | None]] = {
    "AE": {
        "url": "https://skse.silverlock.org/beta/skse64_2_02_06.7z",
        "sha256": "D7297F1A1D613E5265E1AF4DBBFE8BD37A32719C1CCEF363FC6187FA6EBA0848",
        "dll": "skse64_1_6_1170.dll",
        "loader": "skse64_loader.exe",
        "steam_loader": "skse64_steam_loader.dll",
    },
    "SE": {
        "url": "https://skse.silverlock.org/beta/skse64_2_00_20.7z",
        "sha256": "46F70B963B22E3C242BAC45E1716C39349798FBA5B74C978419A10D604B542D7",
        "dll": "skse64_1_5_97.dll",
        "loader": "skse64_loader.exe",
        "steam_loader": "skse64_steam_loader.dll",
    },
    "LE": {
        "url": "https://skse.silverlock.org/beta/skse_1_07_03.7z",
        "sha256": "1825024212A20A6A197BBCD308073B497E516139AD48D60685D5B18AB4BBE76D",
        "dll": "skse_1_9_32.dll",
        "loader": "skse_loader.exe",
        "steam_loader": None,  # LE no tiene steam_loader
    },
}


#: Tamaño máximo aceptado para el archive de SKSE. Los payloads oficiales pesan ~10-15 MB;
#: el techo existe para que un origen comprometido o mal configurado no pueda llenar el
#: disco de staging: el timeout de la descarga acota el TIEMPO, no los BYTES.
_SKSE_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

#: Ejecutables que cuentan como "hay un Skyrim instalado acá", en orden de preferencia.
#: Compartido por los dos lectores del PE (`_detect_skyrim_edition_from_exe` y
#: `_leer_version_del_ejecutable`): con dos listas separadas, agregar una edición a una
#: y no a la otra deja el gate de compatibilidad ciego justo para esa edición.
_SKYRIM_EXE_NAMES = ("SkyrimSE.exe", "SkyrimVR.exe", "Skyrim.exe")

#: Techo absoluto de respaldo para ``_download_asset`` (helper GitHub compartido
#: por todos los ``ensure_*``). Vale cuando la API no publica ``size`` (0): sin
#: él, el streaming escribe sin límite hasta el timeout total de 600 s. Con size
#: publicado rige el size declarado, más estricto. El mayor asset conocido del
#: helper es el core de Community Shaders (~44 MB): 512 MiB dejan margen de sobra
#: sin habilitar un sumidero de disco. Mismo criterio que ``_SKSE_MAX_ARCHIVE_BYTES``.
_GITHUB_ASSET_MAX_BYTES = 512 * 1024 * 1024

# El decodificador de versión de los DLL de SKSE vive en `discovery.scanner` porque el
# scanner necesita la MISMA regla para decidir si la instalación en disco sirve. Se
# importa en vez de duplicarse: dos copias divergen en cuanto alguien agregue un payload.
_skse_dll_game_version = skse_dll_game_version
_game_version_matches = skyrim_version_matches


def _reject_oversized_skse_content_length(resp: Any) -> None:
    """Corta la descarga de SKSE si el origen ya declara un tamaño imposible.

    Es un atajo, no la defensa: un ``Content-Length`` ausente, malformado o mentiroso
    no habilita nada, porque el contador real de bytes se sigue evaluando por chunk.
    """
    raw = getattr(resp, "headers", {}) or {}
    declared = raw.get("Content-Length")
    if declared is None:
        return
    try:
        size = int(declared)
    except (TypeError, ValueError):
        return
    if size > _SKSE_MAX_ARCHIVE_BYTES:
        raise ToolInstallError(
            f"El origen declara un archive de SKSE de {size} bytes, que excede el tamaño "
            f"máximo permitido ({_SKSE_MAX_ARCHIVE_BYTES} bytes)."
        )


def _skse_cfg_field(cfg: dict[str, str | None], field: str) -> str:
    """Lee un campo obligatorio de un payload de ``SKSE_CONFIG``, fallando explícito si falta.

    No usa ``assert``: con ``python -O`` los asserts se compilan fuera y un payload
    incompleto sigue de largo hasta construir un ``install_dir / None`` (``TypeError``
    críptico a mitad de la instalación, después de la aprobación HITL) en vez de cortar
    con el contrato del método.
    """
    value = cfg.get(field)
    if not value:
        raise ToolInstallError(f"Payload de SKSE mal configurado: falta el campo obligatorio '{field}' en SKSE_CONFIG.")
    return value


# Dependencias del precache de grass (SOP §2.8): NGIO-NG desde GitHub; Address
# Library y Grass Cache Helper NG desde Nexus. Los nombres son los directorios
# que ensure_ngio crea bajo mods/ (el orquestador los activa en modlist.txt).
NGIO_MOD_NAME = "No Grass In Objects NG"
ADDRESS_LIBRARY_MOD_NAME = "Address Library for SKSE Plugins"
GRASS_CACHE_HELPER_MOD_NAME = "Grass Cache Helper NG"
_ADDRESS_LIBRARY_NEXUS_ID = 32444
_GRASS_CACHE_HELPER_NEXUS_ID = 101095

# Autoinstalador de Community Shaders (blueprint 1786043077717): el core se
# descarga del release oficial de GitHub — el archivo principal de Nexus ES el
# mismo asset (nexus-upload.yaml: artifact_pattern "CommunityShaders-*.zip") —
# y las dependencias obligatorias (Address Library, SSE Engine Fixes) viven en
# Nexus. SSE Engine Fixes es requisito DURO de runtime de CS (XSEPlugin.cpp:
# LoadLibrary("Data/SKSE/Plugins/EngineFixes.dll")). Los nombres son los
# directorios que ensure_community_shaders crea bajo mods/.
COMMUNITY_SHADERS_MOD_NAME = "Community Shaders"
SSE_ENGINE_FIXES_MOD_NAME = "SSE Engine Fixes"
_CS_RELEASES_URL = "https://api.github.com/repos/community-shaders/skyrim-community-shaders/releases/latest"
_SSE_ENGINE_FIXES_NEXUS_ID = 17230
#: Gates de versión del autoinstalador de CS (texto oficial de Nexus 86492 +
#: descripción de SSE Engine Fixes 7.0.20): SE solo 1.5.97; AE el ecosistema
#: exige 1.6.1170+ — 1.6.640 y otras AE no-latest NO están soportadas por CS.
_CS_SE_VERSION = "1.5.97"
_CS_AE_MIN_VERSION = "1.6.1170"
_CS_SE_VERSION_TUPLE = (1, 5, 97)
_CS_AE_MIN_VERSION_TUPLE = (1, 6, 1170)

#: Pin opcional de SHA-256 por nombre exacto de asset/file. Presente y no
#: coincide → abort + borrar archivo. Ausente → TOFU: se loguea el hash
#: completo (copiable a este dict para pinear). Un release nuevo cambia el
#: nombre del asset, así que un pin viejo nunca bloquea versiones nuevas.
_PINNED_SHA256: dict[str, str] = {}

# Common Windows paths where LOOT / SSEEdit may already be installed.
LOOT_COMMON_PATHS: tuple[pathlib.Path, ...] = (
    SystemPaths.modding_root() / "LOOT",
    SystemPaths.get_base_drive() / "LOOT",
    SystemPaths.get_base_drive() / "Program Files/LOOT",
    SystemPaths.get_base_drive() / "Program Files (x86)/LOOT",
)

XEDIT_COMMON_PATHS: tuple[pathlib.Path, ...] = (
    SystemPaths.modding_root() / "SSEEdit",
    SystemPaths.get_base_drive() / "SSEEdit",
    SystemPaths.get_base_drive() / "Program Files/SSEEdit",
    SystemPaths.get_base_drive() / "Program Files (x86)/SSEEdit",
)

# Chunk size for streaming downloads (1 MB).
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024

#: Techo para cada subproceso de ``7z`` (listado y extracción por separado). Un
#: xEdit de ~30 MB con BCJ2 tarda segundos, pero el binario puede quedarse
#: esperando input si el archive está corrupto: sin timeout el hilo queda colgado
#: para siempre. 5 min es holgado para el caso legítimo y acota el patológico.
_SEVENZIP_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """Metadata for a single GitHub release asset.

    Attributes:
        name: Nombre del asset.
        size: Tamaño declarado en bytes (0 cuando es desconocido).
        download_url: URL de la API de GitHub para el asset.
        browser_download_url: URL pública de descarga.
        sha256: Digest sha256 publicado por la API (``assets[].digest``), en
            hex minúsculo; vacío cuando la API no lo expone (TOFU).
    """

    name: str
    size: int
    download_url: str
    browser_download_url: str
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Resultado de una operación de auto-instalación."""

    tool_name: str
    exe_path: pathlib.Path
    version: str
    already_existed: bool
    success: bool = True
    message: str = ""


@dataclass(frozen=True, slots=True)
class ModInstallResult:
    """Resultado de instalar un componente NGIO como mod de MO2 (sin exe).

    Attributes:
        mod_name: Nombre del directorio del mod bajo ``mods/``.
        mod_dir: Ruta del mod instalado.
        version: Tag de GitHub, ``file_name`` de Nexus, o ``"existing"``.
        already_existed: True si el mod ya estaba instalado (sentinel presente).
    """

    mod_name: str
    mod_dir: pathlib.Path
    version: str
    already_existed: bool


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ToolInstallError(Exception):
    """Raised when a tool installation fails."""


# ---------------------------------------------------------------------------
# Zip-slip protection
# ---------------------------------------------------------------------------


def _is_safe_path(member_path: str) -> bool:
    """Reject paths with traversal components."""
    try:
        from sky_claw.app.core.validators import validate_path_strict

        # Rechazar explícitamente rutas absolutas o con letra de unidad
        if pathlib.PureWindowsPath(member_path).is_absolute() or pathlib.PurePosixPath(member_path).is_absolute():
            return False

        validate_path_strict(member_path)
        return True
    except Exception:
        return False


def _extract_zip_safe(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """Extract a zip archive with zip-slip protection.

    Validates both the relative path (no '..' or absolute paths) and the
    resolved destination path (must remain inside *dest* after resolution).

    Note: ZIP entries with symlink metadata are not extracted as symlinks by Python's
    zipfile module on Windows (the primary target platform), mitigating symlink-escape attacks.
    """
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not _is_safe_path(info.filename):
                raise PathViolationError(f"Zip-slip detected: {info.filename!r}")
            # Secondary check: resolved path must stay inside dest
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest_resolved):
                raise PathViolationError(f"Zip-slip (resolved path escapes sandbox): {info.filename!r}")
            zf.extract(info, dest)


#: Forma ``Clave = valor`` de cada línea del listado ``-slt``. Todo lo que no
#: matchee (ni sea línea en blanco) es salida que este parser no entiende.
_7Z_CLAVE_VALOR = re.compile(r"^[A-Za-z][A-Za-z0-9 ()-]* = ")

#: Claves de ``-slt`` que delatan un enlace. Su destino NO está en ``Path``, así
#: que validar el path del miembro no dice nada sobre a dónde apunta.
_7Z_CLAVES_DE_LINK = ("Symbolic Link = ", "Hard Link = ")

#: ``C:``/``d:`` al principio de un valor — ruta absoluta con letra de unidad.
_7Z_LETRA_DE_UNIDAD = re.compile(r"^[A-Za-z]:")


def _valor_con_traversal(valor: str) -> bool:
    """True si *valor* trae componentes ``..``, raíz, o letra de unidad."""
    normalizado = valor.strip().replace("\\", "/")
    if not normalizado:
        return False
    if normalizado.startswith("/") or _7Z_LETRA_DE_UNIDAD.match(normalizado):
        return True
    return any(parte == ".." for parte in normalizado.split("/"))


def _parse_7z_listing(stdout: str) -> list[str]:
    """Extrae los nombres de miembro de la salida de ``7z l -slt``.

    ``-slt`` imprime un bloque ``clave = valor`` por entrada, separado del
    encabezado del archive por una línea ``----------``. Solo se leen las líneas
    ``Path = ...`` POSTERIORES a ese separador: antes del separador ``Path``
    es la ruta del propio archive, no un miembro.

    El ataque que cierra: un nombre de miembro con un salto de línea embebido
    (legal en NTFS) parte el bloque en dos. El parser ve ``Path = ok.txt``,
    valida eso, y ``7z x`` escribe el nombre COMPLETO
    ``ok.txt\\nAlgo = ../../evil.txt`` — cuyos ``../..`` son separadores reales y
    escapan de *dest*. La segunda línea es la continuación del nombre.

    Exigir forma ``clave = valor`` NO alcanza: el atacante elige el nombre, así
    que puede hacer que la continuación parezca un par válido. Lo que no puede
    evitar es que el payload contenga traversal — sin ``..`` no escapa de *dest*
    y el ataque no sirve. Por eso se valida el valor de TODAS las líneas, no solo
    el de las ``Path =``: la inyección se delata por lo que necesita llevar, no
    por cómo se llama la clave que falsifica.

    Raises:
        ToolInstallError: Si el listado tiene líneas que este parser no entiende
            o que delatan un nombre partido por un salto de línea.
        PathViolationError: Si el archive trae enlaces.
    """
    members: list[str] = []
    en_cuerpo = False
    for linea in stdout.splitlines():
        if not en_cuerpo:
            if linea.strip() == "----------":
                en_cuerpo = True
            continue
        if not linea.strip():
            continue
        if not _7Z_CLAVE_VALOR.match(linea):
            raise ToolInstallError(
                f"Línea inesperada en el listado de 7z: {linea!r}. No se extrae: un listado que no se "
                "entiende por completo no permite garantizar que todos los miembros fueron validados."
            )
        if linea.startswith(_7Z_CLAVES_DE_LINK):
            # Validar el `Path` de un symlink no alcanza: el destino viaja en OTRA
            # clave del bloque. `7z x` materializa el link (siempre en POSIX, y en
            # Windows con privilegios) apuntando fuera de dest, y después escribe
            # contenido A TRAVÉS de él — todos los nombres "seguros", el sandbox
            # igual perforado. Ningún release de las tools que instalamos trae
            # links, así que rechazarlos de plano no cuesta nada.
            raise PathViolationError(f"El archive trae un enlace y no se extrae con el 7z del sistema: {linea!r}")
        _clave, _, valor = linea.partition(" = ")
        if not linea.startswith("Path = ") and _valor_con_traversal(valor):
            # Ninguna clave legítima de `-slt` (Size, CRC, Modified, Attributes,
            # Method…) lleva rutas con `..`. Un valor así es la continuación de un
            # nombre partido, disfrazada de par clave-valor.
            raise ToolInstallError(
                f"Valor con traversal en una clave que no es Path: {linea!r}. Delata un nombre de miembro "
                "partido por un salto de línea — no se extrae."
            )
        if linea.startswith("Path = "):
            nombre = linea[len("Path = ") :].strip()
            if any(ord(ch) < 32 for ch in nombre):
                raise ToolInstallError(f"Nombre de miembro con caracteres de control: {nombre!r}. No se extrae.")
            members.append(nombre)
    return members


def _extract_7z_via_system(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """Extrae un ``.7z`` con el binario ``7z`` del sistema, validando las rutas primero.

    Fallback para lo que ``py7zr`` no soporta (filtros BCJ2 de x86, que es como
    vienen empaquetados los releases de xEdit).

    El sandbox de rutas solo vale si TODOS los extractores validan los nombres de
    miembro: delegar en el ``-y`` de ``7z`` sería delegar el control de seguridad a
    un binario externo cuya versión no controlamos. Por eso el listado (``7z l``)
    pasa entero por :func:`_is_safe_path` ANTES de correr ``7z x`` — un solo nombre
    con traversal aborta sin haber escrito un byte.

    Bloqueante por diseño: los callers entran vía ``asyncio.to_thread`` (ver
    ``ToolsInstaller._extract``), y el ``timeout`` explícito acota el cuelgue.

    Raises:
        PathViolationError: Si algún miembro del archive escapa de *dest*.
        ToolInstallError: Timeout, fallo de ``7z``, ``7z`` ausente del PATH, o un
            listado que no se puede parsear (fail-closed: sin nombres validados no
            se extrae).
    """
    import subprocess

    base_cmd = ["7z", "l", "-slt", "-y", str(archive)]
    try:
        listado = subprocess.run(
            base_cmd,
            check=True,
            capture_output=True,
            timeout=_SEVENZIP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolInstallError(f"El listado de 7z para {archive.name} superó el timeout.") from exc
    except subprocess.CalledProcessError as exc:
        detalle = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
        raise ToolInstallError(f"7z no pudo listar {archive.name}: {detalle}") from exc
    except FileNotFoundError as exc:
        raise ToolInstallError(
            f"Hace falta 7z en el PATH para extraer {archive.name} porque py7zr falló con este archive."
        ) from exc

    stdout = listado.stdout.decode("utf-8", errors="ignore") if listado.stdout else ""
    miembros = _parse_7z_listing(stdout)
    if not miembros:
        # Fail-closed. Un listado que no parsea (otra versión de 7z, otro locale,
        # formato de salida distinto) deja la lista vacía, el loop de abajo itera
        # cero veces y `7z x` correría SIN haber validado un solo nombre: el mismo
        # bypass del sandbox que esta función existe para cerrar, pero silencioso.
        # No poder validar no es lo mismo que estar validado.
        raise ToolInstallError(
            f"No pude parsear el listado de 7z para {archive.name}: sin miembros que validar, "
            "no se extrae (posible versión o locale de 7z no soportado)."
        )
    for name in miembros:
        if not _is_safe_path(name):
            raise PathViolationError(f"Zip-slip detectado en el listado de 7z: {name!r}")

    try:
        subprocess.run(
            ["7z", "x", f"-o{dest}", "-y", str(archive)],
            check=True,
            capture_output=True,
            timeout=_SEVENZIP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolInstallError(f"La extracción con 7z de {archive.name} superó el timeout.") from exc
    except subprocess.CalledProcessError as exc:
        detalle = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
        raise ToolInstallError(f"La extracción con 7z falló: {detalle}") from exc


def _extract_7z_safe(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """Extract a 7z archive with zip-slip protection."""
    try:
        import py7zr

        with py7zr.SevenZipFile(archive, "r") as szf:
            for member in szf.list():
                name = member.filename
                if not _is_safe_path(name):
                    raise PathViolationError(f"Zip-slip detected in 7z: {name!r}")
                if member.is_symlink:
                    raise PathViolationError(f"El archive 7z contiene un enlace simbólico: {name!r}")
            szf.extractall(dest)
    except PathViolationError:
        # Una falla de SEGURIDAD nunca degrada a fallback: reintentar con otro
        # extractor sería exactamente el bypass del sandbox que se está atajando.
        raise
    except Exception as exc:
        logger.warning("La extracción con py7zr falló (%s). Cayendo al 7z del sistema.", exc)
        _extract_7z_via_system(archive, dest)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_exe_in_dir(directory: pathlib.Path, exe_name: str) -> pathlib.Path | None:
    """Recursively search *directory* for *exe_name* and return its path."""
    if not directory.is_dir():
        return None
    for path in directory.rglob(exe_name):
        if path.is_file():
            return path
    return None


def find_any_exe_in_dir(directory: pathlib.Path, exe_names: tuple[str, ...]) -> pathlib.Path | None:
    """Busca en *directory* el primero de *exe_names* que exista.

    Para las tools que el upstream renombra entre releases (Pandora pasó de
    ``Pandora.exe`` a ``Pandora Behaviour Engine+.exe``): buscar un solo nombre
    hace que una instalación previa válida se reporte como faltante.
    """
    for exe_name in exe_names:
        found = find_exe_in_dir(directory, exe_name)
        if found is not None:
            return found
    return None


def scan_common_paths(common_paths: tuple[pathlib.Path, ...], exe_name: str) -> pathlib.Path | None:
    """Check common installation directories for an executable."""
    for base in common_paths:
        found = find_exe_in_dir(base, exe_name)
        if found is not None:
            return found
    return None


def _file_id_nexus(f: dict[str, Any]) -> int:
    """Extrae el ``file_id`` tolerando el quirk de Nexus: lista ``[id, game]`` → segundo elemento."""
    fid = f.get("file_id")
    if isinstance(fid, list):
        return int(fid[1])
    return int(fid)  # type: ignore[arg-type]


def _haystack_nexus(f: dict[str, Any]) -> str:
    """Nombre + file_name lowercased: la superficie sobre la que matchean los selectores de Nexus."""
    return f"{f.get('name', '')} {f.get('file_name', '')}".lower()


def _select_address_library_file(files: list[dict[str, Any]], edition: SkyrimEdition) -> int:
    """Elige el ``file_id`` de Address Library (Nexus 32444) para la edición.

    Cascada de degradación, verificada contra ``files.json`` real (v11, feb-2026):
    el MAIN pasó a un único file universal "Address Library - All in one (all
    game versions)" y los específicos por edición ("All in one (1.5.X)/(1.6.X)",
    "All in one (Anniversary Edition)") viven en OPTIONAL/ARCHIVED. El selector
    viejo por keyword único abortaba contra Nexus vivo — ``ensure_ngio`` caído
    sin saberlo. Pasos:

    1. Candidatos: ``category_name == "MAIN"``; si queda vacío, todos los files.
    2. Específico por edición (naming histórico): "anniversary" (AE) /
       "special" (SE).
    3. Familia de versión (naming intermedio): "1.6" (AE) / "1.5" (SE).
    4. Universal (naming actual): "all game versions".
    5. Nada matchea → ToolInstallError con los nombres disponibles.

    Un match específico gana sobre el universal cuando ambos conviven en MAIN
    (períodos de transición). Quirk: ``file_id`` puede ser lista ``[id, game]``
    → se toma el segundo elemento, mismo criterio que ``get_file_info``.

    Raises:
        ToolInstallError: Si ningún file matchea (lista los disponibles).
    """
    main_files = [f for f in files if str(f.get("category_name", "")).upper() == "MAIN"] or list(files)
    por_edicion = ("anniversary" if edition is SkyrimEdition.AE else "special",)
    por_familia = ("1.6" if edition is SkyrimEdition.AE else "1.5",)
    for f in main_files:
        if any(k in _haystack_nexus(f) for k in por_edicion):
            return _file_id_nexus(f)
    for f in main_files:
        if any(k in _haystack_nexus(f) for k in por_familia):
            return _file_id_nexus(f)
    for f in main_files:
        if "all game versions" in _haystack_nexus(f):
            return _file_id_nexus(f)
    available = [str(f.get("name") or f.get("file_name") or "?") for f in files]
    raise ToolInstallError(
        f"Ningún archivo de Address Library coincide con la edición {edition.value}. Disponibles: {available}"
    )


def _select_engine_fixes_file(files: list[dict[str, Any]]) -> int:
    """Elige el file MAIN del plugin SKSE de SSE Engine Fixes (Nexus 17230).

    Verificado contra ``files.json`` real (v7.0.20): MAIN tiene DOS archivos —
    "Engine Fixes - Main File" (el plugin SKSE, ~7.6 MB) y "Engine Fixes -
    SKSE64 Preloader" (24 KB, se extrae a la RAÍZ del juego, no a Data/) — y
    NINGUNO es ``is_primary``: el camino "primary" de ``get_file_info`` cae al
    fallback ``files_list[-1]`` sobre ~134 entradas, un orden inestable.
    Instalar el preloader en ``mods/`` lo deja inerte bajo USVFS → Engine Fixes
    no carga y Community Shaders aborta su check de runtime. Selección
    explícita: MAIN, excluye "preloader", prefiere "main file"; ambigüedad →
    abort (nunca elegir al azar).

    Raises:
        ToolInstallError: Sin candidato o candidatos ambiguos (lista disponible).
    """
    main_files = [f for f in files if str(f.get("category_name", "")).upper() == "MAIN"] or list(files)
    candidatos = [f for f in main_files if "preloader" not in _haystack_nexus(f)]
    if not candidatos:
        available = [str(f.get("name") or f.get("file_name") or "?") for f in files]
        raise ToolInstallError(
            "SSE Engine Fixes: ningún file MAIN sin 'preloader' (el preloader va en la "
            f"raíz del juego, no en mods/). Disponibles: {available}"
        )
    preferidos = [f for f in candidatos if "main file" in _haystack_nexus(f)]
    elegibles = preferidos or candidatos
    if len(elegibles) > 1:
        available = [str(f.get("name") or f.get("file_name") or "?") for f in elegibles]
        raise ToolInstallError(
            f"SSE Engine Fixes: candidatos ambiguos para el plugin SKSE ({available}). "
            "Se aborta en vez de elegir al azar."
        )
    return _file_id_nexus(elegibles[0])


def _select_community_shaders_asset(assets: list[dict[str, Any]]) -> dict[str, Any]:
    """Elige el ZIP core de Community Shaders entre los assets del release.

    Verificado en CMakeLists del upstream: el core es ``CommunityShaders-<UTC>.zip``
    y el AIO es ``CommunityShaders_AIO-<UTC>.zip|.7z`` — prefijos casi
    idénticos. El discriminador es el GUION: ``communityshaders-`` no matchea
    ``communityshaders_aio-``. ``_find_github_asset`` ya filtró a
    ``.zip``/``.7z``; el ``endswith(".zip")`` extra defiende contra nombres
    raros. Dos matches con el prefijo del core → abort por ambigüedad (nunca
    elegir al azar).

    Raises:
        ToolInstallError: Sin match o más de un match (lista los disponibles).
    """
    candidatos = [
        a
        for a in assets
        if str(a.get("name", "")).lower().startswith("communityshaders-")
        and str(a.get("name", "")).lower().endswith(".zip")
    ]
    if not candidatos:
        available = [str(a.get("name") or "?") for a in assets]
        raise ToolInstallError(
            "Ningún asset de Community Shaders matchea 'CommunityShaders-*.zip' "
            f"(el AIO 'CommunityShaders_AIO-*' NO es el core). Disponibles: {available}"
        )
    if len(candidatos) > 1:
        available = [str(a.get("name") or "?") for a in candidatos]
        raise ToolInstallError(
            f"Release ambigua de Community Shaders: {len(candidatos)} assets con el "
            f"prefijo del core ({available}). Se aborta en vez de elegir al azar."
        )
    return candidatos[0]


def _parse_game_version(version: str) -> tuple[int, ...] | None:
    """Parsea "1.6.1170" → ``(1, 6, 1170)``; vacío o ilegible → ``None``.

    ``None`` es fail-closed para el gate de versión del autoinstalador de
    Community Shaders: una versión que no se puede leer no se aprueba.
    """
    try:
        return tuple(int(p) for p in version.strip().split("."))
    except ValueError:
        return None


def _parse_github_digest(digest: object) -> str:
    """Extrae el sha256 hex de ``assets[].digest`` de la API de GitHub.

    La API expone ``"sha256:<64 hex>"``; se acepta también el hex pelado.
    Malformado o ausente → ``""`` (TOFU: la descarga sigue, con warning).
    """
    if not isinstance(digest, str):
        return ""
    digest = digest.strip()
    if digest.startswith("sha256:"):
        digest = digest[len("sha256:") :]
    if len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
        return digest.lower()
    return ""


# Nombres de host que verifican los gates del autoinstalador de Community
# Shaders (la raíz del juego = donde vive SkyrimSE.exe).
_CS_SKSE_LOADER_NAMES = ("skse64_loader.exe", "skse_loader.exe")
#: El preloader de SSE Engine Fixes se materializa en la raíz del juego como
#: ``d3dx9_42.dll`` (Part 2 del mod; file "Engine Fixes - SKSE64 Preloader").
#: Tupla de nombres conocidos: si el upstream cambiara el nombre, agregar uno
#: es una línea. No verificable por API (el contenido del .7z no se lista).
_CS_PRELOADER_NAMES = ("d3dx9_42.dll",)


def _detectar_skse(game_dir: pathlib.Path) -> bool:
    """True si hay un loader de SKSE en la raíz del juego (preflight de CS)."""
    return any((game_dir / nombre).is_file() for nombre in _CS_SKSE_LOADER_NAMES)


def _detectar_enb(game_dir: pathlib.Path) -> bool:
    """True si la firma de ENB está en la raíz del juego.

    Señal fuerte sin falsos positivos: ``enbseries.ini`` (o el dir
    ``enbseries/``) es la firma de ENB; ``d3d11.dll``/``d3dx9_42.dll`` no
    bastan (Skyrim/DXGI también los traen). CS se auto-deshabilita cuando ENB
    está presente (XSEPlugin.cpp: ENB_API::RequestENBAPI()).
    """
    return (game_dir / "enbseries.ini").is_file() or (game_dir / "enbseries").is_dir()


def _detectar_preloader(game_dir: pathlib.Path) -> bool:
    """True si el preloader de SSE Engine Fixes está en la raíz del juego."""
    return any((game_dir / nombre).is_file() for nombre in _CS_PRELOADER_NAMES)


def _transform_fomod_engine_fixes(mod_dir: pathlib.Path, edition: SkyrimEdition) -> None:
    """Resuelve el FOMOD de SSE Engine Fixes a un layout MO2 válido.

    Verificado descargando el archive real de Nexus (file MAIN, 7.0.20): NO es
    un mod plano — es un FOMOD con ``EngineFixes FOMOD Installer/{AE,SE}/SKSE/
    Plugins/EngineFixes.dll`` (rama por edición), ``Required/SKSE/Plugins/``
    (config común: EngineFixes.toml, preload.txt, SNCT.ini) y ``fomod/``.
    Este transform copia la rama de la edición + Required a la raíz del mod y
    elimina el resto — el equivalente programático de elegir "SE/AE" en MO2.

    Raises:
        ToolInstallError: Si la rama de la edición no está (upstream cambió el
            layout) — fail-closed, nunca adivinar.
    """
    rama = "AE" if edition is SkyrimEdition.AE else "SE"
    # El transform corre ANTES del flatten: el wrapper puede estar presente
    # ("EngineFixes FOMOD Installer/…") o ya aplanado ({AE,SE,Required,fomod} en
    # la raíz). Nunca depende de renames frágiles del flatten (WinError 5/32 con
    # el antivirus escaneando contenido recién extraído).
    fomod = mod_dir / "EngineFixes FOMOD Installer"
    base = fomod if fomod.is_dir() else mod_dir
    rama_skse = base / rama / "SKSE"
    sentinel = rama_skse / "Plugins" / "EngineFixes.dll"
    if not sentinel.is_file():
        raise ToolInstallError(
            f"Layout inesperado del FOMOD de SSE Engine Fixes: falta "
            f"{rama}/SKSE/Plugins/EngineFixes.dll (el upstream cambió el FOMOD?)."
        )
    # Fail-closed también para la config: un FOMOD sin Required/SKSE dejaría un
    # mod "instalado" sin EngineFixes.toml (el plugin arranca con defaults
    # incorrectos). La política es nunca adivinar — ni en la rama ni en la config.
    required_skse = base / "Required" / "SKSE"
    if not required_skse.is_dir():
        raise ToolInstallError(
            "Layout inesperado del FOMOD de SSE Engine Fixes: falta "
            "Required/SKSE (config del plugin: EngineFixes.toml)."
        )
    dest_skse = mod_dir / "SKSE"
    dest_skse.mkdir(exist_ok=True)
    for origen in (rama_skse, required_skse):
        for item in origen.iterdir():
            if item.is_dir():
                shutil.copytree(item, dest_skse / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest_skse / item.name)
    if fomod.is_dir():
        rmtree_link_aware(fomod, limpiar_readonly=True)
    else:
        for resto in ("AE", "SE", "Required", "fomod"):
            rmtree_link_aware(mod_dir / resto, limpiar_readonly=True)
    logger.info("FOMOD de SSE Engine Fixes resuelto (rama %s) en %s", rama, mod_dir)


def _select_ngio_asset(assets: list[dict[str, Any]], edition: SkyrimEdition) -> dict[str, Any]:
    """Elige el asset de NGIO-NG (GitHub) para la edición.

    Las releases publican builds SEPARADOS, y sus nombres no contienen la
    palabra "grass" ni el nombre del repo: ``NGIO-AE-<ver>.zip`` (Anniversary,
    1.6.x), ``NGIO-NG_<ver>.7z`` (1.5.97/SE) y ``NGIO-NG-<ver>-VR.zip`` (VR,
    SIEMPRE excluido). Un keyword único bajaría el build equivocado o ninguno
    (bug del #293), por eso se elige por edición: AE → nombre con ``ngio-ae``;
    SE → un asset de NGIO sin ese marcador.

    Raises:
        ToolInstallError: Si ningún asset matchea la edición (lista los nombres
            disponibles para diagnóstico, sin caer al build equivocado).
    """
    candidates = [
        a for a in assets if "ngio" in str(a.get("name", "")).lower() and "vr" not in str(a.get("name", "")).lower()
    ]
    want_ae = edition is SkyrimEdition.AE
    for a in candidates:
        is_ae = "ngio-ae" in str(a.get("name", "")).lower()
        if is_ae == want_ae:
            return a
    available = [str(a.get("name") or "?") for a in assets]
    raise ToolInstallError(
        f"Ningún asset de NGIO-NG coincide con la edición {edition.value} (VR excluido). Disponibles: {available}"
    )


def _flatten_single_root(mod_dir: pathlib.Path) -> None:
    """Si el archive envolvió el payload en UNA carpeta raíz, sube su contenido.

    MO2 exige ``SKSE/`` en la raíz del mod; algunos archives de Nexus/GitHub
    envuelven todo en ``NombreMod-1.2.3/``. No aplana si la única carpeta ya
    es ``SKSE`` (layout correcto).
    """
    children = list(mod_dir.iterdir())
    if len(children) != 1 or not children[0].is_dir() or children[0].name.lower() == "skse":
        return
    root = children[0]
    for item in root.iterdir():
        item.rename(mod_dir / item.name)
    root.rmdir()
    logger.info("Aplanada la carpeta raíz %r del archive en %s", root.name, mod_dir)


def _write_mod_meta_ini(mod_dir: pathlib.Path, mod_name: str, version: str, comment: str | None = None) -> None:
    """``meta.ini`` mínimo para que MO2 muestre el mod (patrón de GrassProfile).

    *comment* reemplaza el comentario por defecto; ``None`` → el genérico.
    Antes el comentario estaba hardcodeado al texto del precache de grass, lo
    que hacía mentir a los mods instalados por otros caminos (p.ej. SSE Engine
    Fixes aparecía como "dependencia del precache de grass").
    """
    config = configparser.ConfigParser()
    config["General"] = {
        "modid": "0",
        "version": version,
        "name": mod_name,
        "comments": comment or "Instalado por Sky-Claw.",
    }
    with (mod_dir / "meta.ini").open("w", encoding="utf-8") as fh:
        config.write(fh)


def _promover_payload_verificado(payload_dir: pathlib.Path, mod_dir: pathlib.Path, mod_name: str) -> None:
    """Publica un payload propio sin reemplazar un destino aparecido en carrera."""
    if mod_dir.exists():
        raise ToolInstallError(
            f"El destino {mod_dir} apareció durante la instalación de {mod_name}; "
            "se conserva intacto y no se mezcla con el payload de Sky-Claw."
        )
    # Windows ya ofrece no-replace para rename de directorios. POSIX, en
    # cambio, reemplaza un directorio destino vacío: reclamar primero el nombre
    # con mkdir (O_EXCL a nivel de filesystem) garantiza que el rename posterior
    # sólo sustituye nuestro propio placeholder. Si otro proceso gana la carrera,
    # mkdir falla sin tocar ni su destino ni nuestro payload.
    if not _ES_WINDOWS:
        try:
            mod_dir.mkdir()
        except FileExistsError:
            raise ToolInstallError(
                f"El destino {mod_dir} apareció durante la instalación de {mod_name}; "
                "se conserva intacto y no se mezcla con el payload de Sky-Claw."
            ) from None
    try:
        payload_dir.rename(mod_dir)
    except OSError as exc:
        if not _ES_WINDOWS:
            # Sólo remueve el placeholder propio si sigue vacío. Si un escritor
            # externo lo pobló, rmdir falla y sus archivos quedan intactos.
            with contextlib.suppress(OSError):
                mod_dir.rmdir()
        raise ToolInstallError(f"No pude promover el payload verificado de {mod_name}: {exc}") from exc


def _limpiar_payload_temporal(payload_dir: pathlib.Path, mod_name: str) -> None:
    """Limpia staging propio sin ocultar la excepción funcional original."""
    try:
        rmtree_link_aware(payload_dir, limpiar_readonly=True)
    except OSError as cleanup_exc:
        logger.warning(
            "No se pudo limpiar el payload temporal de %s (%s): %s",
            mod_name,
            payload_dir,
            type(cleanup_exc).__name__,
        )


# ---------------------------------------------------------------------------
# ToolsInstaller
# ---------------------------------------------------------------------------


class ToolsInstaller:
    """Downloads and extracts LOOT and SSEEdit from official GitHub releases.

    Every download:
    - Requires HITL operator approval.
    - Goes through NetworkGateway egress control.
    - Uses PathValidator for all file operations.
    - Validates file size post-download.
    - Applies zip-slip protection during extraction.

    Parameters
    ----------
    hitl:
        Human-in-the-loop guard for mandatory approval.
    gateway:
        Network gateway for egress authorization.
    path_validator:
        Sandbox validator for file I/O.
    """

    def __init__(
        self,
        hitl: HITLGuard,
        gateway: NetworkGateway,
        path_validator: PathValidator,
        lock_manager: DistributedLockManager,
        install_ttl: float = _INSTALL_TTL_SECONDS,
    ) -> None:
        self._hitl = hitl
        self._gateway = gateway
        self._validator = path_validator
        self._lock_manager = lock_manager
        self._install_ttl = install_ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_loot(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
    ) -> InstallResult:
        """Ensure LOOT is available, downloading if necessary.

        Args:
            install_dir: Directory to install LOOT into.
            session: Active HTTP session.

        Returns:
            :class:`InstallResult` with the path to ``loot.exe``.
        """
        self._validator.validate(install_dir)
        # Lock cross-process del ciclo COMPLETO: el early-return del chequeo de
        # "ya instalado" va DENTRO del lock, o dos procesos verían el directorio
        # vacío a la vez y ambos descargarían (TOCTOU de T-31).
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(install_dir),
            ttl=self._install_ttl,
        ):
            exe = find_exe_in_dir(install_dir, "loot.exe")
            if exe is not None:
                logger.info("LOOT already installed at %s", exe)
                return InstallResult(
                    tool_name="LOOT",
                    exe_path=exe,
                    version="existing",
                    already_existed=True,
                )

            asset, version = await self._find_github_asset(
                session,
                _LOOT_RELEASES_URL,
                keyword="win64",
            )

            decision = await self._hitl.request_approval(
                request_id=f"install-loot-{version}",
                reason=f"Install LOOT {version}?",
                url=asset.browser_download_url,
                detail=(
                    f"Asset: {asset.name}\nSize: {asset.size / (1024 * 1024):.1f} MB\nSource: GitHub loot/loot releases"
                ),
                category="download",
            )
            if decision is not Decision.APPROVED:
                raise ToolInstallError(f"LOOT installation denied by operator (decision={decision.value})")

            archive = await self._download_asset(session, asset, install_dir)
            await _esperar_hilo_ininterrumpible(self._extract, archive, install_dir)
            archive.unlink(missing_ok=True)

            exe = find_exe_in_dir(install_dir, "loot.exe")
            if exe is None:
                raise ToolInstallError("LOOT extraction succeeded but loot.exe not found in output")

            logger.info("LOOT %s installed at %s", version, exe)
            return InstallResult(
                tool_name="LOOT",
                exe_path=exe,
                version=version,
                already_existed=False,
            )

    async def ensure_xedit(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
    ) -> InstallResult:
        """Ensure SSEEdit is available, downloading if necessary.

        Args:
            install_dir: Directory to install SSEEdit into.
            session: Active HTTP session.

        Returns:
            :class:`InstallResult` with the path to ``SSEEdit.exe``.
        """
        self._validator.validate(install_dir)
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(install_dir),
            ttl=self._install_ttl,
        ):
            exe = find_exe_in_dir(install_dir, "SSEEdit.exe")
            if exe is not None:
                logger.info("SSEEdit already installed at %s", exe)
                return InstallResult(
                    tool_name="SSEEdit",
                    exe_path=exe,
                    version="existing",
                    already_existed=True,
                )

            asset, version = await self._find_github_asset(
                session,
                _XEDIT_RELEASES_URL,
                keyword="xEdit",
            )

            decision = await self._hitl.request_approval(
                request_id=f"install-xedit-{version}",
                reason=f"Install SSEEdit {version}?",
                url=asset.browser_download_url,
                detail=(
                    f"Asset: {asset.name}\n"
                    f"Size: {asset.size / (1024 * 1024):.1f} MB\n"
                    f"Source: GitHub TES5Edit/TES5Edit releases"
                ),
                category="download",
            )
            if decision is not Decision.APPROVED:
                raise ToolInstallError(f"SSEEdit installation denied by operator (decision={decision.value})")

            archive = await self._download_asset(session, asset, install_dir)
            await _esperar_hilo_ininterrumpible(self._extract, archive, install_dir)
            archive.unlink(missing_ok=True)

            # xEdit se publica con nombre genérico; renombrarlo a SSEEdit.exe evita el
            # popup de selección de juego que bloquea la GUI. Solo si NO hay ya un
            # SSEEdit.exe al lado: el archive puede traer ambos y pisarlo destruiría el
            # binario bueno.
            #
            # `.rename()` y NO `.replace()`: os.replace PISA el destino, que es
            # justo lo que hay que evitar. En Windows os.rename falla con
            # FileExistsError si el destino apareció entre el chequeo y el renombrado
            # → fail-closed, se conserva el SSEEdit.exe bueno. En POSIX os.rename
            # pisa igual, así que el guard de abajo es lo único que protege ahí; la
            # protección real contra instalaciones concurrentes es el lock de T-31,
            # no este bloque.
            xedit_exe = find_exe_in_dir(install_dir, "xEdit.exe")
            if xedit_exe is not None:
                target = xedit_exe.with_name("SSEEdit.exe")
                if target.exists():
                    logger.info("SSEEdit.exe ya presente en %s; se conserva y no se renombra xEdit.exe", install_dir)
                else:
                    try:
                        xedit_exe.rename(target)
                    except FileExistsError:
                        logger.info("SSEEdit.exe apareció durante la instalación; se conserva el existente.")

            exe = find_exe_in_dir(install_dir, "SSEEdit.exe")
            if exe is None:
                raise ToolInstallError(
                    "SSEEdit extraction succeeded but neither SSEEdit.exe nor xEdit.exe found in output"
                )

            logger.info("SSEEdit %s installed at %s", version, exe)
            return InstallResult(
                tool_name="SSEEdit",
                exe_path=exe,
                version=version,
                already_existed=False,
            )

    async def ensure_pandora(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
    ) -> InstallResult:
        """Ensure Pandora Behavior Engine is available, downloading if necessary.

        Args:
            install_dir: Directory to install Pandora into.
            session: Active HTTP session.

        Returns:
            :class:`InstallResult` con la ruta al ejecutable de Pandora — el
            release actual lo publica como ``Pandora Behaviour Engine+.exe``,
            pero se acepta cualquiera de :data:`PANDORA_EXE_NAMES` (las releases
            viejas usaban ``Pandora.exe``).
        """
        self._validator.validate(install_dir)
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(install_dir),
            ttl=self._install_ttl,
        ):
            # Check if already installed
            exe = find_any_exe_in_dir(install_dir, PANDORA_EXE_NAMES)
            if exe is not None:
                logger.info("Pandora already installed at %s", exe)
                return InstallResult(
                    tool_name="Pandora",
                    exe_path=exe,
                    version="existing",
                    already_existed=True,
                )

            asset, version = await self._find_github_asset(
                session,
                _PANDORA_RELEASES_URL,
                keyword="Pandora_Behaviour_Engine",
            )

            decision = await self._hitl.request_approval(
                request_id=f"install-pandora-{version}",
                reason=f"Install Pandora Behavior Engine {version}?",
                url=asset.browser_download_url,
                detail=(
                    f"Asset: {asset.name}\n"
                    f"Size: {asset.size / (1024 * 1024):.1f} MB\n"
                    f"Source: GitHub Monitor221hz/Pandora-Behaviour-Engine-Plus"
                ),
                category="download",
            )
            if decision is not Decision.APPROVED:
                raise ToolInstallError(f"Pandora installation denied by operator (decision={decision.value})")

            archive = await self._download_asset(session, asset, install_dir)
            await _esperar_hilo_ininterrumpible(self._extract, archive, install_dir)
            archive.unlink(missing_ok=True)

            exe = find_any_exe_in_dir(install_dir, PANDORA_EXE_NAMES)
            if exe is None:
                raise ToolInstallError(
                    f"La extracción de Pandora terminó pero no apareció ninguno de {list(PANDORA_EXE_NAMES)} en la salida"
                )

            logger.info("Pandora %s installed at %s", version, exe)
            return InstallResult(
                tool_name="Pandora",
                exe_path=exe,
                version=version,
                already_existed=False,
            )

    async def ensure_skse(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        edition: SkyrimEdition | None = None,
    ) -> InstallResult:
        """Asegura que SKSE esté disponible en el directorio del juego, descargándolo si es necesario.

        Args:
            install_dir: Directorio raíz del juego Skyrim (donde reside SkyrimSE.exe).
            session: Sesión HTTP activa.
            edition: Override opcional de la edición de Skyrim. Si es None, se deriva
                de la versión del PE del ejecutable del juego.

        Returns:
            :class:`InstallResult` con la ruta a ``skse64_loader.exe``.

        Raises:
            ToolInstallError: Si la instalación falla o la edición no está soportada (ej. MS Store).
        """
        self._validator.validate(install_dir)
        # Lock cross-process keyed por game_dir (8º mutador de T-31). Cubre el
        # ciclo completo: chequeo → descarga → copia → limpieza de DLL huérfanos.
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(install_dir),
            ttl=self._install_ttl,
        ):
            # Determinar edición. La autoridad es la versión del PE del juego, NO qué DLL
            # de SKSE ya está en disco: si SKSE ya estuviera instalado saldríamos por el
            # early-return de idempotencia de más abajo, así que en el único caso donde
            # esto corre —máquina limpia— no hay DLL que mirar. Defaultear a AE ahí le
            # instala `skse64_1_6_1170.dll` a un runtime 1.5.97 y SKSE no carga.
            #
            # Quién nombra la EDICIÓN y quién prueba la VERSIÓN son cosas distintas: el
            # caller puede traer la edición, pero la compatibilidad la decide el build
            # del ejecutable que haya en disco. Por eso los dos caminos terminan
            # llenando `detected_version` y `hay_ejecutable`.
            detected_version = ""
            if edition is None:
                edition, detected_version = await self._detect_skyrim_edition_from_exe(install_dir)
                # `_detect_skyrim_edition_from_exe` levanta si no encuentra ninguno.
                hay_ejecutable = True
            else:
                # Con `edition` explícita no se EXIGE un .exe en disco (staging, tests,
                # instalación en curso), pero si lo hay, su versión manda igual: la
                # compatibilidad de SKSE es del BUILD, y quién nombró la edición no
                # cambia qué runtime va a cargar el DLL. Sin esto, el gate quedaba
                # atado al camino de autodetección y cualquier caller que resolviera
                # la edición por su cuenta —config, snapshot, un consumidor futuro—
                # se saltaba la prueba de compatibilidad entera.
                hay_ejecutable, detected_version = await self._leer_version_del_ejecutable(install_dir)

            ed_key = self._edition_to_config_key(edition)
            cfg = SKSE_CONFIG.get(ed_key)

            if cfg is None:
                raise ToolInstallError(f"Edición {ed_key} no compatible (probablemente MS Store, que no soporta SKSE).")

            # Idempotencia: verificar si ya está instalado correctamente
            loader_name = _skse_cfg_field(cfg, "loader")
            dll_name = _skse_cfg_field(cfg, "dll")

            # Gate de versión exacta: SKSE está pinneado al BUILD exacto del juego, no
            # solo a su edición — dos AE distintos (1.6.640 vs 1.6.1170) comparten
            # edición pero el DLL de uno no carga sobre el otro. Corre ANTES del HITL,
            # del egress y de cualquier escritura en el directorio del juego: la
            # propiedad es "autoinstalar exige PODER PROBAR la compatibilidad", no
            # "exige no haberla desmentido".
            #
            # De ahí que la versión ILEGIBLE también corte. Degradarse ahí "a edición
            # sola" era fail-open: `read_skyrim_version` devuelve "" en tres casos
            # reales (sin `pefile`, PE que no parsea, PE sin recurso de versión) y en
            # esa MISMA rama la edición no sale del PE sino de una heurística de TAMAÑO
            # de archivo (`scanner._detect_skyrim_version`: >60 MB ⇒ AE). O sea que lo
            # que se instalaba era el payload de un build adivinado sobre un runtime
            # desconocido — incluido el AE de GOG (1.6.1179), que el gate de mismatch
            # solo ataja cuando la versión se pudo leer.
            #
            # La condición es "hay un runtime en disco que no pude probar", NO "la
            # edición vino por autodetección": atarla al origen de la edición dejaba el
            # gate ciego para cualquier caller que la resolviera por su cuenta, que es
            # la misma clase de defecto —un camino cubierto y su gemelo intacto— que
            # este gate existe para cerrar.
            expected_version = _skse_dll_game_version(dll_name)
            loader_path = install_dir / loader_name
            dll_path = install_dir / dll_name
            ya_instalado = loader_path.exists() and dll_path.exists()

            # El corte por versión ilegible NO se le aplica a una instalación que ya
            # está: ese camino no descarga ni escribe nada, así que negarle el no-op a
            # una máquina cuyo PE no se puede leer rompe una instalación buena sin
            # ganar ninguna garantía. Es la misma política que el hermano de detección
            # ya documenta (`scanner.find_skse_installation`: versión vacía degrada a
            # "loader + algún DLL de runtime", no falla cerrado), y acá vale por la
            # misma razón: lo que exige prueba es MUTAR, no reportar.
            if hay_ejecutable and not detected_version and not ya_instalado:
                raise ToolInstallError(
                    f"No pude leer la versión exacta de tu Skyrim {ed_key} en {install_dir} "
                    "(¿falta `pefile`, o el ejecutable no expone su recurso de versión?). "
                    "SKSE está pinneado a la versión EXACTA del ejecutable, así que sin poder "
                    "probar la compatibilidad no se instala nada: elegir el payload por edición "
                    "sola escribe un DLL que puede no cargar. Instalá manualmente el build "
                    "correspondiente desde https://skse.silverlock.org/."
                )
            if detected_version and not _game_version_matches(detected_version, expected_version):
                raise ToolInstallError(
                    f"Tu Skyrim {ed_key} está en la versión {detected_version}, pero el único "
                    f"SKSE {ed_key} soportado acá es para {expected_version} (SKSE está pinneado "
                    "a la versión EXACTA del ejecutable, no solo a la edición). Instalá "
                    "manualmente el build correspondiente desde https://skse.silverlock.org/."
                )

            if ya_instalado:
                logger.info("SKSE ya instalado en %s", loader_path)
                return InstallResult(
                    tool_name="SKSE",
                    exe_path=loader_path,
                    version="existing",
                    already_existed=True,
                )

            url = _skse_cfg_field(cfg, "url")

            # Solicitar aprobación HITL
            decision = await self._hitl.request_approval(
                request_id=f"install-skse-{ed_key}",
                reason=f"Install SKSE for Skyrim {ed_key}?",
                url=url,
                detail=(
                    f"URL: {url}\nLoader: {loader_name}\nDLL: {dll_name}\nSource: skse.silverlock.org (sitio oficial)"
                ),
                category="download",
            )

            if decision is not Decision.APPROVED:
                raise ToolInstallError(f"SKSE installation denied by operator (decision={decision.value})")

            # Staging: la MISMA raíz que `AppContext` registra en el PathValidator
            # (`tempfile.gettempdir() / "sky_claw"`). Inventar otra —p. ej. C:/tmp— hace
            # que el `validate(tmp_path)` de acá abajo falle siempre, porque esa ruta no
            # es ninguna de las raíces del sandbox.
            skse_sandbox = pathlib.Path(tempfile.gettempdir()) / "sky_claw"
            skse_sandbox.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(dir=skse_sandbox) as tmpdir:
                tmp_path = pathlib.Path(tmpdir)
                self._validator.validate(tmp_path)

                archive_name = url.split("/")[-1]
                archive_path = tmp_path / archive_name
                self._validator.validate(archive_path)

                # Descarga segura vía NetworkGateway con timeout
                await self._download_skse_archive(session, cfg, archive_path)

                # Extraer con protección zip-slip
                extract_path = tmp_path / "extracted"
                await _esperar_hilo_ininterrumpible(self._extract, archive_path, extract_path)

                # Buscar loader excluyendo __MACOSX
                skse_root = self._find_skse_root(extract_path, cfg)

                # SEGUNDO GATE, pegado a la primera escritura. El de arriba probó la
                # compatibilidad al ARRANCAR la operación; desde entonces pasaron tres
                # cosas que duran —la espera del HITL, la descarga y la extracción— y
                # Skyrim es un ejecutable que otro proceso (Steam) actualiza sin
                # avisarnos. Sin releer, lo que quedaba demostrado era "el runtime ERA
                # compatible cuando empecé", y lo que decide si el DLL carga es si lo
                # SIGUE siendo cuando escribo.
                #
                # Va acá y no antes de la descarga: cuanto más lejos de la copia, más
                # ventana queda sin cubrir. Y va antes de `_copy_skse_files` porque esa
                # es la primera mutación del directorio del juego — todo lo anterior
                # (sandbox, archive, extracción) vive en staging temporal.
                await self._revalidar_runtime_antes_de_mutar(
                    install_dir,
                    ed_key,
                    expected_version,
                    habia_ejecutable=hay_ejecutable,
                )

                # Copiar archivos al directorio del juego. `_copy_skse_files` solo toca
                # nombres presentes en el payload nuevo (loader/dll de esta edición +
                # Data) — no depende de que los DLL huérfanos ya estén borrados.
                await self._copy_skse_files(skse_root, install_dir, cfg)

                # LIMPIEZA DE DIRTY UPGRADES: recién acá, después de que la copia haya
                # terminado sin excepción. Es el verdadero punto de no retorno: si se
                # borrara antes y `_copy_skse_files` fallara a mitad de camino (disco
                # lleno, permisos), el juego se queda sin la versión vieja Y sin la
                # nueva completa, sin rollback que lo recupere. Corriendo al final, un
                # fallo de copia deja los DLL de otra edición intactos y el juego sigue
                # arrancando con el SKSE que tenía antes.
                await self._cleanup_orphaned_skse_dlls(install_dir, cfg)

            logger.info("SKSE %s instalado en %s", ed_key, loader_path)
            return InstallResult(
                tool_name="SKSE",
                exe_path=loader_path,
                version=pathlib.Path(url).stem,
                already_existed=False,
            )

    async def _revalidar_runtime_antes_de_mutar(
        self,
        game_dir: pathlib.Path,
        ed_key: str,
        expected_version: str,
        *,
        habia_ejecutable: bool,
    ) -> None:
        """Vuelve a probar la compatibilidad contra el ejecutable REAL, justo antes de escribir.

        No confía en nada de la primera pasada: relee el PE del disco. Ni la versión
        detectada al arrancar, ni la edición, ni el nombre del DLL elegido sirven acá
        — todos son de antes de la ventana, y la ventana es justamente el problema.

        ``habia_ejecutable`` es lo único que se hereda, y no es una versión sino un
        hecho sobre la precondición: distingue "el juego se desinstaló o se movió a
        mitad de la operación" (había uno, ya no) de "esta instalación nunca tuvo un
        .exe acá", que es el camino legítimo de staging con ``edition`` explícita y
        que el contrato existente permite.

        Falla cerrado en las tres formas de no poder probar: sin ejecutable, sin
        versión legible, o versión que no matchea el payload que se bajó.
        """
        hay_ejecutable, version_actual = await self._leer_version_del_ejecutable(game_dir)

        if not hay_ejecutable:
            if not habia_ejecutable:
                # Nunca hubo ejecutable: staging con edición explícita. Nada cambió
                # bajo nuestros pies, así que no hay TOCTOU que atajar.
                return
            raise ToolInstallError(
                f"El payload de SKSE {ed_key} ya se descargó, pero ya no encuentro el ejecutable "
                f"de Skyrim en {game_dir}: desapareció mientras se preparaba la instalación "
                "(¿se desinstaló o se movió el juego?). No se copió nada."
            )

        if not version_actual:
            raise ToolInstallError(
                f"La compatibilidad de tu Skyrim {ed_key} en {game_dir} dejó de poder verificarse "
                "mientras se preparaba la instalación: el ejecutable está pero ya no expone su "
                "versión (¿una actualización en curso?). No se copió nada; reintentá cuando el "
                "juego esté en reposo."
            )

        if not _game_version_matches(version_actual, expected_version):
            raise ToolInstallError(
                f"Tu Skyrim {ed_key} cambió de versión mientras se preparaba la instalación: "
                f"ahora está en {version_actual} y el payload que se descargó es para "
                f"{expected_version}. Copiarlo dejaría un SKSE que no carga, así que no se copió "
                "nada. Volvé a intentarlo para que se elija el build correspondiente a "
                f"{version_actual}."
            )

    async def _leer_version_del_ejecutable(self, game_dir: pathlib.Path) -> tuple[bool, str]:
        """``(¿hay ejecutable de Skyrim?, versión exacta o "")`` sin exigir que exista.

        Hermano de :meth:`_detect_skyrim_edition_from_exe` para el camino en que el
        caller ya trae la edición: ahí no hace falta deducirla, pero sí hace falta
        saber si hay un runtime real cuya compatibilidad se pueda probar. Los dos
        desenlaces que devuelve ``""`` son distintos y por eso se devuelve también el
        booleano: "no hay Skyrim acá" es legítimo (staging), "hay uno y no pude leerle
        la versión" es lo que tiene que fallar cerrado.
        """
        for exe_name in _SKYRIM_EXE_NAMES:
            exe = game_dir / exe_name
            if exe.is_file():
                # pefile hace I/O síncrono de PE: fuera del event loop.
                return True, await asyncio.to_thread(read_skyrim_version, exe)
        return False, ""

    async def _detect_skyrim_edition_from_exe(self, game_dir: pathlib.Path) -> tuple[SkyrimEdition, str]:
        """Deriva edición y versión exacta leyendo el PE del ejecutable en *game_dir*.

        Devuelve ambos: SKSE está pinneado a la versión EXACTA del juego, no solo
        a su edición, así que el caller necesita la versión para el gate de
        compatibilidad además de la edición para elegir el payload.
        """
        for exe_name in _SKYRIM_EXE_NAMES:
            exe = game_dir / exe_name
            if exe.is_file():
                # pefile hace I/O síncrono de PE: fuera del event loop.
                edition = await asyncio.to_thread(detect_skyrim_edition, exe)
                version = await asyncio.to_thread(read_skyrim_version, exe)
                return edition, version

        raise ToolInstallError(
            f"No encontré el ejecutable de Skyrim en {game_dir}: no puedo determinar "
            "la edición para elegir el SKSE correcto. Revisá skyrim_path."
        )

    def _edition_to_config_key(self, edition: SkyrimEdition) -> str:
        """Convierte el enum SkyrimEdition a una clave del diccionario SKSE_CONFIG."""
        if edition == SkyrimEdition.UNKNOWN:
            raise ToolInstallError("La edición UNKNOWN no es compatible con SKSE.")
        mapping = {
            SkyrimEdition.AE: "AE",
            SkyrimEdition.SE: "SE",
            SkyrimEdition.LE: "LE",
        }
        # Fail-closed: una edición nueva en el enum (p. ej. VR) sin entrada acá debe
        # cortar, no caer a "AE". Defaultear silenciosamente elige el payload de otra
        # edición y escribe un DLL incompatible en el directorio del juego.
        key = mapping.get(edition)
        if key is None:
            raise ToolInstallError(f"La edición {edition.value} no tiene payload de SKSE configurado en SKSE_CONFIG.")
        return key

    async def _cleanup_orphaned_skse_dlls(
        self,
        game_dir: pathlib.Path,
        current_cfg: dict[str, str | None],
    ) -> None:
        """Elimina archivos DLL huérfanos de SKSE de versiones previas (limpieza de dirty upgrades)."""
        import stat

        # El glob de abajo es `skse*.dll`, que también matchea los steam loaders de
        # AMBAS familias. El de LE (`skse_steam_loader.dll`) no se deducía de
        # `current_cfg` porque LE tiene `steam_loader=None`, así que se lo nombra
        # explícito: borrarlo deja la instalación de LE sin loader de Steam.
        protected_names = {
            current_cfg["dll"],
            current_cfg.get("steam_loader"),
            "skse64_steam_loader.dll",
            "skse_steam_loader.dll",
        }
        protected_names.discard(None)

        for old_dll in game_dir.glob("skse*.dll"):
            if old_dll.name in protected_names:
                continue

            try:
                if old_dll.exists():
                    try:
                        mode = old_dll.stat().st_mode
                        if not (mode & stat.S_IWRITE):
                            old_dll.chmod(mode | stat.S_IWRITE)
                            logger.debug("Cleared read-only flag on %s", old_dll)
                    except OSError:
                        pass
                    old_dll.unlink()
                    logger.info("Removed orphaned SKSE DLL: %s", old_dll.name)
            except PermissionError as exc:
                logger.warning(
                    "No se pudo eliminar %s (¿Skyrim o MO2 están abiertos?): %s",
                    old_dll.name,
                    exc,
                )
                raise ToolInstallError(
                    f"Permiso denegado al limpiar {old_dll.name}. Cierra Skyrim y Mod Organizer."
                ) from exc
            except OSError as exc:
                logger.warning("Error limpiando %s: %s", old_dll.name, exc)

    async def _download_skse_archive(
        self,
        session: aiohttp.ClientSession,
        cfg: dict[str, str | None],
        dest_path: pathlib.Path,
    ) -> None:
        """Descarga el archivo SKSE vía NetworkGateway con timeout y validación de hash."""
        import hashlib

        self._validator.validate(dest_path)

        timeout = aiohttp.ClientTimeout(total=120, sock_read=60)
        downloaded = 0
        hasher = hashlib.sha256()

        url = _skse_cfg_field(cfg, "url")
        expected_sha256 = cfg.get("sha256")

        logger.info("Descargando SKSE desde %s ...", url)

        try:
            resp = await self._gateway.request(
                "GET",
                url,
                session,
                headers={"Accept": "application/octet-stream"},
                timeout=timeout,
                allowed_redirect_hosts=frozenset(["skse.silverlock.org"]),
            )
        except (EgressViolationError, NetworkGatewayTimeoutError) as exc:
            # El contrato del método es ToolInstallError; sin este wrap una denegación
            # de egress escapa cruda y el caller la reporta como fallo desconocido.
            raise ToolInstallError(f"Egress denegado o expirado para SKSE ({url}): {exc}") from exc

        try:
            resp.raise_for_status()

            # Techo de bytes. El `ClientTimeout` de arriba acota cuánto TIEMPO puede
            # durar la descarga, no cuántos bytes entran: contra un origen comprometido
            # o mal configurado que sirva una respuesta infinita, el streaming llena el
            # filesystem de staging sin que ningún timeout lo corte. El Content-Length
            # declarado se chequea primero para ni empezar cuando ya se sabe imposible,
            # pero no se confía en él: puede mentir o faltar, así que el contador real
            # se sigue evaluando chunk a chunk.
            _reject_oversized_skse_content_length(resp)

            with dest_path.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                    downloaded += len(chunk)
                    if downloaded > _SKSE_MAX_ARCHIVE_BYTES:
                        raise ToolInstallError(
                            f"La descarga de SKSE excede el tamaño máximo permitido "
                            f"({_SKSE_MAX_ARCHIVE_BYTES} bytes): se aborta antes de llenar el disco."
                        )
                    fh.write(chunk)
                    hasher.update(chunk)

                    if downloaded % (10 * _DOWNLOAD_CHUNK_SIZE) == 0:
                        logger.info("  ... %d bytes descargados", downloaded)
        except ToolInstallError:
            # El techo de bytes ya trae su propio mensaje accionable: propagarlo tal cual
            # y limpiar el parcial (el `finally` de abajo libera la respuesta).
            dest_path.unlink(missing_ok=True)
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            logger.error("Download failed for SKSE: %s", exc)
            if dest_path.exists():
                dest_path.unlink(missing_ok=True)
            raise ToolInstallError(f"Error descargando SKSE: {exc}") from exc
        finally:
            resp.release()

        if downloaded == 0:
            dest_path.unlink(missing_ok=True)
            raise ToolInstallError("SKSE download returned empty content")

        calculated_hash = hasher.hexdigest().upper()
        if expected_sha256 and calculated_hash != expected_sha256.upper():
            dest_path.unlink(missing_ok=True)
            raise ToolInstallError(
                f"Validación de hash fallida para SKSE. Esperado: {expected_sha256}, Calculado: {calculated_hash}"
            )

        logger.info(
            "SKSE descargado (%d bytes, sha256=%s...)",
            downloaded,
            calculated_hash[:16],
        )

    def _find_skse_root(self, extract_path: pathlib.Path, cfg: dict[str, str | None]) -> pathlib.Path:
        """Encuentra el directorio raíz de SKSE, excluyendo __MACOSX y validando estructura."""
        loader_name = _skse_cfg_field(cfg, "loader")
        dll_name = _skse_cfg_field(cfg, "dll")

        valid_loaders = [p for p in extract_path.rglob(loader_name) if "__MACOSX" not in p.parts and p.is_file()]

        if not valid_loaders:
            raise ToolInstallError(
                f"No se encontró {loader_name} válido en el archivo SKSE. "
                "El archivo puede estar corrupto o tener una estructura inesperada."
            )

        if len(valid_loaders) > 1:
            raise ToolInstallError(
                f"Estructura ambigua: se encontraron {len(valid_loaders)} instancias de {loader_name} en el archivo SKSE."
            )

        skse_root = valid_loaders[0].parent

        if not (skse_root / dll_name).exists():
            raise ToolInstallError(f"Payload SKSE incompleto: se encontró el loader pero falta el DLL ({dll_name}).")

        logger.debug("SKSE root encontrado: %s", skse_root)
        return skse_root

    def _assert_safe_copy_target(self, target: pathlib.Path, game_dir: pathlib.Path) -> None:
        """Rechaza *target* como destino de copia si puede escribir fuera de *game_dir*.

        ``shutil.copy2`` sigue los enlaces al escribir, y ``PathValidator`` opina sobre la
        ruta tal cual se la pasa. Con eso solo, un enlace precreado en el directorio del
        juego —o un ancestro enlazado, p. ej. ``Data/`` apuntando afuera— hace que la
        escritura aterrice fuera del sandbox aunque la ruta sin resolver caiga adentro.
        Por eso se rechaza cualquier enlace existente, no solo los que apuntan afuera: un
        enlace a otro archivo DENTRO del juego igual sobrescribe algo que no es el destino.
        """
        if target.is_symlink():
            raise ToolInstallError(
                f"Destino {target} es un enlace simbólico: SKSE no sobrescribe enlaces "
                "(la copia terminaría escribiendo en el archivo apuntado). Borralo y reintentá."
            )

        self._validator.validate(target)

        # El padre real puede no existir todavía (`Data/Scripts/`): se sube hasta el
        # primer ancestro que sí exista, que es el que `mkdir`/`copy2` van a atravesar.
        anchor = target.parent
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent

        game_root = game_dir.resolve()
        resolved = anchor.resolve()
        if resolved != game_root and game_root not in resolved.parents:
            raise ToolInstallError(
                f"Destino {target} resuelve fuera del directorio del juego ({resolved}): "
                "hay un enlace simbólico en el camino. Se aborta la instalación."
            )

    async def _copy_skse_files(
        self,
        skse_root: pathlib.Path,
        game_dir: pathlib.Path,
        cfg: dict[str, str | None],
    ) -> None:
        """Copia los binarios de SKSE y la carpeta Data al directorio del juego de forma segura."""
        import shutil

        copied_count = 0

        for item in skse_root.iterdir():
            target = game_dir / item.name

            if item.is_file():
                if item.suffix.lower() in (".dll", ".exe"):
                    self._assert_safe_copy_target(target, game_dir)

                    if target.exists():
                        try:
                            import stat

                            mode = target.stat().st_mode
                            if not (mode & stat.S_IWRITE):
                                target.chmod(mode | stat.S_IWRITE)
                        except OSError:
                            pass
                    # `copy2` es I/O de disco sincrónico: en árboles como el `Data/` de
                    # SKSE bloquea el event loop de NiceGUI (y con él las aprobaciones
                    # HITL) todo lo que dure la copia. Va al threadpool, igual que la
                    # extracción, pero la espera no es cancelable: el hilo copia bajo
                    # el lock y una cancelación no puede liberar el lock con la copia
                    # a medias.
                    await _esperar_hilo_ininterrumpible(shutil.copy2, item, target)
                    copied_count += 1
                    logger.debug("Copiado: %s → %s", item.name, target)
            elif item.is_dir() and item.name == "Data":
                # Validar la copia recursiva archivo por archivo
                for data_item in item.rglob("*"):
                    if data_item.is_file():
                        rel_path = data_item.relative_to(item.parent)
                        data_target = game_dir / rel_path

                        self._assert_safe_copy_target(data_target, game_dir)
                        data_target.parent.mkdir(parents=True, exist_ok=True)
                        await _esperar_hilo_ininterrumpible(shutil.copy2, data_item, data_target)
                        copied_count += 1
                logger.info("Copiada carpeta Data de SKSE")

        if copied_count == 0:
            raise ToolInstallError(
                f"No se copiaron archivos SKSE desde {skse_root}. "
                "Verifica que el archivo descargado contenga los binarios esperados."
            )

        self._verify_skse_installed(skse_root, game_dir, cfg)
        logger.info("SKSE: archivos copiados a %s", game_dir)

    def _verify_skse_installed(
        self,
        skse_root: pathlib.Path,
        game_dir: pathlib.Path,
        cfg: dict[str, str | None],
    ) -> None:
        """Verifica en DESTINO que la instalación de SKSE quedó completa.

        Contar archivos copiados no alcanza: un payload con el loader y un ``Data/``
        poblado deja ``copied_count > 0`` y reportaba éxito aunque el DLL de runtime
        —lo único que hace que SKSE cargue— nunca llegara al directorio del juego.
        """
        loader_name = _skse_cfg_field(cfg, "loader")
        dll_name = _skse_cfg_field(cfg, "dll")

        faltantes = [name for name in (loader_name, dll_name) if not (game_dir / name).is_file()]
        if faltantes:
            raise ToolInstallError(
                f"La instalación de SKSE quedó incompleta en {game_dir}: falta {', '.join(faltantes)}. "
                "El juego no cargaría SKSE. Revisá el archive descargado."
            )

        # El steam loader no entra en el corte duro: solo hace falta para arrancar desde
        # Steam, y MO2 lanza el loader directo. Fallar acá tiraría abajo una instalación
        # que funciona; avisar deja el rastro sin romperla.
        #
        # Pero la expectativa sale del PAYLOAD, no de `SKSE_CONFIG`: los archives no
        # traen todos el mismo set de archivos, así que condicionar el aviso a la
        # edición lo dispara en TODA instalación buena de un build que no lo incluye —
        # y le dice al operador que le falta algo que su build nunca tuvo. Mirando el
        # payload el aviso queda acotado a lo que sí es un defecto: el archive lo traía
        # y no aterrizó (copia incompleta), que es el caso de los builds históricos que
        # realmente lo necesitan. Se compara contra la raíz del payload porque es
        # exactamente el nivel que `_copy_skse_files` recorre: si el archivo no está
        # ahí, no había nada que copiar.
        steam_loader = cfg.get("steam_loader")
        if steam_loader and (skse_root / steam_loader).is_file() and not (game_dir / steam_loader).is_file():
            logger.warning(
                "SKSE instalado sin %s: el arranque desde Steam puede no cargar SKSE "
                "(lanzándolo desde MO2 o desde el loader funciona igual).",
                steam_loader,
            )

    async def ensure_bodyslide(
        self,
        install_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        downloader: NexusDownloader | None = None,
    ) -> InstallResult:
        """Ensure BodySlide is available, downloading from Nexus if necessary.

        Args:
            install_dir: Directory to install BodySlide into.
            session: Active HTTP session.
            downloader: Optional Nexus downloader.

        Returns:
            :class:`InstallResult` with the path to ``BodySlide.exe``.
        """
        self._validator.validate(install_dir)
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(install_dir),
            ttl=self._install_ttl,
        ):
            # Check if already installed
            exe = find_exe_in_dir(install_dir, "BodySlide.exe")
            if exe is not None:
                logger.info("BodySlide already installed at %s", exe)
                return InstallResult(
                    tool_name="BodySlide",
                    exe_path=exe,
                    version="existing",
                    already_existed=True,
                )

            if downloader is None:
                raise ToolInstallError("BodySlide requires a NexusDownloader for installation (not on GitHub).")

            # BodySlide is Mod ID 201 on SSE.
            nexus_id = 201
            logger.info("Fetching BodySlide metadata from Nexus (ID %d)...", nexus_id)
            try:
                file_info = await downloader.get_file_info(nexus_id, None, session)
            except Exception as exc:
                raise ToolInstallError(f"Failed to fetch BodySlide info from Nexus: {exc}") from exc

            decision = await self._hitl.request_approval(
                request_id=f"install-bodyslide-{file_info.file_id}",
                reason=f"Install BodySlide and Outfit Studio (Nexus ID {nexus_id})?",
                url=f"https://www.nexusmods.com/skyrimspecialedition/mods/{nexus_id}",
                detail=(
                    f"File: {file_info.file_name}\nSize: {file_info.size_bytes / (1024 * 1024):.1f} MB\nSource: Nexus Mods"
                ),
                category="download",
            )
            if decision is not Decision.APPROVED:
                raise ToolInstallError(f"BodySlide installation denied by operator (decision={decision.value})")

            # Download to staging dir first (as per NexusDownloader logic)
            archive_path = await downloader.download(file_info, session)

            # Extract into the specialized tools directory
            await _esperar_hilo_ininterrumpible(self._extract, archive_path, install_dir)

            # Cleanup archive in staging
            archive_path.unlink(missing_ok=True)

            exe = find_exe_in_dir(install_dir, "BodySlide.exe")
            if exe is None:
                raise ToolInstallError("BodySlide extraction succeeded but BodySlide.exe not found in output")

            logger.info("BodySlide installed at %s", exe)
            return InstallResult(
                tool_name="BodySlide",
                exe_path=exe,
                version="Nexus",
                already_existed=False,
            )

    async def ensure_ngio(
        self,
        mods_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        downloader: NexusDownloader | None = None,
        *,
        edition: SkyrimEdition,
    ) -> list[ModInstallResult]:
        """Instala las dependencias del precache de grass NGIO como mods de MO2.

        Componentes (SOP §2.8): NGIO-NG (GitHub DwemerEngineer), Address
        Library for SKSE Plugins (Nexus 32444, file según edición) y — SOLO en
        Anniversary Edition — Grass Cache Helper NG (Nexus 101095). Cada mod
        se extrae directo a ``mods_dir/<Nombre>/`` con su ``meta.ini``; NO se
        toca ``modlist.txt`` — la activación es responsabilidad del ritual
        (``MO2Controller.add_mod_to_modlist`` con los ``mod_name`` devueltos).

        Idempotente: un componente con sentinel (``SKSE/Plugins/*.dll`` o
        ``*.bin``) presente no se re-descarga ni pide HITL. Si el operador
        deniega a mitad de camino, lo ya instalado queda y el próximo run
        pide solo lo faltante.

        Args:
            mods_dir: Directorio ``mods/`` de la instancia MO2.
            session: Sesión HTTP activa.
            downloader: NexusDownloader (obligatorio — Address Library solo
                existe en Nexus).
            edition: Edición detectada del juego (SE/AE); LE/UNKNOWN abortan.

        Returns:
            Lista ordenada de :class:`ModInstallResult` (NGIO, Address Library
            y, en AE, Grass Cache Helper NG).
        """
        if edition not in (SkyrimEdition.SE, SkyrimEdition.AE):
            raise ToolInstallError(
                f"NGIO-NG requiere Skyrim SE o AE (edición detectada: {edition.value}). "
                "Corré el escaneo de entorno o configurá skyrim_path antes de reintentar."
            )
        if downloader is None:
            raise ToolInstallError(
                "Address Library viene de Nexus: hace falta un NexusDownloader (configurá la API key de Nexus)."
            )
        self._validator.validate(mods_dir)

        results = [
            await self._ensure_github_mod(
                mods_dir,
                session,
                mod_name=NGIO_MOD_NAME,
                releases_url=_NGIO_RELEASES_URL,
                asset_select=lambda assets: _select_ngio_asset(assets, edition),
                sentinel_glob="*.dll",
                request_slug="ngio",
                source_hint="GitHub DwemerEngineer/No-Grass-In-Objects-NG",
                # Comentario NGIO preservado explícitamente: el default de
                # _write_mod_meta_ini pasó a ser genérico (blueprint Fase 2).
                install_comment="Instalado por Sky-Claw (dependencias del precache de grass NGIO).",
            ),
            await self._ensure_nexus_mod(
                mods_dir,
                session,
                downloader,
                mod_name=ADDRESS_LIBRARY_MOD_NAME,
                nexus_id=_ADDRESS_LIBRARY_NEXUS_ID,
                sentinel_glob="version-1-5-*.bin" if edition is SkyrimEdition.SE else "version-1-6-*.bin",
                request_slug="address-library",
                select_file=lambda files: _select_address_library_file(files, edition),
                install_comment="Instalado por Sky-Claw (dependencias del precache de grass NGIO).",
            ),
        ]
        if edition is SkyrimEdition.AE:
            results.append(
                await self._ensure_nexus_mod(
                    mods_dir,
                    session,
                    downloader,
                    mod_name=GRASS_CACHE_HELPER_MOD_NAME,
                    nexus_id=_GRASS_CACHE_HELPER_NEXUS_ID,
                    sentinel_glob="*.dll",
                    request_slug="grass-cache-helper",
                    install_comment="Instalado por Sky-Claw (dependencias del precache de grass NGIO).",
                )
            )
        return results

    async def ensure_community_shaders(
        self,
        mods_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        downloader: NexusDownloader | None = None,
        *,
        edition: SkyrimEdition,
        game_version: str,
        game_dir: pathlib.Path,
    ) -> list[ModInstallResult]:
        """Instala Community Shaders y sus dependencias obligatorias como mods de MO2.

        Componentes (orden de dependencia = orden más barato para abortar):
        Address Library (Nexus 32444), SSE Engine Fixes (Nexus 17230, el plugin
        SKSE — nunca el preloader) y el core de Community Shaders (GitHub
        community-shaders/skyrim-community-shaders). Cada mod se extrae a
        ``mods_dir/<Nombre>/`` con su ``meta.ini``; NO se toca ``modlist.txt``
        — el caller persiste los nombres para la activación (contrato NGIO).

        Requisitos verificados en el código fuente de CS (XSEPlugin.cpp):
        Address Library es obligatoria (``UsesAddressLibrary``), Engine Fixes
        es requisito DURO de runtime (``LoadLibrary EngineFixes.dll`` →
        ``Required DLL missing``), y con ENB presente CS se auto-deshabilita
        (no-op silencioso).

        Gates fail-closed — TODOS corren antes del primer HITL y del primer
        byte de egress:
        1. Edición SE/AE (LE/UNKNOWN/VR abortan).
        2. ``downloader`` presente (Address Library y Engine Fixes solo existen
           en Nexus).
        3. Versión del juego: SE == 1.5.97 exacta; AE >= 1.6.1170 (intersección
           CS "latest" ∩ Engine Fixes "1.6.1170+"); vacía/ilegible → abort.
        4. SKSE instalado en la raíz del juego.
        5. ENB ausente (firma ``enbseries.ini`` / dir ``enbseries/``).
        6. Preloader de Engine Fixes presente en la raíz del juego.
        7. Validación del ``mods_dir``.

        Idempotente por sentinel por componente; denegación a mitad aborta el
        resto y conserva lo ya instalado (patrón NGIO).

        Args:
            mods_dir: Directorio ``mods/`` de la instancia MO2.
            session: Sesión HTTP activa.
            downloader: NexusDownloader (obligatorio).
            edition: Edición detectada del juego (SE/AE).
            game_version: Versión del juego (p.ej. ``"1.6.1170"``).
            game_dir: Raíz del juego (donde vive ``SkyrimSE.exe``).

        Returns:
            Lista ordenada de :class:`ModInstallResult` (Address Library,
            SSE Engine Fixes, Community Shaders).
        """
        if edition not in (SkyrimEdition.SE, SkyrimEdition.AE):
            raise ToolInstallError(
                f"Community Shaders requiere Skyrim SE o AE (edición detectada: {edition.value}). "
                "Corré el escaneo de entorno o configurá skyrim_path antes de reintentar."
            )
        if downloader is None:
            raise ToolInstallError(
                "Address Library y SSE Engine Fixes vienen de Nexus: hace falta un "
                "NexusDownloader (configurá la API key de Nexus)."
            )
        parsed = _parse_game_version(game_version)
        # ProductVersion de Windows trae 4 componentes (p.ej. "1.5.97.0") y
        # read_skyrim_version lo devuelve sin recortar: se normaliza a
        # major.minor.build antes de comparar (hallazgo de review del PR #442).
        if parsed is not None:
            parsed = parsed[:3]
        if edition is SkyrimEdition.SE:
            if parsed != _CS_SE_VERSION_TUPLE:
                raise ToolInstallError(
                    f"En Special Edition, Community Shaders requiere exactamente la versión "
                    f"{_CS_SE_VERSION} (detectada: {game_version!r}). Versión ilegible → abort fail-closed."
                )
        else:
            if parsed is None or parsed < _CS_AE_MIN_VERSION_TUPLE:
                raise ToolInstallError(
                    f"Community Shaders y SSE Engine Fixes requieren Skyrim AE {_CS_AE_MIN_VERSION} "
                    f"o superior (versión detectada: {game_version!r}). 1.6.640 y otras AE no-latest "
                    "no están soportadas (texto oficial de Community Shaders)."
                )
        if not _detectar_skse(game_dir):
            raise ToolInstallError(
                f"No se detectó SKSE en {game_dir}: Community Shaders es un plugin SKSE. "
                "Instalá SKSE primero (auto-instalador de SKSE del dashboard)."
            )
        if _detectar_enb(game_dir):
            raise ToolInstallError(
                f"ENB detectado en {game_dir} (enbseries.ini): Community Shaders se "
                "auto-deshabilita cuando ENB está presente — instalar 44 MB sería un no-op "
                "silencioso. Desinstalá ENB o no instales Community Shaders."
            )
        if not _detectar_preloader(game_dir):
            raise ToolInstallError(
                f"No se detectó el preloader de SSE Engine Fixes ({', '.join(_CS_PRELOADER_NAMES)}) "
                f"en {game_dir}: sin él, Engine Fixes no carga y Community Shaders aborta su check "
                "de runtime. Descargá el file 'Engine Fixes - SKSE64 Preloader' (Nexus 17230) y "
                "extraelo a la raíz del juego (o instalalo con Root Builder de MO2)."
            )
        self._validator.validate(mods_dir)

        results = [
            await self._ensure_nexus_mod(
                mods_dir,
                session,
                downloader,
                mod_name=ADDRESS_LIBRARY_MOD_NAME,
                nexus_id=_ADDRESS_LIBRARY_NEXUS_ID,
                sentinel_glob="version-1-5-*.bin" if edition is SkyrimEdition.SE else "version-1-6-*.bin",
                request_slug="address-library",
                select_file=lambda files: _select_address_library_file(files, edition),
            ),
            await self._ensure_nexus_mod(
                mods_dir,
                session,
                downloader,
                mod_name=SSE_ENGINE_FIXES_MOD_NAME,
                nexus_id=_SSE_ENGINE_FIXES_NEXUS_ID,
                sentinel_glob="EngineFixes.dll",
                request_slug="sse-engine-fixes",
                select_file=_select_engine_fixes_file,
                # El file MAIN de EF (verificado en el archive real de Nexus) es un
                # FOMOD con ramas SE/AE: se resuelve a la rama de la edición.
                post_extract=lambda mod_dir: _transform_fomod_engine_fixes(mod_dir, edition),
                # La idempotencia exige la config, igual que el fresh install:
                # _transform_fomod_engine_fixes es fail-closed ante un FOMOD sin
                # Required/SKSE (un EF sin EngineFixes.toml arranca con defaults
                # incorrectos), así que un mod ya instalado sin el toml está ROTO
                # y se re-instala en vez de reportarse listo (review de #442).
                required_files=("SKSE/Plugins/EngineFixes.toml",),
            ),
            await self._ensure_github_mod(
                mods_dir,
                session,
                mod_name=COMMUNITY_SHADERS_MOD_NAME,
                releases_url=_CS_RELEASES_URL,
                asset_select=_select_community_shaders_asset,
                sentinel_glob="CommunityShaders.dll",
                request_slug="community-shaders",
                source_hint="GitHub community-shaders/skyrim-community-shaders",
                required_rel=("Shaders",),
                install_comment="Instalado por Sky-Claw (Community Shaders).",
            ),
        ]
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _existing_mod_result(
        self,
        mod_dir: pathlib.Path,
        mod_name: str,
        sentinel_glob: str,
        required_rel: tuple[str, ...] = (),
        required_files: tuple[str, ...] = (),
    ) -> ModInstallResult | None:
        """Idempotencia: mod con sentinel presente → resultado sin HITL ni red.

        *required_rel* y *required_files* se re-verifican en el camino
        ya-instalado: un mod con el DLL pero sin los directorios/archivos
        requeridos NO es ``already_existed``: se re-instala en vez de reportar
        un mod roto como instalado. Ejemplos reales: CS 1.8.0 con el bug
        "AIO zip missing feature files" (Shaders/ ausente) y SSE Engine Fixes
        sin ``EngineFixes.toml`` (el plugin arrancaría con defaults
        incorrectos; el fresh install es fail-closed ante eso, la
        idempotencia no puede ser más laxa).
        """
        if (
            any((mod_dir / "SKSE" / "Plugins").glob(sentinel_glob))
            and all((mod_dir / rel).is_dir() for rel in required_rel)
            and all((mod_dir / rel).is_file() for rel in required_files)
        ):
            logger.info("%s ya instalado en %s", mod_name, mod_dir)
            return ModInstallResult(
                mod_name=mod_name,
                mod_dir=mod_dir,
                version="existing",
                already_existed=True,
            )
        return None

    def _verify_mod_payload(
        self,
        mod_dir: pathlib.Path,
        mod_name: str,
        sentinel_glob: str,
        required_rel: tuple[str, ...] = (),
        required_files: tuple[str, ...] = (),
    ) -> None:
        """Fail-closed si el archive no trajo el payload esperado.

        *sentinel_glob* verifica ``SKSE/Plugins/``; *required_rel* exige además
        directorios relativos a la raíz del mod y *required_files* archivos
        relativos a la raíz (verificación compuesta contra zips incompletos del
        upstream — p.ej. el bug real de Community Shaders "AIO zip missing
        feature files", 1.8.0 #2588, y un SSE Engine Fixes sin su
        ``EngineFixes.toml``).
        """
        if not any((mod_dir / "SKSE" / "Plugins").glob(sentinel_glob)):
            raise ToolInstallError(
                f"La extracción de {mod_name} terminó pero no se encontró "
                f"SKSE/Plugins/{sentinel_glob} — layout inesperado del archive."
            )
        for rel in required_rel:
            if not (mod_dir / rel).is_dir():
                raise ToolInstallError(
                    f"La extracción de {mod_name} terminó pero falta el directorio "
                    f"requerido {rel!r} — archive incompleto del upstream."
                )
        for rel in required_files:
            if not (mod_dir / rel).is_file():
                raise ToolInstallError(
                    f"La extracción de {mod_name} terminó pero falta el archivo "
                    f"requerido {rel!r} — archive incompleto del upstream."
                )

    async def _ensure_github_mod(
        self,
        mods_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        *,
        mod_name: str,
        releases_url: str,
        keyword: str | None = None,
        asset_select: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
        sentinel_glob: str,
        request_slug: str,
        source_hint: str,
        required_rel: tuple[str, ...] = (),
        required_files: tuple[str, ...] = (),
        clean_on_fail: bool = True,
        install_comment: str | None = None,
    ) -> ModInstallResult:
        """Instala un mod desde un release de GitHub en ``mods_dir/<mod_name>/``.

        Con *asset_select* elige el asset por criterio (p.ej. edición, cuando el
        release trae varios builds); si no, usa *keyword*.

        *required_rel* / *required_files*: verificación compuesta del payload
        (ver :meth:`_verify_mod_payload`). Si el destino no existía, la
        extracción ocurre en un staging propio y sólo se promueve al terminar;
        *clean_on_fail* elimina exclusivamente ese staging. Un destino
        preexistente se repara in-place y nunca se limpia. En denegación HITL o
        ``CancelledError`` tampoco se limpia (``except Exception`` no captura
        ``BaseException``). *install_comment*: comentario del ``meta.ini``;
        ``None`` → genérico.
        """
        mod_dir = mods_dir / mod_name
        # Lock por mod_dir, NO global: un componente no debe frenar a otro.
        # Reentrante: ``ensure_ngio`` pre-toma este lock via ContextVar si la
        # misma cadena de llamadas ya lo tiene.
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(mod_dir),
            ttl=self._install_ttl,
        ):
            existing = self._existing_mod_result(mod_dir, mod_name, sentinel_glob, required_rel, required_files)
            if existing is not None:
                return existing

            asset, version = await self._find_github_asset(session, releases_url, keyword=keyword, select=asset_select)

            decision = await self._hitl.request_approval(
                request_id=f"install-{request_slug}-{version}",
                reason=f"¿Instalar el mod {mod_name} {version}?",
                url=asset.browser_download_url,
                detail=(f"Asset: {asset.name}\nSize: {asset.size / (1024 * 1024):.1f} MB\nSource: {source_hint}"),
                category="download",
            )
            if decision is not Decision.APPROVED:
                raise ToolInstallError(
                    f"Instalación de {mod_name} denegada por el operador (decision={decision.value})"
                )

            # Si el destino no existe, extraer directamente allí crea un TOCTOU:
            # MO2/el operador puede poblarlo y un fallo posterior no distingue
            # esos archivos de los propios. El payload nuevo vive en un staging
            # único y sólo se promueve cuando está completamente verificado.
            destino_preexistia = mod_dir.exists()
            # Staging SEPARADO de mod_dir y POR MOD (mo2_root/.skyclaw-dl/<mod>):
            # un zip stale/parcial jamás vive dentro del mod — ni bloquea el
            # flatten, ni envenena `preexistia`, ni es visible en MO2. Hermano
            # Nexus (staging propio) alineado. El subdirectorio por mod es la
            # garantía de concurrencia: dos instalaciones de mods DISTINTOS
            # corren en paralelo (locks per-mod_dir, no globales) y con un
            # staging único el ``rmdir()`` del finally de la que terminaba podía
            # borrarle el directorio a la que seguía descargando → el ``open``
            # del destino moría con FileNotFoundError. Se keyea por ``mod_dir.name``
            # —la MISMA identidad que el lock de instalación— y no por
            # ``request_slug``: el lock lo deriva el caller de ``mod_dir``, así que
            # mismo staging ⟺ mismo mod_dir ⟺ mismo lock ⟺ serializadas. Keyear
            # por slug dejaba la garantía en la disciplina del caller: un slug
            # repetido entre dos mods distintos tendría locks distintos sobre el
            # MISMO staging y reintroduciría la carrera (review Codex). Se VALIDA
            # contra el PathValidator antes del mkdir: `mods_dir.parent` deriva de
            # configuración del usuario y "dentro de los roots" no puede quedar en
            # el comentario (mismo patrón que ensure_skse).
            staging = mods_dir.parent / ".skyclaw-dl" / mod_dir.name
            self._validator.validate(staging)
            staging.mkdir(parents=True, exist_ok=True)
            payload_dir = (
                mod_dir if destino_preexistia else pathlib.Path(tempfile.mkdtemp(prefix="payload-", dir=staging))
            )
            try:
                archive = await self._download_asset(
                    session,
                    asset,
                    staging,
                    # El pin explícito gana sobre el digest de la API; ambos
                    # vacíos degeneran a None (TOFU) — un str vacío NO es None
                    # y activaría la rama de validación con un hash vacío.
                    expected_sha256=_PINNED_SHA256.get(asset.name) or asset.sha256 or None,
                )
                await _esperar_hilo_ininterrumpible(self._extract, archive, payload_dir)
                try:
                    archive.unlink(missing_ok=True)
                except OSError:
                    # WinError 32 transitorio (handle del subproceso 7z/antivirus
                    # aún liberándose en Windows): el archive stale queda en el
                    # staging (.skyclaw-dl), fuera del mod, y la próxima corrida
                    # lo reescribe ("wb" lo trunca). El cleanup del archive es
                    # basura tolerable — nunca puede tumbar un payload ya extraído
                    # (cazado en el smoke E2E premium: el unlink falló y el except
                    # de clean_on_fail borró un mod VÁLIDO).
                    logger.warning("No se pudo borrar el archive temporal %s (lock transitorio)", archive)
                _flatten_single_root(payload_dir)
                self._verify_mod_payload(payload_dir, mod_name, sentinel_glob, required_rel, required_files)
                _write_mod_meta_ini(payload_dir, mod_name, version, comment=install_comment)
                if not destino_preexistia:
                    _promover_payload_verificado(payload_dir, mod_dir, mod_name)
            except Exception:
                if clean_on_fail and not destino_preexistia:
                    _limpiar_payload_temporal(payload_dir, mod_name)
                raise
            finally:
                # Limpieza del staging vacío (un zip stale hace fallar rmdir — se
                # tolera: la próxima corrida lo reescribe).
                with contextlib.suppress(OSError):
                    staging.rmdir()

            logger.info("%s %s instalado como mod en %s", mod_name, version, mod_dir)
            return ModInstallResult(mod_name=mod_name, mod_dir=mod_dir, version=version, already_existed=False)

    async def _ensure_nexus_mod(
        self,
        mods_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        downloader: NexusDownloader,
        *,
        mod_name: str,
        nexus_id: int,
        sentinel_glob: str,
        request_slug: str,
        select_file: Callable[[list[dict[str, Any]]], int] | None = None,
        required_rel: tuple[str, ...] = (),
        required_files: tuple[str, ...] = (),
        clean_on_fail: bool = True,
        install_comment: str | None = None,
        post_extract: Callable[[pathlib.Path], None] | None = None,
    ) -> ModInstallResult:
        """Instala un mod desde Nexus en ``mods_dir/<mod_name>/``.

        Con *select_file* el caller elige el file entre todos los de
        ``list_files`` (p.ej. Address Library SE vs AE); sin él se usa el
        file primary de :meth:`NexusDownloader.get_file_info`.

        *required_rel*, *required_files*, *clean_on_fail* e *install_comment*:
        mismo contrato que :meth:`_ensure_github_mod` (los dos hermanos
        comparten las tres invariantes de limpieza y la verificación
        compuesta). *post_extract*: transform del layout extraído antes de la
        verificación (p.ej. resolver un FOMOD a la rama de la edición — ver
        :func:`_transform_fomod_engine_fixes`).
        """
        mod_dir = mods_dir / mod_name
        # Lock por mod_dir, NO global; reentrante via ContextVar (idem _ensure_github_mod).
        async with _bajo_lock_de_instalacion(
            self._lock_manager,
            _install_lock_resource_id(mod_dir),
            ttl=self._install_ttl,
        ):
            existing = self._existing_mod_result(mod_dir, mod_name, sentinel_glob, required_rel, required_files)
            if existing is not None:
                return existing

            try:
                file_id: int | None = None
                if select_file is not None:
                    files = await downloader.list_files(nexus_id, session)
                    file_id = select_file(files)
                file_info = await downloader.get_file_info(nexus_id, file_id, session)
            except ToolInstallError:
                raise
            except Exception as exc:
                raise ToolInstallError(
                    f"No pude obtener metadata de Nexus para {mod_name} (mod {nexus_id}): {exc}"
                ) from exc

            decision = await self._hitl.request_approval(
                request_id=f"install-{request_slug}-{file_info.file_id}",
                reason=f"¿Instalar el mod {mod_name} (Nexus ID {nexus_id})?",
                url=f"https://www.nexusmods.com/skyrimspecialedition/mods/{nexus_id}",
                detail=(
                    f"File: {file_info.file_name}\nSize: {file_info.size_bytes / (1024 * 1024):.1f} MB\nSource: Nexus Mods"
                ),
                category="download",
            )
            if decision is not Decision.APPROVED:
                raise ToolInstallError(
                    f"Instalación de {mod_name} denegada por el operador (decision={decision.value})"
                )

            # Pin de SHA-256 keyed por file_name: NexusDownloader.download ya
            # enforcea FileInfo.sha256 (HashValidationError + cleanup del parcial).
            pin = _PINNED_SHA256.get(file_info.file_name)
            if pin:
                file_info = replace(file_info, sha256=pin)

            destino_preexistia = mod_dir.exists()
            staging = mods_dir.parent / ".skyclaw-dl" / mod_dir.name
            self._validator.validate(staging)
            staging.mkdir(parents=True, exist_ok=True)
            payload_dir = (
                mod_dir if destino_preexistia else pathlib.Path(tempfile.mkdtemp(prefix="payload-", dir=staging))
            )
            try:
                archive = await downloader.download(file_info, session)
                await _esperar_hilo_ininterrumpible(self._extract, archive, payload_dir)
                try:
                    archive.unlink(missing_ok=True)
                except OSError:
                    # WinError 32 transitorio (idem _ensure_github_mod): el cleanup
                    # del archive de staging no puede tumbar la instalación.
                    logger.warning("No se pudo borrar el archive temporal %s (lock transitorio)", archive)
                if post_extract is not None:
                    # Antes del flatten: el transform resuelve layouts no planos
                    # (p.ej. el FOMOD de EF) sin depender de renames frágiles.
                    # F5: es I/O pesado (copytree/copy2/rmtree del FOMOD de EF) —
                    # fuera del event loop, igual que la extracción; inline
                    # congelaba el loop principal (hang visible en NiceGUI).
                    await _esperar_hilo_ininterrumpible(post_extract, payload_dir)
                _flatten_single_root(payload_dir)
                self._verify_mod_payload(payload_dir, mod_name, sentinel_glob, required_rel, required_files)
                _write_mod_meta_ini(payload_dir, mod_name, file_info.file_name, comment=install_comment)
                if not destino_preexistia:
                    _promover_payload_verificado(payload_dir, mod_dir, mod_name)
            except Exception:
                if clean_on_fail and not destino_preexistia:
                    _limpiar_payload_temporal(payload_dir, mod_name)
                raise
            finally:
                with contextlib.suppress(OSError):
                    staging.rmdir()

            logger.info("%s (%s) instalado como mod en %s", mod_name, file_info.file_name, mod_dir)
            return ModInstallResult(
                mod_name=mod_name,
                mod_dir=mod_dir,
                version=file_info.file_name,
                already_existed=False,
            )

    async def _find_github_asset(
        self,
        session: aiohttp.ClientSession,
        releases_url: str,
        *,
        keyword: str | None = None,
        select: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    ) -> tuple[ReleaseAsset, str]:
        """Fetch the latest GitHub release and find a matching asset.

        Args:
            session: HTTP session.
            releases_url: GitHub API URL for latest release.
            keyword: Substring the asset name must contain (modo simple, un solo
                asset esperado). Ignorado si se pasa *select*.
            select: Selector sobre los assets de archivo (``.zip``/``.7z``) que
                decide cuál usar — p.ej. por edición cuando el release publica
                varios builds (NGIO: SE/AE/VR). Puede lanzar ``ToolInstallError``
                propio si nada matchea.

        Returns:
            Tuple of (:class:`ReleaseAsset`, version tag).
        """
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"Accept": "application/vnd.github+json"}

        resp = await self._gateway.request(
            "GET",
            releases_url,
            session,
            headers=headers,
            timeout=timeout,
            allowed_redirect_hosts=GITHUB_RELEASE_ASSET_REDIRECT_HOSTS,
        )
        try:
            if resp.status != 200:
                raise ToolInstallError(f"GitHub API returned {resp.status} for {releases_url}")
            data: dict[str, Any] = await resp.json()
        except (aiohttp.ClientError, TimeoutError, EgressViolationError, NetworkGatewayTimeoutError) as exc:
            logger.error("GitHub API request failed for %s: %s", releases_url, exc)
            raise
        finally:
            resp.release()

        version: str = data.get("tag_name", "unknown")
        assets: list[dict[str, Any]] = data.get("assets", [])
        archives = [a for a in assets if str(a.get("name", "")).lower().endswith((".zip", ".7z"))]

        if select is not None:
            chosen = select(archives)  # el selector lanza ToolInstallError si nada matchea
        else:
            chosen = next(
                (a for a in archives if keyword is not None and keyword.lower() in str(a.get("name", "")).lower()),
                None,
            )
            if chosen is None:
                available = [a.get("name", "?") for a in assets]
                raise ToolInstallError(
                    f"No asset matching '{keyword}' (.zip/.7z) in release {version}. Available: {available}"
                )

        name = str(chosen.get("name", ""))
        api_url = chosen.get("url")
        browser_download_url = chosen.get("browser_download_url")
        if not isinstance(api_url, str) or not api_url:
            raise ToolInstallError(f"Release asset {name!r} is missing its GitHub API URL")
        if not isinstance(browser_download_url, str) or not browser_download_url:
            raise ToolInstallError(f"Release asset {name!r} is missing its browser download URL")
        # Integridad: la API publica assets[].digest ("sha256:<hex>"). Presente →
        # la descarga se verifica; ausente → TOFU con warning (ver _download_asset).
        sha256 = _parse_github_digest(chosen.get("digest"))
        if sha256:
            logger.info("Digest sha256 de la API disponible para %s", name)
        else:
            logger.warning("Sin digest sha256 de la API para %s — descarga TOFU", name)
        return (
            ReleaseAsset(
                name=name,
                size=int(chosen.get("size", 0)),
                download_url=api_url,
                browser_download_url=browser_download_url,
                sha256=sha256,
            ),
            version,
        )

    async def _download_asset(
        self,
        session: aiohttp.ClientSession,
        asset: ReleaseAsset,
        dest_dir: pathlib.Path,
        expected_sha256: str | None = None,
    ) -> pathlib.Path:
        """Download a GitHub release asset to *dest_dir* via the API endpoint.

        Validates file size after download.  Logs progress every 10 MB.
        Con *expected_sha256* pinneado, un mismatch borra el archivo y aborta;
        sin pin se loguea el hash completo (TOFU, copiable para pinear).
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / asset.name
        self._validator.validate(dest)

        timeout = aiohttp.ClientTimeout(total=600, sock_read=60)
        headers = {"Accept": "application/octet-stream"}
        downloaded = 0
        hasher = hashlib.sha256()

        logger.info(
            "Downloading %s (%.1f MB) ...",
            asset.name,
            asset.size / (1024 * 1024),
        )

        resp = await self._gateway.request(
            "GET",
            asset.download_url,
            session,
            headers=headers,
            timeout=timeout,
            allowed_redirect_hosts=GITHUB_RELEASE_ASSET_REDIRECT_HOSTS,
        )
        try:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                    # Techo de bytes DURANTE el streaming: un cuerpo mayor al
                    # size declarado (CDN mintiendo, o metadata stale) aborta
                    # apenas se excede, sin escribir el chunk que excede. Si la
                    # API no publicó size (0), rige el techo absoluto de respaldo
                    # (_GITHUB_ASSET_MAX_BYTES) — sin él no hay límite hasta el
                    # timeout total de 600 s.
                    downloaded += len(chunk)
                    techo = asset.size if asset.size > 0 else _GITHUB_ASSET_MAX_BYTES
                    if downloaded > techo:
                        if asset.size > 0:
                            raise ToolInstallError(
                                f"La descarga de {asset.name} excede el tamaño declarado ({asset.size} bytes) — abortada."
                            )
                        raise ToolInstallError(
                            f"La descarga de {asset.name} excede el techo absoluto de respaldo "
                            f"({_GITHUB_ASSET_MAX_BYTES} bytes): la API no publicó size — abortada."
                        )
                    fh.write(chunk)
                    hasher.update(chunk)
                    if downloaded % (10 * _DOWNLOAD_CHUNK_SIZE) == 0:
                        logger.info(
                            "  ... %d / %d bytes (%.0f%%)",
                            downloaded,
                            asset.size,
                            (downloaded / asset.size * 100) if asset.size else 0,
                        )
        except (
            aiohttp.ClientError,
            OSError,
            TimeoutError,
            EgressViolationError,
            NetworkGatewayTimeoutError,
            ToolInstallError,
        ) as exc:
            logger.error("Download failed for %s: %s", asset.name, exc)
            if dest.exists():
                dest.unlink()
            raise
        finally:
            resp.release()

        # Size validation.
        if asset.size > 0 and downloaded != asset.size:
            dest.unlink(missing_ok=True)
            raise ToolInstallError(f"Size mismatch for {asset.name}: expected {asset.size}, got {downloaded}")

        actual_sha256 = hasher.hexdigest()
        if expected_sha256 is not None:
            if actual_sha256.lower() != expected_sha256.lower():
                dest.unlink(missing_ok=True)
                raise ToolInstallError(
                    f"SHA-256 mismatch para {asset.name}: esperado {expected_sha256}, "
                    f"obtenido {actual_sha256} — descarga abortada y archivo borrado."
                )
            logger.info("SHA-256 validado OK para %s (%d bytes)", asset.name, downloaded)
        else:
            logger.warning(
                "Sin pin ni digest sha256 para %s (TOFU). %d bytes, sha256=%s",
                asset.name,
                downloaded,
                actual_sha256,
            )
        return dest

    def _extract(self, archive: pathlib.Path, dest: pathlib.Path) -> None:
        """Extract archive into *dest* with zip-slip protection."""
        suffix = archive.suffix.lower()
        if suffix == ".zip":
            _extract_zip_safe(archive, dest)
        elif suffix == ".7z":
            _extract_7z_safe(archive, dest)
        else:
            raise ToolInstallError(f"Unsupported archive format: {suffix}")
        logger.info("Extracted %s → %s", archive.name, dest)
