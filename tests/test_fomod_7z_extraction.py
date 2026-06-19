"""Real (un-mocked) 7z extraction regression.

py7zr's ``SevenZipFile`` is a sequential reader: extracting one target at a time
in a loop re-reads the compressed stream and raises ``CrcError`` on
py7zr >= 1.0.  The CVE-2026-23879 fix only ships in py7zr 1.1.3, forcing that
major bump, so this pins a real multi-file extraction through both extractors —
the existing tests mock py7zr and never caught the per-target loop.
"""

from __future__ import annotations

import pathlib

import pytest

py7zr = pytest.importorskip("py7zr")

from sky_claw.local.fomod.installer import _extract_7z  # noqa: E402
from sky_claw.local.tools_installer import _extract_7z_safe  # noqa: E402


def _make_multifile_7z(tmp_path: pathlib.Path) -> pathlib.Path:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "sub" / "b.esp").write_text("bravo-plugin")
    arc = tmp_path / "mod.7z"
    with py7zr.SevenZipFile(arc, "w") as z:
        z.writeall(src, "mod")
    return arc


def test_fomod_extract_7z_multifile(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _extract_7z(_make_multifile_7z(tmp_path), out)
    assert (out / "mod" / "a.txt").read_text() == "alpha"
    assert (out / "mod" / "sub" / "b.esp").read_text() == "bravo-plugin"


def test_tools_extract_7z_safe_multifile(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _extract_7z_safe(_make_multifile_7z(tmp_path), out)
    assert (out / "mod" / "a.txt").read_text() == "alpha"
    assert (out / "mod" / "sub" / "b.esp").read_text() == "bravo-plugin"
