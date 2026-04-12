"""Async masterlist fetcher for LOOT/Nexus mod metadata.

All network I/O passes through :class:`NetworkGateway.request` to
enforce the egress allow-list.  Uses ``aiohttp`` exclusively – no
blocking ``requests`` calls.

A lightweight Circuit Breaker prevents cascading failures when the
Nexus API is persistently unreachable.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from sky_claw.security.network_gateway import NetworkGateway

logger = logging.getLogger(__name__)

NEXUS_MOD_API_URL = (
    "https://api.nexusmods.com/v1/games/skyrimspecialedition/mods/{mod_id}.json"
)

# Circuit breaker defaults.
_CB_FAILURE_THRESHOLD = 5
_CB_RECOVERY_TIMEOUT = 60  # seconds


class MasterlistFetchError(Exception):
    """Raised when a masterlist / mod-info fetch fails."""


class CircuitOpenError(MasterlistFetchError):
    """Raised when the circuit breaker is open and requests are blocked."""


class _CircuitBreaker:
    """Minimal circuit breaker: CLOSED → OPEN → HALF-OPEN → CLOSED.

    Parameters
    ----------
    failure_threshold:
        Consecutive failures that trip the breaker.
    recovery_timeout:
        Seconds to wait in OPEN state before allowing a probe request.
    """

    def __init__(
        self,
        failure_threshold: int = _CB_FAILURE_THRESHOLD,
        recovery_timeout: float = _CB_RECOVERY_TIMEOUT,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._state: str = "closed"  # closed | open | half-open
        logger.debug("Circuit breaker initialized in CLOSED state")

    def reset(self) -> None:
        """Force reset the breaker to CLOSED and clear failure counter."""
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state = "closed"
        logger.info("Circuit breaker has been manually reset to CLOSED")

    @property
    def state(self) -> str:
        """Return the current state, promoting OPEN → HALF-OPEN when the
        recovery timeout has elapsed."""
        if self._state == "open":
            if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                self._state = "half-open"
        return self._state

    def record_success(self) -> None:
        """Record a successful call – reset the breaker to CLOSED."""
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self) -> None:
        """Record a failed call – trip the breaker when threshold is reached."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            self._state = "open"
            logger.warning(
                "Circuit breaker tripped after %d consecutive failures",
                self._failure_count,
            )

    def allow_request(self) -> bool:
        """Return ``True`` if a request is currently allowed."""
        state = self.state  # may promote open → half-open
        if state == "closed":
            return True
        if state == "half-open":
            return True  # allow one probe
        return False


class MasterlistClient:
    """Async client for fetching mod metadata from Nexus Mods API.

    Parameters
    ----------
    gateway:
        ``NetworkGateway`` instance used to authorize and execute requests.
    api_key:
        Nexus Mods API key sent as ``apikey`` header.
    failure_threshold:
        Consecutive fetch failures that trip the circuit breaker.
    recovery_timeout:
        Seconds to wait in OPEN state before allowing a probe request.
    """

    def __init__(
        self,
        gateway: NetworkGateway,
        api_key: str,
        failure_threshold: int = _CB_FAILURE_THRESHOLD,
        recovery_timeout: float = _CB_RECOVERY_TIMEOUT,
    ) -> None:
        self._gw = gateway
        self._api_key = api_key
        self._cb = _CircuitBreaker(failure_threshold, recovery_timeout)
        self._cb.reset()  # Ensure clean state at start.
        logger.info("MasterlistClient initialized with fresh circuit breaker state")

    @property
    def circuit_state(self) -> str:
        """Expose the current circuit-breaker state for observability."""
        return self._cb.state

    async def fetch_mod_info(
        self,
        mod_id: int,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        """Fetch metadata for a single mod from Nexus Mods.

        Returns the parsed JSON response dict.

        Raises :class:`CircuitOpenError` if the breaker is open.
        Raises :class:`MasterlistFetchError` on HTTP errors.
        """
        if not self._cb.allow_request():
            raise CircuitOpenError(
                "Circuit breaker is open — Nexus API calls are temporarily blocked"
            )

        url = NEXUS_MOD_API_URL.format(mod_id=mod_id)
        headers = {"apikey": self._api_key}

        try:
            resp = await self._gw.request(
                "GET",
                url,
                session,
                headers=headers,
            )
            try:
                if resp.status != 200:
                    body = await resp.text()
                    raise MasterlistFetchError(
                        f"HTTP {resp.status} for mod {mod_id}: {body[:200]}"
                    )
                data: dict[str, Any] = await resp.json()
            finally:
                await resp.release()
        except MasterlistFetchError:
            self._cb.record_failure()
            raise
        except (aiohttp.ClientError, OSError, TimeoutError):
            self._cb.record_failure()
            raise

        self._cb.record_success()
        return data
