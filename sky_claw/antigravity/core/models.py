import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("SkyClaw.Models")


class CircuitBreakerTrippedError(Exception):
    """Excepción lanzada cuando un cortacircuitos se abre.

    Usada tanto por el cortacircuitos de red (masterlist.py) como por el
    cortacircuitos cognitivo (AgenticLoopGuardrail).
    """

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        occurrences: int | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.occurrences = occurrences


class WSLInteropError(Exception):
    """Error al intentar ejecutar un binario de Windows desde Linux."""

    pass


class StealthScrapeAction(BaseModel):
    """Input para scraping profundo en Reddit o foros de Nexus buscando incompatibilidades."""

    model_config = {"strict": True}

    search_query: str = Field(..., min_length=3, description="Query de búsqueda exacta.")
    target_site: Literal["reddit_skyrimmods", "nexus_forums"] = Field(..., description="Foro objetivo.")
    max_threads_to_analyze: int = Field(3, le=10, description="Límite de hilos para evitar bloqueos/timeouts.")


class LootExecutionParams(BaseModel):
    """Input para ejecutar LOOT en el entorno Windows desde WSL2."""

    model_config = {"strict": True}

    profile_name: str = Field("Default", description="Nombre del perfil de MO2.")
    update_masterlist: bool = Field(True, description="Descargar la última masterlist de GitHub antes de ordenar.")


class XEditConflictAnalysisParams(BaseModel):
    """Input para automatización de xEdit (Headless)."""

    model_config = {"strict": True}

    target_plugins: list[str] = Field(..., min_length=1, description="Lista de archivos .esp/.esm a analizar.")
    pascal_script_name: str = Field(..., description="Nombre del script .pas a ejecutar (ej. 'list_conflicts.pas').")


class HitlApprovalRequest(BaseModel):
    """Input para solicitar intervención humana vía Telegram."""

    model_config = {"strict": True}

    action_type: Literal[
        "download_external",
        "destructive_xedit",
        "circuit_breaker_halt",
        "reorder_load_order",
    ] = Field(..., description="Tipo de acción que requiere aprobación.")
    reason: str = Field(
        ...,
        description="Justificación técnica generada por el agente (Thought) para el usuario.",
    )
    context_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Metadatos en JSON para mostrar en los botones de Telegram.",
    )
