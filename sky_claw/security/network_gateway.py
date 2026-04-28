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

import asyncio
import ipaddress
import logging
import socket
import ssl
from collections import OrderedDict
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


class EgressViolationError(Exception):
    """Raised when a request violates egress policy."""


class NetworkGatewayTimeoutError(Exception):
    """Raised when an egress request exceeds the safe timeout bounds."""


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Immutable snapshot of the egress rules the gateway evaluates."""

    allowed_hosts: frozenset[str] = field(default_factory=lambda: ALLOWED_HOSTS)
    allowed_methods: dict[str, frozenset[str]] = field(default_factory=lambda: dict(ALLOWED_METHODS))
    telegram_path_prefix: str = TELEGRAM_PATH_PREFIX
    block_private_ips: bool = True


class GatewayTCPConnector(aiohttp.TCPConnector):
    """Custom TCPConnector that enforces strict SSL and uses a safe resolver to prevent SSRF."""

    def __init__(self, gateway: NetworkGateway, **kwargs):
        resolver = SafeResolver(gateway._policy)
        if "ssl" not in kwargs:
            kwargs["ssl"] = ssl.create_default_context()
        super().__init__(resolver=resolver, **kwargs)


_MAX_PINNED_ENTRIES = 1024


class SafeResolver(aiohttp.abc.AbstractResolver):
    """DNS resolver with IP validation and pinning against TOCTOU/rebinding.

    Once a hostname is resolved and validated, the result is *pinned* for the
    lifetime of this resolver instance.  Subsequent ``resolve()`` calls for the
    same ``(host, port)`` return the cached answer, preventing an attacker from
    swapping DNS records between the check and the use (DNS rebinding).

    The pin cache is an LRU OrderedDict capped at ``_MAX_PINNED_ENTRIES`` entries
    to prevent unbounded memory growth when many distinct hostnames are resolved
    within a single session.
    """

    def __init__(self, policy: EgressPolicy) -> None:
        self._policy = policy
        self._pinned: OrderedDict[tuple[str, int], list[dict[str, Any]]] = OrderedDict()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        pin_key = (host, port)

        # DNS Pinning: return the previously validated result if available.
        pinned = self._pinned.get(pin_key)
        if pinned is not None:
            # LRU: promote to most-recently-used end.
            self._pinned.move_to_end(pin_key)
            return pinned

        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host,
                port,
                family=family,
                type=socket.SOCK_STREAM,
            )
        except (socket.gaierror, OSError) as e:
            raise OSError(f"DNS resolution failed for '{host}': {e}") from e

        if not infos:
            raise OSError(f"DNS resolution failed for '{host}'")

        result: list[dict[str, Any]] = []
        for info in infos:
            ip_str = info[4][0]
            addr = ipaddress.ip_address(ip_str)  # ValueError → reject unparseable IPs
            if self._policy.block_private_ips and (addr.is_private or addr.is_loopback or addr.is_link_local):
                raise EgressViolationError(f"Resolved address {addr} for '{host}' is private/loopback (SSRF block)")

            result.append(
                {
                    "hostname": host,
                    "host": ip_str,
                    "port": info[4][1],
                    "family": info[0],
                    "proto": info[2],
                    "flags": socket.AI_NUMERICHOST,
                }
            )

        if not result:
            raise OSError(f"No valid addresses resolved for '{host}'")

        # Pin the validated addresses — all future calls bypass DNS entirely.
        self._pinned[pin_key] = result
        # LRU eviction: drop oldest entry when cap is exceeded.
        if len(self._pinned) > _MAX_PINNED_ENTRIES:
            self._pinned.popitem(last=False)
        return result

    async def close(self) -> None:
        self._pinned.clear()


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

    async def authorize(self, method: str, url: str) -> None:
        """Validate *method* + *url* against the egress policy.

        Raises :class:`EgressViolationError` when the request is not permitted.
        """
        method = method.upper()
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        if not hostname:
            raise EgressViolationError(f"URL has no hostname: {url}")

        # Check raw literal IPs just in case
        try:
            addr = ipaddress.ip_address(hostname)
            if self._policy.block_private_ips and (addr.is_private or addr.is_loopback or addr.is_link_local):
                raise EgressViolationError(f"Literal address {addr} is a private/loopback IP")
        except ValueError:
            pass

        self._check_host_allowed(hostname)
        self._check_method_allowed(method, hostname)
        self._check_telegram_path(hostname, parsed.path)

    async def request(
        self,
        method: str,
        url: str,
        session: aiohttp.ClientSession,
        max_redirects: int = 5,
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        """Authorize and execute an HTTP request with redirect validation.

        DNS Pinning is handled automatically by GatewayTCPConnector.
        Redirect validation (H4): ``allow_redirects=False`` is enforced. Each redirect
        Location is re-authorized through the full egress policy before being followed.
        """
        kwargs["allow_redirects"] = False

        current_url = url
        for hop in range(max_redirects + 1):
            await self.authorize(method, current_url)

            parsed = urlparse(current_url)
            is_loopback = self._is_loopback_host(parsed.hostname or "")
            if parsed.scheme != "https" and not is_loopback:
                raise EgressViolationError(f"Insecure scheme '{parsed.scheme}' blocked: {current_url}")

            hop_kwargs = dict(kwargs)
            safe_timeout = aiohttp.ClientTimeout(total=45, connect=10)
            if "timeout" not in hop_kwargs:
                hop_kwargs["timeout"] = safe_timeout
            if is_loopback and parsed.scheme == "http":
                hop_kwargs["ssl"] = False

            try:
                response = await session.request(method, current_url, **hop_kwargs)
            except TimeoutError as _exc:
                logger.error(f"Timeout al contactar {current_url}")
                raise NetworkGatewayTimeoutError(f"La petición a {current_url} excedió el tiempo límite.") from _exc

            if response.status in (301, 302, 303, 307, 308):
                redirect_url = response.headers.get("Location")
                if not redirect_url:
                    return response
                # Resolve relative URLs before the next authorize() call
                if not urlparse(redirect_url).netloc:
                    base = urlparse(current_url)
                    redirect_url = f"{base.scheme}://{base.netloc}{redirect_url}"
                current_url = redirect_url
                logger.debug("Following redirect hop %d: %s", hop + 1, current_url)
                continue

            return response

        raise EgressViolationError(f"Maximum redirect limit ({max_redirects}) exceeded for URL: {url}")

    async def validate_redirection_chain(self, url: str, history: list[str]) -> None:
        """Explicit validation for a chain of URLs (SSRF Protection)."""
        for hop_url in history:
            await self.authorize("GET", hop_url)
            parsed = urlparse(hop_url)
            if parsed.scheme != "https":
                raise EgressViolationError(f"Non-HTTPS hop detected: {hop_url}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_loopback_host(self, hostname: str) -> bool:
        if not hostname:
            return False
        hostname_lower = hostname.lower()
        if hostname_lower in ("localhost", "127.0.0.1", "::1"):
            return True
        try:
            addr = ipaddress.ip_address(hostname_lower)
            return addr.is_loopback
        except ValueError:
            return False

    def _matching_pattern(self, hostname: str) -> str | None:
        """Return the first allow-list pattern that matches *hostname*, or None.

        Pattern semantics (strict DNS-aware, replaces the former glob/fnmatch logic):

        - ``"*.example.com"``  — wildcard prefix: matches ``"api.example.com"`` and
          ``"a.b.example.com"`` (any depth), but NOT ``"example.com"`` (base domain)
          and NOT ``"evil.example.com.attacker.com"`` (superdomain injection).
        - ``"example.com"``    — exact match only; subdomains do NOT match.

        Matching is case-insensitive (DNS hostnames are case-insensitive, RFC 4343).
        The *hostname* argument is normalised to lowercase internally.

        Malformed patterns (e.g. bare ``"*"`` or dotless literals) are handled
        gracefully: ``"*"`` will never match as a wildcard (it lacks the ``*.`` prefix)
        and will only match the literal string ``"*"``; dotless literals match exactly.

        Returns:
            The matched pattern string from the allow-list, or ``None`` if no
            pattern matches *hostname*.
        """
        hostname_lower = hostname.lower()
        for pattern in self._policy.allowed_hosts:
            pattern_lower = pattern.lower()
            if pattern_lower.startswith("*."):
                base_domain = pattern_lower[2:]  # "example.com"
                # Must end with ".example.com" (dot + base) — subdomain only
                if hostname_lower.endswith("." + base_domain):
                    return pattern
            else:
                # Exact match (case-insensitive)
                if hostname_lower == pattern_lower:
                    return pattern
        return None

    def _check_host_allowed(self, hostname: str) -> None:
        if self._matching_pattern(hostname) is None:
            raise EgressViolationError(f"Host '{hostname}' is not in the allow-list")

    def _check_method_allowed(self, method: str, hostname: str) -> None:
        pattern = self._matching_pattern(hostname)
        if pattern is None:
            return  # already caught by _check_host_allowed
        allowed = self._policy.allowed_methods.get(pattern)
        if allowed is not None and method not in allowed:
            raise EgressViolationError(f"Method '{method}' is not allowed for host pattern '{pattern}'")

    def _check_telegram_path(self, hostname: str, path: str) -> None:
        """Ensure Telegram requests go through /bot<token>/…."""
        if hostname == "api.telegram.org" and not path.startswith(self._policy.telegram_path_prefix):
            raise EgressViolationError(
                f"Telegram path '{path}' does not start with '{self._policy.telegram_path_prefix}'"
            )
