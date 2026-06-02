"""Audit #155 (L-1) — first_run wizard must validate MO2 / Skyrim paths.

Without this validation, a typo in the MO2 root path is only discovered
during the first real tool run, with a confusing low-level error. The
fix extracts a pure ``_validate_path`` helper that the wizard calls
before saving — present paths are accepted, missing ones return a
human-readable reason that the wizard surfaces and re-prompts on.

Contracts verified here (helper-level so we do not need to drive the
interactive input loop):

- Existing directory: ``(True, "")``.
- Missing path: ``(False, ...)`` with a reason mentioning the path.
- ``require_file`` with the file present: ``(True, "")``.
- ``require_file`` with the file missing: ``(False, ...)`` with a reason
  mentioning the expected filename (e.g. ``ModOrganizer.exe``).
- Empty / None input is rejected with a "ruta vacía" reason — typing
  nothing is a user mistake, not a valid skip.
"""

from __future__ import annotations

import pathlib


def _import_helper():
    """Helper kept lazy so the RED state of this test is missing-attribute,
    not import-time failure on the wizard module's ``sys.path`` mutation.
    """
    from local_scripts.scripts.first_run import _validate_path

    return _validate_path


class TestValidatePath:
    def test_existing_directory_is_accepted(self, tmp_path: pathlib.Path) -> None:
        _validate_path = _import_helper()
        ok, reason = _validate_path(str(tmp_path), label="MO2 Root")
        assert ok is True, f"Existing directory must validate; got reason={reason!r}"
        assert reason == ""

    def test_missing_path_is_rejected_with_reason(self, tmp_path: pathlib.Path) -> None:
        _validate_path = _import_helper()
        bogus = tmp_path / "does-not-exist"
        ok, reason = _validate_path(str(bogus), label="MO2 Root")
        assert ok is False
        assert "MO2 Root" in reason or "no existe" in reason.lower(), (
            f"Reason must surface the label or 'no existe' for the user: got {reason!r}"
        )

    def test_empty_string_is_rejected(self) -> None:
        _validate_path = _import_helper()
        ok, reason = _validate_path("", label="Skyrim")
        assert ok is False
        assert reason, "An empty input must produce a non-empty reason for the user"

    def test_require_file_present_is_accepted(self, tmp_path: pathlib.Path) -> None:
        _validate_path = _import_helper()
        (tmp_path / "ModOrganizer.exe").write_bytes(b"")
        ok, reason = _validate_path(str(tmp_path), label="MO2 Root", require_file="ModOrganizer.exe")
        assert ok is True, f"Path with ModOrganizer.exe must validate; got {reason!r}"

    def test_require_file_missing_is_rejected_with_filename_in_reason(self, tmp_path: pathlib.Path) -> None:
        _validate_path = _import_helper()
        ok, reason = _validate_path(str(tmp_path), label="MO2 Root", require_file="ModOrganizer.exe")
        assert ok is False
        assert "ModOrganizer.exe" in reason, "Reason must name the expected file so the user knows what to look for"

    def test_file_instead_of_directory_is_rejected(self, tmp_path: pathlib.Path) -> None:
        """Pointing at a regular file when a directory is expected fails."""
        _validate_path = _import_helper()
        target = tmp_path / "not-a-dir.txt"
        target.write_text("hi", encoding="utf-8")
        ok, reason = _validate_path(str(target), label="MO2 Root")
        assert ok is False
        assert reason, "Reason must explain why a regular file is not a valid root"


def test_validate_path_exists_as_module_attribute() -> None:
    """Sanity guard: the wizard module exposes the helper symbol so the
    interactive flow imports it cleanly. Catches accidental rename / move.
    """
    import local_scripts.scripts.first_run as first_run_module

    assert hasattr(first_run_module, "_validate_path"), (
        "first_run.py must expose ``_validate_path`` at module level so the "
        "validation contract is unit-testable without driving the input loop"
    )
