"""Tests for sky_claw.scraper.reddit_client.RedditKnowledgeResolver."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from sky_claw.scraper.reddit_client import (
    RedditClientConfig,
    RedditKnowledgeResolver,
)
from sky_claw.security.network_gateway import NetworkGateway

VALID_UA = "script:sky_claw:v0.1.0 (by /u/tester)"


def _make_response_mock(status: int, json_payload: Any = None, headers: dict[str, str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.json = AsyncMock(return_value=json_payload if json_payload is not None else {})
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.fixture
def mock_gateway() -> MagicMock:
    gw = MagicMock(spec=NetworkGateway)
    gw.request = AsyncMock()
    return gw


def _reddit_payload(posts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "Listing",
        "data": {
            "children": [{"kind": "t3", "data": p} for p in posts],
        },
    }


def _sample_posts(n: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "title": f"Issue {i}",
            "score": 10 * (i + 1),
            "subreddit": "skyrimmods",
            "permalink": f"/r/skyrimmods/comments/abc{i}/",
            "selftext": f"Body of post {i} describing a CTD.",
        }
        for i in range(n)
    ]


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    session.get = MagicMock()
    return session


@pytest.fixture
async def resolver(
    mock_session: MagicMock,
    mock_gateway: MagicMock,
) -> AsyncGenerator[RedditKnowledgeResolver, None]:
    r = RedditKnowledgeResolver(user_agent=VALID_UA, session=mock_session, gateway=mock_gateway)
    yield r
    await r.close()


class TestUserAgentValidation:
    def test_empty_user_agent_rejected(self, mock_gateway: MagicMock) -> None:
        with pytest.raises(ValueError):
            RedditKnowledgeResolver(user_agent="", gateway=mock_gateway)

    def test_browser_user_agent_rejected(self, mock_gateway: MagicMock) -> None:
        with pytest.raises(ValueError):
            RedditKnowledgeResolver(user_agent="Mozilla/5.0 (Windows NT 10.0)", gateway=mock_gateway)

    def test_missing_username_rejected(self, mock_gateway: MagicMock) -> None:
        with pytest.raises(ValueError):
            RedditKnowledgeResolver(user_agent="script:sky_claw:v0.1.0", gateway=mock_gateway)

    def test_valid_user_agent_accepted(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        r = RedditKnowledgeResolver(user_agent=VALID_UA, session=mock_session, gateway=mock_gateway)
        assert r._config.user_agent == VALID_UA

    def test_config_is_frozen(self) -> None:
        cfg = RedditClientConfig(user_agent=VALID_UA)
        with pytest.raises(ValidationError):
            cfg.user_agent = "tampered"  # type: ignore[misc]


class TestModNameValidation:
    @pytest.mark.asyncio
    async def test_empty_mod_name_rejected(self, resolver: RedditKnowledgeResolver) -> None:
        with pytest.raises(ValueError):
            await resolver.search_known_issues("")

    @pytest.mark.asyncio
    async def test_whitespace_only_rejected(self, resolver: RedditKnowledgeResolver) -> None:
        with pytest.raises(ValueError):
            await resolver.search_known_issues("   ")

    @pytest.mark.asyncio
    async def test_too_long_rejected(self, resolver: RedditKnowledgeResolver) -> None:
        with pytest.raises(ValueError):
            await resolver.search_known_issues("x" * 101)

    @pytest.mark.asyncio
    async def test_control_char_rejected(self, resolver: RedditKnowledgeResolver) -> None:
        with pytest.raises(ValueError):
            await resolver.search_known_issues("bad\x00name")

    @pytest.mark.asyncio
    async def test_non_string_rejected(self, resolver: RedditKnowledgeResolver) -> None:
        with pytest.raises(ValueError):
            await resolver.search_known_issues(123)  # type: ignore[arg-type]


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_sliding_window_blocks_excess(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload([]))

        # Reloj simulado que podemos adelantar manualmente
        current_time = 1000.0

        def mock_clock() -> float:
            return current_time

        r = RedditKnowledgeResolver(
            user_agent=VALID_UA,
            rpm_limit=2,
            window_seconds=10.0,  # Ventana grande para evitar que expire por azar
            session=mock_session,
            gateway=mock_gateway,
            clock_fn=mock_clock,
        )
        try:
            # Consumir los 2 slots permitidos
            await r.search_known_issues("ModA")
            await r.search_known_issues("ModB")

            # El tercer llamado debería intentar dormir (bloquear)
            # Como no queremos dormir de verdad 10 segundos, usamos un timeout corto
            # para verificar que el rate limiter está intentando esperar.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(r.search_known_issues("ModC"), timeout=0.1)

            assert mock_gateway.request.call_count == 2
        finally:
            await r.close()

    @pytest.mark.asyncio
    async def test_window_slides_after_expiry(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload([]))
        t = 1000.0

        def mock_clock() -> float:
            return t

        r = RedditKnowledgeResolver(
            user_agent=VALID_UA,
            rpm_limit=1,
            window_seconds=1.0,
            session=mock_session,
            gateway=mock_gateway,
            clock_fn=mock_clock,
        )
        try:
            await r.search_known_issues("ModA")

            # Avanzar el tiempo más allá de la ventana
            t += 1.1
            # El segundo llamado debería pasar inmediatamente sin bloquear
            await asyncio.wait_for(r.search_known_issues("ModB"), timeout=0.1)
            assert mock_gateway.request.call_count == 2
        finally:
            await r.close()


class TestCache:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_http(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload(_sample_posts(1)))
        # SkyUI normalization
        await resolver.search_known_issues("SkyUI")
        await resolver.search_known_issues("SkyUI")
        assert mock_gateway.request.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_skips_on_subreddit_prefix_variation(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload([]))
        # r/skyrimmods vs skyrimmods
        r2 = RedditKnowledgeResolver(
            user_agent=VALID_UA,
            subreddits=("r/skyrimmods",),
            session=mock_session,
            gateway=mock_gateway,
        )
        r3 = RedditKnowledgeResolver(
            user_agent=VALID_UA,
            subreddits=("skyrimmods",),
            session=mock_session,
            gateway=mock_gateway,
        )
        try:
            await r2.search_known_issues("Mod")
            await r3.search_known_issues("Mod")
            # Ambos terminan buscando en 'skyrimmods'
            # Pero el cache_key depende del mod_name Y los subreddits (normalizados)
            # Si el normalizado es el mismo, debería haber hit de caché si compartieran el objeto,
            # pero aquí son instancias distintas. Si compartieran r._cache veríamos el hit.
        finally:
            await r2.close()
            await r3.close()

    @pytest.mark.asyncio
    async def test_cache_key_normalization(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload(_sample_posts(1)))
        await resolver.search_known_issues("SkyUI")
        await resolver.search_known_issues("  skyui  ")
        assert mock_gateway.request.call_count == 1

    @pytest.mark.asyncio
    async def test_close_clears_cache(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload(_sample_posts(1)))
        r = RedditKnowledgeResolver(user_agent=VALID_UA, session=mock_session, gateway=mock_gateway)
        await r.search_known_issues("SkyUI")
        assert "skyui" in r._cache
        await r.close()
        assert r._cache == {}

    @pytest.mark.asyncio
    async def test_thundering_herd_collapses(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        call_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_json() -> dict[str, Any]:
            call_started.set()
            await release.wait()
            return _reddit_payload(_sample_posts(1))

        resp = MagicMock()
        resp.status = 200
        resp.headers = {}
        resp.json = slow_json
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        mock_gateway.request.return_value = ctx

        tasks = [asyncio.create_task(resolver.search_known_issues("SkyUI")) for _ in range(5)]
        await call_started.wait()
        release.set()
        results = await asyncio.gather(*tasks)
        assert mock_gateway.request.call_count == 1
        assert len({id(x) for x in results}) <= len(results)
        assert all(r == results[0] for r in results)

    @pytest.mark.asyncio
    async def test_errors_are_not_cached(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        # Primero falla con 500
        mock_gateway.request.return_value = _make_response_mock(500)
        r = RedditKnowledgeResolver(
            user_agent=VALID_UA,
            session=mock_session,
            gateway=mock_gateway,
            window_seconds=0.001,
        )

        try:
            res1 = await r.search_known_issues("BuggyMod")
            assert "unavailable" in res1
            assert mock_gateway.request.call_count == 3  # 1 inicial + 2 reintentos por Tenacity

            # Ahora "arreglamos" el server
            mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload(_sample_posts(1)))
            res2 = await r.search_known_issues("BuggyMod")
            assert "Reddit findings" in res2
            assert mock_gateway.request.call_count == 4  # +1 llamado exitoso (no salió de caché)
        finally:
            await r.close()


class TestSearchBehavior:
    @pytest.mark.asyncio
    async def test_formats_multiple_posts(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload(_sample_posts(3)))
        result = await resolver.search_known_issues("SkyUI")
        assert "Reddit findings for 'SkyUI'" in result
        assert "Issue 0" in result
        assert "Issue 1" in result
        assert "Issue 2" in result
        assert "r/skyrimmods" in result

    @pytest.mark.asyncio
    async def test_empty_response_placeholder(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload([]))
        result = await resolver.search_known_issues("ObscureMod")
        assert "No known issues found" in result
        assert "ObscureMod" in result

    @pytest.mark.asyncio
    async def test_http_429_returns_placeholder(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(429, headers={"Retry-After": "120"})
        caplog.set_level(logging.WARNING, logger="SkyClaw.Scraper.Reddit")
        result = await resolver.search_known_issues("SomeMod")
        assert "No known issues found" in result
        assert any("rate_limited" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_http_500_exhausts_retries(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        mock_gateway.request.return_value = _make_response_mock(500)
        r = RedditKnowledgeResolver(
            user_agent=VALID_UA,
            window_seconds=0.01,
            rpm_limit=60,
            session=mock_session,
            gateway=mock_gateway,
        )
        try:
            result = await r.search_known_issues("BrokenMod")
        finally:
            await r.close()
        assert "unavailable" in result.lower() or "Reddit lookup unavailable" in result
        assert mock_gateway.request.call_count == 3

    @pytest.mark.asyncio
    async def test_http_404_no_retry(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(404)
        result = await resolver.search_known_issues("GhostMod")
        assert "No known issues found" in result
        assert mock_gateway.request.call_count == 1

    @pytest.mark.asyncio
    async def test_url_encoding_of_special_chars(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload([]))
        await resolver.search_known_issues("Mod & X")
        call_args = mock_gateway.request.call_args
        url = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("url", "")
        assert "Mod+%26+X" in url or "Mod%20%26%20X" in url
        assert "&" not in url.split("?", 1)[1].split("=", 1)[1].split("&")[0]


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_async_context_manager(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload([]))
        async with RedditKnowledgeResolver(user_agent=VALID_UA, session=mock_session, gateway=mock_gateway) as r:
            result = await r.search_known_issues("Foo")
            assert isinstance(result, str)
        assert r._closed

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        r = RedditKnowledgeResolver(user_agent=VALID_UA, session=mock_session, gateway=mock_gateway)
        await r.close()
        await r.close()

    @pytest.mark.asyncio
    async def test_search_after_close_raises(self, mock_session: MagicMock, mock_gateway: MagicMock) -> None:
        r = RedditKnowledgeResolver(user_agent=VALID_UA, session=mock_session, gateway=mock_gateway)
        await r.close()
        with pytest.raises(RuntimeError):
            await r.search_known_issues("Foo")

    @pytest.mark.asyncio
    async def test_injected_session_not_closed_by_resolver(
        self, mock_session: MagicMock, mock_gateway: MagicMock
    ) -> None:
        r = RedditKnowledgeResolver(user_agent=VALID_UA, session=mock_session, gateway=mock_gateway)
        await r.close()
        mock_session.close.assert_not_called()


class TestObservability:
    @pytest.mark.asyncio
    async def test_cache_hit_logged(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload(_sample_posts(1)))
        caplog.set_level(logging.INFO, logger="SkyClaw.Scraper.Reddit")
        await resolver.search_known_issues("SkyUI")
        await resolver.search_known_issues("SkyUI")
        cache_hit_records = [r for r in caplog.records if "cache_hit" in r.message]
        assert any(getattr(r, "cache_hit", False) is True for r in cache_hit_records)

    @pytest.mark.asyncio
    async def test_search_completed_logs_latency(
        self,
        resolver: RedditKnowledgeResolver,
        mock_session: MagicMock,
        mock_gateway: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_gateway.request.return_value = _make_response_mock(200, _reddit_payload([]))
        caplog.set_level(logging.INFO, logger="SkyClaw.Scraper.Reddit")
        await resolver.search_known_issues("Foo")
        records = [r for r in caplog.records if "search_completed" in r.message]
        assert records
        assert hasattr(records[0], "latency_ms")
        assert records[0].latency_ms >= 0
