"""Handlers para herramientas de interacción con base de datos.

Extraído de tools.py como parte de la refactorización M-13.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .schemas import InstallModParams, SearchModParams

if TYPE_CHECKING:
    from sky_claw.db.async_registry import AsyncModRegistry


async def search_mod(registry: AsyncModRegistry, mod_name: str) -> str:
    """Implementación de _search_mod.

    Args:
        registry: Instancia de AsyncModRegistry.
        mod_name: Mod name (or partial name) to search for.

    Returns:
        JSON string with matching mod records.
    """
    params = SearchModParams(mod_name=mod_name)
    results = await registry.search_mods(params.mod_name)
    return json.dumps({"matches": results})


async def install_mod(registry: AsyncModRegistry, nexus_id: int, version: str) -> str:
    """Implementación de _install_mod.

    Args:
        registry: Instancia de AsyncModRegistry.
        nexus_id: Nexus Mods numeric ID.
        version: Mod version string.

    Returns:
        JSON string confirming registration.
    """
    params = InstallModParams(nexus_id=nexus_id, version=version)
    mod_id = await registry.upsert_mod(
        nexus_id=params.nexus_id,
        name=f"nexus-{params.nexus_id}",
        version=params.version,
    )
    await registry.log_tasks_batch(
        [(mod_id, "install_mod", "registered", f"v{params.version}")]
    )
    return json.dumps(
        {
            "mod_id": mod_id,
            "nexus_id": params.nexus_id,
            "version": params.version,
            "status": "registered",
        }
    )


__all__ = ["install_mod", "search_mod"]
