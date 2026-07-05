"""Tests for sky_claw.antigravity.security.network_gateway."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.antigravity.security.network_gateway import (
    EgressPolicy,
    EgressViolationError,
    NetworkGateway,
)
from sky_claw.config import GITHUB_RELEASE_ASSET_REDIRECT_HOSTS


class _GatewayResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.release = MagicMock()


@pytest.fixture()
def gw() -> NetworkGateway:
    """Gateway with private-IP checks disabled (avoids DNS in CI)."""
    policy = EgressPolicy(block_private_ips=False)
    return NetworkGateway(policy)


@pytest.fixture()
def gw_strict() -> NetworkGateway:
    """Gateway with full policy (including private-IP blocking)."""
    return NetworkGateway()


# ------------------------------------------------------------------
# Domain allow-list
# ------------------------------------------------------------------


class TestHostAllowList:
    @pytest.mark.asyncio
    async def test_nexus_www_allowed(self, gw: NetworkGateway) -> None:
        await gw.authorize("GET", "https://www.nexusmods.com/skyrimspecialedition/mods/1234")

    @pytest.mark.asyncio
    async def test_nexus_subdomain_allowed(self, gw: NetworkGateway) -> None:
        await gw.authorize("GET", "https://staticdelivery.nexusmods.com/file.7z")

    @pytest.mark.asyncio
    async def test_telegram_allowed(self, gw: NetworkGateway) -> None:
        await gw.authorize("POST", "https://api.telegram.org/bot123456:ABC/sendMessage")

    @pytest.mark.asyncio
    async def test_random_host_blocked(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="not in the allow-list"):
            await gw.authorize("GET", "https://evil.example.com/payload")

    @pytest.mark.asyncio
    async def test_github_com_blocked_h02(self, gw: NetworkGateway) -> None:
        # H-02: github.com removed from ALLOWED_HOSTS (was also in OUT_OF_SCOPE_HOSTS).
        # api.github.com remains allowed.
        with pytest.raises(EgressViolationError, match="not in the allow-list"):
            await gw.authorize("GET", "https://github.com/some/repo")

    @pytest.mark.asyncio
    async def test_github_api_get_allowed(self, gw: NetworkGateway) -> None:
        # api.github.com is still allowed for API access.
        await gw.authorize("GET", "https://api.github.com/repos/some/repo")

    @pytest.mark.asyncio
    async def test_empty_url_rejected(self, gw: NetworkGateway) -> None:
        # H-01: Empty URLs are now caught by strict pre-validation.
        with pytest.raises(EgressViolationError, match="URL rejected"):
            await gw.authorize("GET", "")

    @pytest.mark.asyncio
    async def test_malformed_ipv6_url_rejected_as_egress_violation(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="Malformed authority"):
            await gw.authorize("GET", "http://[::1")

    @pytest.mark.asyncio
    async def test_github_release_asset_cdn_is_not_general_egress(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="not in the allow-list"):
            await gw.authorize("GET", "https://objects.githubusercontent.com/github-release.zip")


# ------------------------------------------------------------------
# Redirect validation
# ------------------------------------------------------------------


class TestRedirectValidation:
    @pytest.mark.asyncio
    async def test_github_release_asset_api_redirect_to_configured_cdn_allowed(self, gw: NetworkGateway) -> None:
        asset_url = "https://api.github.com/repos/loot/loot/releases/assets/1001"
        cdn_url = "https://objects.githubusercontent.com/github-production-release-asset-2e65be/loot.zip"
        redirect_response = _GatewayResponse(302, {"Location": cdn_url})
        final_response = _GatewayResponse(200)
        session = MagicMock(spec=aiohttp.ClientSession)
        session.request = AsyncMock(side_effect=[redirect_response, final_response])

        response = await gw.request(
            "GET", asset_url, session, allowed_redirect_hosts=GITHUB_RELEASE_ASSET_REDIRECT_HOSTS
        )

        assert response is final_response
        assert [call.args[1] for call in session.request.await_args_list] == [asset_url, cdn_url]

    @pytest.mark.asyncio
    async def test_redirect_hop_response_is_closed_before_following(self, gw: NetworkGateway) -> None:
        asset_url = "https://api.github.com/repos/loot/loot/releases/assets/1001"
        cdn_url = "https://objects.githubusercontent.com/github-production-release-asset-2e65be/loot.zip"
        redirect_response = _GatewayResponse(302, {"Location": cdn_url})
        final_response = _GatewayResponse(200)
        session = MagicMock(spec=aiohttp.ClientSession)
        session.request = AsyncMock(side_effect=[redirect_response, final_response])

        response = await gw.request(
            "GET", asset_url, session, allowed_redirect_hosts=GITHUB_RELEASE_ASSET_REDIRECT_HOSTS
        )

        assert response is final_response
        redirect_response.release.assert_called_once_with()
        final_response.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_release_asset_api_redirect_to_unapproved_host_blocked(self, gw: NetworkGateway) -> None:
        asset_url = "https://api.github.com/repos/loot/loot/releases/assets/1001"
        redirect_response = _GatewayResponse(302, {"Location": "https://github.com/loot/loot/releases/download/x.zip"})
        session = MagicMock(spec=aiohttp.ClientSession)
        session.request = AsyncMock(return_value=redirect_response)

        with pytest.raises(EgressViolationError, match="not in the allow-list"):
            await gw.request("GET", asset_url, session, allowed_redirect_hosts=GITHUB_RELEASE_ASSET_REDIRECT_HOSTS)
        redirect_response.release.assert_called_once_with()


# ------------------------------------------------------------------
# Method restrictions
# ------------------------------------------------------------------


class TestMethodRestrictions:
    @pytest.mark.asyncio
    async def test_nexus_get_ok(self, gw: NetworkGateway) -> None:
        await gw.authorize("GET", "https://www.nexusmods.com/mods")

    @pytest.mark.asyncio
    async def test_nexus_post_blocked(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="not allowed"):
            await gw.authorize("POST", "https://www.nexusmods.com/api/upload")

    @pytest.mark.asyncio
    async def test_telegram_get_ok(self, gw: NetworkGateway) -> None:
        await gw.authorize("GET", "https://api.telegram.org/bot123/getUpdates")

    @pytest.mark.asyncio
    async def test_telegram_post_ok(self, gw: NetworkGateway) -> None:
        await gw.authorize("POST", "https://api.telegram.org/bot123/sendMessage")


# ------------------------------------------------------------------
# Telegram path prefix
# ------------------------------------------------------------------


class TestTelegramPathPrefix:
    @pytest.mark.asyncio
    async def test_valid_bot_path(self, gw: NetworkGateway) -> None:
        await gw.authorize("GET", "https://api.telegram.org/bot123/getMe")

    @pytest.mark.asyncio
    async def test_missing_bot_prefix(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="does not start with"):
            await gw.authorize("GET", "https://api.telegram.org/file/something")


# ------------------------------------------------------------------
# Private-IP blocking
# ------------------------------------------------------------------


class TestPrivateIPBlocking:
    @pytest.mark.asyncio
    async def test_public_http_scheme_rejected_by_authorize(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="Insecure scheme"):
            await gw.authorize("GET", "http://www.nexusmods.com/mods")

    @pytest.mark.asyncio
    async def test_loopback_http_scheme_allowed_when_policy_allows_host(self) -> None:
        policy = EgressPolicy(
            allowed_hosts=frozenset(["127.0.0.1"]),
            allowed_methods={"127.0.0.1": frozenset(["GET"])},
            block_private_ips=False,
        )
        gw = NetworkGateway(policy)
        await gw.authorize("GET", "http://127.0.0.1:8765/health")

    @pytest.mark.asyncio
    async def test_ipv6_loopback_http_scheme_allowed_when_policy_allows_host(self) -> None:
        policy = EgressPolicy(
            allowed_hosts=frozenset(["::1"]),
            allowed_methods={"::1": frozenset(["GET"])},
            block_private_ips=False,
        )
        gw = NetworkGateway(policy)
        await gw.authorize("GET", "http://[::1]:8765/health")

    @pytest.mark.asyncio
    async def test_loopback_literal_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "http://127.0.0.1:8080/data")

    @pytest.mark.asyncio
    async def test_private_10_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "http://10.0.0.1/data")

    @pytest.mark.asyncio
    async def test_private_192_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "http://192.168.1.1/data")

    @pytest.mark.asyncio
    async def test_link_local_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "http://169.254.0.1/x")

    # --- Hardening SSRF (P0.1): categorías que el filtro previo omitía ---

    @pytest.mark.asyncio
    async def test_ipv4_mapped_ipv6_loopback_blocked(self, gw_strict: NetworkGateway) -> None:
        """El bypass clásico: ``::ffff:127.0.0.1`` tiene ``is_loopback == False``
        en su forma IPv6, pero enruta a loopback. Debe normalizarse y bloquearse."""
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "https://[::ffff:127.0.0.1]/data")

    @pytest.mark.asyncio
    async def test_ipv4_mapped_ipv6_link_local_blocked(self, gw_strict: NetworkGateway) -> None:
        """``::ffff:169.254.169.254`` (metadata endpoint envuelto en IPv6)."""
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "https://[::ffff:169.254.169.254]/latest/meta-data")

    @pytest.mark.asyncio
    async def test_multicast_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "https://224.0.0.1/x")

    @pytest.mark.asyncio
    async def test_reserved_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "https://240.0.0.1/x")

    @pytest.mark.asyncio
    async def test_unspecified_blocked(self, gw_strict: NetworkGateway) -> None:
        """``0.0.0.0`` enruta a interfaces locales; debe bloquearse."""
        with pytest.raises(EgressViolationError, match="private/loopback"):
            await gw_strict.authorize("GET", "https://0.0.0.0/x")


class TestIsBlockedIP:
    """Unidad del helper de clasificación de IP (guarda de regresión SSRF)."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback IPv4
            "::1",  # loopback IPv6
            "10.0.0.1",  # privada
            "192.168.1.1",  # privada
            "169.254.0.1",  # link-local
            "::ffff:127.0.0.1",  # IPv4-mapped loopback (bypass)
            "::ffff:169.254.169.254",  # IPv4-mapped link-local (metadata)
            "::ffff:10.0.0.1",  # IPv4-mapped privada
            "224.0.0.1",  # multicast IPv4
            "ff02::1",  # multicast IPv6
            "240.0.0.1",  # reserved
            "0.0.0.0",  # unspecified IPv4
            "::",  # unspecified IPv6
        ],
    )
    def test_direcciones_peligrosas_bloqueadas(self, ip: str) -> None:
        import ipaddress

        from sky_claw.antigravity.security.network_gateway import _is_blocked_ip

        assert _is_blocked_ip(ipaddress.ip_address(ip)) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",  # público
            "1.1.1.1",  # público
            "9.9.9.9",  # público (Quad9)
            "2606:4700:4700::1111",  # IPv6 público (Cloudflare)
        ],
    )
    def test_direcciones_publicas_permitidas(self, ip: str) -> None:
        import ipaddress

        from sky_claw.antigravity.security.network_gateway import _is_blocked_ip

        assert _is_blocked_ip(ipaddress.ip_address(ip)) is False


class TestSafeResolverSSRF:
    """El bloqueo a nivel DNS (``SafeResolver.resolve``) es la barrera crítica:
    un hostname público que resuelve a una IP interna debe rechazarse."""

    @pytest.mark.asyncio
    async def test_resolver_bloquea_ipv4_mapped_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        from sky_claw.antigravity.security.network_gateway import SafeResolver

        resolver = SafeResolver(EgressPolicy())

        async def _fake_getaddrinfo(*_args, **_kwargs):
            # Simula un DNS que devuelve la IPv4 loopback envuelta en IPv6.
            return [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("::ffff:127.0.0.1", 443, 0, 0),
                )
            ]

        import asyncio

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _fake_getaddrinfo)

        with pytest.raises(EgressViolationError, match="private/loopback"):
            await resolver.resolve("rebind.evil.example", 443, socket.AF_INET6)


class TestSharedDNSPinCache:
    """El pin cache DNS compartido a nivel gateway cierra el gap de rebinding
    app-wide: todos los SafeResolver del mismo gateway comparten una caché, así
    que el primer host validado queda pineado para TODAS las conexiones."""

    @pytest.mark.asyncio
    async def test_shared_pin_cache_defeats_rebinding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        import socket
        from collections import OrderedDict

        from sky_claw.antigravity.security.network_gateway import SafeResolver

        shared: OrderedDict = OrderedDict()
        policy = EgressPolicy(block_private_ips=False)
        resolver_a = SafeResolver(policy, shared)
        resolver_b = SafeResolver(policy, shared)

        calls = {"n": 0}

        async def _fake_getaddrinfo(*_args, **_kwargs):
            calls["n"] += 1
            # 1ª resolución: IP buena. Un "rebind" posterior devolvería otra IP.
            ip = "93.184.216.34" if calls["n"] == 1 else "203.0.113.99"
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _fake_getaddrinfo)

        res_a = await resolver_a.resolve("cdn.example.test", 443)
        res_b = await resolver_b.resolve("cdn.example.test", 443)  # otro resolver, MISMA caché

        assert calls["n"] == 1  # getaddrinfo NO se volvió a llamar → rebinding derrotado
        assert res_a == res_b
        assert res_a[0]["host"] == "93.184.216.34"  # ambos ven la IP pineada

    def test_gateway_exposes_shared_dns_pins(self) -> None:
        from collections import OrderedDict

        from sky_claw.antigravity.security.network_gateway import SafeResolver

        gw = NetworkGateway()
        assert isinstance(gw._dns_pins, OrderedDict)
        r1 = SafeResolver(gw._policy, gw._dns_pins)
        r2 = SafeResolver(gw._policy, gw._dns_pins)
        assert r1._pinned is r2._pinned is gw._dns_pins

    @pytest.mark.asyncio
    async def test_standalone_resolver_owns_and_clears_cache(self) -> None:
        import socket

        from sky_claw.antigravity.security.network_gateway import SafeResolver

        resolver = SafeResolver(EgressPolicy())
        resolver._pinned[("h", 1, socket.AF_INET)] = [{"host": "1.2.3.4"}]
        await resolver.close()
        assert len(resolver._pinned) == 0  # caché propia → close() la limpia

    @pytest.mark.asyncio
    async def test_close_does_not_clear_shared_cache(self) -> None:
        import socket
        from collections import OrderedDict

        from sky_claw.antigravity.security.network_gateway import SafeResolver

        shared: OrderedDict = OrderedDict()
        key = ("h", 1, socket.AF_INET)
        shared[key] = [{"host": "1.2.3.4"}]
        resolver = SafeResolver(EgressPolicy(), shared)
        await resolver.close()
        assert key in shared  # caché compartida → NO se limpia al cerrar un connector

    @pytest.mark.asyncio
    async def test_pin_cache_scoped_by_address_family(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Con caché compartida, el pin key incluye ``family``: un pin IPv4 NO se
        sirve a un resolve que pidió AF_INET6 (Codex P2)."""
        import asyncio
        import socket
        from collections import OrderedDict

        from sky_claw.antigravity.security.network_gateway import SafeResolver

        shared: OrderedDict = OrderedDict()
        resolver = SafeResolver(EgressPolicy(block_private_ips=False), shared)

        families: list[int] = []

        async def _fake_getaddrinfo(host, port, *, family, type):
            families.append(family)
            if family == socket.AF_INET6:
                return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:4700:4700::1111", port, 0, 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _fake_getaddrinfo)

        v4 = await resolver.resolve("dual.example.test", 443, socket.AF_INET)
        v6 = await resolver.resolve("dual.example.test", 443, socket.AF_INET6)  # otra familia → NO usa el pin v4

        assert families == [socket.AF_INET, socket.AF_INET6]  # getaddrinfo llamado por cada familia
        assert v4[0]["host"] == "93.184.216.34"
        assert v6[0]["host"] == "2606:4700:4700::1111"

    @pytest.mark.asyncio
    async def test_network_context_reuses_gateway_across_initialize(self) -> None:
        """El pin cache app-wide no debe partirse en el arranque minimal→full:
        ``NetworkContext.initialize`` reusa el mismo gateway (Codex P1)."""
        from sky_claw.app_context import NetworkContext

        ctx = NetworkContext()
        try:
            await ctx.initialize("", None)  # minimal
            gw1, session1 = ctx.gateway, ctx.session
            await ctx.initialize("", None)  # full (2º llamado)
            assert ctx.gateway is gw1  # mismo gateway → una sola caché _dns_pins
            assert ctx.session is session1  # misma session de larga vida
        finally:
            await ctx.close()


# ------------------------------------------------------------------
# Dot-based domain matcher (_matching_pattern semantics)
# ------------------------------------------------------------------


class TestMatchingPattern:
    """Verify dot-based hostname matching semantics."""

    def make_gateway(self, allowed_hosts: list[str]) -> NetworkGateway:
        policy = EgressPolicy(
            allowed_hosts=frozenset(allowed_hosts),
            block_private_ips=False,
        )
        return NetworkGateway(policy)

    def test_wildcard_matches_subdomain(self) -> None:
        """*.nexusmods.com matches api.nexusmods.com"""
        gw = self.make_gateway(["*.nexusmods.com"])
        assert gw._matching_pattern("api.nexusmods.com") is not None

    def test_wildcard_matches_deep_subdomain(self) -> None:
        """*.nexusmods.com matches staticdelivery.nexusmods.com"""
        gw = self.make_gateway(["*.nexusmods.com"])
        assert gw._matching_pattern("staticdelivery.nexusmods.com") is not None

    def test_wildcard_does_not_match_base(self) -> None:
        """*.nexusmods.com does NOT match nexusmods.com (no subdomain)"""
        gw = self.make_gateway(["*.nexusmods.com"])
        assert gw._matching_pattern("nexusmods.com") is None

    def test_wildcard_does_not_match_superdomain(self) -> None:
        """*.nexusmods.com does NOT match api.nexusmods.com.evil.com"""
        gw = self.make_gateway(["*.nexusmods.com"])
        assert gw._matching_pattern("api.nexusmods.com.evil.com") is None

    def test_wildcard_does_not_match_unrelated_host(self) -> None:
        """*.nexusmods.com does NOT match evil-nexusmods-fake.com"""
        gw = self.make_gateway(["*.nexusmods.com"])
        assert gw._matching_pattern("evil-nexusmods-fake.com") is None

    def test_exact_match(self) -> None:
        """api.telegram.org matches exactly api.telegram.org"""
        gw = self.make_gateway(["api.telegram.org"])
        assert gw._matching_pattern("api.telegram.org") is not None

    def test_exact_no_match_subdomain(self) -> None:
        """api.telegram.org does NOT match x.api.telegram.org"""
        gw = self.make_gateway(["api.telegram.org"])
        assert gw._matching_pattern("x.api.telegram.org") is None

    def test_exact_no_match_different_host(self) -> None:
        """api.telegram.org does NOT match api.telegram.org.evil.com"""
        gw = self.make_gateway(["api.telegram.org"])
        assert gw._matching_pattern("api.telegram.org.evil.com") is None

    def test_case_insensitive_wildcard(self) -> None:
        """API.NEXUSMODS.COM matches *.nexusmods.com (DNS is case-insensitive per RFC 4343)"""
        gw = self.make_gateway(["*.nexusmods.com"])
        assert gw._matching_pattern("API.NEXUSMODS.COM") is not None

    def test_case_insensitive_exact(self) -> None:
        """API.TELEGRAM.ORG matches api.telegram.org"""
        gw = self.make_gateway(["api.telegram.org"])
        assert gw._matching_pattern("API.TELEGRAM.ORG") is not None

    def test_no_match_returns_none(self) -> None:
        """Returns None when no pattern matches"""
        gw = self.make_gateway(["*.nexusmods.com", "api.telegram.org"])
        assert gw._matching_pattern("evil.example.com") is None

    def test_bare_asterisk_does_not_wildcard_match(self) -> None:
        """A bare '*' pattern is treated as a literal, not a wildcard."""
        gw = self.make_gateway(["*"])
        # Should NOT match any real hostname
        assert gw._matching_pattern("nexusmods.com") is None

    def test_dotless_literal_matches_exactly(self) -> None:
        """A pattern without a dot matches only the exact string."""
        gw = self.make_gateway(["localhost"])
        assert gw._matching_pattern("localhost") is not None
        assert gw._matching_pattern("not-localhost") is None
