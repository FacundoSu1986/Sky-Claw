# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for Sky-Claw.

Build with::

    pyinstaller sky_claw.spec --clean
"""

import os
import re
import sys

block_cipher = None


def _resolve_version_tuple():
    """Derive the Windows VERSIONINFO tuple ``(major, minor, patch, 0)`` at build time.

    The single source of truth is the installed package version (``hatch-vcs`` /
    the git tag), so the exe's embedded properties never drift from the release.
    Falls back to ``sky_claw.__version__`` (the frozen-exe literal in
    ``sky_claw/__init__.py``) when dist metadata is unavailable, and to
    ``(0, 0, 0, 0)`` if neither can be parsed. ``hatch-vcs`` dev/dirty suffixes
    such as ``0.2.4.dev3+g1234567`` are tolerated -- only the first three
    integers are used.
    """
    version_str = ""
    try:
        from importlib.metadata import version as _pkg_version

        version_str = _pkg_version("sky-claw")
    except Exception:
        try:
            # Read the literal directly from source to avoid importing the
            # full package (which executes sky_claw/__init__.py and its
            # transitive imports) during PyInstaller spec evaluation.
            import pathlib

            _src = (pathlib.Path(__file__).parent / "sky_claw" / "__init__.py").read_text(encoding="utf-8")
            _m = re.search(r'__version__\s*=\s*["\'](\d[^"\']*)["\']', _src)
            version_str = _m.group(1) if _m else ""
        except Exception:
            version_str = ""

    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version_str or "")
    if match is None:
        return (0, 0, 0, 0)
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch, 0)


_VERSION_TUPLE = _resolve_version_tuple()


def _build_version_info():
    """Construct the Windows VS_VERSION_INFO resource PyInstaller embeds in the exe.

    PyInstaller's ``EXE`` only honours the ``version=`` argument (a
    ``VSVersionInfo`` object or a path to a version text file); a bare ``dict``
    is silently ignored. We build the structure programmatically so the embedded
    FileVersion/ProductVersion always track ``_VERSION_TUPLE`` -- no manual bump.
    """
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    version_display = ".".join(str(part) for part in _VERSION_TUPLE)
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_VERSION_TUPLE,
            prodvers=_VERSION_TUPLE,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,  # VOS_NT_WINDOWS32
            fileType=0x1,  # VFT_APP
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",  # U.S. English (0x0409), Unicode codepage (0x04B0)
                        [
                            StringStruct("CompanyName", "FacundoSu1986"),
                            StringStruct("FileDescription", "Agente autónomo de modding para Skyrim"),
                            StringStruct("FileVersion", version_display),
                            StringStruct("InternalName", "SkyClawApp"),
                            StringStruct("LegalCopyright", "MIT License"),
                            StringStruct("OriginalFilename", "SkyClawApp.exe"),
                            StringStruct("ProductName", "Sky-Claw"),
                            StringStruct("ProductVersion", version_display),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
        ],
    )

# Collect data files: web UI static assets, GUI css + image assets, xEdit
# scripts, and fail-closed security policy data required at import time.
datas = [
    ("sky_claw/antigravity/web/static", "sky_claw/antigravity/web/static"),
    # GUI assets served via add_static_files in sky_claw_gui.setup_app.
    # Without these the frozen exe crashes at startup (the directory does
    # not exist inside sys._MEIPASS).
    ("sky_claw/antigravity/gui/styles.css", "sky_claw/antigravity/gui"),
    ("sky_claw/antigravity/gui/assets", "sky_claw/antigravity/gui/assets"),
    ("sky_claw/local/xedit/scripts", "sky_claw/local/xedit/scripts"),
    ("sky_claw/antigravity/security/security_policy.yaml", "sky_claw/antigravity/security"),
]

# Hidden imports that PyInstaller cannot detect automatically.
hiddenimports = [
    # Async I/O
    "aiohttp",
    "aiosqlite",
    "aiofiles",
    "aiofiles.os",
    # Validation
    "pydantic",
    "pydantic_core",
    "pydantic.deprecated.decorator",
    # Retry logic
    "tenacity",
    # XML parsing
    "defusedxml",
    "defusedxml.ElementTree",
    # Archive extraction (optional, may not be installed)
    "py7zr",
    "rarfile",
    # LLM providers
    "sky_claw.antigravity.agent.providers",
    # Web UI
    "sky_claw.antigravity.web",
    "sky_claw.antigravity.web.app",
    # SSL/TLS for HTTPS
    "ssl",
    "certifi",
    # SQLite
    "sqlite3",
    # All sky_claw submodules
    "sky_claw.antigravity.agent.router",
    "sky_claw.antigravity.agent.tools",
    "sky_claw.antigravity.comms.telegram",
    "sky_claw.antigravity.comms.telegram_sender",
    "sky_claw.antigravity.db.async_registry",
    "sky_claw.antigravity.db.registry",
    "sky_claw.local.fomod.installer",
    "sky_claw.local.fomod.parser",
    "sky_claw.local.fomod.resolver",
    "sky_claw.local.loot.cli",
    "sky_claw.local.mo2.vfs",
    "sky_claw.antigravity.orchestrator.sync_engine",
    "sky_claw.antigravity.scraper.masterlist",
    "sky_claw.antigravity.scraper.nexus_downloader",
    "sky_claw.antigravity.security.hitl",
    "sky_claw.antigravity.security.network_gateway",
    "sky_claw.antigravity.security.path_validator",
    "sky_claw.antigravity.security.sanitize",
    "sky_claw.local.tools_installer",
    "sky_claw.local.local_config",
    "sky_claw.local.xedit.runner",
    "sky_claw.local.xedit.output_parser",
    "sky_claw.local.xedit.conflict_analyzer",
    # New modules (added during migration audit 2026-04-22)
    "sky_claw.antigravity.core.dlq_manager",
    "sky_claw.antigravity.orchestrator.tool_dispatcher",
    "sky_claw.antigravity.orchestrator.tool_strategies",
    "sky_claw.antigravity.orchestrator.tool_strategies.base",
    "sky_claw.antigravity.orchestrator.tool_strategies.execute_loot_sorting",
    "sky_claw.antigravity.orchestrator.tool_strategies.execute_synthesis",
    "sky_claw.antigravity.orchestrator.tool_strategies.generate_bashed_patch",
    "sky_claw.antigravity.orchestrator.tool_strategies.generate_lods",
    "sky_claw.antigravity.orchestrator.tool_strategies.middleware",
    "sky_claw.antigravity.orchestrator.tool_strategies.query_mod_metadata",
    "sky_claw.antigravity.orchestrator.tool_strategies.resolve_conflict_patch",
    "sky_claw.antigravity.orchestrator.tool_strategies.scan_asset_conflicts",
    "sky_claw.antigravity.orchestrator.tool_strategies.validate_plugin_limit",
    "sky_claw.antigravity.security.loop_guardrail",
    "sky_claw.antigravity.agent.hermes_parser",
]

a = Analysis(
    ["sky_claw/__main__.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tests",
        "pytest",
        "_pytest",
        "hypothesis",
        "test",
    ],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SkyClawApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Embedded VS_VERSION_INFO resource. PyInstaller reads the ``version=``
    # kwarg only (a bare ``version_info`` dict is silently dropped), so the
    # version tuple is derived at build time from the package/git-tag version --
    # see ``_resolve_version_tuple``/``_build_version_info`` above.
    version=_build_version_info() if sys.platform == "win32" else None,
)
