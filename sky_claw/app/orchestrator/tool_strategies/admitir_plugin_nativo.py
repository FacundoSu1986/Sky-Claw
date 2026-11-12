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

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from sky_claw.local.tools.xedit_bridge_client import PATRON_PLUGIN, nombre_visible

if TYPE_CHECKING:
    from sky_claw.local.tools.xedit_bridge_service import ContextoAdmision, XEditBridgeService


class AdmitirPluginNativoParams(BaseModel):
    """Payload de la admisión: solo el nombre del plugin."""

    model_config = ConfigDict(strict=True, extra="forbid")

    plugin: str = Field(min_length=5, max_length=255, pattern=PATRON_PLUGIN)


class AdmitirPluginNativoStrategy:
    """Adaptador delgado servicio→dispatcher; la aprobación la exige el gate innermost."""

    name = "admitir_plugin_nativo"

    def __init__(self, service: XEditBridgeService) -> None:
        self.service = service
        #: Contexto que se le mostró al operador, por plugin: ``execute`` lo consume y el servicio lo verifica.
        self._mostrados: dict[str, ContextoAdmision] = {}

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        # Un nombre que no se puede mostrar entero al operador no se aprueba (ValueError antes del pedido).
        nombre_visible(AdmitirPluginNativoParams(**payload_dict).plugin)

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        # El nombre va primero, entero y con lo invisible escapado (U+202E reordenaría lo que se ve): el gate
        # acota el detalle a 800 caracteres y ``nombre_visible`` garantiza que este texto entra sin cortarlo.
        # Se ejecuta el nombre real (``execute``), no el visible. Después va el contexto EXACTO (perfil e
        # instancia MO2), que se guarda para verificarlo al copiar: lo aprobado es eso, no «el perfil activo».
        plugin = AdmitirPluginNativoParams(**payload_dict).plugin
        visible = nombre_visible(plugin)
        contexto = self.service.contexto_de_admision()
        self._mostrados[plugin.casefold()] = contexto
        return (
            f"Admitir {visible} ({contexto.resumen()}) en el xEdit Bridge: copia el plugin y sus masters al "
            "laboratorio del bridge para auditarlo. MO2 y el juego no se modifican."
        )

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        params = AdmitirPluginNativoParams(**payload_dict)
        contexto = self._mostrados.pop(params.plugin.casefold(), None)
        return await self.service.admitir(params.plugin, contexto=contexto)
