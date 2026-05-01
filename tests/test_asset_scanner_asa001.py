"""Tests for ASA-001: Asset scanner must detect .psc (Papyrus source) scripts.

Verifies that the SCRIPT AssetType includes both .pex (compiled) and .psc
(source) extensions, aligning with Skyrim SE/AE modding semantics.
"""

from __future__ import annotations

import pathlib

from sky_claw.local.assets.asset_scanner import AssetConflictDetector, AssetType


class TestAssetScannerPscMapping:
    """ASA-001: .psc must be mapped to AssetType.SCRIPT."""

    def test_psc_is_script_type(self, tmp_path: pathlib.Path):
        """A .psc file must be classified as SCRIPT."""
        detector = AssetConflictDetector(mo2_mods_path=tmp_path)
        psc_file = tmp_path / "scripts" / "MyQuestScript.psc"
        psc_file.parent.mkdir(parents=True, exist_ok=True)
        psc_file.write_text("; Papyrus source", encoding="utf-8")

        asset_type = detector.get_asset_type(psc_file)
        assert asset_type is AssetType.SCRIPT, f"Expected SCRIPT for .psc, got {asset_type}"

    def test_pex_is_script_type(self, tmp_path: pathlib.Path):
        """A .pex file must continue to be classified as SCRIPT."""
        detector = AssetConflictDetector(mo2_mods_path=tmp_path)
        pex_file = tmp_path / "scripts" / "MyQuestScript.pex"
        pex_file.parent.mkdir(parents=True, exist_ok=True)
        pex_file.write_bytes(b"\x00\x01\x02")

        asset_type = detector.get_asset_type(pex_file)
        assert asset_type is AssetType.SCRIPT, f"Expected SCRIPT for .pex, got {asset_type}"

    def test_asset_extensions_include_psc(self):
        """The canonical extension map must list .psc under SCRIPT."""
        assert ".psc" in AssetConflictDetector.ASSET_EXTENSIONS[AssetType.SCRIPT]
        assert ".pex" in AssetConflictDetector.ASSET_EXTENSIONS[AssetType.SCRIPT]
