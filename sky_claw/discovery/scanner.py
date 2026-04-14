"""Environment Scanner — extended auto-detection of Skyrim, MO2, and modding tools.

Builds on the existing :mod:`sky_claw.auto_detect` module but adds:
- Skyrim version + edition detection (SE vs AE vs LE)
- GOG and Epic Games Store detection
- Wrye Bash, DynDOLOD, and Pandora discovery
- Structured :class:`EnvironmentSnapshot` output
- Human-readable health messages in Spanish

Every search has a hard timeout to keep startup fast (<5s total).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shutil

from sky_claw.config import (
    AE_MIN_MINOR_VERSION,
    AE_MIN_SIZE_MB,
    COMMON_TOOL_ROOTS,
    MO2_COMMON_PATHS,
    SEARCH_TIMEOUT_SECONDS,
    SKYRIM_COMMON_PATHS,
    STEAM_DEFAULT_PATHS,
)
from sky_claw.discovery.environment import (
    EnvironmentSnapshot,
    HealthStatus,
    MissingTool,
    MO2Info,
    SkyrimEdition,
    SkyrimInfo,
    ToolInfo,
)

logger = logging.getLogger(__name__)

# ── Windows Registry ──────────────────────────────────────────────────

try:
    import winreg

    _HAS_WINREG = True
except ImportError:
    _HAS_WINREG = False


def _reg_read(hive: int, subkey: str, value_name: str) -> str | None:
    """Read a string value from the Windows registry, or None."""
    if not _HAS_WINREG:
        return None
    try:
        with winreg.OpenKey(hive, subkey) as key:
            data, _ = winreg.QueryValueEx(key, value_name)
            return str(data)
    except OSError:
        return None


# ── Steam Library Parser ──────────────────────────────────────────────


def _parse_steam_libraries() -> list[pathlib.Path]:
    """Find all Steam library folders from libraryfolders.vdf."""
    folders: list[pathlib.Path] = []
    for steam_root in STEAM_DEFAULT_PATHS:
        vdf = pathlib.Path(steam_root) / "steamapps" / "libraryfolders.vdf"
        if not vdf.exists():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                line = line.strip().strip('"')
                if line.startswith("path"):
                    _, _, val = line.partition('"')
                    val = val.strip().strip('"').replace("\\\\", "\\")
                    if val:
                        folders.append(pathlib.Path(val))
        except OSError:
            pass
    return folders


# ── Tool Search Paths ─────────────────────────────────────────────────


_WRYE_BASH_NAMES = ("Wrye Bash.exe", "Wrye Bash Launcher.exe")
_DYNDOLOD_NAMES = ("DynDOLOD64.exe", "DynDOLOD.exe", "DynDOLODx64.exe")
_PANDORA_NAMES = ("Pandora.exe", "Pandora Engine.exe")
_LOOT_NAMES = ("LOOT.exe", "loot.exe")
_XEDIT_NAMES = ("SSEEdit.exe", "TES5Edit.exe", "xEdit.exe")


# ── Skyrim Version Detection ─────────────────────────────────────────


def _detect_skyrim_version(exe_path: pathlib.Path) -> tuple[str, SkyrimEdition]:
    """Try to read the PE version resource from SkyrimSE.exe / Skyrim.exe.

    Returns (version_string, edition).
    Falls back to heuristics if PE parsing fails.
    """
    version = ""
    edition = SkyrimEdition.UNKNOWN

    # Determine edition from executable name
    name_lower = exe_path.name.lower()
    if name_lower == "skyrimse.exe":
        edition = SkyrimEdition.SE  # Will be upgraded to AE if version >= 1.6
    elif name_lower == "skyrim.exe":
        edition = SkyrimEdition.LE

    # Try reading version from PE header
    try:
        import pefile  # type: ignore[import-untyped]

        pe = pefile.PE(str(exe_path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        if hasattr(pe, "FileInfo"):
            for fi_list in pe.FileInfo:
                for fi in fi_list:
                    if hasattr(fi, "StringTable"):
                        for st in fi.StringTable:
                            ver = st.entries.get(b"ProductVersion", b"").decode()
                            if ver:
                                version = ver
                                break
                    if hasattr(fi, "Value"):
                        # VarFileInfo → fixed version info
                        pass
        pe.close()
    except (ImportError, OSError, ValueError):
        # pefile not installed or PE parsing failed — try file size heuristic
        try:
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            if size_mb > AE_MIN_SIZE_MB:  # AE executables are typically >60MB
                edition = SkyrimEdition.AE
        except OSError:
            pass

    # Upgrade SE to AE based on version number
    if edition == SkyrimEdition.SE and version:
        try:
            major_minor = version.split(".")
            if (
                len(major_minor) >= 2
                and int(major_minor[0]) >= 1
                and int(major_minor[1]) >= AE_MIN_MINOR_VERSION
            ):
                edition = SkyrimEdition.AE
        except (ValueError, IndexError):
            pass

    return version, edition


# ── Scanner ───────────────────────────────────────────────────────────


class EnvironmentScanner:
    """Scans the local system for Skyrim, MO2, and modding tools.

    Usage::

        scanner = EnvironmentScanner()
        snapshot = await scanner.scan()
        print(snapshot.health_status, snapshot.health_messages)
    """

    async def scan(self) -> EnvironmentSnapshot:
        """Run all detectors concurrently and build an EnvironmentSnapshot."""
        try:
            return await asyncio.wait_for(
                self._scan_inner(), timeout=SEARCH_TIMEOUT_SECONDS * 3
            )
        except TimeoutError:
            logger.warning("Environment scan timed out")
            snap = EnvironmentSnapshot()
            snap.health_status = HealthStatus.CRITICAL
            snap.health_messages = [
                "⚠️ El escaneo del entorno tardó demasiado. Revisa los discos."
            ]
            return snap

    async def _scan_inner(self) -> EnvironmentSnapshot:
        snap = EnvironmentSnapshot()

        # ── 1. Detect Skyrim ──────────────────────────────────────────
        skyrim_path = await self._find_skyrim()
        if skyrim_path:
            exe_name = (
                "SkyrimSE.exe"
                if (skyrim_path / "SkyrimSE.exe").exists()
                else "Skyrim.exe"
            )
            version, edition = _detect_skyrim_version(skyrim_path / exe_name)
            snap.skyrim = SkyrimInfo(
                path=skyrim_path,
                exe_name=exe_name,
                edition=edition,
                version=version,
                store=self._detect_store(skyrim_path),
            )
            snap.health_messages.append(
                f"✅ Skyrim {edition.value} detectado"
                + (f" (v{version})" if version else "")
            )
        else:
            snap.health_status = HealthStatus.CRITICAL
            snap.health_messages.append(
                "❌ No se encontró Skyrim. Selecciona la carpeta del juego manualmente."
            )
            return snap

        # ── 2. Detect MO2 ─────────────────────────────────────────────
        mo2_path = await self._find_mo2()
        if mo2_path:
            profiles = self._list_mo2_profiles(mo2_path)
            snap.mo2 = MO2Info(path=mo2_path, profiles=profiles)
            snap.health_messages.append(
                f"✅ Mod Organizer 2 detectado ({len(profiles)} perfiles)"
            )
        else:
            snap.health_messages.append(
                "⚠️ Mod Organizer 2 no encontrado. Los mods se gestionarán directamente en la carpeta Data."
            )

        # ── 3. Detect Tools ───────────────────────────────────────────
        tool_defs = [
            (
                "loot",
                _LOOT_NAMES,
                "Ordenar mods automáticamente",
                "https://github.com/loot/loot/releases",
                True,
            ),
            (
                "xedit",
                _XEDIT_NAMES,
                "Limpiar archivos problemáticos",
                "https://github.com/TES5Edit/TES5Edit/releases",
                False,
            ),
            (
                "pandora",
                _PANDORA_NAMES,
                "Generar animaciones",
                "https://github.com/Monitor221hz/Pandora-Behaviour-Engine-Plus/releases",
                False,
            ),
            (
                "wrye_bash",
                _WRYE_BASH_NAMES,
                "Crear parche de compatibilidad",
                "https://www.nexusmods.com/skyrimspecialedition/mods/6837",
                False,
            ),
            (
                "dyndolod",
                _DYNDOLOD_NAMES,
                "Optimizar gráficos (LOD)",
                "https://www.nexusmods.com/skyrimspecialedition/mods/32382",
                False,
            ),
        ]

        mo2_root = mo2_path if mo2_path else None
        search_roots = self._build_search_roots(mo2_root, skyrim_path)

        for key, exe_names, friendly, url, is_critical in tool_defs:
            found = self._find_tool(exe_names, search_roots)
            if found:
                human_name = key.upper().replace("_", " ")
                snap.tools[key] = ToolInfo(
                    name=human_name,
                    exe_path=found,
                    friendly_action=friendly,
                )
                snap.health_messages.append(f"✅ {human_name} encontrado")
            else:
                human_name = key.upper().replace("_", " ")
                snap.missing.append(
                    MissingTool(
                        name=human_name,
                        technical_name=exe_names[0].replace(".exe", ""),
                        friendly_description=friendly,
                        download_url=url,
                        is_critical=is_critical,
                    )
                )
                emoji = "⚠️" if not is_critical else "❌"
                snap.health_messages.append(f"{emoji} {human_name} no encontrado")

        # ── 4. Compute Health ─────────────────────────────────────────
        critical_missing = [m for m in snap.missing if m.is_critical]
        if critical_missing or snap.missing:
            snap.health_status = HealthStatus.NEEDS_SETUP
        else:
            snap.health_status = HealthStatus.READY

        if snap.health_status == HealthStatus.READY:
            snap.health_messages.insert(0, "🟢 Todo listo para jugar")
        elif snap.health_status == HealthStatus.NEEDS_SETUP:
            snap.health_messages.insert(
                0, "🟡 Algunas herramientas faltan — puedes instalarlas abajo"
            )

        return snap

    # ── Skyrim Detection ──────────────────────────────────────────────

    async def _find_skyrim(self) -> pathlib.Path | None:
        """Locate Skyrim (SE/AE/LE) via registry, Steam, GOG, Epic, common paths."""

        # 1. Registry — Steam
        for reg_key in (
            r"SOFTWARE\Bethesda Softworks\Skyrim Special Edition",
            r"SOFTWARE\WOW6432Node\Bethesda Softworks\Skyrim Special Edition",
            r"SOFTWARE\Bethesda Softworks\Skyrim",
        ):
            path_str = _reg_read(
                winreg.HKEY_LOCAL_MACHINE if _HAS_WINREG else 0,
                reg_key,
                "Installed Path",
            )
            if path_str:
                p = pathlib.Path(path_str)
                if (p / "SkyrimSE.exe").exists() or (p / "Skyrim.exe").exists():
                    return p

        # 2. Registry — GOG
        gog_path = _reg_read(
            winreg.HKEY_LOCAL_MACHINE if _HAS_WINREG else 0,
            r"SOFTWARE\WOW6432Node\GOG.com\Games\1711230643",
            "path",
        )
        if gog_path:
            p = pathlib.Path(gog_path)
            if (p / "SkyrimSE.exe").exists():
                return p

        # 3. Steam libraries
        for lib in _parse_steam_libraries():
            candidate = lib / "steamapps" / "common" / "Skyrim Special Edition"
            if (candidate / "SkyrimSE.exe").exists():
                return candidate
            # LE fallback
            candidate_le = lib / "steamapps" / "common" / "Skyrim"
            if (candidate_le / "Skyrim.exe").exists():
                return candidate_le

        # 4. Common direct paths
        for raw in SKYRIM_COMMON_PATHS:
            p = pathlib.Path(raw)
            if (p / "SkyrimSE.exe").exists():
                return p

        return None

    # ── MO2 Detection ─────────────────────────────────────────────────

    async def _find_mo2(self) -> pathlib.Path | None:
        """Locate Mod Organizer 2."""
        for raw in MO2_COMMON_PATHS:
            p = pathlib.Path(raw)
            if (p / "ModOrganizer.exe").exists():
                return p

        # AppData
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            mo_dir = pathlib.Path(local_appdata) / "ModOrganizer"
            if mo_dir.is_dir():
                for child in mo_dir.iterdir():
                    if child.is_dir() and (child / "ModOrganizer.exe").exists():
                        return child

        # Program Files
        for pf in (r"C:\Program Files", r"C:\Program Files (x86)"):
            candidate = pathlib.Path(pf) / "Mod Organizer 2"
            if (candidate / "ModOrganizer.exe").exists():
                return candidate

        return None

    # ── Tool Detection ────────────────────────────────────────────────

    def _find_tool(
        self,
        exe_names: tuple[str, ...],
        search_roots: list[pathlib.Path],
    ) -> pathlib.Path | None:
        """Search for a tool executable in the given roots.

        Performance: Only checks direct children and specific known
        subfolder names. Does NOT iterate all subdirectories.
        """
        # Known subfolder names where tools are typically installed
        _KNOWN_SUBDIRS = (
            "LOOT",
            "SSEEdit",
            "xEdit",
            "Wrye Bash",
            "WryeBash",
            "Pandora",
            "DynDOLOD",
            "BodySlide",
            "tools",
            "Pandora Engine",
            "Pandora Behaviour Engine",
        )

        for root in search_roots:
            if not root.is_dir():
                continue
            for exe_name in exe_names:
                # Direct child
                candidate = root / exe_name
                if candidate.is_file():
                    return candidate

            # Check only known subfolder names (fast)
            for subdir_name in _KNOWN_SUBDIRS:
                subdir = root / subdir_name
                if not subdir.is_dir():
                    continue
                for exe_name in exe_names:
                    candidate = subdir / exe_name
                    if candidate.is_file():
                        return candidate

        # Check PATH as fallback
        for exe_name in exe_names:
            in_path = shutil.which(exe_name.replace(".exe", ""))
            if in_path:
                return pathlib.Path(in_path)

        return None

    def _build_search_roots(
        self,
        mo2_root: pathlib.Path | None,
        skyrim_path: pathlib.Path | None,
    ) -> list[pathlib.Path]:
        """Build a list of directories to scan for tools.

        NOTE: Avoids scanning generic 'Program Files' (thousands of folders).
        Instead, adds specific known tool locations within PF.
        """
        roots: list[pathlib.Path] = []

        # MO2-internal tools (highest priority)
        if mo2_root:
            roots.append(mo2_root)
            tools_dir = mo2_root / "tools"
            if tools_dir.is_dir():
                roots.append(tools_dir)

        # Common modding roots
        for raw in COMMON_TOOL_ROOTS:
            p = pathlib.Path(raw)
            if p.is_dir():
                roots.append(p)

        # Skyrim folder itself
        if skyrim_path:
            roots.append(skyrim_path)

        # Specific PF locations (NOT generic Program Files scan)
        for pf_str in (r"C:\Program Files", r"C:\Program Files (x86)"):
            for tool_name in ("LOOT", "SSEEdit", "Mod Organizer 2", "Wrye Bash"):
                candidate = pathlib.Path(pf_str) / tool_name
                if candidate.is_dir():
                    roots.append(candidate)

        return roots

    # ── Helpers ────────────────────────────────────────────────────────

    def _detect_store(self, skyrim_path: pathlib.Path) -> str:
        """Guess which store the Skyrim installation came from."""
        path_str = str(skyrim_path).lower()
        if "steam" in path_str or "steamapps" in path_str:
            return "steam"
        if "gog" in path_str:
            return "gog"
        if "epic" in path_str:
            return "epic"
        # Check for Steam appmanifest
        appmanifest = skyrim_path.parent / "appmanifest_489830.acf"
        if appmanifest.exists():
            return "steam"
        return "unknown"

    def _list_mo2_profiles(self, mo2_root: pathlib.Path) -> list[str]:
        """List available MO2 profiles."""
        profiles_dir = mo2_root / "profiles"
        if not profiles_dir.is_dir():
            return ["Default"]
        try:
            return sorted(
                [
                    p.name
                    for p in profiles_dir.iterdir()
                    if p.is_dir() and (p / "modlist.txt").exists()
                ]
            ) or ["Default"]
        except OSError:
            return ["Default"]
