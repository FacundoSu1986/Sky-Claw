# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for Sky-Claw.

Build with::

    pyinstaller sky_claw.spec --clean
"""

import os
import sys

block_cipher = None

# Collect data files: web UI static assets and xEdit scripts.
datas = [
    ("sky_claw/antigravity/web/static", "sky_claw/antigravity/web/static"),
    ("sky_claw/local/xedit/scripts", "sky_claw/local/xedit/scripts"),
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
    version_info={
        "FileVersion": (0, 1, 0, 0),
        "ProductVersion": (0, 1, 0, 0),
        "FileDescription": "Agente autónomo de modding para Skyrim",
        "ProductName": "Sky-Claw",
        "CompanyName": "FacundoSu1986",
        "InternalName": "SkyClawApp",
        "OriginalFilename": "SkyClawApp.exe",
        "LegalCopyright": "MIT License",
    } if sys.platform == "win32" else None,
)
