"""Strategy read-only de la Fase A del grass cache (PR-5) — SIN gate HITL.

Diagnóstico previo al precache (Stage 8 del SOP): worldspaces con pasto para
``Only-pregenerate-world-spaces`` + records GRAS con bounds nulos (la causa del
fallo silencioso de NGIO). No muta nada: patrón ``preview_chain`` (wrapping de
errores en el registro, sin ``HitlGateMiddleware``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from sky_claw.local.tools.grass_cache_service import GrassCacheService


class AnalyzeGrassPrerequisitesParams(BaseModel):
    """Payload del diagnóstico: plugins a cargar en xEdit."""

    model_config = ConfigDict(strict=True)

    plugins: list[str] = Field(min_length=1)
    #: El scan de LAND excede los 120s default de xEdit en load orders reales.
    timeout: int | None = None


class AnalyzeGrassPrerequisitesStrategy:
    """Adaptador delgado servicio→dispatcher (read-only, sin gate)."""

    name = "analyze_grass_prerequisites"

    def __init__(self, service: GrassCacheService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        params = AnalyzeGrassPrerequisitesParams(**payload_dict)
        return await self.service.analyze_prerequisites(params.plugins, timeout=params.timeout)
