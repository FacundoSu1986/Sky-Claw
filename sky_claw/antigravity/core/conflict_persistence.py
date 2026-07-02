"""Puente detección→persistencia de conflictos (F5).

La pantalla de Conflictos del Forge (#220) consume la tabla ``conflicts`` de la
DB GUI, pero hasta ahora nada la poblaba en producción. Este módulo convierte
los reportes del :class:`AssetConflictDetector` (camino liviano: escaneo del
VFS de MO2, sin xEdit ni subprocesos) en pares persistidos vía
``DatabaseAgent.add_conflict``.

Diseño:
- ``pair_asset_conflicts`` es un seam puro: reportes → pares únicos
  ``(ganador, pisado, "asset:<tipo>")``. Varios archivos pisados entre los
  mismos dos mods colapsan en un solo par (al usuario le importa la disputa
  entre mods, no cada .nif).
- ``persist_asset_conflicts`` es idempotente sobre las disputas pendientes:
  re-correr la detección no duplica filas sin resolver. Una disputa resuelta
  que reaparece en la detección se registra de nuevo (el estado real manda).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.antigravity.core.database import DatabaseAgent
    from sky_claw.local.assets.asset_scanner import AssetConflictReport

#: Par de conflicto listo para persistir: (mod ganador, mod pisado, tipo).
ConflictPair = tuple[str, str, str]


def pair_asset_conflicts(reports: list[AssetConflictReport]) -> list[ConflictPair]:
    """Convierte reportes de assets en pares únicos de mods en disputa."""
    seen: set[ConflictPair] = set()
    pairs: list[ConflictPair] = []
    for report in reports:
        conflict_type = f"asset:{report.asset_type.value}"
        for loser in report.overwritten_mods:
            pair = (report.winner_mod, loser, conflict_type)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


async def persist_asset_conflicts(reports: list[AssetConflictReport], db: DatabaseAgent) -> int:
    """Persiste los conflictos de assets en la DB GUI; devuelve cuántos son nuevos.

    Los nombres de mod se resuelven a ids con ``add_mod`` (UPSERT por nombre,
    preserva ids — ver #220), y la deduplicación compara contra las disputas
    PENDIENTES sin importar el orden del par.
    """
    pairs = pair_asset_conflicts(reports)
    if not pairs:
        return 0

    mod_ids: dict[str, int] = {}
    for winner, loser, _ in pairs:
        for name in (winner, loser):
            if name not in mod_ids:
                mod_ids[name] = await db.add_mod(name)

    pending = await db.get_conflicts(resolved=False)
    existing: set[tuple[frozenset[int], str]] = {
        (frozenset({c["mod_id_1"], c["mod_id_2"]}), str(c["conflict_type"] or "")) for c in pending
    }

    created = 0
    for winner, loser, conflict_type in pairs:
        key = (frozenset({mod_ids[winner], mod_ids[loser]}), conflict_type)
        if key in existing:
            continue
        await db.add_conflict(mod_ids[winner], mod_ids[loser], conflict_type)
        existing.add(key)
        created += 1
    return created
