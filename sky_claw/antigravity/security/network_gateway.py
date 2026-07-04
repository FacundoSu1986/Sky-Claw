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
import re
import socket
import ssl
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import ParseResult, urlparse

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
        # Comparte el pin cache DNS del gateway → todos los connectors del mismo
        # gateway pinean juntos (cierra el rebinding app-wide, no per-connector).
        resolver = SafeResolver(gateway._policy, gateway._dns_pins)
        if "ssl" not in kwargs:
            kwargs["ssl"] = ssl.create_default_context()
        super().__init__(resolver=resolver, **kwargs)


_MAX_PINNED_ENTRIES = 1024


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Devuelve ``True`` si *addr* debe bloquearse por seguridad SSRF.

    Normaliza direcciones IPv6 que envuelven una IPv4 (``::ffff:127.0.0.1``)
    antes de clasificar: ``IPv6Address.is_loopback`` devuelve ``False`` para la
    forma mapeada aunque la IPv4 subyacente sea loopback — un bypass clásico de
    SSRF en Python < 3.13. Cubre además ``is_multicast``, ``is_reserved`` y
    ``is_unspecified`` (``0.0.0.0`` / ``::``), que enrutan a interfaces locales y
    que el filtro anterior (solo ``is_private``/``is_loopback``/``is_link_local``)
    dejaba pasar.
    """
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


class SafeResolver(aiohttp.abc.AbstractResolver):
    """DNS resolver with IP validation and pinning against TOCTOU/rebinding.

    Once a hostname is resolved and validated, the result is *pinned* for the
    lifetime of this resolver instance.  Subsequent ``resolve()`` calls for the
    same ``(host, port)`` return the cached answer, preventing an attacker from
    swapping DNS records between the check and the use (DNS rebinding).

    The pin cache is an LRU OrderedDict capped at ``_MAX_PINNED_ENTRIES`` entries
    to prevent unbounded memory growth when many distinct hostnames are resolved.

    Cuando se inyecta ``dns_pins`` (la caché del :class:`NetworkGateway`), TODOS
    los resolvers de ese gateway comparten UNA caché: el primer host validado
    queda pineado para todas las conexiones del gateway, cerrando el gap de
    DNS-rebinding app-wide (sin ella, cada connector re-resolvía desde cero). Sin
    inyección (``dns_pins=None``), el resolver es standalone con su propia caché.
    """

    def __init__(
        self,
        policy: EgressPolicy,
        dns_pins: OrderedDict[tuple[str, int], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._policy = policy
        # Caché compartida (del gateway) cuando se inyecta; propia si no. ``_owns_pins``
        # decide si ``close()`` puede limpiarla (nunca wipear la del gateway).
        self._owns_pins = dns_pins is None
        self._pinned: OrderedDict[tuple[str, int], list[dict[str, Any]]] = (
            dns_pins if dns_pins is not None else OrderedDict()
        )

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
            if self._policy.block_private_ips and _is_blocked_ip(addr):
                raise EgressViolationError(
                    f"Resolved address {addr} for '{host}' is private/loopback or non-routable "
                    "(multicast/reserved/unspecified/IPv4-mapped) — SSRF block"
                )

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
        # Solo limpiar la caché propia; la caché compartida del gateway persiste
        # entre connectors (su lifecycle lo maneja el gateway, no un connector suelto).
        if self._owns_pins:
            self._pinned.clear()


class NetworkGateway:
    """Mandatory middleware for all outbound HTTP traffic.

    Usage::

        gw = NetworkGateway()
        gw.authorize("GET", "https://www.nexusmods.com/skyrimspecialedition/mods/1234")
    """

    def __init__(self, policy: EgressPolicy | None = None) -> None:
        self._policy = policy or EgressPolicy()
        # Pin cache DNS ÚNICO por gateway: todos los SafeResolver de sus connectors
        # lo comparten (vía GatewayTCPConnector) → el rebinding queda cerrado para
        # todo el egress que use este gateway, no solo dentro de un connector.
        self._dns_pins: OrderedDict[tuple[str, int], list[dict[str, Any]]] = OrderedDict()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Strict pre-validation: prevents scheme smuggling and CRLF injection.
    # Runs BEFORE urlparse, which is historically lenient with malformed inputs.
    # Allow `[` as the first authority character for IPv6 literals per RFC 3986.
    _STRICT_PREFIX_RE = re.compile(r"^https?://(?:[a-zA-Z0-9]|\[)")

    async def authorize(self, method: str, url: str) -> None:
        """Validate *method* + *url* against the egress policy.

        Raises :class:`EgressViolationError` when the request is not permitted.

        H-01 hardening (Fixed): Rejects embedded control characters (CRLF) and
        triple-slash scheme smuggling before ``urlparse`` sees them.
        """
        method = method.upper()

        stripped = url.strip()
        if stripped != url:
            raise EgressViolationError("URL rejected: Leading or trailing whitespace not allowed.")

        # Block any embedded control characters, spaces, or non-ASCII (CRLF smuggling vector).
        if any(ord(c) <= 32 or ord(c) >= 127 for c in stripped):
            raise EgressViolationError(f"URL rejected: Contains control characters or whitespace: {url!r}")

        # Block triple slashes (e.g. https:///evil.com) and ensure it starts with an alphanumeric host.
        if not self._STRICT_PREFIX_RE.match(stripped):
            raise EgressViolationError(f"URL rejected: Must start with http(s):// followed by alphanumeric: {url!r}")

        try:
            parsed = urlparse(stripped)
        except ValueError as exc:
            # urlparse raises ValueError for malformed IPv6 literals (e.g. http://[bad).
            # Re-raise as EgressViolationError so callers see a consistent gateway error.
            raise EgressViolationError(f"URL rejected: Malformed authority (invalid IPv6 literal?): {url!r}") from exc
        hostname = (parsed.hostname or "").lower()

        if not hostname:
            raise EgressViolationError(f"URL has no hostname: {url}")

        # Check raw literal IPs just in case
        try:
            addr = ipaddress.ip_address(hostname)
            if self._policy.block_private_ips and _is_blocked_ip(addr):
                raise EgressViolationError(
                    f"Literal address {addr} is a private/loopback or non-routable IP "
                    "(multicast/reserved/unspecified/IPv4-mapped)"
                )
        except ValueError:
            pass

        self._check_scheme_allowed(parsed)
        self._check_host_allowed(hostname)
        self._check_method_allowed(method, hostname)
        self._check_telegram_path(hostname, parsed.path)

    async def request(
        self,
        method: str,
        url: str,
        session: aiohttp.ClientSession,
        max_redirects: int = 5,
        allowed_redirect_hosts: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        """Authorize and execute an HTTP request with redirect validation.

        DNS Pinning is handled automatically by GatewayTCPConnector.
        Redirect validation (H4): ``allow_redirects=False`` is enforced. Each redirect
        Location is re-authorized through the full egress policy before being followed.

        Args:
            allowed_redirect_hosts: Optional frozenset of hostnames (e.g.
                GITHUB_RELEASE_ASSET_REDIRECT_HOSTS) that are permitted as
                redirect *targets* only.  These hosts bypass the ALLOWED_HOSTS
                check but still undergo scheme and private-IP validation.
                The *initial* URL is always fully authorized regardless.
        """
        kwargs["allow_redirects"] = False

        current_url = url
        for hop in range(max_redirects + 1):
            # The initial URL always goes through full authorize().
            # Redirect hops to pre-approved CDN hosts skip the ALLOWED_HOSTS
            # check but still enforce scheme and private-IP policy.
            if hop > 0 and allowed_redirect_hosts is not None:
                parsed_redir = urlparse(current_url)
                redir_host = (parsed_redir.hostname or "").lower()
                if redir_host in allowed_redirect_hosts:
                    self._check_scheme_allowed(parsed_redir)
                    try:
                        addr = ipaddress.ip_address(redir_host)
                        if self._policy.block_private_ips and _is_blocked_ip(addr):
                            raise EgressViolationError(
                                f"Redirect target {addr} is a private/loopback or non-routable IP "
                                "(multicast/reserved/unspecified/IPv4-mapped)"
                            )
                    except ValueError:
                        pass
                    logger.debug("Redirect hop %d allowed via redirect-host list: %s", hop, redir_host)
                else:
                    await self.authorize(method, current_url)
            else:
                await self.authorize(method, current_url)

            parsed = urlparse(current_url)
            self._check_scheme_allowed(parsed)
            is_loopback = self._is_loopback_host(parsed.hostname or "")

            hop_kwargs = dict(kwargs)
            safe_timeout = aiohttp.ClientTimeout(total=45, connect=10)
            if "timeout" not in hop_kwargs:
                hop_kwargs["timeout"] = safe_timeout
            if is_loopback and parsed.scheme == "http":
                hop_kwargs["ssl"] = False

            try:
                response = await session.request(method, current_url, **hop_kwargs)
            except TimeoutError as _exc:
                logger.error("Timeout al contactar %s", current_url)
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
                # Release the redirect response body before issuing the next request.
                response.release()
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

    def _check_scheme_allowed(self, parsed: ParseResult) -> None:
        """Allow HTTPS everywhere, HTTP only for explicit loopback targets."""
        if parsed.scheme == "https":
            return
        if parsed.scheme == "http" and self._is_loopback_host(parsed.hostname or ""):
            return
        raise EgressViolationError(f"Insecure scheme '{parsed.scheme}' blocked: {parsed.geturl()}")

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
