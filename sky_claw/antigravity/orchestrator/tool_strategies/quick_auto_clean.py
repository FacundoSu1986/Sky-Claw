"""Strategy for the `quick_auto_clean` tool (SSEEdit QuickAutoClean).

The shared dispatcher HITL gate owns operator approval (cleaning rewrites the
official master plugins in place → destructive). This strategy delegates to the
lock-protected ``XEditPipelineService.quick_auto_clean``; it takes no payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.local.tools.xedit_service import XEditPipelineService


class QuickAutoCleanStrategy:
    name = "quick_auto_clean"

    def __init__(self, service: XEditPipelineService) -> None:
        self.service = service

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        # quick_auto_clean no toma parámetros: limpia los DLC oficiales sucios.
        # Rechazar un payload con claves que NO se honran evita una aprobación
        # engañosa (p.ej. un ``dry_run=True`` que el operador aprueba pero la
        # estrategia ignora y corre la mutación real igual) — Codex on #213.
        if payload_dict:
            raise ValueError(f"{self.name} no acepta parámetros; claves inesperadas: {sorted(payload_dict)}")

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        return "Limpieza QuickAutoClean de los DLC oficiales sucios (sin parámetros)."

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        return await self.service.quick_auto_clean()
