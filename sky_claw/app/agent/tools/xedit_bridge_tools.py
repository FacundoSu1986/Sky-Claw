"""Tools LLM del xEdit Bridge (ADR 0018): auditoría nativa de plugins con SSEEdit.

Hermanas GUI: ``sky_claw/app/orchestrator/tool_strategies/auditar_plugin_nativo.py`` y
``admitir_plugin_nativo.py``. Las dos superficies delegan en el MISMO
``XEditBridgeService``; acá solo vive la aprobación humana de la capa del agente, que no tiene
un middleware HITL general (#217) y por eso la pide el propio handler, como ``download_mod``.

* ``auditar_plugin_nativo`` es de lectura: nunca pide aprobación.
* ``admitir_plugin_nativo`` copia archivos al laboratorio del bridge: sin ``HITLGuard`` se
  rechaza, y solo una decisión ``APPROVED`` ejecuta la copia.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from sky_claw.app.security.hitl import Decision, new_hitl_request_id
from sky_claw.app.security.sanitize import sanitize_for_prompt

if TYPE_CHECKING:
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.local.tools.xedit_bridge_service import XEditBridgeService

logger = logging.getLogger(__name__)

_SIN_SERVICIO = "xEdit Bridge no disponible: definí XEDIT_BRIDGE_ROOT con la carpeta BridgeTrabajo de la instalación"


def _fallo(mensaje: str, **extra: Any) -> str:
    return json.dumps({"success": False, "message": sanitize_for_prompt(mensaje, max_length=300), **extra})


async def auditar_plugin_nativo(
    service: XEditBridgeService | None,
    plugin: str,
    timeout: int | None = None,
) -> str:
    """Chequeo nativo de SSEEdit sobre la copia admitida de ``plugin`` (read-only)."""
    if service is None:
        return _fallo(_SIN_SERVICIO, aprobado=False)
    return json.dumps(await service.auditar(plugin, timeout=timeout))


async def admitir_plugin_nativo(
    service: XEditBridgeService | None,
    hitl: HITLGuard | None,
    plugin: str,
) -> str:
    """Copia ``plugin`` y sus masters al laboratorio del bridge, solo con aprobación humana."""
    if service is None:
        return _fallo(_SIN_SERVICIO)
    if hitl is None:
        return _fallo("La admisión requiere aprobación humana y no hay HITLGuard configurado: no se copió nada.")
    nombre = sanitize_for_prompt(plugin, max_length=120)
    perfil = sanitize_for_prompt(service.perfil_activo(), max_length=120)
    try:
        decision = await hitl.request_approval(
            request_id=new_hitl_request_id("xedit-bridge-admit"),
            reason=f"Admitir {nombre} (perfil MO2 «{perfil}») en el laboratorio del xEdit Bridge",
            detail=(
                "Copia el plugin y sus masters, resueltos con la precedencia de MO2, a la carpeta "
                "Admitidos del bridge. MO2 y el juego solo se leen; no se modifica ningún archivo suyo."
            ),
            category="tool_execution",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # frontera deliberada: JSON para el LLM, nunca propagar crudo
        logger.exception("Solicitud HITL de admisión falló para %s", nombre)
        return _fallo(f"no se pudo pedir la aprobación: {exc}")
    if decision is not Decision.APPROVED:
        logger.warning("Admisión de %s no aprobada: %s", nombre, decision.value)
        return _fallo("Admisión rechazada por el operador: no se copió nada.", status="denied", decision=decision.value)
    return json.dumps(await service.admitir(plugin))


__all__ = ["admitir_plugin_nativo", "auditar_plugin_nativo"]
