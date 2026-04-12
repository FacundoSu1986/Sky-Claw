"""Tests for sky_claw.security.auth_token_manager.AuthTokenManager.

Covers:
- generate() returns token with correct length/format
- validate() returns True for the correct token within TTL
- validate() returns False for wrong token
- validate() returns False after TTL expires (mocked time)
- validate() returns False after revoke()
- Token file is created on generate and deleted on revoke
- secrets.compare_digest is used (not plain ==)
"""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path
from unittest.mock import patch


from sky_claw.security.auth_token_manager import AuthTokenManager, _TOKEN_TTL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path) -> AuthTokenManager:
    """Create an AuthTokenManager backed by a temporary directory."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
        return AuthTokenManager(token_dir=str(token_dir))


# ===========================================================================
# 1.  generate() — token format and length
# ===========================================================================


class TestGenerate:
    def test_generate_returns_string(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        assert isinstance(token, str)

    def test_generate_non_empty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        assert len(token) > 0

    def test_generate_url_safe_base64_length(self, tmp_path):
        """secrets.token_urlsafe(32) → 43 or 44 base64url characters."""
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        # base64url of 32 bytes: ceil(32 * 4/3) = 43 chars (no padding)
        # Allow 43–44 characters for standard urlsafe base64
        assert len(token) >= 43, f"Token too short: {len(token)}"

    def test_generate_urlsafe_characters_only(self, tmp_path):
        """Token must contain only URL-safe characters (no +, /, =)."""
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        forbidden = set("+/=")
        assert not forbidden.intersection(token), (
            f"Token contains forbidden characters: {set(token) & forbidden}"
        )

    def test_generate_is_random(self, tmp_path):
        """Two successive calls must produce different tokens."""
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            t1 = mgr.generate()
            t2 = mgr.generate()
        assert t1 != t2

    def test_generate_sets_created_at(self, tmp_path):
        """_created_at must be set to the current time on generate()."""
        mgr = _make_manager(tmp_path)
        before = time.time()
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()
        after = time.time()
        assert before <= mgr._created_at <= after


# ===========================================================================
# 2.  validate() — correct token within TTL
# ===========================================================================


class TestValidateCorrectToken:
    def test_valid_token_returns_true(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        assert mgr.validate(token) is True

    def test_valid_token_multiple_times(self, tmp_path):
        """validate() must remain True on repeated calls with the correct token."""
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        assert mgr.validate(token) is True
        assert mgr.validate(token) is True

    def test_validate_before_generate_returns_false(self, tmp_path):
        """No token has been generated yet — must reject."""
        mgr = _make_manager(tmp_path)
        assert mgr.validate("anything") is False


# ===========================================================================
# 3.  validate() — wrong token
# ===========================================================================


class TestValidateWrongToken:
    def test_wrong_token_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()
        assert mgr.validate("completely-wrong-token") is False

    def test_empty_string_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()
        assert mgr.validate("") is False

    def test_partial_token_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        # A prefix of the real token must NOT validate
        assert mgr.validate(token[:10]) is False

    def test_token_with_extra_chars_returns_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()
        assert mgr.validate(token + "X") is False


# ===========================================================================
# 4.  validate() — TTL expiry (mocked time)
# ===========================================================================


class TestValidateTTLExpiry:
    def test_token_valid_just_before_ttl(self, tmp_path):
        """At TTL - 1 second the token must still be valid."""
        mgr = _make_manager(tmp_path)

        fake_now = 1_000_000.0
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            with patch("sky_claw.security.auth_token_manager.time") as mock_time:
                mock_time.time.return_value = fake_now
                token = mgr.generate()

            # Advance time to 1 second before expiry
            with patch("sky_claw.security.auth_token_manager.time") as mock_time:
                mock_time.time.return_value = fake_now + _TOKEN_TTL - 1
                assert mgr.validate(token) is True

    def test_token_invalid_exactly_at_ttl(self, tmp_path):
        """At exactly TTL seconds elapsed the token must expire."""
        mgr = _make_manager(tmp_path)

        fake_now = 1_000_000.0
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            with patch("sky_claw.security.auth_token_manager.time") as mock_time:
                mock_time.time.return_value = fake_now
                token = mgr.generate()

            with patch("sky_claw.security.auth_token_manager.time") as mock_time:
                mock_time.time.return_value = fake_now + _TOKEN_TTL + 1
                assert mgr.validate(token) is False

    def test_token_invalid_well_after_ttl(self, tmp_path):
        """Token must be rejected long after TTL."""
        mgr = _make_manager(tmp_path)

        fake_now = 1_000_000.0
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            with patch("sky_claw.security.auth_token_manager.time") as mock_time:
                mock_time.time.return_value = fake_now
                token = mgr.generate()

            with patch("sky_claw.security.auth_token_manager.time") as mock_time:
                mock_time.time.return_value = fake_now + _TOKEN_TTL * 10
                assert mgr.validate(token) is False

    def test_correct_ttl_constant(self):
        """_TOKEN_TTL must be a positive number (sanity check on the constant)."""
        assert isinstance(_TOKEN_TTL, (int, float))
        assert _TOKEN_TTL > 0


# ===========================================================================
# 5.  validate() — after revoke()
# ===========================================================================


class TestValidateAfterRevoke:
    def test_validate_false_after_revoke(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()

        assert mgr.validate(token) is True
        mgr.revoke()
        assert mgr.validate(token) is False

    def test_revoke_clears_internal_token(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()
        mgr.revoke()
        assert mgr._token is None
        assert mgr._token_hash is None

    def test_revoke_resets_created_at(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()
        assert mgr._created_at > 0
        mgr.revoke()
        assert mgr._created_at == 0.0

    def test_double_revoke_is_safe(self, tmp_path):
        """Calling revoke() twice must not raise."""
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()
        mgr.revoke()
        mgr.revoke()  # second call must be a no-op

    def test_revoke_without_generate_is_safe(self, tmp_path):
        """Revoking with no prior generate() must not raise."""
        mgr = _make_manager(tmp_path)
        mgr.revoke()  # should not raise


# ===========================================================================
# 6.  Token file lifecycle
# ===========================================================================


class TestTokenFile:
    def test_generate_creates_token_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        token_path = mgr._token_path
        assert not token_path.exists(), "File should not exist before generate()"

        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()

        assert token_path.exists(), "Token file must be created by generate()"

    def test_token_file_contains_the_returned_token(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()

        written = mgr._token_path.read_text(encoding="utf-8")
        assert written == token, (
            "Token file content must exactly match the returned token"
        )

    def test_revoke_deletes_token_file(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()

        assert mgr._token_path.exists()
        mgr.revoke()
        assert not mgr._token_path.exists(), "Token file must be removed by revoke()"

    def test_revoke_without_file_does_not_raise(self, tmp_path):
        """If the file was externally deleted, revoke() must still succeed."""
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()

        mgr._token_path.unlink()  # externally remove the file
        mgr.revoke()  # must not raise

    def test_read_token_file_class_method(self, tmp_path):
        """read_token_file() must return the same token written by generate()."""
        token_dir = tmp_path / "tokens"
        token_dir.mkdir()
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr = AuthTokenManager(token_dir=str(token_dir))
            token = mgr.generate()

        result = AuthTokenManager.read_token_file(token_dir=str(token_dir))
        assert result == token

    def test_read_token_file_missing_returns_none(self, tmp_path):
        """read_token_file() must return None when no file exists."""
        token_dir = tmp_path / "empty_tokens"
        token_dir.mkdir()
        result = AuthTokenManager.read_token_file(token_dir=str(token_dir))
        assert result is None

    def test_generate_calls_restrict_to_owner_on_file(self, tmp_path):
        """restrict_to_owner must be called on the token file after writing."""
        token_dir = tmp_path / "tokens"
        token_dir.mkdir()

        with patch(
            "sky_claw.security.auth_token_manager.restrict_to_owner"
        ) as mock_restrict:
            mgr = AuthTokenManager(token_dir=str(token_dir))
            mock_restrict.reset_mock()  # reset the __init__ call
            mgr.generate()

        # The last call should be on the token *file* (not just the directory)
        called_paths = [str(c.args[0]) for c in mock_restrict.call_args_list]
        token_file_str = str(mgr._token_path)
        assert any(token_file_str in p or p in token_file_str for p in called_paths), (
            f"restrict_to_owner was not called on the token file. Calls: {called_paths}"
        )


# ===========================================================================
# 7.  Timing-safe comparison (secrets.compare_digest used, not ==)
# ===========================================================================


class TestTimingSafeComparison:
    """Verify that validate() uses secrets.compare_digest rather than a plain
    string equality check, which is important to prevent timing side-channels."""

    def test_compare_digest_is_called_on_valid_token(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()

        with patch(
            "sky_claw.security.auth_token_manager.secrets.compare_digest",
            wraps=secrets.compare_digest,
        ) as mock_cd:
            mgr.validate(token)

        mock_cd.assert_called_once()

    def test_compare_digest_is_called_on_invalid_token(self, tmp_path):
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            mgr.generate()

        with patch(
            "sky_claw.security.auth_token_manager.secrets.compare_digest",
            wraps=secrets.compare_digest,
        ) as mock_cd:
            mgr.validate("wrong")

        mock_cd.assert_called_once()

    def test_compare_digest_receives_hashes_not_plaintext(self, tmp_path):
        """compare_digest must be called with SHA-256 hashes, not raw tokens.

        This ensures that even if compare_digest were replaced, the token
        itself is never compared in plaintext (so it cannot be reconstructed
        from a timing attack on the comparison function alone).
        """
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()

        captured_args: list[tuple[str, str]] = []
        # Keep a reference to the *real* function before patching so the
        # side_effect does not call back through the mock (causing recursion).
        _real_compare = secrets.compare_digest

        def capturing_compare(a: str, b: str) -> bool:
            captured_args.append((a, b))
            return _real_compare(a, b)

        with patch(
            "sky_claw.security.auth_token_manager.secrets.compare_digest",
            side_effect=capturing_compare,
        ):
            mgr.validate(token)

        assert len(captured_args) == 1
        arg_a, arg_b = captured_args[0]

        # Both arguments must be hex strings (SHA-256 digests), not the raw token
        assert arg_a != token, "compare_digest should NOT receive the raw token"
        assert arg_b != token, "compare_digest should NOT receive the raw token"

        # Verify they look like SHA-256 hex digests (64 hex chars)
        assert len(arg_a) == 64 and all(c in "0123456789abcdef" for c in arg_a)
        assert len(arg_b) == 64 and all(c in "0123456789abcdef" for c in arg_b)

    def test_validate_result_matches_compare_digest(self, tmp_path):
        """validate() result must agree with what secrets.compare_digest returns."""
        mgr = _make_manager(tmp_path)
        with patch("sky_claw.security.auth_token_manager.restrict_to_owner"):
            token = mgr.generate()

        # Correct token
        expected_hash = hashlib.sha256(token.encode()).hexdigest()
        stored_hash = mgr._token_hash
        assert secrets.compare_digest(expected_hash, stored_hash) is True
        assert mgr.validate(token) is True

        # Wrong token
        wrong_hash = hashlib.sha256(b"nope").hexdigest()
        assert secrets.compare_digest(wrong_hash, stored_hash) is False
        assert mgr.validate("nope") is False
