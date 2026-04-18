"""Reddit knowledge resolver — aislado, ToS-compliant, async-first.

Ver plan aprobado: dar al orquestador Sky-Claw un canal hacia conocimiento
comunitario (issues conocidos) de mods de Skyrim sin violar los ToS de Reddit.

Invariantes:
    * Solo endpoint JSON público de Reddit (sin scraping evasivo).
    * User-Agent descriptivo obligatorio en formato ToS.
    * Rate limit estricto via ventana deslizante (<=30 RPM por defecto).
    * Caché in-memory atado al ciclo de vida del resolver.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from types import TracebackType
from typing import Any, Callable
from urllib.parse import quote_plus

import aiohttp
from sky_claw.security.network_gateway import (
    EgressViolation,
    NetworkGateway,
    NetworkGatewayTimeout,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("SkyClaw.Scraper.Reddit")

_USER_AGENT_RE = re.compile(
    r"^[A-Za-z0-9_\-]+:[A-Za-z0-9_.\-]+:v?\d+[\w.\-]*\s+\(by\s+/u/[A-Za-z0-9_\-]+\)$"
)
_MOD_NAME_MAX = 100
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_SUBREDDITS: tuple[str, ...] = ("skyrimmods", "skyrimse")
_DEFAULT_WINDOW_SECONDS = 60.0
_DEFAULT_SEARCH_LIMIT = 10
_DEFAULT_TIMEOUT = 15.0
_SNIPPET_MAX = 500
_RESULT_SENTINEL = object()


class _TransientHTTPError(Exception):
    """Marker para fallos 5xx/timeouts que tenacity debe reintentar."""


class RedditPostData(BaseModel):
    """Esquema de datos para una publicación de Reddit."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    title: str = ""
    score: int = 0
    subreddit: str = ""
    permalink: str = ""
    selftext: str = ""


class RedditChild(BaseModel):
    """Envoltorio 't3' de Reddit."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    kind: str
    data: RedditPostData


class RedditListingData(BaseModel):
    """Cuerpo de un listado de Reddit."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    children: list[RedditChild] = Field(default_factory=list)


class RedditListing(BaseModel):
    """Respuesta completa de un listado de Reddit."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    kind: str
    data: RedditListingData


class RedditClientConfig(BaseModel):
    """Configuración inmutable del cliente Reddit (directiva D6)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    user_agent: str = Field(..., min_length=1)
    rpm_limit: int = Field(default=30, ge=1, le=60)
    subreddits: tuple[str, ...] = Field(default=_DEFAULT_SUBREDDITS, min_length=1)
    window_seconds: float = Field(default=_DEFAULT_WINDOW_SECONDS, gt=0.0)
    search_limit: int = Field(default=_DEFAULT_SEARCH_LIMIT, ge=1, le=25)
    timeout_seconds: float = Field(default=_DEFAULT_TIMEOUT, gt=0.0)

    @field_validator("user_agent")
    @classmethod
    def _validate_user_agent(cls, value: str) -> str:
        stripped = value.strip()
        if not _USER_AGENT_RE.match(stripped):
            raise ValueError(
                "user_agent must follow Reddit ToS format "
                "'script:app_name:version (by /u/username)', "
                f"got: {value!r}"
            )
        return stripped

    @field_validator("subreddits")
    @classmethod
    def _validate_subreddits(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(
            s.strip().removeprefix("r/").removeprefix("/").lower()
            for s in value
            if s.strip()
        )
        if not cleaned:
            raise ValueError("subreddits must contain at least one non-empty entry")
        return cleaned


class RedditKnowledgeResolver:
    """Resolver asíncrono de conocimiento comunitario Reddit para mods Skyrim.

    Uso:
        async with RedditKnowledgeResolver(user_agent="script:sky_claw:v0.1.0 (by /u/me)") as r:
            report = await r.search_known_issues("SkyUI")
    """

    _SEARCH_URL = "https://www.reddit.com/search.json"

    def __init__(
        self,
        user_agent: str,
        *,
        subreddits: tuple[str, ...] = ("skyrimmods",),
        rpm_limit: int = 60,
        window_seconds: float = 60.0,
        search_limit: int = 10,
        timeout_seconds: float = 15.0,
        session: aiohttp.ClientSession | None = None,
        gateway: NetworkGateway,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = RedditClientConfig(
            user_agent=user_agent,
            rpm_limit=rpm_limit,
            subreddits=subreddits,
            window_seconds=window_seconds,
            search_limit=search_limit,
            timeout_seconds=timeout_seconds,
        )
        self._session = session
        self._owns_session = session is None
        self._gateway = gateway
        self._clock_fn = clock_fn

        self._cache: dict[str, str] = {}
        self._cache_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[str]] = {}

        self._rate_lock = asyncio.Lock()
        self._rate_window: deque[float] = deque()

        self._closed = False

    async def __aenter__(self) -> RedditKnowledgeResolver:
        await self._ensure_session()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if (
            self._owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()
        async with self._cache_lock:
            self._cache.clear()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self._config.user_agent},
                timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
            )
            self._owns_session = True
        return self._session

    def _validate_mod_name(self, mod_name: str) -> str:
        if not isinstance(mod_name, str):
            raise ValueError(f"mod_name must be str, got {type(mod_name).__name__}")
        stripped = mod_name.strip()
        if not stripped:
            raise ValueError("mod_name must be non-empty")
        if len(stripped) > _MOD_NAME_MAX:
            raise ValueError(f"mod_name exceeds {_MOD_NAME_MAX} characters")
        if _CONTROL_CHAR_RE.search(stripped):
            raise ValueError("mod_name contains control characters")
        return stripped

    async def _acquire_slot(self) -> None:
        async with self._rate_lock:
            now = self._clock_fn()
            window = self._config.window_seconds
            while self._rate_window and (now - self._rate_window[0]) >= window:
                self._rate_window.popleft()
            if len(self._rate_window) >= self._config.rpm_limit:
                wait_for = window - (now - self._rate_window[0])
                if wait_for > 0:
                    logger.debug(
                        "Rate limit reached; sleeping",
                        extra={"wait_seconds": round(wait_for, 3)},
                    )
                    await asyncio.sleep(wait_for)
                now = self._clock_fn()
                while self._rate_window and (now - self._rate_window[0]) >= window:
                    self._rate_window.popleft()
            self._rate_window.append(self._clock_fn())

    async def search_known_issues(self, mod_name: str) -> str:
        if self._closed:
            raise RuntimeError("RedditKnowledgeResolver is closed")

        cleaned = self._validate_mod_name(mod_name)
        cache_key = cleaned.lower()
        started = time.perf_counter()

        async with self._cache_lock:
            if cache_key in self._cache:
                logger.info(
                    "reddit.cache_hit",
                    extra={"mod_name": cleaned, "cache_hit": True},
                )
                return self._cache[cache_key]
            pending = self._inflight.get(cache_key)
            if pending is None:
                loop = asyncio.get_running_loop()
                pending = loop.create_future()
                self._inflight[cache_key] = pending
                own_request = True
            else:
                own_request = False

        if not own_request:
            return await pending

        result = _RESULT_SENTINEL
        try:
            try:
                result = await self._fetch_and_format(cleaned)
                async with self._cache_lock:
                    self._cache[cache_key] = result
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                EgressViolation,
                NetworkGatewayTimeout,
                _TransientHTTPError,
            ) as exc:
                logger.warning(
                    "reddit.search_failed",
                    extra={"mod_name": cleaned, "error": repr(exc)},
                )
                result = f"Reddit lookup unavailable for '{cleaned}'."
        finally:
            async with self._cache_lock:
                inflight = self._inflight.pop(cache_key, None)
                if inflight is not None and not inflight.done():
                    if result is not _RESULT_SENTINEL:
                        inflight.set_result(result)
                    else:
                        inflight.cancel()

        final_result = result if result is not _RESULT_SENTINEL else ""
        logger.info(
            "reddit.search_completed",
            extra={
                "mod_name": cleaned,
                "cache_hit": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return str(final_result)

    async def _fetch_and_format(self, mod_name: str) -> str:
        posts = await self._search(mod_name)
        if not posts:
            return f"No known issues found for '{mod_name}'."
        return self._format_posts(mod_name, posts)

    async def _search(self, mod_name: str) -> list[dict[str, Any]]:
        await self._acquire_slot()
        subreddit_filter = " OR ".join(
            f"subreddit:{s}" for s in self._config.subreddits
        )
        query = f"{mod_name} ({subreddit_filter})"
        params = {
            "q": query,
            "sort": "relevance",
            "limit": str(self._config.search_limit),
            "t": "year",
            "raw_json": "1",
        }
        url = f"{self._SEARCH_URL}?{self._encode_params(params)}"
        session = await self._ensure_session()

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=10),
                retry=retry_if_exception_type(_TransientHTTPError),
                reraise=True,
            ):
                with attempt:
                    return await self._execute_request(session, url)
        except RetryError as exc:
            raise _TransientHTTPError("exhausted retries") from exc
        return []

    async def _execute_request(
        self, session: aiohttp.ClientSession, url: str
    ) -> list[RedditPostData]:
        resp_cm = await self._gateway.request("GET", url, session)

        async with resp_cm as response:
            status = response.status
            if status == 200:
                payload = await response.json()
                return self._extract_posts(payload)
            if status == 429:
                retry_after = response.headers.get("Retry-After", "unknown")
                logger.warning(
                    "reddit.rate_limited",
                    extra={"retry_after": retry_after, "url": url},
                )
                return []
            if 500 <= status < 600:
                raise _TransientHTTPError(f"upstream {status}")
            logger.warning(
                "reddit.http_error",
                extra={"status": status, "url": url},
            )
            return []

    @staticmethod
    def _encode_params(params: dict[str, str]) -> str:
        return "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())

    @staticmethod
    def _extract_posts(payload: Any) -> list[RedditPostData]:
        try:
            listing = RedditListing.model_validate(payload)
            return [child.data for child in listing.data.children]
        except Exception:
            logger.warning(
                "reddit.invalid_payload",
                extra={"payload_preview": str(payload)[:200]},
            )
            return []

    def _format_posts(self, mod_name: str, posts: list[RedditPostData]) -> str:
        lines = [f"Reddit findings for '{mod_name}':"]
        for post in posts:
            title = post.title.strip() or "(untitled)"
            score = post.score
            subreddit = post.subreddit or "?"
            permalink = post.permalink
            url = f"https://www.reddit.com{permalink}" if permalink else ""
            snippet_raw = post.selftext.strip()
            snippet = snippet_raw[:_SNIPPET_MAX]
            if len(snippet_raw) > _SNIPPET_MAX:
                snippet += "..."
            line = f"- [r/{subreddit} score={score}] {title}"
            if url:
                line += f" ({url})"
            if snippet:
                line += f"\n    {snippet}"
            lines.append(line)
        return "\n".join(lines)
