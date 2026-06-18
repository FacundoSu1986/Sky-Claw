"""Regression tests for icacls error 1332 on localized (non-English) Windows.

Root cause (proven on a Spanish Windows dev box, account ``DESKTOP-*\\User``):

  1. ``_restrict_windows`` passed English well-known NAMES to ``icacls /remove``
     (``Users``, ``BUILTIN\\Administrators`` …). On a localized install those
     names are ``Usuarios`` / ``Administradores`` and do NOT resolve to a SID,
     so icacls exits **1332** ("No mapping between account names and SIDs").
     The code treated that non-zero exit as a total failure and fail-closed —
     for FILES that means ``unlink()`` (the secret is destroyed) — even though
     the ``/grant`` (owner-only) had actually succeeded.

  2. The owner grant ``{owner}:(F)`` carried no ``(OI)(CI)`` inheritance flags,
     so on a DIRECTORY it applied "this object only". Applied to the shared
     ``~/.sky_claw`` salt dir, children created afterwards (e.g.
     ``~/.sky_claw/dlq``) did not inherit owner write → ``aiosqlite.connect``
     raised ``OperationalError: unable to open database file`` (the DLQ /
     ``SupervisorAgent.start()`` failure).

The fix keeps ``_verify_dacl`` as the source of truth for the owner-only
guarantee; these tests pin the three behaviours that make it resilient:
locale-independent SID removes, inheritable directory grants, and a
degrade-safe (verify-based, not exit-code-based) fail-closed decision.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import sky_claw.antigravity.security.file_permissions as fp_mod
from sky_claw.antigravity.security.file_permissions import restrict_to_owner

# Well-known SIDs that always resolve regardless of OS display language.
_EVERYONE = "*S-1-1-0"
_USERS = "*S-1-5-32-545"
_SYSTEM = "*S-1-5-18"
_ADMINS = "*S-1-5-32-544"
_CREATOR_OWNER = "*S-1-3-0"


# ---------------------------------------------------------------------------
# Command construction (pure, no subprocess) — locale-independence + inheritance
# ---------------------------------------------------------------------------


class TestHardenCommandConstruction:
    def test_remove_args_use_wellknown_sids_not_english_names(self, tmp_path):
        """/remove must target well-known SIDs, never localizable English names."""
        target = tmp_path / "secret.bin"
        target.write_bytes(b"x")

        cmd = fp_mod._build_harden_command(target, "DESKTOP-ABC\\alice")

        # Every principal removed is referenced by its locale-independent SID.
        for sid in (_EVERYONE, _USERS, _SYSTEM, _ADMINS, _CREATOR_OWNER):
            assert sid in cmd, f"expected SID-based /remove of {sid} in {cmd}"

        # The fragile English names that fail with 1332 on es-ES/… are gone.
        joined = " ".join(cmd)
        assert "Everyone" not in joined
        assert "BUILTIN\\Administrators" not in joined
        # bare "Users"/"NT AUTHORITY\\SYSTEM" must not appear as /remove targets
        assert "NT AUTHORITY\\SYSTEM" not in joined

    def test_directory_grant_is_inheritable(self, tmp_path):
        """Directories grant (OI)(CI)(F) so children inherit owner-only access."""
        d = tmp_path / "dir"
        d.mkdir()
        assert fp_mod._owner_grant_spec(d, "alice") == "alice:(OI)(CI)(F)"

    def test_file_grant_is_not_inheritable(self, tmp_path):
        """Files grant plain (F) — inheritance flags are meaningless on a file."""
        f = tmp_path / "f.bin"
        f.write_bytes(b"x")
        assert fp_mod._owner_grant_spec(f, "alice") == "alice:(F)"


# ---------------------------------------------------------------------------
# Degrade-safe fail-closed: non-zero icacls exit but DACL is actually owner-only
# ---------------------------------------------------------------------------


class TestDegradeSafe:
    @pytest.fixture(autouse=True)
    def force_windows(self):
        with patch.object(fp_mod, "_IS_WINDOWS", True):
            yield

    def test_nonzero_icacls_but_owner_only_dacl_does_not_destroy(self, tmp_path, caplog):
        """icacls exits 1332 (an unrelated /remove) AFTER the owner grant applied.

        The effective DACL is owner-only, so the artifact must be kept (not
        unlinked) and no PermissionError raised — only a warning.
        """
        target = tmp_path / "secret.bin"
        target.write_bytes(b"keepme")
        sid = "S-1-5-21-1-2-3-1001"

        def fake_run(cmd, **kwargs):
            if "powershell" in cmd[0].lower():
                m = MagicMock()
                m.stdout = sid + "\n"
                return m
            # Any hardening icacls (carries /grant:r) "fails" like a localized
            # /remove would — non-zero exit, but the grant already took effect.
            if "/grant:r" in cmd:
                raise subprocess.CalledProcessError(1332, "icacls")
            # Verify pass: `icacls <path>` shows a clean owner-only DACL.
            if cmd[0] == "icacls":
                m = MagicMock(returncode=0)
                m.stdout = f"{target} DESKTOP-ABC\\User:(F)\n"
                return m
            raise AssertionError(f"unexpected call: {cmd}")

        with (
            patch("getpass.getuser", return_value="User"),
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod") as mock_chmod,
            caplog.at_level(logging.WARNING),
        ):
            restrict_to_owner(target)  # must NOT raise

        assert target.exists(), "owner-only artifact was destroyed on a false 1332"
        mock_chmod.assert_not_called()

    def test_nonzero_icacls_and_leaky_dacl_still_fails_closed(self, tmp_path, caplog):
        """icacls non-zero AND the DACL still has a non-owner ACE → fail closed."""
        target = tmp_path / "secret.bin"
        target.write_bytes(b"x")
        sid = "S-1-5-21-1-2-3-1001"

        def fake_run(cmd, **kwargs):
            if "powershell" in cmd[0].lower():
                m = MagicMock()
                m.stdout = sid + "\n"
                return m
            if "/grant:r" in cmd:
                raise subprocess.CalledProcessError(1332, "icacls")
            if cmd[0] == "icacls":
                # Leaky: Everyone still present after the failed hardening.
                m = MagicMock(returncode=0)
                m.stdout = f"{target} DESKTOP-ABC\\User:(F) Everyone:(F)\n"
                return m
            raise AssertionError(f"unexpected call: {cmd}")

        with (
            patch("getpass.getuser", return_value="User"),
            patch("subprocess.run", side_effect=fake_run),
            patch("os.chmod"),
            caplog.at_level(logging.CRITICAL),
            pytest.raises(PermissionError, match="Owner-only ACL enforcement failed"),
        ):
            restrict_to_owner(target)

        assert not target.exists(), "leaky artifact must be destroyed (fail closed)"


# ---------------------------------------------------------------------------
# Real-Windows integration — exercises actual icacls on the live OS language
# ---------------------------------------------------------------------------


class TestTransientLogonSessionSid:
    """A logon-session SID (``S-1-5-5-X-Y``) must not be treated as a leak.

    In restricted-token environments (Windows Sandbox, app containers, some CI
    agents) freshly-written files carry a transient logon-session ACE rendered
    by icacls as ``NT AUTHORITY\\LogonSessionId_0_<n>:(RX)`` which survives
    ``/inheritance:r``.  It is session-local and transient — never a cross-user
    or world leak — so the owner-only check must tolerate it instead of
    fail-closed-destroying the secret over it.
    """

    def test_owner_only_with_logon_session_name_form(self):
        out = (
            "C:\\f\\ws_auth_token DESKTOP-ABC\\User:(F)\n"
            "                    NT AUTHORITY\\LogonSessionId_0_3207687:(RX)\n"
        )
        assert fp_mod._dacl_is_owner_only(out, ["User"]) is True

    def test_owner_only_with_logon_session_sid_form(self):
        out = "C:\\f\\ws_auth_token DESKTOP-ABC\\User:(F) *S-1-5-5-0-3207687:(RX)\n"
        assert fp_mod._dacl_is_owner_only(out, ["User"]) is True

    def test_real_leak_still_rejected_even_with_logon_session_present(self):
        out = (
            "C:\\f\\ws_auth_token DESKTOP-ABC\\User:(F)\n"
            "                    NT AUTHORITY\\LogonSessionId_0_3207687:(RX)\n"
            "                    Everyone:(F)\n"
        )
        assert fp_mod._dacl_is_owner_only(out, ["User"]) is False

    def test_logon_session_alone_is_not_owner_only(self):
        # No owner ACE at all → not successfully hardened → reject.
        out = "C:\\f\\ws_auth_token NT AUTHORITY\\LogonSessionId_0_3207687:(RX)\n"
        assert fp_mod._dacl_is_owner_only(out, ["User"]) is False


@pytest.mark.skipif(sys.platform != "win32", reason="icacls is Windows-only")
class TestWindowsIntegration:
    def test_restrict_directory_does_not_fail_closed(self, tmp_path):
        """On the live (possibly localized) Windows, restricting a dir succeeds."""
        d = tmp_path / "tokens"
        d.mkdir()
        restrict_to_owner(d)  # must not raise on es-ES / de-DE / …
        assert d.exists()

    def test_restricted_directory_child_is_writable(self, tmp_path):
        """DLQ regression: a child created under a restricted dir is writable.

        This is the exact shape of ``~/.sky_claw`` → ``~/.sky_claw/dlq`` →
        ``dlq.db``. Before the inheritable-grant fix the child inherited only a
        non-writable group ACE and ``aiosqlite.connect`` raised
        ``unable to open database file``.
        """
        parent = tmp_path / "sky_claw_like"
        parent.mkdir()
        restrict_to_owner(parent)

        child = parent / "dlq"
        child.mkdir()
        db = child / "dlq.db"
        db.write_bytes(b"sqlite-stand-in")  # the write that used to fail
        assert db.read_bytes() == b"sqlite-stand-in"

    def test_restrict_file_does_not_destroy_it(self, tmp_path):
        """A secret FILE survives hardening on localized Windows (no false unlink)."""
        f = tmp_path / "ws_auth_token"
        f.write_text("secret-token", encoding="utf-8")
        restrict_to_owner(f)
        assert f.exists()
        assert f.read_text(encoding="utf-8") == "secret-token"
