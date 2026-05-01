from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from pydantic import ValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

# Playwright and requests-html strictly banned locally in WSL2 per SRE (Cloudflare constraints).
from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.core.models import CircuitBreakerTrippedError
from sky_claw.antigravity.core.schemas import ModMetadata, ScrapingQuery

if TYPE_CHECKING:
    from sky_claw.antigravity.security.network_gateway import NetworkGateway

logger = logging.getLogger("SkyClaw.Scraper")


class ScraperAgent:
    def __init__(self, db: DatabaseAgent, gateway: NetworkGateway | None = None) -> None:
        self.db = db
        # NetworkGateway enforces the egress allow-list (SSRF protection).
        # All outbound requests in _api_request MUST route through it when provided.
        self._gateway = gateway
        self.nexus_api_key: str | None = None  # Loaded via secure config (e.g. keyring)
        self.max_failures: int = 3

    async def query_nexus(self, query: ScrapingQuery) -> ModMetadata:
        """Consulta Nexus Mods usando el esquema ScrapingQuery validado.

        Enrutador Híbrido: Intenta API, si falla o fuerza stealth, usa Playwright.
        Pasa por el Circuit Breaker.

        Args:
            query: ScrapingQuery con los parámetros validados de la consulta.

        Returns:
            ModMetadata con la información del mod consultado.
        """
        domain = "nexusmods.com"
        state = await self.db.get_circuit_breaker_state(domain)

        # 1. Evaluar Circuit Breaker.
        # time.time() is correct here: locked_until is persisted to SQLite and read
        # across process restarts.  time.monotonic() resets on each process start,
        # so comparing it against a stored value would keep the breaker locked forever.
        if time.time() < state["locked_until"]:
            logger.error("RCA: Circuit Breaker abierto para %s. Abortando para proteger IP local.", domain)
            raise CircuitBreakerTrippedError(f"Bloqueo activo hasta {state['locked_until']}")

        try:
            if not query.force_stealth and self.nexus_api_key:
                result = await self._api_request(query)
            else:
                result = await self._stealth_scrape(query)

            # DETECCIÓN DE ToS_Blocked - Manejo resiliente sin detener el flujo
            if result.get("source") == "ToS_Blocked":
                logger.warning(
                    "ToS_Blocked detectado para query '%s'. "
                    "Retornando metadata placeholder sin detener el flujo de LangGraph.",
                    query.query,
                )
                return ModMetadata(
                    mod_id=query.mod_id or 999999,
                    name=f"[ToS_Blocked] {query.query}",
                    version="0.0.0",
                    category="other",
                    author="N/A - ToS Blocked",
                    description=result.get("reason", "Scraping bloqueado por cumplimiento ToS Nexus Mods"),
                )

            return self._build_mod_metadata(result, query)

        except (TimeoutError, aiohttp.ClientError, ValueError, ValidationError) as e:
            # 2. RCA del fallo y actualización del Circuit Breaker
            new_failures = state["failures"] + 1
            lock_time = time.time() + (300 * new_failures) if new_failures >= self.max_failures else 0
            await self.db.update_circuit_breaker(domain, new_failures, lock_time)
            logger.warning("Fallo de extracción en %s. Fallos: %d. Error: %s", domain, new_failures, e)
            raise

    async def _api_request(
        self,
        query: ScrapingQuery,
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, Any]:
        """Realiza petición API REST estándar con reintentos exponenciales.

        Injects an ``aiohttp.ClientSession``; creates one per-call if not provided.

        Args:
            query: ScrapingQuery con los parámetros validados.
            session: Optional shared ClientSession (avoids per-call overhead).

        Returns:
            dict con los datos de respuesta del API.
        """
        nexus_id = query.mod_id or 0
        url = f"https://api.nexusmods.com/v1/games/skyrimspecialedition/mods/{nexus_id}.json"
        headers = {"apikey": self.nexus_api_key or "", "User-Agent": "SkyClaw/1.0"}

        async def _fetch(s: aiohttp.ClientSession) -> dict[str, Any]:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
                stop=stop_after_attempt(5),
                wait=wait_exponential(multiplier=2, min=2, max=30),
                reraise=True,
            ):
                with attempt:
                    if self._gateway is not None:
                        # Route through NetworkGateway to enforce egress allow-list
                        # and prevent SSRF.  gateway.request() validates the URL
                        # before forwarding and follows redirects safely.
                        resp = await self._gateway.request("GET", url, s, headers=headers)
                        resp.raise_for_status()
                        data: dict[str, Any] = await resp.json()
                    else:
                        async with s.get(url, headers=headers) as resp:
                            resp.raise_for_status()
                            data = await resp.json()
                    return {"status": "success", "source": "API", "data": data}
            return {}  # unreachable: reraise=True propagates on exhaustion

        if session is not None:
            return await _fetch(session)
        async with aiohttp.ClientSession() as own_session:
            return await _fetch(own_session)

    async def _stealth_scrape(self, query: ScrapingQuery) -> dict[str, Any]:
        """Método deshabilitado por cumplimiento ToS - Nexus Mods.

        El scraping evasivo ha sido permanentemente desactivado.
        Use la API oficial de Nexus Mods en su lugar.
        """
        logger.warning(
            "Acceso bloqueado por ToS: Scraping evasivo deshabilitado. El recurso no está disponible vía API."
        )
        return {
            "status": "error",
            "source": "ToS_Blocked",
            "data": None,
            "reason": "Scraping evasivo deshabilitado por cumplimiento ToS Nexus Mods",
        }

    def _build_mod_metadata(self, result: dict[str, Any], query: ScrapingQuery) -> ModMetadata:
        """Construye un objeto ModMetadata a partir del resultado de la consulta.

        Args:
            result: Diccionario con los datos obtenidos del API o scraping.
            query: ScrapingQuery original con los parámetros de búsqueda.

        Returns:
            ModMetadata validado con Pydantic.

        Raises:
            ValueError: Si los datos son insuficientes para construir ModMetadata.
        """
        if result.get("status") != "success" or not result.get("data"):
            raise ValueError(f"Consulta fallida: {result.get('reason', 'Unknown error')}")

        data: dict[str, Any] = result.get("data") or {}

        mod_id = data.get("nexus_id") or data.get("mod_id") or query.mod_id
        if not mod_id:
            raise ValueError("No se pudo determinar el mod_id")

        name: str = data.get("name") or data.get("title") or query.query
        version: str = data.get("version", "1.0.0")
        author: str = data.get("author", "Unknown")
        category: str = data.get("category", "other")
        dependencies: list[int] = data.get("dependencies", [])
        description: str | None = data.get("description")

        return ModMetadata(
            mod_id=int(mod_id),
            name=name,
            version=version,
            category=category,
            author=author,
            dependencies=dependencies,
            description=description,
        )
