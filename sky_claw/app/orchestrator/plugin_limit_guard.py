"""PluginLimitGuard: seam extraído del SupervisorAgent (PR5 del Strangler Fig).

Gate preventivo (M-04/M-05) que valida el límite de 254 plugins antes de los
rituales destructivos (DynDOLOD, Synthesis, Wrye Bash). El comportamiento
histórico se preserva: ``plugins.txt`` tiene precedencia; ``loadorder.txt``
solo se consulta como fallback si ``plugins.txt`` no existe (no hay fallback
cuando ``plugins.txt`` existe pero produce lista vacía).

Recibe el ``PathResolutionService`` explícitamente — NO el supervisor, NO un
service locator. La lectura del archivo se delega a ``asyncio.to_thread``
para no bloquear el event loop.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import TYPE_CHECKING, Any

from sky_claw.app.orchestrator.active_plugins import PluginListSource, parse_active_plugins
from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer

if TYPE_CHECKING:
    from sky_claw.app.core.path_resolver import PathResolutionService

logger = logging.getLogger(__name__)

# Precedencia congelada: ``plugins.txt`` primero (con su ``source`` estricto),
# ``loadorder.txt`` solo como fallback histórico. La anotación fija cada
# ``source`` a ``PluginListSource`` para que ``parse_active_plugins`` reciba el
# ``Literal`` exacto (si esto se infiere como ``str`` el gate estricto rompe).
_LOAD_ORDER_CANDIDATES: tuple[tuple[str, PluginListSource], ...] = (
    ("plugins.txt", "plugins_txt"),
    ("loadorder.txt", "loadorder"),
)


def _read_active_plugins_blocking(modlist_path: pathlib.Path) -> list[str]:
    """Lee el load order del perfil y devuelve sus plugins habilitados.

    PT-1 (S-6): aislada para envolverla en ``asyncio.to_thread`` desde el guard
    async y no bloquear el event loop.

    ``modlist_path`` se usa solamente para localizar el directorio del perfil:
    ``modlist.txt`` enumera *mods*, no el load order. ``plugins.txt`` contiene
    las entradas habilitadas (``*``) y tiene precedencia; ``loadorder.txt`` es
    el fallback para perfiles donde el primero no existe. Si ``plugins.txt``
    existe pero produce ``[]``, esa respuesta gana — no se cae a ``loadorder``
    (regla histórica congelada; un "fallback en lista vacía" sería un cambio
    funcional fuera del scope de PR5).
    """
    for filename, source in _LOAD_ORDER_CANDIDATES:
        load_order_path = modlist_path.parent / filename
        try:
            if load_order_path.is_file():
                return parse_active_plugins(
                    load_order_path.read_text(encoding="utf-8-sig", errors="replace"),
                    source=source,
                )
        except OSError as exc:
            logger.warning("No se pudo leer el load order %s: %s", load_order_path, exc)
    return []


class PluginLimitGuard:
    """Gate M-04/M-05: valida que el load order activo no exceda 254 plugins.

    Se inyecta al ``build_orchestration_composition`` como callable
    ``plugin_limit_guard=<instancia>.run``; se entrega así a la strategy
    ``validate_plugin_limit`` y al servicio de Wrye Bash sin que el
    SupervisorAgent participe.
    """

    def __init__(self, *, path_resolver: PathResolutionService) -> None:
        self._path_resolver = path_resolver

    async def run(self, profile: str) -> dict[str, Any]:
        logger.info(
            "[M-04] Ejecutando validación de límite de plugins para perfil '%s'...",
            profile,
        )
        try:
            active_plugins: list[str] = []
            modlist_path = self._path_resolver.resolve_modlist_path(profile)
            # PT-1 (S-6): leer el modlist (I/O de archivo) en un thread para no
            # bloquear el event loop; el parseo con criterio L-1 vive en el helper.
            active_plugins = await asyncio.to_thread(_read_active_plugins_blocking, modlist_path)

            analyzer = ConflictAnalyzer()
            analyzer.validate_load_order_limit(active_plugins)
        except RuntimeError as exc:
            logger.critical(
                "[M-04] Plugin limit EXCEEDED para perfil '%s': %s",
                profile,
                exc,
            )
            return {
                "valid": False,
                "profile": profile,
                "plugin_count": len(active_plugins),
                "limit": 254,
                "error": str(exc),
            }
        except (ValueError, OSError) as exc:
            logger.error(
                "[M-04] Error inesperado durante validación de plugins: %s",
                exc,
                exc_info=True,
            )
            return {"valid": False, "error": str(exc)}

        logger.info(
            "[M-04] Plugin limit OK: %d / 254 activos en perfil '%s'.",
            len(active_plugins),
            profile,
        )
        return {
            "valid": True,
            "profile": profile,
            "plugin_count": len(active_plugins),
            "limit": 254,
        }
