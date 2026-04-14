"""Tests for sky_claw.security.network_gateway."""

from __future__ import annotations

import pytest
from sky_claw.security.network_gateway import (
    EgressPolicy,
    EgressViolation,
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
        await gw.authorize(
            "GET", "https://www.nexusmods.com/skyrimspecialedition/mods/1234"
        )

    @pytest.mark.asyncio
    async def test_nexus_subdomain_allowed(self, gw: NetworkGateway) -> None:
        await gw.authorize("GET", "https://staticdelivery.nexusmods.com/file.7z")

    @pytest.mark.asyncio
    async def test_telegram_allowed(self, gw: NetworkGateway) -> None:
        await gw.authorize("POST", "https://api.telegram.org/bot123456:ABC/sendMessage")

    @pytest.mark.asyncio
    async def test_random_host_blocked(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolation, match="not in the allow-list"):
            await gw.authorize("GET", "https://evil.example.com/payload")

    @pytest.mark.asyncio
    async def test_github_get_allowed(self, gw: NetworkGateway) -> None:
        # github.com is in the allow-list for GET (tool auto-install).
        await gw.authorize("GET", "https://github.com/some/repo")

    @pytest.mark.asyncio
    async def test_github_post_blocked(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolation, match="not allowed"):
            await gw.authorize("POST", "https://github.com/some/repo")

    @pytest.mark.asyncio
    async def test_empty_url_rejected(self, gw: NetworkGateway) -> None:
        with pytest.raises(EgressViolation, match="no hostname"):
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
        with pytest.raises(EgressViolation, match="not allowed"):
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
        with pytest.raises(EgressViolation, match="does not start with"):
            await gw.authorize("GET", "https://api.telegram.org/file/something")


# ------------------------------------------------------------------
# Private-IP blocking
# ------------------------------------------------------------------


class TestPrivateIPBlocking:
    @pytest.mark.asyncio
    async def test_loopback_literal_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolation, match="private/loopback"):
            await gw_strict.authorize("GET", "http://127.0.0.1:8080/data")

    @pytest.mark.asyncio
    async def test_private_10_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolation, match="private/loopback"):
            await gw_strict.authorize("GET", "http://10.0.0.1/data")

    @pytest.mark.asyncio
    async def test_private_192_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolation, match="private/loopback"):
            await gw_strict.authorize("GET", "http://192.168.1.1/data")

    @pytest.mark.asyncio
    async def test_link_local_blocked(self, gw_strict: NetworkGateway) -> None:
        with pytest.raises(EgressViolation, match="private/loopback"):
            await gw_strict.authorize("GET", "http://169.254.0.1/x")
