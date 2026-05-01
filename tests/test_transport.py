"""Tests for sky_claw.antigravity.comms._transport — Zero-Trust transport policy.

Covers:
- assert_safe_ws_url: accepted URLs, rejected URLs, hardened loopback policy
- authenticated_connect: URL policy enforcement, token injection, file fallback,
  missing-token behaviour, AuthError subclass contract
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sky_claw.antigravity.comms._transport import (
    AuthError,
    InsecureTransportError,
    assert_safe_ws_url,
    authenticated_connect,
)

# ---------------------------------------------------------------------------
# assert_safe_ws_url — accepted URLs
# ---------------------------------------------------------------------------


class TestAcceptedUrls:
    """URLs that must pass the policy without raising."""

    def test_wss_remote_host(self):
        url = "wss://api.example.com/ws"
        assert assert_safe_ws_url(url) == url

    def test_wss_loopback(self):
        url = "wss://127.0.0.1:8080"
        assert assert_safe_ws_url(url) == url

    def test_ws_localhost(self):
        url = "ws://localhost:18789"
        assert assert_safe_ws_url(url) == url

    def test_ws_127(self):
        url = "ws://127.0.0.1:9000"
        assert assert_safe_ws_url(url) == url

    def test_ws_ipv6_loopback(self):
        url = "ws://[::1]:7777"
        assert assert_safe_ws_url(url) == url

    def test_wss_uppercase_scheme_normalised(self):
        # urlparse lowercases the scheme, so WSS:// is treated as wss://
        url = "WSS://example.com/endpoint"
        assert assert_safe_ws_url(url) == url

    def test_ws_uppercase_loopback_normalised(self):
        url = "WS://localhost:1234"
        assert assert_safe_ws_url(url) == url

    def test_returns_original_string_unchanged(self):
        url = "wss://example.com/path?q=1"
        result = assert_safe_ws_url(url)
        assert result is url


# ---------------------------------------------------------------------------
# assert_safe_ws_url — rejected URLs
# ---------------------------------------------------------------------------


class TestRejectedUrls:
    """URLs that must raise InsecureTransportError."""

    def test_ws_non_loopback(self):
        with pytest.raises(InsecureTransportError, match="non-loopback"):
            assert_safe_ws_url("ws://api.example.com/ws")

    def test_ws_private_ip_not_loopback(self):
        # 192.168.x.x is private but not loopback — must be rejected over plain ws
        with pytest.raises(InsecureTransportError, match="non-loopback"):
            assert_safe_ws_url("ws://192.168.1.100:8080")

    def test_http_scheme(self):
        with pytest.raises(InsecureTransportError, match="scheme"):
            assert_safe_ws_url("http://localhost:80")

    def test_https_scheme(self):
        with pytest.raises(InsecureTransportError, match="scheme"):
            assert_safe_ws_url("https://example.com")

    def test_ftp_scheme(self):
        with pytest.raises(InsecureTransportError, match="scheme"):
            assert_safe_ws_url("ftp://files.example.com")

    def test_empty_string(self):
        with pytest.raises(InsecureTransportError):
            assert_safe_ws_url("")

    def test_none_value(self):
        with pytest.raises(InsecureTransportError):
            assert_safe_ws_url(None)  # type: ignore[arg-type]

    def test_missing_host(self):
        with pytest.raises(InsecureTransportError, match="missing a host"):
            assert_safe_ws_url("ws:///path")

    def test_integer_value(self):
        with pytest.raises(InsecureTransportError):
            assert_safe_ws_url(12345)  # type: ignore[arg-type]

    def test_tcp_uri_scheme(self):
        with pytest.raises(InsecureTransportError, match="scheme"):
            assert_safe_ws_url("tcp://localhost:9000")


# ---------------------------------------------------------------------------
# assert_safe_ws_url — hardened loopback policy
# ---------------------------------------------------------------------------


class TestHardenedLoopbackPolicy:
    """allow_plaintext_loopback=False must reject even ws://localhost."""

    def test_ws_loopback_rejected_when_disabled(self):
        with pytest.raises(InsecureTransportError, match="disabled by configuration"):
            assert_safe_ws_url("ws://localhost:1234", allow_plaintext_loopback=False)

    def test_wss_loopback_still_accepted_when_disabled(self):
        url = "wss://localhost:1234"
        assert assert_safe_ws_url(url, allow_plaintext_loopback=False) == url


# ---------------------------------------------------------------------------
# authenticated_connect
# ---------------------------------------------------------------------------


class TestAuthenticatedConnect:
    """authenticated_connect behaviour — uses mocks to avoid network I/O."""

    def _make_connect_mock(self):
        """Return a mock for websockets.connect that records call args."""
        mock = MagicMock(name="websockets.connect")
        mock.return_value = MagicMock(name="connection_ctx")
        return mock

    def test_url_policy_enforced_before_connect(self):
        with pytest.raises(InsecureTransportError):
            authenticated_connect("ws://evil.example.com/ws", require_auth=False)

    def test_token_attached_as_header(self):
        mock_connect = self._make_connect_mock()
        with patch("sky_claw.antigravity.comms._transport.websockets.connect", mock_connect):
            authenticated_connect(
                "ws://localhost:9000",
                auth_token="tok-abc",
                require_auth=True,
            )
        _, kwargs = mock_connect.call_args
        headers = kwargs["additional_headers"]
        assert headers["X-Auth-Token"] == "tok-abc"

    def test_file_token_used_when_no_explicit_token(self):
        mock_connect = self._make_connect_mock()
        with (
            patch("sky_claw.antigravity.comms._transport.AuthTokenManager.read_token_file", return_value="file-tok"),
            patch("sky_claw.antigravity.comms._transport.websockets.connect", mock_connect),
        ):
            authenticated_connect("ws://localhost:9000", token_dir="/tmp/tokens")
        _, kwargs = mock_connect.call_args
        assert kwargs["additional_headers"]["X-Auth-Token"] == "file-tok"

    def test_explicit_token_overrides_file_token(self):
        mock_connect = self._make_connect_mock()
        with (
            patch("sky_claw.antigravity.comms._transport.AuthTokenManager.read_token_file", return_value="file-tok"),
            patch("sky_claw.antigravity.comms._transport.websockets.connect", mock_connect),
        ):
            authenticated_connect(
                "ws://localhost:9000",
                auth_token="explicit-tok",
                token_dir="/tmp/tokens",
            )
        _, kwargs = mock_connect.call_args
        assert kwargs["additional_headers"]["X-Auth-Token"] == "explicit-tok"

    def test_missing_token_raises_auth_error_when_required(self):
        with (
            patch("sky_claw.antigravity.comms._transport.AuthTokenManager.read_token_file", return_value=None),
            pytest.raises(AuthError, match="No auth token"),
        ):
            authenticated_connect("ws://localhost:9000", require_auth=True)

    def test_require_auth_false_allows_missing_token(self):
        mock_connect = self._make_connect_mock()
        with (
            patch("sky_claw.antigravity.comms._transport.AuthTokenManager.read_token_file", return_value=None),
            patch("sky_claw.antigravity.comms._transport.websockets.connect", mock_connect),
        ):
            authenticated_connect("ws://localhost:9000", require_auth=False)
        mock_connect.assert_called_once()
        _, kwargs = mock_connect.call_args
        # No token → additional_headers should be None (not an empty dict)
        assert kwargs.get("additional_headers") is None

    def test_caller_supplied_headers_preserved(self):
        mock_connect = self._make_connect_mock()
        with (
            patch("sky_claw.antigravity.comms._transport.AuthTokenManager.read_token_file", return_value="tok"),
            patch("sky_claw.antigravity.comms._transport.websockets.connect", mock_connect),
        ):
            authenticated_connect(
                "ws://localhost:9000",
                additional_headers={"X-Custom": "value"},
            )
        _, kwargs = mock_connect.call_args
        headers = kwargs["additional_headers"]
        assert headers["X-Custom"] == "value"
        assert headers["X-Auth-Token"] == "tok"

    def test_auth_error_is_connection_refused_error_subclass(self):
        """AuthError must be caught by existing ConnectionRefusedError handlers."""
        assert issubclass(AuthError, ConnectionRefusedError)
        err = AuthError("test")
        assert isinstance(err, ConnectionRefusedError)
