"""Tests for :class:`EnvironmentScanner` honouring manually-configured paths.

Follow-up #2 from PR #209: on setups where the user pinned ``skyrim_path`` /
``loot_exe`` / ``xedit_exe`` in the config, the scanner must

1. prefer the configured ``skyrim_path`` over auto-detection, and
2. NOT early-return before seeding tools when Skyrim isn't auto-detected — it
   must seed ``snap.tools[key]`` from the configured executables instead of
   sending them all to ``snap.missing`` (which made installed tools show up as
   "No instalado" in the Rituales).
"""

from __future__ import annotations

from pathlib import Path

from sky_claw.local.discovery.scanner import EnvironmentScanner, _read_pe_product_version


def _touch_exe(directory: Path, name: str) -> Path:
    """Create a stub executable; only its existence/size are probed by the scan."""
    exe = directory / name
    exe.write_bytes(b"MZ")
    return exe


async def test_configured_tool_path_seeds_tools_without_skyrim(tmp_path: Path) -> None:
    # No Skyrim is auto-detectable on the test host, so without config every tool
    # would land in ``missing``. A configured loot_exe must seed snap.tools["loot"].
    loot_exe = _touch_exe(tmp_path, "loot.exe")
    scanner = EnvironmentScanner(tool_paths={"loot": str(loot_exe)})

    snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.tools["loot"].exe_path == loot_exe
    assert not any(m.technical_name.lower() == "loot" for m in snap.missing)


async def test_configured_skyrim_path_is_respected(tmp_path: Path) -> None:
    skyrim_dir = tmp_path / "Skyrim Special Edition"
    skyrim_dir.mkdir()
    _touch_exe(skyrim_dir, "SkyrimSE.exe")
    scanner = EnvironmentScanner(skyrim_path=str(skyrim_dir))

    snap = await scanner.scan()

    assert snap.skyrim is not None
    assert snap.skyrim.path == skyrim_dir


async def test_multiple_configured_tool_paths_seeded(tmp_path: Path) -> None:
    loot_exe = _touch_exe(tmp_path, "loot.exe")
    xedit_exe = _touch_exe(tmp_path, "SSEEdit.exe")
    scanner = EnvironmentScanner(tool_paths={"loot": str(loot_exe), "xedit": str(xedit_exe)})

    snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.has_tool("xedit")
    assert snap.tools["xedit"].exe_path == xedit_exe


async def test_missing_configured_tool_path_falls_back_to_missing(tmp_path: Path) -> None:
    # A configured path that doesn't exist must not crash and must not be claimed
    # as installed — the tool falls through to auto-detection (also empty) → missing.
    scanner = EnvironmentScanner(tool_paths={"loot": str(tmp_path / "nope" / "loot.exe")})

    snap = await scanner.scan()

    assert not snap.has_tool("loot")
    assert any(m.technical_name.lower() == "loot" for m in snap.missing)


def test_read_pe_product_version_devuelve_none_con_pe_corrupto(tmp_path: Path) -> None:
    # Ancla del contrato del helper (limpieza post-#275): el stub "MZ" de 2 bytes
    # dispara pefile.PEFormatError, que hereda de Exception a secas (no de
    # OSError/ValueError). El helper debe absorberla y devolver None — señal de
    # "usar heurística de tamaño" — sin propagar jamás la excepción.
    exe = _touch_exe(tmp_path, "SkyrimSE.exe")

    assert _read_pe_product_version(exe) is None


async def test_bare_scanner_without_skyrim_stays_critical(tmp_path: Path, monkeypatch) -> None:
    # Regression guard: with no config and no Skyrim, behaviour is unchanged —
    # the scan still reports the game as not found.
    async def mock_find_skyrim(*args, **kwargs):
        return None

    monkeypatch.setattr(EnvironmentScanner, "_find_skyrim", mock_find_skyrim)
    scanner = EnvironmentScanner()

    snap = await scanner.scan()

    assert snap.skyrim is None
