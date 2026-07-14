"""Auto-installer for external tools (LOOT, SSEEdit).

Downloads official releases from GitHub when the tools are not found
locally.  Every download requires mandatory HITL operator approval and
passes through :class:`NetworkGateway` for egress control.
"""

from __future__ import annotations

import configparser
import hashlib
import logging
import pathlib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import aiohttp

from sky_claw.antigravity.security.hitl import Decision, HITLGuard
from sky_claw.antigravity.security.network_gateway import (
    EgressViolationError,
    NetworkGatewayTimeoutError,
)
from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError
from sky_claw.config import (
    GITHUB_RELEASE_ASSET_REDIRECT_HOSTS,
    SystemPaths,
)
from sky_claw.local.discovery.environment import SkyrimEdition

if TYPE_CHECKING:
    from sky_claw.antigravity.scraper.nexus_downloader import NexusDownloader
    from sky_claw.antigravity.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

# GitHub API endpoints for official releases.
_LOOT_RELEASES_URL = "https://api.github.com/repos/loot/loot/releases/latest"
_XEDIT_RELEASES_URL = "https://api.github.com/repos/TES5Edit/TES5Edit/releases/latest"
_PANDORA_RELEASES_URL = "https://api.github.com/repos/Monitor221hz/Pandora-Behaviour-Engine-Plus/releases/latest"
_NGIO_RELEASES_URL = "https://api.github.com/repos/DwemerEngineer/No-Grass-In-Objects-NG/releases/latest"

# Dependencias del precache de grass (SOP §2.8): NGIO-NG desde GitHub; Address
# Library y Grass Cache Helper NG desde Nexus. Los nombres son los directorios
# que ensure_ngio crea bajo mods/ (el orquestador los activa en modlist.txt).
NGIO_MOD_NAME = "No Grass In Objects NG"
ADDRESS_LIBRARY_MOD_NAME = "Address Library for SKSE Plugins"
GRASS_CACHE_HELPER_MOD_NAME = "Grass Cache Helper NG"
_ADDRESS_LIBRARY_NEXUS_ID = 32444
_GRASS_CACHE_HELPER_NEXUS_ID = 101095

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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """Metadata for a single GitHub release asset."""

    name: str
    size: int
    download_url: str
    browser_download_url: str


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Result of an auto-install operation."""

    tool_name: str
    exe_path: pathlib.Path
    version: str
    already_existed: bool


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
        from sky_claw.antigravity.core.validators import validate_path_strict

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


def _extract_7z_safe(archive: pathlib.Path, dest: pathlib.Path) -> None:
    """Extract a 7z archive with zip-slip protection."""
    try:
        import py7zr
    except ModuleNotFoundError as exc:
        raise RuntimeError("py7zr is required for .7z extraction — pip install py7zr") from exc

    with py7zr.SevenZipFile(archive, "r") as szf:
        for name in szf.getnames():
            if not _is_safe_path(name):
                raise PathViolationError(f"Zip-slip detected in 7z: {name!r}")
        szf.extractall(dest)


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


def scan_common_paths(common_paths: tuple[pathlib.Path, ...], exe_name: str) -> pathlib.Path | None:
    """Check common installation directories for an executable."""
    for base in common_paths:
        found = find_exe_in_dir(base, exe_name)
        if found is not None:
            return found
    return None


def _select_address_library_file(files: list[dict[str, Any]], edition: SkyrimEdition) -> int:
    """Elige el ``file_id`` de Address Library (Nexus 32444) para la edición.

    Filtra ``category_name == "MAIN"`` (fallback: todos los files) y matchea
    ``"anniversary"`` (AE) / ``"special"`` (SE) en ``name``/``file_name``
    lowercased. Nexus a veces sirve ``file_id`` como lista ``[id, game]`` —
    se toma el segundo elemento, mismo quirk que ``get_file_info``.

    Raises:
        ToolInstallError: Si ningún file matchea la edición (lista los nombres
            disponibles para diagnóstico).
    """
    keyword = "anniversary" if edition is SkyrimEdition.AE else "special"
    main_files = [f for f in files if str(f.get("category_name", "")).upper() == "MAIN"] or list(files)
    for f in main_files:
        haystack = f"{f.get('name', '')} {f.get('file_name', '')}".lower()
        if keyword in haystack:
            fid = f.get("file_id")
            if isinstance(fid, list):
                return int(fid[1])
            return int(fid)  # type: ignore[arg-type]
    available = [str(f.get("name") or f.get("file_name") or "?") for f in files]
    raise ToolInstallError(
        f"Ningún archivo de Address Library coincide con la edición {edition.value}. Disponibles: {available}"
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


def _write_mod_meta_ini(mod_dir: pathlib.Path, mod_name: str, version: str) -> None:
    """``meta.ini`` mínimo para que MO2 muestre el mod (patrón de GrassProfile)."""
    config = configparser.ConfigParser()
    config["General"] = {
        "modid": "0",
        "version": version,
        "name": mod_name,
        "comments": "Instalado por Sky-Claw (dependencias del precache de grass NGIO).",
    }
    with (mod_dir / "meta.ini").open("w", encoding="utf-8") as fh:
        config.write(fh)


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
    ) -> None:
        self._hitl = hitl
        self._gateway = gateway
        self._validator = path_validator

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
        self._extract(archive, install_dir)
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
            keyword="SSEEdit",
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
        self._extract(archive, install_dir)
        archive.unlink(missing_ok=True)

        exe = find_exe_in_dir(install_dir, "SSEEdit.exe")
        if exe is None:
            raise ToolInstallError("SSEEdit extraction succeeded but SSEEdit.exe not found in output")

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
            :class:`InstallResult` with the path to ``Pandora.exe``.
        """
        self._validator.validate(install_dir)
        # Check if already installed
        exe = find_exe_in_dir(install_dir, "Pandora.exe")
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
        self._extract(archive, install_dir)
        archive.unlink(missing_ok=True)

        exe = find_exe_in_dir(install_dir, "Pandora.exe")
        if exe is None:
            raise ToolInstallError("Pandora extraction succeeded but Pandora.exe not found in output")

        logger.info("Pandora %s installed at %s", version, exe)
        return InstallResult(
            tool_name="Pandora",
            exe_path=exe,
            version=version,
            already_existed=False,
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
        self._extract(archive_path, install_dir)

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
                keyword="NoGrassInObjectsNG",
                sentinel_glob="*.dll",
                request_slug="ngio",
                source_hint="GitHub DwemerEngineer/No-Grass-In-Objects-NG",
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
                )
            )
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _existing_mod_result(self, mod_dir: pathlib.Path, mod_name: str, sentinel_glob: str) -> ModInstallResult | None:
        """Idempotencia: mod con sentinel presente → resultado sin HITL ni red."""
        if any((mod_dir / "SKSE" / "Plugins").glob(sentinel_glob)):
            logger.info("%s ya instalado en %s", mod_name, mod_dir)
            return ModInstallResult(
                mod_name=mod_name,
                mod_dir=mod_dir,
                version="existing",
                already_existed=True,
            )
        return None

    def _verify_mod_payload(self, mod_dir: pathlib.Path, mod_name: str, sentinel_glob: str) -> None:
        """Fail-closed si el archive no trajo el payload SKSE esperado."""
        if not any((mod_dir / "SKSE" / "Plugins").glob(sentinel_glob)):
            raise ToolInstallError(
                f"La extracción de {mod_name} terminó pero no se encontró "
                f"SKSE/Plugins/{sentinel_glob} — layout inesperado del archive."
            )

    async def _ensure_github_mod(
        self,
        mods_dir: pathlib.Path,
        session: aiohttp.ClientSession,
        *,
        mod_name: str,
        releases_url: str,
        keyword: str,
        sentinel_glob: str,
        request_slug: str,
        source_hint: str,
    ) -> ModInstallResult:
        """Instala un mod desde un release de GitHub en ``mods_dir/<mod_name>/``."""
        mod_dir = mods_dir / mod_name
        existing = self._existing_mod_result(mod_dir, mod_name, sentinel_glob)
        if existing is not None:
            return existing

        asset, version = await self._find_github_asset(session, releases_url, keyword=keyword)

        decision = await self._hitl.request_approval(
            request_id=f"install-{request_slug}-{version}",
            reason=f"¿Instalar el mod {mod_name} {version}?",
            url=asset.browser_download_url,
            detail=(f"Asset: {asset.name}\nSize: {asset.size / (1024 * 1024):.1f} MB\nSource: {source_hint}"),
            category="download",
        )
        if decision is not Decision.APPROVED:
            raise ToolInstallError(f"Instalación de {mod_name} denegada por el operador (decision={decision.value})")

        archive = await self._download_asset(
            session,
            asset,
            mod_dir,
            expected_sha256=_PINNED_SHA256.get(asset.name),
        )
        self._extract(archive, mod_dir)
        archive.unlink(missing_ok=True)
        _flatten_single_root(mod_dir)
        self._verify_mod_payload(mod_dir, mod_name, sentinel_glob)
        _write_mod_meta_ini(mod_dir, mod_name, version)

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
    ) -> ModInstallResult:
        """Instala un mod desde Nexus en ``mods_dir/<mod_name>/``.

        Con *select_file* el caller elige el file entre todos los de
        ``list_files`` (p.ej. Address Library SE vs AE); sin él se usa el
        file primary de :meth:`NexusDownloader.get_file_info`.
        """
        mod_dir = mods_dir / mod_name
        existing = self._existing_mod_result(mod_dir, mod_name, sentinel_glob)
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
            raise ToolInstallError(f"Instalación de {mod_name} denegada por el operador (decision={decision.value})")

        # Pin de SHA-256 keyed por file_name: NexusDownloader.download ya
        # enforcea FileInfo.sha256 (HashValidationError + cleanup del parcial).
        pin = _PINNED_SHA256.get(file_info.file_name)
        if pin:
            file_info = replace(file_info, sha256=pin)

        archive = await downloader.download(file_info, session)
        mod_dir.mkdir(parents=True, exist_ok=True)
        self._extract(archive, mod_dir)
        archive.unlink(missing_ok=True)
        _flatten_single_root(mod_dir)
        self._verify_mod_payload(mod_dir, mod_name, sentinel_glob)
        _write_mod_meta_ini(mod_dir, mod_name, file_info.file_name)

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
        keyword: str,
    ) -> tuple[ReleaseAsset, str]:
        """Fetch the latest GitHub release and find a matching asset.

        Args:
            session: HTTP session.
            releases_url: GitHub API URL for latest release.
            keyword: Substring the asset name must contain.

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

        # Find the first asset whose name contains the keyword and is
        # a .zip or .7z archive.
        for a in assets:
            name: str = a.get("name", "")
            if keyword.lower() in name.lower() and (name.endswith(".zip") or name.endswith(".7z")):
                api_url = a.get("url")
                browser_download_url = a.get("browser_download_url")
                if not isinstance(api_url, str) or not api_url:
                    raise ToolInstallError(f"Release asset {name!r} is missing its GitHub API URL")
                if not isinstance(browser_download_url, str) or not browser_download_url:
                    raise ToolInstallError(f"Release asset {name!r} is missing its browser download URL")
                return (
                    ReleaseAsset(
                        name=name,
                        size=int(a.get("size", 0)),
                        download_url=api_url,
                        browser_download_url=browser_download_url,
                    ),
                    version,
                )

        available = [a.get("name", "?") for a in assets]
        raise ToolInstallError(f"No asset matching '{keyword}' (.zip/.7z) in release {version}. Available: {available}")

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
                    fh.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if downloaded % (10 * _DOWNLOAD_CHUNK_SIZE) == 0:
                        logger.info(
                            "  ... %d / %d bytes (%.0f%%)",
                            downloaded,
                            asset.size,
                            (downloaded / asset.size * 100) if asset.size else 0,
                        )
        except (aiohttp.ClientError, OSError, TimeoutError, EgressViolationError, NetworkGatewayTimeoutError) as exc:
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
                "Sin pin SHA-256 para %s (TOFU). %d bytes, sha256=%s",
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
