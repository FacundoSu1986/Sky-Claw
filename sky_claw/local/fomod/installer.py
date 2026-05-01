"""FOMOD Installer — extract archives and install mods into MO2.

Bridges the gap between a downloaded archive and a fully installed mod
inside an MO2 portable instance.  Supports FOMOD wizard-style installers
as well as simple "dump everything" mods.

Extraction formats: ``.zip``, ``.7z``, ``.rar``.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiofiles
import aiofiles.os

from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError, safe_join
from sky_claw.local.fomod.parser import FomodParseError, parse_fomod
from sky_claw.local.fomod.resolver import FomodResolver

if TYPE_CHECKING:
    from sky_claw.local.fomod.models import FileInstall


async def async_copy2(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy src to dst using shutil.copy2 in a thread pool."""
    await asyncio.to_thread(shutil.copy2, str(src), str(dst))


logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Result models
# ------------------------------------------------------------------


@dataclass
class InstallResult:
    """Outcome of a mod installation."""

    mod_name: str
    files_copied: list[str] = field(default_factory=list)
    pending_decisions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    installed: bool = False


@dataclass
class FomodPreview:
    """Preview of FOMOD installation options."""

    mod_name: str
    has_fomod: bool
    steps: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------
# Extraction helpers
# ------------------------------------------------------------------


def _is_safe_path(member_path: str) -> bool:
    """Reject paths containing traversal components (zip-slip)."""
    parts = pathlib.PurePosixPath(member_path).parts
    return ".." not in parts


def _extract_zip(archive: pathlib.Path, dest: pathlib.Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not _is_safe_path(info.filename):
                raise PathViolationError(f"Zip-slip detected: {info.filename!r}")
            target_path = (dest_resolved / info.filename).resolve()
            if not target_path.is_relative_to(dest_resolved):
                raise PathViolationError(f"Path traversal detected: {info.filename!r} escapes sandbox")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_7z(archive: pathlib.Path, dest: pathlib.Path) -> None:
    try:
        import py7zr
    except ModuleNotFoundError as exc:
        raise RuntimeError("py7zr is required for .7z extraction — pip install py7zr") from exc

    dest_resolved = dest.resolve()
    with py7zr.SevenZipFile(archive, "r") as szf:
        for name in szf.getnames():
            if not _is_safe_path(name):
                raise PathViolationError(f"Zip-slip detected in 7z: {name!r}")
            target_path = (dest_resolved / name).resolve()
            if not target_path.is_relative_to(dest_resolved):
                raise PathViolationError(f"Path traversal detected in 7z: {name!r} escapes sandbox")

        # Validated strictly. We extract one by one avoiding extractall.
        for name in szf.getnames():
            szf.extract(path=dest_resolved, targets=[name])


def _extract_rar(archive: pathlib.Path, dest: pathlib.Path) -> None:
    try:
        import rarfile
    except ModuleNotFoundError as exc:
        raise RuntimeError("rarfile is required for .rar extraction — pip install rarfile") from exc

    dest_resolved = dest.resolve()
    with rarfile.RarFile(archive, "r") as rf:
        for info in rf.infolist():
            if info.is_dir():
                continue
            if not _is_safe_path(info.filename):
                raise PathViolationError(f"Zip-slip detected in rar: {info.filename!r}")
            target_path = (dest_resolved / info.filename).resolve()
            if not target_path.is_relative_to(dest_resolved):
                raise PathViolationError(f"Path traversal detected in rar: {info.filename!r} escapes sandbox")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with rf.open(info) as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


_EXTRACTORS: dict[str, Any] = {
    ".zip": _extract_zip,
    ".7z": _extract_7z,
    ".rar": _extract_rar,
}

_SUPPORTED_EXTENSIONS = frozenset(_EXTRACTORS.keys())


# ------------------------------------------------------------------
# FOMOD Installer
# ------------------------------------------------------------------


class FomodInstaller:
    """Extract and install mods using FOMOD configuration.

    Args:
        path_validator: Sandbox validator for all file operations.
    """

    def __init__(self, path_validator: PathValidator) -> None:
        self._validator = path_validator

    async def preview(self, archive_path: pathlib.Path) -> FomodPreview:
        """Preview FOMOD installation options without full extraction.

        Args:
            archive_path: Path to the mod archive.

        Returns:
            Preview with available steps and options.
        """
        archive_path = self._validator.validate(archive_path)
        mod_name = archive_path.stem

        # RND-03: Delegar I/O síncrono (zipfile) a thread pool
        fomod_xml = await asyncio.to_thread(self._extract_fomod_xml, archive_path)
        if fomod_xml is None:
            return FomodPreview(mod_name=mod_name, has_fomod=False)

        try:
            config = parse_fomod_string(fomod_xml)
        except FomodParseError as exc:
            logger.warning("FOMOD parse error in %s: %s", archive_path.name, exc)
            return FomodPreview(mod_name=mod_name, has_fomod=False)

        steps = []
        for step in config.install_steps:
            step_info: dict[str, Any] = {"name": step.name, "groups": []}
            for group in step.groups:
                group_info = {
                    "name": group.name,
                    "type": group.group_type.value,
                    "options": [p.name for p in group.plugins],
                }
                step_info["groups"].append(group_info)
            steps.append(step_info)

        return FomodPreview(
            mod_name=config.module_name or mod_name,
            has_fomod=True,
            steps=steps,
        )

    async def install(
        self,
        archive_path: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
        selections: dict[str, list[str]] | None = None,
    ) -> InstallResult:
        """Extract and install a mod archive into MO2.

        Args:
            archive_path: Path to the mod archive.
            mo2_mods_dir: MO2 ``mods/`` directory.
            selections: FOMOD selections ``{step_name: [plugin_names]}``.

        Returns:
            Result with copied files and any pending decisions.
        """
        archive_path = self._validator.validate(archive_path)
        mo2_mods_dir = self._validator.validate(mo2_mods_dir)
        mod_name = archive_path.stem

        suffix = archive_path.suffix.lower()
        extractor = _EXTRACTORS.get(suffix)
        if extractor is None:
            return InstallResult(
                mod_name=mod_name,
                errors=[
                    f"Unsupported archive format: {suffix!r}. Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
                ],
            )

        tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="skyclaw_install_"))
        try:
            # Extract — RND-03: Delegar extracción síncrona a thread pool
            try:
                await asyncio.to_thread(extractor, archive_path, tmp_dir)
            except PathViolationError:
                raise
            except Exception as exc:
                return InstallResult(
                    mod_name=mod_name,
                    errors=[f"Extraction failed: {exc}"],
                )

            # Locate FOMOD
            fomod_xml_path = self._find_fomod_xml(tmp_dir)

            if fomod_xml_path is not None:
                return await self._install_fomod(
                    tmp_dir,
                    fomod_xml_path,
                    mo2_mods_dir,
                    mod_name,
                    selections or {},
                )
            else:
                return await self._install_simple(
                    tmp_dir,
                    mo2_mods_dir,
                    mod_name,
                )
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_fomod_xml(self, archive_path: pathlib.Path) -> str | None:
        """Try to read fomod/ModuleConfig.xml from archive without full extraction."""

        suffix = archive_path.suffix.lower()
        if suffix == ".zip":
            return self._read_from_zip(archive_path)
        # For other formats, we need full extraction — return None
        # to trigger the full extract path.
        return None

    @staticmethod
    def _read_from_zip(archive_path: pathlib.Path) -> str | None:
        """Read FOMOD XML directly from a zip without extracting."""
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                names = zf.namelist()
                for name in names:
                    normalized = name.replace("\\", "/").lower()
                    if normalized.endswith("fomod/moduleconfig.xml"):
                        return zf.read(name).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError, OSError):
            pass
        return None

    @staticmethod
    def _find_fomod_xml(extract_dir: pathlib.Path) -> pathlib.Path | None:
        """Find fomod/ModuleConfig.xml in extracted directory."""
        for candidate in extract_dir.rglob("ModuleConfig.xml"):
            if candidate.parent.name.lower() == "fomod":
                return candidate
        return None

    async def _install_fomod(
        self,
        extract_dir: pathlib.Path,
        fomod_xml_path: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
        fallback_name: str,
        selections: dict[str, list[str]],
    ) -> InstallResult:
        """Install using FOMOD configuration."""
        try:
            config = parse_fomod(fomod_xml_path)
        except FomodParseError as exc:
            return InstallResult(
                mod_name=fallback_name,
                errors=[f"FOMOD parse error: {exc}"],
            )

        mod_name = config.module_name or fallback_name
        resolver = FomodResolver(config)
        result = resolver.resolve(selections)

        if result.pending_decisions and not result.files:
            return InstallResult(
                mod_name=mod_name,
                pending_decisions=result.pending_decisions,
            )

        # The FOMOD content root is the parent of the fomod/ directory.
        content_root = fomod_xml_path.parent.parent

        mod_dest = mo2_mods_dir / mod_name
        copied = await self._copy_resolved_files(
            content_root,
            mod_dest,
            result.files,
        )

        return InstallResult(
            mod_name=mod_name,
            files_copied=copied,
            pending_decisions=result.pending_decisions,
            installed=len(copied) > 0,
        )

    async def _install_simple(
        self,
        extract_dir: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
        mod_name: str,
    ) -> InstallResult:
        """Install a simple mod (no FOMOD) by copying all files."""
        # Detect if there's a single top-level directory
        top_items = list(extract_dir.iterdir())
        if len(top_items) == 1 and top_items[0].is_dir():
            content_root = top_items[0]
            mod_name = top_items[0].name
        else:
            content_root = extract_dir

        mod_dest = mo2_mods_dir / mod_name
        copied: list[str] = []

        for src_file in content_root.rglob("*"):
            if src_file.is_dir():
                continue
            rel = src_file.relative_to(content_root)
            dest_file = mod_dest / rel

            await aiofiles.os.makedirs(dest_file.parent, exist_ok=True)
            # Use asynchronous copy to avoid blocking the event loop
            await async_copy2(src_file, dest_file)
            copied.append(str(rel))

        return InstallResult(
            mod_name=mod_name,
            files_copied=copied,
            installed=len(copied) > 0,
        )

    async def _copy_resolved_files(
        self,
        content_root: pathlib.Path,
        mod_dest: pathlib.Path,
        files: list[FileInstall],
    ) -> list[str]:
        """Copy resolved FOMOD files to the mod destination.

        Each ``<file source="..." destination="..."/>`` attribute pair from the
        FOMOD manifest is attacker-controlled.  We use :func:`safe_join` to
        sandbox both the source (must stay inside *content_root*) and the
        destination (must stay inside *mod_dest*) before any I/O.

        Poisoned entries are logged at CRITICAL level and skipped — the rest of
        the install continues so legitimate files are not lost.
        """
        copied: list[str] = []

        for file_install in files:
            dest_rel = file_install.destination or file_install.source

            # --- sandbox source ---
            try:
                src = safe_join(content_root, file_install.source)
            except PathViolationError as exc:
                logger.critical("FOMOD source path escapes content root — skipping entry: %s", exc)
                continue

            # --- sandbox destination ---
            try:
                dest = safe_join(mod_dest, dest_rel)
            except PathViolationError as exc:
                logger.critical("FOMOD destination path escapes mod dest — skipping entry: %s", exc)
                continue

            if src.is_dir():
                for child in src.rglob("*"):
                    if child.is_dir():
                        continue
                    child_rel = child.relative_to(src)

                    try:
                        child_dest = safe_join(dest, child_rel)
                    except PathViolationError as exc:
                        logger.critical("FOMOD child dest escapes sandbox — skipping: %s", exc)
                        continue

                    await aiofiles.os.makedirs(child_dest.parent, exist_ok=True)
                    await async_copy2(child, child_dest)
                    copied.append(str(pathlib.PurePosixPath(dest_rel) / child_rel))
            elif src.is_file():
                await aiofiles.os.makedirs(dest.parent, exist_ok=True)
                await async_copy2(src, dest)
                copied.append(str(dest_rel))
            else:
                logger.warning(
                    "FOMOD source not found: %s (in %s)",
                    file_install.source,
                    content_root,
                )

        return copied


# Re-import for preview
from sky_claw.local.fomod.parser import parse_fomod_string  # noqa: E402
