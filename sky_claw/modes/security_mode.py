from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.app_context import AppContext

logger = logging.getLogger(__name__)


async def _run_security(ctx: AppContext, command_str: str | None) -> None:
    """Ejecuta operaciones de auditoría Purple Team desde la CLI."""
    if not command_str:
        logger.info(
            "Uso: python -m sky_claw --mode security 'scan <path>' o 'approve <path>'"
        )
        return

    parts = command_str.split(maxsplit=1)
    action = parts[0].lower()
    path_str = parts[1] if len(parts) > 1 else "."

    from sky_claw.security.governance import GovernanceManager
    from sky_claw.security.metacognitive_logic import audit_resource

    if action == "scan":
        logger.info("Iniciando auditoría Purple Team para: %s...", path_str)
        result = await audit_resource(path_str)

        # Formatear salida similar al agente
        confidence = result.get("confidence", 0.0)
        logger.info("Resultados de Auditoría (Confianza: %.2f):", confidence)
        logger.info("Decisión: %s", result.get("summary", {}).get("is_safe", False))

        for find in result.get("findings", []):
            severity = find.get("severity", "LOW")
            logger.warning(
                "[%s] %s (%s:%s)",
                severity,
                find.get("message"),
                find.get("file"),
                find.get("line"),
            )

        if result.get("summary", {}).get("is_safe"):
            logger.info("El recurso es seguro según las políticas de Abril 2026.")
        else:
            logger.warning(
                "SE HAN DETECTADO RIESGOS CRÍTICOS. Se recomienda revisión manual."
            )

    elif action == "approve":
        GovernanceManager.get_instance().approve_file(path_str)
        logger.info("Archivo '%s' añadido a la whitelist local.", path_str)
    else:
        logger.warning("Acción de seguridad desconocida: %s", action)
