"""Strategy for the `generate_animations` tool (Pandora behavior graphs).

The shared dispatcher HITL gate owns operator approval (Pandora is destructive: it
rewrites behavior graphs). This strategy just delegates to the lock-protected
``PandoraPipelineService``; it takes no payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.pandora_service import PandoraPipelineService


class GenerateAnimationsStrategy:
    name = "generate_animations"

    def __init__(self, service: PandoraPipelineService) -> None:
        self.service = service

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        # generate_animations no toma parámetros. Rechazar un payload con claves que
        # NO se honran evita una aprobación engañosa (p.ej. un ``dry_run=True`` que el
        # operador aprueba pero la estrategia ignora y corre la mutación real igual) —
        # Codex on #213.
        if payload_dict:
            raise ValueError(f"{self.name} no acepta parámetros; claves inesperadas: {sorted(payload_dict)}")

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        return "Generación de animaciones con Pandora (sin parámetros)."

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return await self.service.generate_animations()
