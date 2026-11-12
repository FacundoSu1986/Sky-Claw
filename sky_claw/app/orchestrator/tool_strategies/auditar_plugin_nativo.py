"""Strategy read-only ``auditar_plugin_nativo`` (xEdit Bridge, ADR 0018) — SIN gate HITL.

Corre el chequeo nativo de SSEEdit sobre la copia ya admitida del plugin en el laboratorio del
bridge. No muta nada de Sky-Claw ni de MO2: patrón ``analyze_grass_prerequisites`` (wrapping de
errores en el registro, sin ``HitlGateMiddleware``). Hermana LLM:
``sky_claw/app/agent/tools/xedit_bridge_tools.py::auditar_plugin_nativo``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from sky_claw.local.tools.xedit_bridge_client import PATRON_PLUGIN

if TYPE_CHECKING:
    from sky_claw.local.tools.xedit_bridge_service import XEditBridgeService


class AuditarPluginNativoParams(BaseModel):
    """Payload de la auditoría: el plugin admitido y, opcional, el timeout del motor."""

    model_config = ConfigDict(strict=True, extra="forbid")

    plugin: str = Field(min_length=5, max_length=255, pattern=PATRON_PLUGIN)
    timeout: int | None = Field(default=None, ge=5, le=120)


class AuditarPluginNativoStrategy:
    """Adaptador delgado servicio→dispatcher (read-only, sin gate)."""

    name = "auditar_plugin_nativo"

    def __init__(self, service: XEditBridgeService) -> None:
        self.service = service

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        params = AuditarPluginNativoParams(**payload_dict)
        return await self.service.auditar(params.plugin, timeout=params.timeout)
