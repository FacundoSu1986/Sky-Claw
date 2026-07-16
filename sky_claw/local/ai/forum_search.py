"""Búsqueda de soluciones conocidas en foros de la comunidad — stub de Fase 1.

La Fase 2 reemplaza este módulo por el orquestador real (Nexus API + Reddit +
AFK Mods vía ``NetworkGateway``, cache con TTL, fail-closed por fuente). En
Fase 1 el advisor recomienda solo con el diff del record: integrar los foros
el día 1 es ruido — primero hay que medir la calidad base del LLM.

La firma es estable a propósito: el call-site del advisor no cambia cuando la
Fase 2 aterrice.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.local.ai.recommendation import ForumReference

logger = logging.getLogger(__name__)


async def search_forums(
    mods: tuple[str, ...],
    record_type: str,
    *,
    limit: int = 5,
) -> list[ForumReference]:
    """Busca soluciones conocidas para un conflicto entre *mods*.

    Args:
        mods: Plugins involucrados en el conflicto (ganador + perdedores).
        record_type: Firma del record en conflicto (``NPC_``, ``QUST``…).
        limit: Máximo de referencias a devolver (Fase 2).

    Returns:
        Fase 1: siempre ``[]`` — el advisor lo declara en el prompt (sección
        de foros ausente) y el LLM recomienda solo con el diff.
    """
    logger.debug(
        "forum_search (stub Fase 1): sin fuentes cableadas para %s (%s, limit=%d).",
        mods,
        record_type,
        limit,
    )
    return []


__all__ = ["search_forums"]
