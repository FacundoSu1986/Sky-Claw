"""Strategy for the `query_mod_metadata` tool.

Replaces supervisor.py:244-249 (case "query_mod_metadata"). Owns its
ScrapingQuery validation; on invalid payload, pydantic.ValidationError
propagates (current behavior preserved).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.core.schemas import ScrapingQuery

if TYPE_CHECKING:
    from sky_claw.antigravity.scraper.scraper_agent import ScraperAgent


class QueryModMetadataStrategy:
    name = "query_mod_metadata"

    def __init__(self, scraper: ScraperAgent) -> None:
        self.scraper = scraper

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        query = ScrapingQuery(**payload_dict)
        result = await self.scraper.query_nexus(query)
        return result.model_dump()
