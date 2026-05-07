"""Tests for sky_claw/antigravity/security/file_permissions.py.

Covers the Windows ICACLS hardening path and its os.chmod fallback:
  1. icacls succeeds → os.chmod NOT called.
  2. icacls fails (CalledProcessError) → os.chmod called with correct mode.
  3. icacls fails AND os.chmod fails → CRITICAL log emitted.
  4. POSIX path → os.chmod called, icacls NOT called.
  5. getpass.getuser() raises → falls through directly to os.chmod fallback.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sky_claw.antigravity.security.file_permissions as fp_mod
from sky_claw.antigravity.security.file_permissions import restrict_to_owner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(tmp_path: Path) -> Path:
    p = tmp_path / "secret.bin"
    p.write_bytes(b"data")
    return p


def _make_dir(tmp_path: Path) -> Path:
    d = tmp_path / "secret_dir"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Windows path
# ---------------------------------------------------------------------------


class TestRestrictWindows:
    """Tests for _restrict_windows() executed via restrict_to_owner()."""

    @pytest.fixture(autouse=True)
    def force_windows(self):
        """Patch _IS_WINDOWS so the Windows branch always runs."""
        with patch.object(fp_mod, "_IS_WINDOWS", True):
            yield

    def test_icacls_success_no_chmod(self, tmp_path):
        """icacls succeeds → os.chmod must NOT be called."""
        target = _make_file(tmp_path)
        with (
            patch("subprocess.run") as mock_run,
            patch("os.chmod") as mock_chmod,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            restrict_to_owner(target)

        mock_run.assert_called_once()
        mock_chmod.assert_not_called()

    def test_icacls_failure_triggers_chmod_file(self, tmp_path):
        """icacls CalledProcessError → os.chmod(path, 0o600) called."""
        target = _make_file(tmp_path)
        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1332, "icacls"),
            ),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_called_once_with(target, 0o600)

    def test_icacls_failure_triggers_chmod_dir(self, tmp_path):
        """icacls failure on a directory → os.chmod(path, 0o700) called."""
        target = _make_dir(tmp_path)
        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1332, "icacls"),
            ),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_called_once_with(target, 0o700)

    def test_icacls_not_found_triggers_chmod(self, tmp_path):
        """icacls binary missing (FileNotFoundError) → falls back to os.chmod."""
        target = _make_file(tmp_path)
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("icacls not found")),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_called_once_with(target, 0o600)

    def test_icacls_timeout_triggers_chmod(self, tmp_path):
        """icacls timeout → falls back to os.chmod."""
        target = _make_file(tmp_path)
        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("icacls", 10),
            ),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_called_once_with(target, 0o600)

    def test_both_fail_logs_critical(self, tmp_path, caplog):
        """icacls fails AND os.chmod fails → CRITICAL log emitted."""
        target = _make_file(tmp_path)
        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1332, "icacls"),
            ),
            patch("os.chmod", side_effect=OSError("permission denied")),
            caplog.at_level(logging.CRITICAL),
        ):
            restrict_to_owner(target)

        assert any("Both icacls and chmod failed" in r.message for r in caplog.records)

    def test_getuser_raises_falls_back_to_chmod(self, tmp_path):
        """getpass.getuser() raises → skips icacls, falls directly to os.chmod."""
        target = _make_file(tmp_path)
        with (
            patch("getpass.getuser", side_effect=Exception("no user")),
            patch("subprocess.run") as mock_run,
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_run.assert_not_called()
        mock_chmod.assert_called_once_with(target, 0o600)

    def test_nonexistent_path_skipped(self, tmp_path):
        """Non-existent path → function returns early without calling icacls."""
        ghost = tmp_path / "ghost.bin"
        with (
            patch("subprocess.run") as mock_run,
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(ghost)

        mock_run.assert_not_called()
        mock_chmod.assert_not_called()


# ---------------------------------------------------------------------------
# POSIX path
# ---------------------------------------------------------------------------


class TestRestrictPosix:
    """Tests for the POSIX (non-Windows) branch."""

    @pytest.fixture(autouse=True)
    def force_posix(self):
        with patch.object(fp_mod, "_IS_WINDOWS", False):
            yield

    def test_posix_file_chmod_600(self, tmp_path):
        target = _make_file(tmp_path)
        with (
            patch("os.chmod") as mock_chmod,
            patch("subprocess.run") as mock_run,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_called_once_with(target, 0o600)
        mock_run.assert_not_called()

    def test_posix_dir_chmod_700(self, tmp_path):
        target = _make_dir(tmp_path)
        with (
            patch("os.chmod") as mock_chmod,
            patch("subprocess.run") as mock_run,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_called_once_with(target, 0o700)
        mock_run.assert_not_called()

    def test_posix_chmod_failure_logged(self, tmp_path, caplog):
        """os.chmod failure on POSIX → warning logged, no exception raised."""
        target = _make_file(tmp_path)
        with (
            patch("os.chmod", side_effect=OSError("read-only fs")),
            caplog.at_level(logging.WARNING),
        ):
            restrict_to_owner(target)  # must not raise

        assert any("chmod" in r.message for r in caplog.records)
