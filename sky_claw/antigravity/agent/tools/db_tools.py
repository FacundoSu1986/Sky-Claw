"""Handlers para herramientas de interacción con base de datos.

Extraído de tools.py como parte de la refactorización M-13.

TASK-011 Tech Debt Cleanup: Removed redundant Pydantic instantiation.
Validation is now centralized in AsyncToolRegistry.execute().
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.antigravity.db.async_registry import AsyncModRegistry


async def search_mod(registry: AsyncModRegistry, mod_name: str) -> str:
    """Implementación de _search_mod.

    Args are pre-validated by AsyncToolRegistry.execute() via SearchModParams.

    Args:
        registry: Instancia de AsyncModRegistry.
        mod_name: Mod name (or partial name) to search for.

    Returns:
        JSON string with matching mod records.
    """
    results = await registry.search_mods(mod_name)
    return json.dumps({"matches": results})


async def install_mod(registry: AsyncModRegistry, nexus_id: int, version: str) -> str:
    """Implementación de _install_mod.

    Args are pre-validated by AsyncToolRegistry.execute() via InstallModParams.

    Args:
        registry: Instancia de AsyncModRegistry.
        nexus_id: Nexus Mods numeric ID.
        version: Mod version string.

    Returns:
        JSON string confirming registration.
    """
    mod_id = await registry.upsert_mod(
        nexus_id=nexus_id,
        name=f"nexus-{nexus_id}",
        version=version,
    )
    await registry.log_tasks_batch([(mod_id, "install_mod", "registered", f"v{version}")])
    return json.dumps(
        {
            "mod_id": mod_id,
            "nexus_id": nexus_id,
            "version": version,
            "status": "registered",
        }
    )


__all__ = ["install_mod", "search_mod"]
