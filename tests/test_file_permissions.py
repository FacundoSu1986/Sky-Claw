"""Tests for sky_claw/antigravity/security/file_permissions.py.

Covers the Windows ICACLS hardening path and its SID-based retry:
  1. Username-based icacls succeeds + post-validation passes → done.
  2. Username-based icacls fails → SID resolved → SID-based icacls succeeds + verifies.
  3. Username-based icacls fails → SID resolved → SID-based icacls fails → fail closed.
  4. Username-based icacls fails → SID resolution fails → fail closed (no os.chmod).
  5. getpass.getuser() raises → skip to SID-based path.
  6. Non-existent path → returns early without calling icacls.
  7. POSIX path → os.chmod called, icacls NOT called.

The DACL post-validation behavior (parser, verify call, fail-closed cleanup)
is exercised in tests/test_file_permissions_post_validation.py — these tests
remain focused on the icacls strategy itself, with verify mocked to pass.
"""

from __future__ import annotations

import logging
import os
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


def _verify_stdout(path: Path, identifier: str) -> str:
    """Synthesize the kind of `icacls <path>` stdout we get after a clean
    owner-only hardening (single ACE for the current user)."""
    return f"{path} {identifier}:(F)\n\nSuccessfully processed 1 files; Failed processing 0 files\n"


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
        """Username-based icacls succeeds + DACL verify passes → no SID lookup, no os.chmod."""
        target = _make_file(tmp_path)
        verify_ok = MagicMock(returncode=0)
        verify_ok.stdout = _verify_stdout(target, "DESKTOP-ABC\\testuser")

        def fake_run(cmd, **kwargs):
            # First call: hardening icacls (with /grant:r flag)
            if "/grant:r" in cmd:
                return MagicMock(returncode=0)
            # Second call: verify icacls (just `icacls <path>`)
            if cmd[0] == "icacls":
                return verify_ok
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run) as mock_run,
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        # Two icacls calls: one for /grant:r hardening, one for verification.
        assert mock_run.call_count == 2
        mock_chmod.assert_not_called()

    def test_username_fails_sid_icacls_succeeds(self, tmp_path):
        """Username icacls fails (1332) → SID lookup → SID-based icacls succeeds + verifies."""
        target = _make_file(tmp_path)
        sid = "S-1-5-21-123-456-789-1001"

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            # First call is username-based icacls hardening → fail
            if "icacls" in cmd[0] and "/grant:r" in cmd and not any(arg.startswith("*S-") for arg in cmd):
                raise subprocess.CalledProcessError(1332, "icacls")
            # PowerShell SID resolution
            if "powershell" in cmd[0].lower():
                m = MagicMock()
                m.stdout = sid + "\n"
                return m
            # SID-based icacls hardening → succeed
            if "icacls" in cmd[0] and "/grant:r" in cmd and any(f"*{sid}" in str(a) for a in cmd):
                return MagicMock(returncode=0)
            # Verify icacls → return a DACL with the SID resolved to bare username
            if cmd[0] == "icacls":
                m = MagicMock(returncode=0)
                m.stdout = _verify_stdout(target, "testuser")
                return m
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        # Verify SID was used in the hardening call
        sid_calls = [c for c in run_calls if any(f"*{sid}" in str(a) for a in c)]
        assert sid_calls, "Expected SID-based icacls call not found"

    def test_username_fails_sid_icacls_also_fails_closed(self, tmp_path, caplog):
        """Both icacls attempts fail → CRITICAL logged, no os.chmod, PermissionError, file destroyed."""
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
            pytest.raises(PermissionError, match="Owner-only ACL enforcement failed"),
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        assert any("SECURITY" in r.message for r in caplog.records)
        # M-03: artifact must be destroyed when ACL enforcement fails.
        assert not target.exists()

    def test_sid_resolution_fails_closed(self, tmp_path, caplog):
        """SID resolution fails → CRITICAL logged, no os.chmod, PermissionError, file destroyed."""
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
            pytest.raises(PermissionError, match="SID resolution failed"),
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        assert any("SECURITY" in r.message for r in caplog.records)
        assert not target.exists()

    def test_getuser_raises_falls_back_to_sid(self, tmp_path):
        """getpass.getuser() raises → skips username grant, attempts SID-based grant + verify."""
        target = _make_file(tmp_path)
        sid = "S-1-5-21-123-456-789-1001"

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            if "powershell" in cmd[0].lower():
                m = MagicMock()
                m.stdout = sid + "\n"
                return m
            # SID-based hardening
            if "icacls" in cmd[0] and "/grant:r" in cmd:
                return MagicMock(returncode=0)
            # Verify call: icacls reports the SID literally because resolution
            # is expected to be unavailable in this scenario.
            if cmd[0] == "icacls":
                m = MagicMock(returncode=0)
                m.stdout = _verify_stdout(target, sid)
                return m
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        with (
            patch("getpass.getuser", side_effect=Exception("no user")),
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        # No username-based icacls should have been attempted
        username_calls = [
            c for c in run_calls if "icacls" in c[0] and "/grant:r" in c and not any(f"*{sid}" in str(a) for a in c)
        ]
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

    def test_icacls_not_found_escalates_to_sid_then_fails_closed(self, tmp_path, caplog):
        """icacls missing → escalates to SID path, then fails closed if SID path fails."""
        target = _make_file(tmp_path)

        def fake_run(cmd, **kwargs):
            if "powershell" in cmd[0].lower():
                raise subprocess.CalledProcessError(1, "powershell")
            raise FileNotFoundError("icacls not found")

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
            caplog.at_level(logging.CRITICAL),
            pytest.raises(PermissionError, match="SID resolution failed"),
        ):
            restrict_to_owner(target)

        mock_chmod.assert_not_called()
        assert any("SECURITY" in r.message for r in caplog.records)
        assert not target.exists()


# ---------------------------------------------------------------------------
# POSIX path
# ---------------------------------------------------------------------------


class TestRestrictPosix:
    """Tests for the POSIX (non-Windows) branch.

    PR #141 review fix: el path POSIX ahora usa ``os.open(O_NOFOLLOW)`` +
    ``os.fchmod(fd)`` en lugar de ``os.chmod(path, mode)``. Cierra el TOCTOU
    residual entre is_symlink() y chmod. Estos tests reflejan el nuevo
    contracto.
    """

    @pytest.fixture(autouse=True)
    def force_posix(self):
        with patch.object(fp_mod, "_IS_WINDOWS", False):
            yield

    def test_posix_file_uses_atomic_fchmod_600(self, tmp_path):
        """File POSIX path: open(O_NOFOLLOW) + fchmod(0o600), NO subprocess."""
        target = _make_file(tmp_path)
        # En Windows local os.O_NOFOLLOW no existe → el código cae al fallback
        # de os.chmod. En Linux CI sí existe → usa open+fchmod. Patcheamos
        # AMBOS paths para que el test funcione cross-platform.
        with (
            patch("os.open", return_value=42) as mock_open,
            patch("os.fchmod") as mock_fchmod,
            patch("os.close") as mock_close,
            patch("os.chmod") as mock_chmod_fallback,
            patch("subprocess.run") as mock_run,
        ):
            restrict_to_owner(target)

        # Path principal (Linux O_NOFOLLOW): open + fchmod.
        if hasattr(os, "O_NOFOLLOW"):
            mock_open.assert_called_once()
            mock_fchmod.assert_called_once_with(42, 0o600)
            mock_close.assert_called_once_with(42)
            mock_chmod_fallback.assert_not_called()
        else:
            # Fallback (Windows runtime sin O_NOFOLLOW): chmod directo.
            mock_chmod_fallback.assert_called_once_with(target, 0o600)
        mock_run.assert_not_called()

    def test_posix_dir_uses_atomic_fchmod_700(self, tmp_path):
        """Dir POSIX path: open(O_NOFOLLOW|O_DIRECTORY) + fchmod(0o700)."""
        target = _make_dir(tmp_path)
        with (
            patch("os.open", return_value=43) as mock_open,
            patch("os.fchmod") as mock_fchmod,
            patch("os.close") as mock_close,
            patch("os.chmod") as mock_chmod_fallback,
            patch("subprocess.run") as mock_run,
        ):
            restrict_to_owner(target)

        if hasattr(os, "O_NOFOLLOW"):
            mock_open.assert_called_once()
            mock_fchmod.assert_called_once_with(43, 0o700)
            mock_close.assert_called_once_with(43)
            mock_chmod_fallback.assert_not_called()
        else:
            mock_chmod_fallback.assert_called_once_with(target, 0o700)
        mock_run.assert_not_called()

    def test_posix_fchmod_failure_fails_closed(self, tmp_path, caplog):
        """fchmod (o chmod fallback) failure → ERROR log + PermissionError."""
        target = _make_file(tmp_path)
        if hasattr(os, "O_NOFOLLOW"):
            mock_setup = (
                patch("os.open", return_value=42),
                patch("os.fchmod", side_effect=OSError("read-only fs")),
                patch("os.close"),
            )
            match_msg = "Owner-only fchmod failed"
            log_kw = "fchmod"
        else:
            mock_setup = (patch("os.chmod", side_effect=OSError("read-only fs")),)
            match_msg = "Owner-only chmod failed"
            log_kw = "chmod"

        from contextlib import ExitStack

        with ExitStack() as stack:
            for cm in mock_setup:
                stack.enter_context(cm)
            stack.enter_context(caplog.at_level(logging.ERROR))
            with pytest.raises(PermissionError, match=match_msg):
                restrict_to_owner(target)

        assert any(log_kw in r.message for r in caplog.records)

    def test_posix_open_failure_on_symlink_race_fails_closed(self, tmp_path, caplog):
        """En Linux, si el path cambia a symlink entre is_symlink() y open(),
        os.open con O_NOFOLLOW falla con ELOOP → PermissionError."""
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW not available on this platform")
        target = _make_file(tmp_path)
        with (
            patch("os.open", side_effect=OSError(40, "Too many levels of symbolic links")),
            caplog.at_level(logging.ERROR),
            pytest.raises(PermissionError, match="symlink race"),
        ):
            restrict_to_owner(target)
        assert any("O_NOFOLLOW" in r.message or "open" in r.message for r in caplog.records)
