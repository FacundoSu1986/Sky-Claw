"""Strategy destructiva del precache de grass (PR-5) — gate HITL obligatorio.

Lanza el ritual completo (Fases B→D): perfil clonado + mod de config +
crash-loop de SkyrimSE.exe durante horas. Es una operación de larga duración
que muta el árbol MO2, así que:

- vive en ``DESTRUCTIVE_TOOL_PATTERNS`` (gate único, PR #173);
- ``validate_for_approval`` corta payloads malformados ANTES de molestar al
  operador (worldspaces vacío escanearía TODO el load order);
- ``describe_for_approval`` resume lo que el humano aprueba: worldspaces, mods
  a desactivar, presupuestos, y — crítico — si se saltea el guard Stage 5→8.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sky_claw.local.tools.grass_cache_service import GenerateGrassCacheParams

if TYPE_CHECKING:
    from sky_claw.local.tools.grass_cache_service import GrassCacheService


class GenerateGrassCacheStrategy:
    """Adaptador delgado servicio→dispatcher con capacidades HITL."""

    name = "generate_grass_cache"

    def __init__(self, service: GrassCacheService) -> None:
        self.service = service

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        """Levanta si el payload es inválido — sin prompt HITL en vano."""
        self._parse_params(payload_dict)

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        """Resumen operador-legible de lo que se aprueba (sin claves sensibles)."""
        params = self._parse_params(payload_dict)
        partes = [
            f"worldspaces={params.worldspaces!r}",
            f"conflicting_mods={params.conflicting_mods!r}",
        ]
        if params.max_runtime_s is not None:
            partes.append(f"max_runtime_s={params.max_runtime_s!r}")
        if params.max_restarts is not None:
            partes.append(f"max_restarts={params.max_restarts!r}")
        if params.force_stage_guard:
            # Un bypass del guard Stage 5→8 jamás pasa desapercibido al humano.
            partes.append("force_stage_guard=True (SALTEA el guard LOOT→grass)")
        return ", ".join(partes)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return await self.service.generate(payload_dict)

    @staticmethod
    def _parse_params(payload_dict: dict[str, Any]) -> GenerateGrassCacheParams:
        return GenerateGrassCacheParams(**payload_dict)
