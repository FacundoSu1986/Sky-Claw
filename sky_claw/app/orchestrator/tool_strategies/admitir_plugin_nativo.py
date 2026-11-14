"""Strategy ``admitir_plugin_nativo`` (xEdit Bridge, ADR 0018) — CON gate HITL.

Copia el plugin y sus masters, resueltos con la precedencia de MO2, al laboratorio del bridge
(``run_admit.py``). MO2 y el juego se leen pero nunca se escriben; aun así la operación escribe
archivos, así que el gate compartido del dispatcher exige aprobación humana antes de ejecutar
(``DESTRUCTIVE_TOOL_PATTERNS``). El perfil y la instancia de MO2 los pone Sky-Claw: el payload solo
admite el nombre del plugin, y cualquier otra clave se rechaza para que el operador no apruebe algo
que la estrategia ignora (precedente Codex #213). Hermana LLM:
``sky_claw/app/agent/tools/xedit_bridge_tools.py::admitir_plugin_nativo`` (``HITLGuard`` explícito).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from sky_claw.app.orchestrator.tool_strategies.middleware import (
    MAX_APPROVAL_DETAIL_LENGTH,
    hitl_invocation_state,
)
from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.tools.xedit_bridge_client import PATRON_PLUGIN, nombre_visible

if TYPE_CHECKING:
    from sky_claw.local.tools.xedit_bridge_service import ContextoAdmision, XEditBridgeService

logger = logging.getLogger("SkyClaw.XEditBridge.HITL")
_SCOPE_CONTEXT_KEY = "admitir_plugin_nativo.contexto"
_SCOPE_PREPARED_KEY = "admitir_plugin_nativo.detalle_preparado"


class AdmitirPluginNativoParams(BaseModel):
    """Payload de la admisión: solo el nombre del plugin."""

    model_config = ConfigDict(strict=True, extra="forbid")

    plugin: str = Field(min_length=5, max_length=255, pattern=PATRON_PLUGIN)


class AdmitirPluginNativoStrategy:
    """Adaptador delgado servicio→dispatcher; la aprobación la exige el gate innermost."""

    name = "admitir_plugin_nativo"

    def __init__(self, service: XEditBridgeService) -> None:
        self.service = service
        # Solo para invocaciones directas sin HitlGateMiddleware, indexado por nombre exacto (no casefold).
        self._direct_pendientes: dict[str, ContextoAdmision] = {}

    def _preparar_detalle(self, payload_dict: dict[str, Any]) -> tuple[str, ContextoAdmision, str]:
        plugin = AdmitirPluginNativoParams(**payload_dict).plugin
        visible = nombre_visible(plugin)
        contexto = self.service.contexto_de_admision()
        detalle = (
            f"Admitir {visible} ({contexto.resumen()}) en el xEdit Bridge: copia el plugin y sus masters al "
            "laboratorio del bridge para auditarlo. MO2 y el juego no se modifican."
        )
        if len(detalle) > MAX_APPROVAL_DETAIL_LENGTH:
            raise ValueError(
                f"El contexto de admisión para {visible[:80]} supera el límite visible HITL "
                f"({len(detalle)} > {MAX_APPROVAL_DETAIL_LENGTH} caracteres) y no se puede mostrar íntegro al operador."
            )
        return plugin, contexto, detalle

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        # Un pedido que no entra íntegro (nombre y contexto completo) dentro del límite visible del gate no se
        # aprueba: se rechaza antes de consultar al operador y comparte lo preparado con ``describe_for_approval``
        # dentro del alcance HITL para no leer ``contexto_de_admision()`` dos veces.
        plugin, contexto, detalle = self._preparar_detalle(payload_dict)
        scope = hitl_invocation_state()
        if scope is not None:
            scope[_SCOPE_PREPARED_KEY] = (scope.get("request_id"), plugin, contexto, detalle)

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        # El nombre va primero, entero y con lo invisible escapado (U+202E reordenaría lo que se ve), seguido
        # del contexto EXACTO (perfil e instancia MO2). ``_preparar_detalle`` garantiza que ambas piezas entran
        # íntegras en los 800 caracteres del gate; se ejecuta el nombre real, vinculado al alcance HITL actual.
        scope = hitl_invocation_state()
        plugin = AdmitirPluginNativoParams(**payload_dict).plugin
        preparado = scope.pop(_SCOPE_PREPARED_KEY, None) if scope is not None else None
        if (
            isinstance(preparado, tuple)
            and len(preparado) == 4
            and scope is not None
            and preparado[0] == scope.get("request_id")
            and preparado[1] == plugin
        ):
            _, _, contexto, detalle = preparado
        else:
            plugin, contexto, detalle = self._preparar_detalle(payload_dict)
        if scope is not None:
            scope[_SCOPE_CONTEXT_KEY] = (scope.get("request_id"), plugin, contexto)
        else:
            self._direct_pendientes[plugin] = contexto
        return detalle

    def clear_approval_preparation(self, payload_dict: dict[str, Any]) -> None:
        if hitl_invocation_state() is not None:
            return
        plugin = payload_dict.get("plugin")
        if isinstance(plugin, str):
            self._direct_pendientes.pop(plugin, None)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        params = AdmitirPluginNativoParams(**payload_dict)
        contexto = self._consumir_contexto_autorizado(params.plugin)
        if contexto is None:
            logger.warning("Admisión GUI rechazada en execute: falta contexto autorizado para %r", params.plugin)
            return {
                "plugin": sanitize_for_prompt(params.plugin, max_length=255),
                "success": False,
                "message": (
                    "admisión cancelada: falta el contexto de autorización aprobado para esta solicitud "
                    "(o ya fue consumido); no se copió nada"
                ),
            }
        return await self.service.admitir(params.plugin, contexto=contexto)

    def _consumir_contexto_autorizado(self, plugin: str) -> ContextoAdmision | None:
        scope = hitl_invocation_state()
        if scope is not None:
            entrada = scope.pop(_SCOPE_CONTEXT_KEY, None)
            if not (isinstance(entrada, tuple) and len(entrada) == 3):
                return None
            request_id, bound_plugin, contexto = entrada
            if request_id != scope.get("request_id") or bound_plugin != plugin:
                return None
            return contexto
        return self._direct_pendientes.pop(plugin, None)
