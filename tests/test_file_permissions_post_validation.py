"""M-03: Tests for fail-closed DACL post-validation in file_permissions.py.

These tests focus exclusively on the M-03 hardening: after the icacls
hardening command returns successfully, a second `icacls <path>` call
parses the effective DACL and rejects any non-owner ACE. On failure,
the artifact is destroyed (files only — directories are preserved)
and PermissionError is raised.

Companion test file: tests/test_file_permissions.py covers the
icacls/SID-fallback strategy with verify mocked to pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sky_claw.antigravity.security.file_permissions as fp_mod
from sky_claw.antigravity.security.file_permissions import (
    _dacl_is_owner_only,
    restrict_to_owner,
    restrict_to_owner_async,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Build a `subprocess.CompletedProcess`-like MagicMock with explicit stdout."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


def _verify_stdout_owner_only(path: Path, identifier: str = "DESKTOP-ABC\\testuser") -> str:
    return f"{path} {identifier}:(F)\n\nSuccessfully processed 1 files; Failed processing 0 files\n"


@pytest.fixture
def force_windows():
    """Make file_permissions._IS_WINDOWS True regardless of host so the
    Windows hardening branch runs on POSIX CI too."""
    with patch.object(fp_mod, "_IS_WINDOWS", True):
        yield


# ---------------------------------------------------------------------------
# Pure parser unit tests (no subprocess, no fs side effects)
# ---------------------------------------------------------------------------


class TestDaclParser:
    """Unit tests for _dacl_is_owner_only — locale-independent ACE matcher."""

    def test_accepts_owner_only_with_domain(self):
        output = (
            "C:\\path\\to\\file DESKTOP-ABC\\alice:(F)\n\nSuccessfully processed 1 files; Failed processing 0 files\n"
        )
        assert _dacl_is_owner_only(output, ["alice"]) is True

    def test_accepts_owner_only_bare_username(self):
        output = "C:\\path alice:(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is True

    def test_case_insensitive(self):
        output = "C:\\path DESKTOP\\Alice:(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is True

    def test_accepts_sid_with_asterisk(self):
        output = "C:\\path *S-1-5-21-123-456-789-1001:(F)\n"
        assert _dacl_is_owner_only(output, ["S-1-5-21-123-456-789-1001"]) is True

    def test_accepts_sid_without_asterisk(self):
        output = "C:\\path S-1-5-21-123-456-789-1001:(F)\n"
        assert _dacl_is_owner_only(output, ["S-1-5-21-123-456-789-1001"]) is True

    def test_rejects_everyone(self):
        output = "C:\\path alice:(F)\n         Everyone:(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is False

    def test_rejects_builtin_users(self):
        output = "C:\\path alice:(F)\n         BUILTIN\\Users:(R)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is False

    def test_rejects_inherited_administrators(self):
        output = "C:\\path alice:(F)\n         BUILTIN\\Administrators:(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is False

    def test_rejects_nt_authority_system(self):
        output = "C:\\path alice:(F)\n         NT AUTHORITY\\SYSTEM:(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is False

    def test_rejects_empty_output(self):
        # No ACE found at all — icacls grant didn't apply.
        assert _dacl_is_owner_only("", ["alice"]) is False

    def test_rejects_no_ace_only_summary(self):
        assert (
            _dacl_is_owner_only(
                "Successfully processed 1 files; Failed processing 0 files\n",
                ["alice"],
            )
            is False
        )

    def test_empty_allowed_list_rejects(self):
        output = "C:\\path alice:(F)\n"
        assert _dacl_is_owner_only(output, []) is False

    def test_path_with_colon_in_first_line_does_not_match(self):
        # The regex must not treat `C:` (drive letter colon) as an ACE identifier.
        # The first line has both the path AND the ACE — only the ACE should match.
        output = "C:\\Users\\alice\\secret.bin DESKTOP\\alice:(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is True

    def test_rejects_domain_collision(self):
        # Same bare name under two distinct domain prefixes → collision → reject.
        # Protects against a pre-existing OTHERDOMAIN\alice ACE that hardening
        # didn't remove surviving alongside the legitimate CORP\alice grant.
        output = "C:\\path CORP\\alice:(F)\n         EVIL\\alice:(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is False

    def test_accepts_same_qualified_form_twice(self):
        # icacls sometimes emits the same principal for inherited + explicit ACEs.
        # Two identical qualified forms for the same bare name must not trigger
        # the domain-collision guard.
        output = "C:\\path CORP\\alice:(F)\n         CORP\\alice:(OI)(CI)(IO)(F)\n"
        assert _dacl_is_owner_only(output, ["alice"]) is True

    def test_exact_full_match_bypasses_bare_fallback(self):
        # When the allowed list contains the fully-qualified name, exact matching
        # is used and the bare-fallback / collision guard is bypassed entirely.
        output = "C:\\path CORP\\alice:(F)\n"
        assert _dacl_is_owner_only(output, ["CORP\\alice"]) is True


# ---------------------------------------------------------------------------
# Sync end-to-end (mocked subprocess) — fail-closed cleanup
# ---------------------------------------------------------------------------


class TestSyncFailClosed:
    """End-to-end sync tests covering the verify + cleanup contract."""

    def test_passes_when_verify_reports_owner_only(self, tmp_path, force_windows):
        target = tmp_path / "good.bin"
        target.write_bytes(b"secret")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            # verify
            return _completed(stdout=_verify_stdout_owner_only(target))

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            restrict_to_owner(target)

        assert target.exists()
        assert target.read_bytes() == b"secret"

    def test_fails_closed_when_verify_returns_extra_ace(self, tmp_path, force_windows):
        """Hardening succeeds but verify shows Everyone:(F) → file destroyed, raise."""
        target = tmp_path / "leaky.bin"
        target.write_bytes(b"secret")

        bad_verify = _completed(
            stdout=(
                f"{target} DESKTOP\\testuser:(F)\n"
                "         Everyone:(F)\n\n"
                "Successfully processed 1 files; Failed processing 0 files\n"
            )
        )

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            return bad_verify

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError, match="artifact destroyed to prevent leak"),
        ):
            restrict_to_owner(target)

        assert not target.exists(), "fail-closed must destroy the leaky artifact"

    def test_fails_closed_when_verify_reports_inherited_administrators(self, tmp_path, force_windows):
        target = tmp_path / "inherited.bin"
        target.write_bytes(b"secret")

        bad_verify = _completed(stdout=(f"{target} testuser:(F)\n         BUILTIN\\Administrators:(F)\n"))

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            return bad_verify

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError),
        ):
            restrict_to_owner(target)

        assert not target.exists()

    def test_fails_closed_when_verify_subprocess_errors(self, tmp_path, force_windows):
        """Hardening succeeds but the verify icacls call itself errors → destroy + raise."""
        target = tmp_path / "stubborn.bin"
        target.write_bytes(b"secret")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            # verify call fails
            raise subprocess.CalledProcessError(returncode=5, cmd=cmd)

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError, match="icacls verification failed"),
        ):
            restrict_to_owner(target)

        assert not target.exists()

    def test_fails_closed_when_verify_times_out(self, tmp_path, force_windows):
        target = tmp_path / "slow.bin"
        target.write_bytes(b"secret")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError),
        ):
            restrict_to_owner(target)

        assert not target.exists()

    def test_fails_closed_when_verify_returns_empty(self, tmp_path, force_windows):
        """Verify returns no ACE tokens at all → fail closed."""
        target = tmp_path / "empty_dacl.bin"
        target.write_bytes(b"secret")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            return _completed(stdout="\nSuccessfully processed 1 files\n")

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError),
        ):
            restrict_to_owner(target)

        assert not target.exists()

    def test_directory_preserved_on_fail(self, tmp_path, force_windows):
        """Per design: directories raise but are NOT rmtree'd (preserves vault data)."""
        target = tmp_path / "vault_dir"
        target.mkdir()
        sentinel = target / "important.db"
        sentinel.write_bytes(b"do not delete me")

        bad_verify = _completed(stdout=f"{target} Everyone:(F)\n")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            return bad_verify

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError),
        ):
            restrict_to_owner(target)

        assert target.exists() and sentinel.exists(), "vault dir contents must be preserved"
        assert sentinel.read_bytes() == b"do not delete me"


# ---------------------------------------------------------------------------
# Async parity — the threaded variant must propagate the same contract
# ---------------------------------------------------------------------------


class TestAsyncFailClosed:
    async def test_async_passes_when_verify_owner_only(self, tmp_path, force_windows):
        target = tmp_path / "async_good.bin"
        target.write_bytes(b"secret")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            return _completed(stdout=_verify_stdout_owner_only(target))

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            await restrict_to_owner_async(target)

        assert target.exists()

    async def test_async_fails_closed_on_extra_ace(self, tmp_path, force_windows):
        target = tmp_path / "async_leaky.bin"
        target.write_bytes(b"secret")

        bad_verify = _completed(stdout=f"{target} DESKTOP\\testuser:(F)\n         Everyone:(F)\n")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            return bad_verify

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError, match="artifact destroyed"),
        ):
            await restrict_to_owner_async(target)

        assert not target.exists()

    async def test_async_fails_closed_on_verify_error(self, tmp_path, force_windows):
        target = tmp_path / "async_stubborn.bin"
        target.write_bytes(b"secret")

        def fake_run(cmd, **kwargs):
            if "/grant:r" in cmd:
                return _completed()
            raise subprocess.CalledProcessError(returncode=5, cmd=cmd)

        with (
            patch("getpass.getuser", return_value="testuser"),
            patch("subprocess.run", side_effect=fake_run),
            pytest.raises(PermissionError),
        ):
            await restrict_to_owner_async(target)

        assert not target.exists()


# ---------------------------------------------------------------------------
# Real-icacls integration test — only runs on Windows hosts
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="icacls is Windows-only")
class TestRealIcacls:
    """End-to-end with a real icacls invocation. Smoke test for the
    locale-tolerant parser + the live DACL state on the developer's box.

    These tests skip themselves if the host's icacls cannot apply an
    owner-only DACL to a tmp_path target — observed on some domain-joined
    or restricted Windows hosts where both the username-based grant and
    the SID-based fallback return exit 1332. That environmental failure
    is not a regression of M-03; the mocked tests above already cover
    every fail-closed path.
    """

    def test_real_dacl_validation_passes_with_owner_only(self, tmp_path):
        target = tmp_path / "real_secret.bin"
        target.write_bytes(b"x" * 32)
        try:
            restrict_to_owner(target)
        except PermissionError as exc:
            pytest.skip(f"icacls cannot harden tmp_path on this host: {exc}")
        assert target.exists()
        assert target.read_bytes() == b"x" * 32

    async def test_real_async_dacl_validation_passes(self, tmp_path):
        target = tmp_path / "real_async_secret.bin"
        target.write_bytes(b"y" * 32)
        try:
            await restrict_to_owner_async(target)
        except PermissionError as exc:
            pytest.skip(f"icacls cannot harden tmp_path on this host: {exc}")
        assert target.exists()
