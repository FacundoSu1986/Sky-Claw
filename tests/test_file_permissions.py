"""Tests for sky_claw/antigravity/security/file_permissions.py.

Covers the Windows ICACLS hardening path and its SID-based retry:
  1. Username-based icacls succeeds → done.
  2. Username-based icacls fails → SID resolved → SID-based icacls succeeds.
  3. Username-based icacls fails → SID resolved → SID-based icacls fails → CRITICAL.
  4. Username-based icacls fails → SID resolution fails → CRITICAL (no os.chmod).
  5. getpass.getuser() raises → skip to SID-based path.
  6. Non-existent path → returns early without calling icacls.
  7. POSIX path → os.chmod called, icacls NOT called.
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

    def test_username_icacls_success(self, tmp_path):
        """Username-based icacls succeeds → no SID lookup, no os.chmod."""
        target = _make_file(tmp_path)
        with (
            patch("subprocess.run") as mock_run,
            patch("os.chmod") as mock_chmod,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            restrict_to_owner(target)

        mock_run.assert_called_once()
        mock_chmod.assert_not_called()

    def test_username_fails_sid_icacls_succeeds(self, tmp_path):
        """Username icacls fails (1332) → SID lookup → SID-based icacls succeeds."""
        target = _make_file(tmp_path)
        sid = "S-1-5-21-123-456-789-1001"

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            # First call is username-based icacls → fail
            if "icacls" in cmd and not any(arg.startswith("*S-") for arg in cmd):
                raise subprocess.CalledProcessError(1332, "icacls")
            # Second call is powershell SID resolution
            if "powershell" in cmd[0].lower():
                m = MagicMock()
                m.stdout = sid + "\n"
                return m
            # Third call is SID-based icacls → succeed
            return MagicMock(returncode=0)

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        # Verify SID was used in the final icacls call
        sid_calls = [c for c in run_calls if any(f"*{sid}" in str(a) for a in c)]
        assert sid_calls, "Expected SID-based icacls call not found"

    def test_username_fails_sid_icacls_also_fails_logs_critical(self, tmp_path, caplog):
        """Both icacls attempts fail → CRITICAL logged, no os.chmod."""
        target = _make_file(tmp_path)
        sid = "S-1-5-21-123-456-789-1001"

        def fake_run(cmd, **kwargs):
            if "powershell" in cmd[0].lower():
                m = MagicMock()
                m.stdout = sid + "\n"
                return m
            # All icacls calls fail
            raise subprocess.CalledProcessError(1332, "icacls")

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
            caplog.at_level(logging.CRITICAL),
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        assert any("SECURITY" in r.message for r in caplog.records)

    def test_sid_resolution_fails_logs_critical(self, tmp_path, caplog):
        """SID resolution fails (PowerShell error) → CRITICAL, no icacls, no os.chmod."""
        target = _make_file(tmp_path)

        def fake_run(cmd, **kwargs):
            if "powershell" in cmd[0].lower():
                raise subprocess.CalledProcessError(1, "powershell")
            # First icacls (username-based) fails
            raise subprocess.CalledProcessError(1332, "icacls")

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
            caplog.at_level(logging.CRITICAL),
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        assert any("SECURITY" in r.message for r in caplog.records)

    def test_getuser_raises_falls_back_to_sid(self, tmp_path):
        """getpass.getuser() raises → skips username grant, attempts SID-based grant."""
        target = _make_file(tmp_path)
        sid = "S-1-5-21-123-456-789-1001"

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            if "powershell" in cmd[0].lower():
                m = MagicMock()
                m.stdout = sid + "\n"
                return m
            return MagicMock(returncode=0)

        with (
            patch("getpass.getuser", side_effect=Exception("no user")),
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        # No username-based icacls should have been attempted
        username_calls = [c for c in run_calls if "icacls" in c and not any(f"*{sid}" in str(a) for a in c)]
        assert not username_calls, "Should not have attempted username-based icacls"

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

    def test_icacls_not_found_escalates_to_sid(self, tmp_path, caplog):
        """icacls binary missing (FileNotFoundError) → escalates to SID path."""
        target = _make_file(tmp_path)

        def fake_run(cmd, **kwargs):
            if "powershell" in cmd[0].lower():
                raise subprocess.CalledProcessError(1, "powershell")
            raise FileNotFoundError("icacls not found")

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
            caplog.at_level(logging.CRITICAL),
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        assert any("SECURITY" in r.message for r in caplog.records)


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
