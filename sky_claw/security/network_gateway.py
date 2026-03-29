"""Network Gateway Proxy – egress control for all HTTP traffic.

Every outbound request made by Sky-Claw **must** pass through
:class:`NetworkGateway`.  The gateway enforces:

* **Domain allow-list** – only ``*.nexusmods.com`` and
  ``api.telegram.org/bot*`` traffic is permitted.
* **Method restrictions** – e.g. only ``GET`` towards Nexus Mods.
* **Private-IP blocking** – prevents SSRF to ``127.0.0.0/8``,
  ``10.0.0.0/8``, ``192.168.0.0/16``, link-local, etc.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp

from sky_claw.config import (
    ALLOWED_HOSTS,
    ALLOWED_METHODS,
    TELEGRAM_PATH_PREFIX,
)

logger = logging.getLogger("SkyClaw.Security")


class EgressViolation(Exception):
    """Raised when a request violates egress policy."""


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Immutable snapshot of the egress rules the gateway evaluates."""

    allowed_hosts: frozenset[str] = field(default_factory=lambda: ALLOWED_HOSTS)
    allowed_methods: dict[str, frozenset[str]] = field(
        default_factory=lambda: dict(ALLOWED_METHODS)
    )
    telegram_path_prefix: str = TELEGRAM_PATH_PREFIX
    block_private_ips: bool = True


class NetworkGateway:
    """Mandatory middleware for all outbound HTTP traffic.

    Usage::

        gw = NetworkGateway()
        gw.authorize("GET", "https://www.nexusmods.com/skyrimspecialedition/mods/1234")
    """

    def __init__(self, policy: EgressPolicy | None = None) -> None:
        self._policy = policy or EgressPolicy()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def authorize(self, method: str, url: str) -> None:
        """Validate *method* + *url* against the egress policy.

        Raises :class:`EgressViolation` when the request is not permitted.
        """
        method = method.upper()
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        if not hostname:
            raise EgressViolation(f"URL has no hostname: {url}")

        # Private-IP check runs first so that literal IPs (e.g. 127.0.0.1)
        # are rejected with a clear "private/loopback" message rather than
        # the generic "not in the allow-list" error.
        if self._policy.block_private_ips:
            self._check_not_private_ip(hostname)

        self._check_host_allowed(hostname)
        self._check_method_allowed(method, hostname)
        self._check_telegram_path(hostname, parsed.path)

    async def request(
        self,
        method: str,
        url: str,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        """Authorize and execute an HTTP request through *session*.

        Raises :class:`EgressViolation` if the request violates policy.
        """
        self.authorize(method, url)
        response = await session.request(method, url, **kwargs)
        return response

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _matching_pattern(self, hostname: str) -> str | None:
        """Return the first allow-list pattern that matches *hostname*."""
        for pattern in self._policy.allowed_hosts:
            if fnmatch.fnmatch(hostname, pattern):
                return pattern
        return None

    def _check_host_allowed(self, hostname: str) -> None:
        if self._matching_pattern(hostname) is None:
            raise EgressViolation(
                f"Host '{hostname}' is not in the allow-list"
            )

    def _check_method_allowed(self, method: str, hostname: str) -> None:
        pattern = self._matching_pattern(hostname)
        if pattern is None:
            return  # already caught by _check_host_allowed
        allowed = self._policy.allowed_methods.get(pattern)
        if allowed is not None and method not in allowed:
            raise EgressViolation(
                f"Method '{method}' is not allowed for host pattern '{pattern}'"
            )

    def _check_telegram_path(self, hostname: str, path: str) -> None:
        """Ensure Telegram requests go through /bot<token>/…."""
        if hostname == "api.telegram.org":
            if not path.startswith(self._policy.telegram_path_prefix):
                raise EgressViolation(
                    f"Telegram path '{path}' does not start with "
                    f"'{self._policy.telegram_path_prefix}'"
                )

    def _check_not_private_ip(self, hostname: str) -> None:
        """Block requests to private / loopback / link-local IPs."""
        try:
            # Try direct parse first (covers literal IPs in URLs).
            addr = ipaddress.ip_address(hostname)
        except ValueError:
            # hostname is a DNS name – resolve it.
            try:
                infos: list[Any] = socket.getaddrinfo(
                    hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
                )
                addr = ipaddress.ip_address(infos[0][4][0])
            except (socket.gaierror, IndexError, ValueError):
                # Cannot resolve → allow (the actual HTTP client will fail).
                return

        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise EgressViolation(
                f"Resolved address {addr} for '{hostname}' is a private/loopback IP"
            )
