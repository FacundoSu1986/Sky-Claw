"""Los HECHOS del latido de TexGen/DynDOLOD (H-11), en UN solo lugar.

``lods_status`` (agente) y la tarjeta de la etapa 9 (GUI) cuentan el progreso con esta misma función: repartirla
garantizaba que cada superficie lo contara a su manera. Son hechos medidos, no un veredicto: no hay «colgado». El umbral
de «sin actividad» lo juzga quien mira, con la edad de la muestra a la vista (un latido que dejó de llegar también es
información).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.app.core.event_payloads import DynDOLODPipelineProgressPayload


def progreso_en_hechos(muestra: DynDOLODPipelineProgressPayload, *, ahora: float) -> dict[str, Any]:
    """Hechos de ``muestra`` vistos en el instante ``ahora`` (epoch). ``None`` en el log = no se pudo medir."""
    sin_cambios = muestra.log_idle_seconds
    return {
        "herramienta": muestra.tool_name,
        "transcurrido_segundos": round(muestra.elapsed_seconds, 1),
        "log_bytes": muestra.log_bytes,
        "log_sin_cambios_segundos": None if sin_cambios is None else round(sin_cambios, 1),
        "muestra_hace_segundos": round(max(0.0, ahora - muestra.sampled_at), 1),
    }
