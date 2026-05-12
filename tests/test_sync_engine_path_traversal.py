"""P0.7 R-04 — path traversal guard in _extract_nexus_id.

_extract_nexus_id builds a filesystem path from the caller-supplied
``mod_name`` string.  Without validation, a crafted name like
``../../etc/passwd`` silently escapes the mods root and reads arbitrary
files.

``assert_safe_component`` from path_validator.py already exists and
covers exactly this; it was simply never imported in sync_engine.py.

Contracts:
- Traversal-containing names return None before any FS access.
- Normal mod names with an embedded numeric ID are unaffected.
- The FS branch (meta.ini) is blocked for traversal inputs even when
  the crafted target file exists on disk.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.antigravity.orchestrator.sync_engine import _extract_nexus_id
from sky_claw.config import SystemPaths


class TestExtractNexusIdPathTraversal:
    """Verifies that assert_safe_component blocks out-of-bounds FS access in _extract_nexus_id."""

    def test_traversal_blocked_even_when_crafted_file_exists(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        """Guard must return None before touching the FS, even when the target file exists.

        Without assert_safe_component, _extract_nexus_id("../../evil")
        constructs <modding_root>/MO2/mods/../../evil/meta.ini, which
        resolves to <modding_root>/evil/meta.ini — an arbitrary read.

        This test confirms the guard blocks the traversal; without it the
        function would return 9999 (read from the crafted meta.ini).
        """
        # Set up a fake modding root so the traversal lands in tmp_path.
        monkeypatch.setattr(SystemPaths, "modding_root", staticmethod(lambda: tmp_path))
        (tmp_path / "MO2" / "mods").mkdir(parents=True)

        # Create the "evil" meta.ini that the traversal would reach:
        # tmp_path / "MO2" / "mods" / "../../evil" / "meta.ini"
        # resolves to tmp_path / "evil" / "meta.ini"
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        (evil_dir / "meta.ini").write_text("[General]\nmodid=9999\n", encoding="utf-8")

        # The guard must reject this before any FS access.
        result = _extract_nexus_id("../../evil")
        assert result is None, (
            f"Path traversal not blocked — _extract_nexus_id('../../evil') "
            f"read an out-of-bounds meta.ini and returned {result!r} instead of None"
        )

    @pytest.mark.parametrize(
        "malicious",
        [
            "../../etc/passwd",
            "..\\Windows\\System32",
            "../secret",
            "mod\x00name",
            "mod\x01name",
        ],
    )
    def test_traversal_inputs_return_none(self, malicious: str) -> None:
        """assert_safe_component must reject these before any FS access."""
        result = _extract_nexus_id(malicious)
        assert result is None, f"_extract_nexus_id({malicious!r}) should return None, got {result!r}"

    def test_valid_name_with_numeric_id_still_works(self) -> None:
        """Normal mod names with an embedded numeric ID are unaffected."""
        result = _extract_nexus_id("SomePlugin-12345-1-0")
        assert result == 12345
