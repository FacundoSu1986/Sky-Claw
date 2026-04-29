"""Zero-Trust transport policy for all WebSocket clients.

Enforces two guarantees before any connection is opened:

1. **URL safety** — only ``ws://`` on loopback or ``wss://`` anywhere are
   permitted.  Plain-text connections to non-loopback hosts are rejected at
   construction time, not at runtime.

2. **Bearer-token injection** — callers that require authentication receive
   the token from :class:`~sky_claw.security.auth_token_manager.AuthTokenManager`
   (or an explicit override) and it is attached as the ``X-Auth-Token`` header
   before the handshake.

Public API
----------
InsecureTransportError
    Raised when the URL violates the Zero-Trust policy.
AuthError
    Raised when a bearer token is required but unavailable.
    Subclasses :class:`ConnectionRefusedError` so it slots into existing
    reconnection loops without code changes.
assert_safe_ws_url(url, *, allow_plaintext_loopback=True)
    Validate a WebSocket URL and return it unchanged.
authenticated_connect(url, *, auth_token, token_dir, require_auth, ...)
    Validate URL, attach bearer token, return a ``websockets.connect`` context.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

import websockets

from sky_claw.security.auth_token_manager import AuthTokenManager

__all__ = [
    "AuthError",
    "InsecureTransportError",
    "assert_safe_ws_url",
    "authenticated_connect",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InsecureTransportError(ValueError):
    """Raised when a transport URL violates the Zero-Trust policy."""


class AuthError(ConnectionRefusedError):
    """Raised when a bearer token is required but unavailable.

    Subclasses :class:`ConnectionRefusedError` so existing
    reconnection-with-backoff handlers catch it automatically.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTNAMES: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback(host: str) -> bool:
    """Return True if *host* is a loopback address."""
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assert_safe_ws_url(
    url: str,
    *,
    allow_plaintext_loopback: bool = True,
) -> str:
    """Validate *url* against the Zero-Trust transport policy.

    Rules
    -----
    * Scheme must be ``ws`` or ``wss`` (case-insensitive).
    * ``wss://`` is always accepted regardless of host.
    * ``ws://`` is accepted only when the host is a loopback address
      **and** *allow_plaintext_loopback* is True.

    Parameters
    ----------
    url:
        The WebSocket URL to validate.
    allow_plaintext_loopback:
        When False, even loopback ``ws://`` connections are rejected.
        Useful for hardened environments that require TLS everywhere.

    Returns
    -------
    str
        The original *url*, unchanged, if it passes all checks.

    Raises
    ------
    InsecureTransportError
        When any rule is violated.
    """
    if not isinstance(url, str) or not url:
        raise InsecureTransportError("transport URL must be a non-empty string")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme not in {"ws", "wss"}:
        raise InsecureTransportError(f"unsupported transport scheme {scheme!r}; expected 'ws' or 'wss'")

    if not parsed.hostname:
        raise InsecureTransportError(f"transport URL is missing a host: {url!r}")

    # wss:// is unconditionally safe.
    if scheme == "wss":
        return url

    # ws:// — only loopback is permitted.
    host = parsed.hostname.lower()
    if not _is_loopback(host):
        raise InsecureTransportError(
            f"plaintext ws:// is forbidden for non-loopback host {host!r}; use wss:// or bind to 127.0.0.1/localhost"
        )

    if not allow_plaintext_loopback:
        raise InsecureTransportError("plaintext ws:// is disabled by configuration; use wss:// even on loopback")

    return url


def authenticated_connect(
    url: str,
    *,
    auth_token: str | None = None,
    token_dir: str | None = None,
    require_auth: bool = True,
    allow_plaintext_loopback: bool = True,
    **connect_kwargs: Any,
) -> Any:
    """Return a ``websockets.connect`` context manager with auth headers.

    Steps
    -----
    1. Validate *url* via :func:`assert_safe_ws_url`.
    2. Resolve the bearer token:
       * *auth_token* (explicit) takes priority.
       * Falls back to :meth:`~sky_claw.security.auth_token_manager.AuthTokenManager.read_token_file`
         using *token_dir*.
    3. If *require_auth* is True and no token is available, raise
       :class:`AuthError`.
    4. Merge ``X-Auth-Token`` into ``additional_headers`` and delegate to
       ``websockets.connect``.

    Parameters
    ----------
    url:
        WebSocket URL — validated before any network I/O.
    auth_token:
        Explicit token string.  If provided, skips file-based lookup.
    token_dir:
        Directory passed to :meth:`AuthTokenManager.read_token_file`.
        Ignored when *auth_token* is given.
    require_auth:
        Raise :class:`AuthError` if no token is available.
        Set to False for unauthenticated connections (e.g. public endpoints).
    allow_plaintext_loopback:
        Forwarded to :func:`assert_safe_ws_url`.
    **connect_kwargs:
        Forwarded verbatim to ``websockets.connect``.  A caller-supplied
        ``additional_headers`` mapping is preserved and merged with the
        auth header.

    Returns
    -------
    Any
        The return value of ``websockets.connect(...)`` — typically an async
        context manager that yields a :class:`websockets.WebSocketClientProtocol`.

    Raises
    ------
    InsecureTransportError
        If the URL fails the Zero-Trust policy check.
    AuthError
        If *require_auth* is True and no token can be resolved.
    """
    safe_url = assert_safe_ws_url(url, allow_plaintext_loopback=allow_plaintext_loopback)

    # Resolve token — explicit arg wins, then file-based fallback.
    token: str | None = auth_token if auth_token is not None else AuthTokenManager.read_token_file(token_dir)

    if not token and require_auth:
        raise AuthError(
            f"No auth token available for handshake to {safe_url!r}; "
            "AuthTokenManager.generate() must run before this client connects"
        )

    # Merge caller headers with the auth header.
    headers: dict[str, str] = dict(connect_kwargs.pop("additional_headers", None) or {})
    if token:
        headers["X-Auth-Token"] = token

    return websockets.connect(
        safe_url,
        additional_headers=headers or None,
        **connect_kwargs,
    )
