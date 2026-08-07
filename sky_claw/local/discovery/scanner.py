"""Environment Scanner — extended auto-detection of Skyrim, MO2, and modding tools.

Builds on the existing :mod:`sky_claw.local.auto_detect` module but adds:
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
from sky_claw.local.discovery.environment import (
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
#: Nombres bajo los que puede venir el ejecutable de Pandora. Público a propósito:
#: es la ÚNICA fuente de verdad, compartida con ``ToolsInstaller.ensure_pandora``
#: (``sky_claw/local/tools_installer.py``). Detectar e instalar son dos superficies
#: del mismo recurso — si el instalador conociera su propia lista, instalar bajo un
#: nombre que el scanner no busca reporta "falta Pandora" tras reiniciar y ofrece
#: reinstalarlo (defecto reportado por Codex en el PR #418).
#: Anclado por igualdad literal en ``tests/test_tools_installer.py``.
#:
#: El ORDEN es preferencia, no cosmética: tanto el scanner como
#: ``find_any_exe_in_dir`` devuelven el primero que encuentran. Va primero el
#: nombre del release actual — con un `Pandora.exe` viejo conviviendo con el
#: nuevo (upgrade manual in-place), listar el legacy primero exportaba el binario
#: viejo por `PANDORA_EXE` y los rituales seguían lanzando ése.
PANDORA_EXE_NAMES = ("Pandora Behaviour Engine+.exe", "Pandora Engine.exe", "Pandora.exe")
_PANDORA_NAMES = PANDORA_EXE_NAMES
_LOOT_NAMES = ("LOOT.exe", "loot.exe")
_XEDIT_NAMES = ("SSEEdit.exe", "TES5Edit.exe", "xEdit.exe")
# NO agregar `sksevr_loader.exe` acá (se intentó y se revirtió — revisión de #425):
# a diferencia de _XEDIT_NAMES, donde las tres variantes SON el mismo tool renombrado
# por upstream entre releases, los loaders de SKSE son binarios DISTINTOS por edición.
# `_find_tool` matchea cualquiera de los nombres sin saber qué edición se detectó, así
# que sumar el de VR no ayuda al caso que buscaba cubrir (`_find_skyrim` no reconoce
# SkyrimVR.exe, así que un setup VR-only corta en CRITICAL antes de llegar a este
# chequeo) y sí amplía el riesgo de que una instalación SE/AE se reporte "SKSE
# encontrado" con un loader de VR que no le sirve. Arreglarlo bien requiere matchear
# el loader a la edición ya detectada, no ampliar el OR compartido.
_SKSE_LOADER_NAMES = ("skse64_loader.exe", "skse_loader.exe")
#: Loader que le corresponde a cada edición. SE/AE cargan la familia ``skse64_*`` y LE
#: la familia ``skse_*``: no son intercambiables, así que con la edición ya detectada
#: se busca el que aplica en vez del OR de ambos.
_SKSE_LOADERS_BY_EDITION: dict[SkyrimEdition, tuple[str, ...]] = {
    SkyrimEdition.AE: ("skse64_loader.exe",),
    SkyrimEdition.SE: ("skse64_loader.exe",),
    SkyrimEdition.LE: ("skse_loader.exe",),
}
#: DLLs que matchean ``skse*.dll`` en la raíz del juego pero NO son el DLL de runtime:
#: su nombre no codifica versión de juego y su presencia no implica que SKSE cargue.
_SKSE_NON_RUNTIME_DLLS = frozenset({"skse64_steam_loader.dll", "skse_steam_loader.dll"})


# ── SKSE ──────────────────────────────────────────────────────────────


def skse_dll_game_version(dll_name: str) -> str:
    """Versión exacta del juego (major.minor.patch) que targetea un DLL de SKSE.

    Se deriva del propio nombre del archivo (``skse64_1_6_1170.dll`` -> ``"1.6.1170"``)
    en vez de guardarla en un campo separado de ``SKSE_CONFIG``: un campo redundante
    puede desincronizarse del DLL real si alguien actualiza uno sin el otro.
    """
    stem = dll_name.removesuffix(".dll")
    return ".".join(stem.split("_")[-3:])


def skyrim_version_matches(detected: str, expected: str) -> bool:
    """¿*detected* (PE del ejecutable) es compatible con *expected* (la que targetea el DLL)?

    Compara por PREFIJO de segmentos, no por igualdad estricta: algunos recursos
    PE incluyen un cuarto segmento de build (``1.6.1170.0``) que no cambia la
    compatibilidad real con un DLL targeteado a ``1.6.1170``.
    """
    detected_parts = detected.split(".")
    expected_parts = expected.split(".")
    return detected_parts[: len(expected_parts)] == expected_parts


def find_skse_installation(
    game_dir: pathlib.Path,
    *,
    edition: SkyrimEdition = SkyrimEdition.UNKNOWN,
    game_version: str = "",
) -> pathlib.Path | None:
    """Loader de una instalación de SKSE **utilizable** en *game_dir*, o ``None``.

    SKSE no es un tool que Sky-Claw ejecute desde donde esté (como LOOT o xEdit): es un
    runtime que el JUEGO carga por ruta fija, y solo carga si el loader **y** el DLL de
    runtime del build exacto del ejecutable están en la raíz de Skyrim. Por eso no pasa
    por ``_resolve_tool_path``, que acepta cualquier loader que aparezca en MO2, en la
    carpeta de tools, en el PATH o pinneado por config: con ese resolver el snapshot
    reporta ✅ para un loader suelto en otra carpeta, o para un upgrade que quedó a mitad
    de camino (loader sí, DLL no), y el usuario se entera recién al arrancar el juego.

    Cuando *game_version* viene vacío (sin ``pefile``, o PE sin recurso de versión) el
    chequeo se degrada a "loader + algún DLL de runtime" en vez de fallar cerrado:
    reportar faltante toda instalación buena de las máquinas sin pefile sería peor que
    no verificar el build, y el caso del upgrade a medias se sigue atajando igual.
    """
    expected_loaders = _SKSE_LOADERS_BY_EDITION.get(edition, _SKSE_LOADER_NAMES)
    loader = next((game_dir / name for name in expected_loaders if (game_dir / name).is_file()), None)
    if loader is None:
        return None

    runtime_dlls = [
        dll for dll in game_dir.glob("skse*.dll") if dll.is_file() and dll.name.lower() not in _SKSE_NON_RUNTIME_DLLS
    ]
    if not runtime_dlls:
        return None

    if game_version and not any(
        skyrim_version_matches(game_version, skse_dll_game_version(dll.name)) for dll in runtime_dlls
    ):
        return None

    return loader


# ── Skyrim Version Detection ─────────────────────────────────────────


def _read_pe_product_version(exe_path: pathlib.Path) -> str | None:
    """ProductVersion del recurso PE del ejecutable.

    Devuelve ``""`` si el PE es válido pero no trae versión, y ``None`` si
    ``pefile`` no está instalada o el parseo falla — señal para que el caller
    recurra a la heurística de tamaño. ``PEFormatError`` hereda de ``Exception``
    a secas (no de ``OSError``/``ValueError``), por eso se nombra explícita.
    """
    try:
        import pefile  # type: ignore[import-untyped]
    except ImportError:
        return None

    version = ""
    try:
        pe = pefile.PE(str(exe_path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        if hasattr(pe, "FileInfo"):
            for fi_list in pe.FileInfo:
                for fi in fi_list:
                    if hasattr(fi, "StringTable"):
                        for st in fi.StringTable:
                            ver = st.entries.get(b"ProductVersion", b"").decode()
                            if ver:
                                version = ver
                                break
        pe.close()
    except (OSError, ValueError, pefile.PEFormatError):
        return None
    return version


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
    pe_version = _read_pe_product_version(exe_path)
    if pe_version is None:
        # pefile not installed or PE parsing failed — try file size heuristic
        try:
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            if size_mb > AE_MIN_SIZE_MB:  # AE executables are typically >60MB
                edition = SkyrimEdition.AE
        except OSError:
            pass
    else:
        version = pe_version

    # Upgrade SE to AE based on version number
    if edition == SkyrimEdition.SE and version:
        try:
            major_minor = version.split(".")
            if len(major_minor) >= 2 and int(major_minor[0]) >= 1 and int(major_minor[1]) >= AE_MIN_MINOR_VERSION:
                edition = SkyrimEdition.AE
        except (ValueError, IndexError):
            pass

    return version, edition


def detect_skyrim_edition(exe_path: pathlib.Path) -> SkyrimEdition:
    """Edición (SE/AE/LE/UNKNOWN) del ejecutable de Skyrim en *exe_path*."""
    return _detect_skyrim_version(exe_path)[1]


def read_skyrim_version(exe_path: pathlib.Path) -> str:
    """ProductVersion (PE) del ejecutable de Skyrim en *exe_path*, o ``""`` si no se pudo leer.

    Distinto de la edición: dos ejecutables pueden compartir edición (ambos AE)
    y tener versiones exactas distintas (1.6.640 vs 1.6.1170), lo que sí importa
    para binarios pinneados a la versión exacta del juego como SKSE.
    """
    return _detect_skyrim_version(exe_path)[0]


# ── Scanner ───────────────────────────────────────────────────────────


class EnvironmentScanner:
    """Scans the local system for Skyrim, MO2, and modding tools.

    Usage::

        scanner = EnvironmentScanner()
        snapshot = await scanner.scan()
        print(snapshot.health_status, snapshot.health_messages)

    Optionally seeded with the user's configured paths so setups with manual
    ``skyrim_path`` / ``loot_exe`` / ``xedit_exe`` (etc.) are honoured instead of
    relying purely on auto-detection:

    * ``skyrim_path`` — preferred over auto-detection when it holds a Skyrim exe.
    * ``tool_paths`` — maps a scanner tool key (``loot``, ``xedit``, ``pandora``,
      ``wrye_bash``, ``dyndolod``) to its executable path; each existing file
      seeds ``snap.tools`` directly, falling back to auto-detection otherwise.
    """

    def __init__(
        self,
        *,
        skyrim_path: str | pathlib.Path | None = None,
        tool_paths: dict[str, str] | None = None,
    ) -> None:
        self._configured_skyrim_path = pathlib.Path(skyrim_path) if skyrim_path else None
        # Drop empty/whitespace entries (Config defaults the keys to "") so a blank
        # value never masks auto-detection.
        self._configured_tool_paths: dict[str, pathlib.Path] = {
            key: pathlib.Path(raw) for key, raw in (tool_paths or {}).items() if raw and str(raw).strip()
        }

    async def scan(self) -> EnvironmentSnapshot:
        """Run all detectors concurrently and build an EnvironmentSnapshot."""
        try:
            return await asyncio.wait_for(self._scan_inner(), timeout=SEARCH_TIMEOUT_SECONDS * 3)
        except TimeoutError:
            logger.warning("Environment scan timed out")
            snap = EnvironmentSnapshot()
            snap.health_status = HealthStatus.CRITICAL
            snap.health_messages = ["⚠️ El escaneo del entorno tardó demasiado. Revisa los discos."]
            return snap

    async def _scan_inner(self) -> EnvironmentSnapshot:
        snap = EnvironmentSnapshot()

        # ── 1. Detect Skyrim ──────────────────────────────────────────
        skyrim_path = await self._find_skyrim()
        skyrim_found = skyrim_path is not None
        if skyrim_path:
            exe_name = "SkyrimSE.exe" if (skyrim_path / "SkyrimSE.exe").exists() else "Skyrim.exe"
            version, edition = _detect_skyrim_version(skyrim_path / exe_name)
            snap.skyrim = SkyrimInfo(
                path=skyrim_path,
                exe_name=exe_name,
                edition=edition,
                version=version,
                store=self._detect_store(skyrim_path),
            )
            snap.health_messages.append(f"✅ Skyrim {edition.value} detectado" + (f" (v{version})" if version else ""))
        else:
            snap.health_status = HealthStatus.CRITICAL
            snap.health_messages.append("❌ No se encontró Skyrim. Selecciona la carpeta del juego manualmente.")
            # Without Skyrim AND without any configured tool paths there is nothing
            # left to seed, so keep the original fast exit. But when the user pinned
            # tool executables in config, keep scanning so those tools are reported
            # as installed instead of all landing in `missing` (follow-up #2 / #209).
            if not self._configured_tool_paths:
                return snap

        # ── 2. Detect MO2 ─────────────────────────────────────────────
        mo2_path = await self._find_mo2()
        if mo2_path:
            profiles = self._list_mo2_profiles(mo2_path)
            snap.mo2 = MO2Info(path=mo2_path, profiles=profiles)
            snap.health_messages.append(f"✅ Mod Organizer 2 detectado ({len(profiles)} perfiles)")
        else:
            snap.health_messages.append(
                "⚠️ Mod Organizer 2 no encontrado. Los mods se gestionarán directamente en la carpeta Data."
            )

        # ── 3. Detect Tools ───────────────────────────────────────────
        tool_defs = [
            (
                "skse",
                _SKSE_LOADER_NAMES,
                "Extensor del motor (SKSE) - Requiere instalación manual o auto-instalador",
                "https://skse.silverlock.org/",
                True,
            ),
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
            (
                "community_shaders",
                # Exe ficticio con sufijo .exe: NO hay tal binario — la detección
                # real es por sentinel del mod bajo mo2_root/mods (caso especial
                # en el loop); `exe_names[0]` solo alimenta el `technical_name`
                # del MissingTool (el bloque lo deriva con .replace(".exe", "")).
                ("community_shaders.exe",),
                "Renderizado moderno (Community Shaders) - Instalable desde el dashboard",
                "https://www.nexusmods.com/skyrimspecialedition/mods/86492",
                False,
            ),
        ]

        mo2_root = mo2_path if mo2_path else None
        search_roots = self._build_search_roots(mo2_root, skyrim_path)

        for key, exe_names, friendly, url, is_critical in tool_defs:
            if key == "skse":
                # SKSE no usa el resolver genérico: ver `find_skse_installation`.
                found = (
                    find_skse_installation(
                        skyrim_path,
                        edition=snap.skyrim.edition if snap.skyrim else SkyrimEdition.UNKNOWN,
                        game_version=snap.skyrim.version if snap.skyrim else "",
                    )
                    if skyrim_path is not None
                    else None
                )
            elif key == "community_shaders":
                # Mod de MO2, no un exe: se detecta por el sentinel físico bajo
                # mods/ — el VFS de MO2 no es visible para el scanner. Importación
                # diferida: tools_installer importa de scanner en producción (ciclo).
                from sky_claw.local.tools_installer import COMMUNITY_SHADERS_MOD_NAME

                if mo2_root is None:
                    found = None
                else:
                    # Mismo criterio de validez que el instalador (hallazgo de
                    # review del PR #442): un mod con el DLL pero sin Shaders/
                    # (bug real CS 1.8.0) NO se muestra como instalado — la GUI
                    # conserva la acción de reparación.
                    mod_dir = pathlib.Path(mo2_root) / "mods" / COMMUNITY_SHADERS_MOD_NAME
                    sentinel = mod_dir / "SKSE" / "Plugins" / "CommunityShaders.dll"
                    found = sentinel if sentinel.is_file() and (mod_dir / "Shaders").is_dir() else None
            else:
                found = self._resolve_tool_path(key, exe_names, search_roots)
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

        # ── 3b. mteFunctions.pas ──────────────────────────────────────
        # Los scripts Pascal de Sky-Claw declaran `uses mteFunctions`, pero
        # xEdit no lo trae de fábrica: sin él la compilación falla a mitad de
        # un Ritual con error críptico (T-09). Detectarlo acá, con link.
        xedit_info = snap.tools.get("xedit")
        if xedit_info is not None:
            mte_path = xedit_info.exe_path.parent / "Edit Scripts" / "mteFunctions.pas"
            if not mte_path.exists():
                snap.missing.append(
                    MissingTool(
                        name="mteFunctions (xEdit)",
                        technical_name="mteFunctions",
                        friendly_description="Librería requerida por los scripts de parcheo de Sky-Claw",
                        download_url="https://github.com/matortheeternal/TES5EditScripts",
                        is_critical=False,
                    )
                )
                snap.health_messages.append(
                    "⚠️ xEdit encontrado pero falta mteFunctions.pas en 'Edit Scripts/': "
                    "los scripts de parcheo de Sky-Claw no compilarán."
                )

        # ── 4. Compute Health ─────────────────────────────────────────
        # A missing game is fatal regardless of which tools were seeded from
        # config, so don't let the tool tally downgrade CRITICAL → NEEDS_SETUP.
        if not skyrim_found:
            snap.health_status = HealthStatus.CRITICAL
        elif snap.missing:
            snap.health_status = HealthStatus.NEEDS_SETUP
        else:
            snap.health_status = HealthStatus.READY

        if snap.health_status == HealthStatus.READY:
            snap.health_messages.insert(0, "🟢 Todo listo para jugar")
        elif snap.health_status == HealthStatus.NEEDS_SETUP:
            snap.health_messages.insert(0, "🟡 Algunas herramientas faltan — puedes instalarlas abajo")

        return snap

    # ── Skyrim Detection ──────────────────────────────────────────────

    async def _find_skyrim(self) -> pathlib.Path | None:
        """Locate Skyrim (SE/AE/LE) via config, registry, Steam, GOG, Epic, common paths."""

        # 0. Configured path wins (the user pinned skyrim_path in config).
        configured = self._configured_skyrim_path
        if configured and ((configured / "SkyrimSE.exe").exists() or (configured / "Skyrim.exe").exists()):
            return configured

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
            # `Skyrim.exe` además de `SkyrimSE.exe`: las otras tres fuentes de este
            # método ya reconocían LE y esta se había quedado atrás.
            if (p / "SkyrimSE.exe").exists() or (p / "Skyrim.exe").exists():
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
        try:
            local_appdata = pathlib.Path.home() / "AppData" / "Local"
        except RuntimeError:
            local_appdata = None
        if local_appdata is not None:
            mo_dir = local_appdata / "ModOrganizer"
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

    def _resolve_tool_path(
        self,
        key: str,
        exe_names: tuple[str, ...],
        search_roots: list[pathlib.Path],
    ) -> pathlib.Path | None:
        """Resolve a tool's executable, preferring the user's configured path.

        A configured exe that exists on disk wins; otherwise fall back to the
        auto-detection scan over ``search_roots`` (and PATH). A configured path
        that no longer exists is ignored, never claimed as installed.
        """
        configured = self._configured_tool_paths.get(key)
        if configured is not None and configured.is_file():
            return configured
        return self._find_tool(exe_names, search_roots)

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
        _known_subdirs = (
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
            for subdir_name in _known_subdirs:
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
            return sorted([p.name for p in profiles_dir.iterdir() if p.is_dir() and (p / "modlist.txt").exists()]) or [
                "Default"
            ]
        except OSError:
            return ["Default"]
