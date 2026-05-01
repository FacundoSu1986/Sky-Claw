"""Tests for sky_claw.antigravity.security.network_gateway."""

from __future__ import annotations

import pytest

from sky_claw.antigravity.security.network_gateway import (
    EgressPolicy,
    EgressViolationError,
    NetworkGateway,
)


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
    async def test_github_get_allowed(self, gw: NetworkGateway) -> None:
        # github.com is in the allow-list for GET (tool auto-install).
        await gw.authorize("GET", "https://github.com/some/repo")

    @pytest.mark.asyncio
    async def test_github_post_blocked(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="not allowed"):
            await gw.authorize("POST", "https://github.com/some/repo")

    @pytest.mark.asyncio
    async def test_empty_url_rejected(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolationError, match="no hostname"):
            await gw.authorize("GET", "")


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
