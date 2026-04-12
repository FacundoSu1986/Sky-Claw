import logging
from pydantic import BaseModel, Field
from typing import List, Literal

logger = logging.getLogger("SkyClaw.Models")


class CircuitBreakerTripped(Exception):
    """Excepción lanzada cuando la IP está en riesgo de baneo."""

    pass


class WSLInteropError(Exception):
    """Error al intentar ejecutar un binario de Windows desde Linux."""

    pass


class StealthScrapeAction(BaseModel):
    """Input para scraping profundo en Reddit o foros de Nexus buscando incompatibilidades."""

    model_config = {"strict": True}

    search_query: str = Field(
        ..., min_length=3, description="Query de búsqueda exacta."
    )
    target_site: Literal["reddit_skyrimmods", "nexus_forums"] = Field(
        ..., description="Foro objetivo."
    )
    max_threads_to_analyze: int = Field(
        3, le=10, description="Límite de hilos para evitar bloqueos/timeouts."
    )


class LootExecutionParams(BaseModel):
    """Input para ejecutar LOOT en el entorno Windows desde WSL2."""

    model_config = {"strict": True}

    profile_name: str = Field("Default", description="Nombre del perfil de MO2.")
    update_masterlist: bool = Field(
        True, description="Descargar la última masterlist de GitHub antes de ordenar."
    )


class XEditConflictAnalysisParams(BaseModel):
    """Input para automatización de xEdit (Headless)."""

    model_config = {"strict": True}

    target_plugins: List[str] = Field(
        ..., min_length=1, description="Lista de archivos .esp/.esm a analizar."
    )
    pascal_script_name: str = Field(
        ..., description="Nombre del script .pas a ejecutar (ej. 'list_conflicts.pas')."
    )


class HitlApprovalRequest(BaseModel):
    """Input para solicitar intervención humana vía Telegram."""

    model_config = {"strict": True}

    action_type: Literal[
        "download_external", "destructive_xedit", "circuit_breaker_halt"
    ] = Field(..., description="Tipo de acción que requiere aprobación.")
    reason: str = Field(
        ...,
        description="Justificación técnica generada por el agente (Thought) para el usuario.",
    )
    context_data: dict = Field(
        default_factory=dict,
        description="Metadatos en JSON para mostrar en los botones de Telegram.",
    )
