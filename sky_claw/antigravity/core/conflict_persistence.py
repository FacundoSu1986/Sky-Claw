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
    from sky_claw.local.xedit.conflict_analyzer import ConflictReport

#: Par de conflicto listo para persistir: (mod ganador, mod pisado, tipo).
ConflictPair = tuple[str, str, str]

#: Clave del store GUI para el single-flight del escaneo (guard de doble click).
SCAN_IN_FLIGHT_KEY = "conflict_scan_in_flight"


def claim_scan_slot(store: object) -> bool:
    """Reclama el slot de escaneo; ``False`` si ya hay uno en curso.

    Dos escaneos concurrentes verían el mismo snapshot de disputas pendientes e
    insertarían duplicados (la tabla no tiene constraint única) — review
    Copilot #223. ``store`` es duck-typed (``get``/``set``) para no acoplar
    core → GUI; en producción es el ``ReactiveStore``.
    """
    if store.get(SCAN_IN_FLIGHT_KEY):  # type: ignore[attr-defined]
        return False
    store.set(SCAN_IN_FLIGHT_KEY, True)  # type: ignore[attr-defined]
    return True


def release_scan_slot(store: object) -> None:
    """Libera el slot de escaneo (llamar siempre en ``finally``)."""
    store.set(SCAN_IN_FLIGHT_KEY, False)  # type: ignore[attr-defined]


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


#: Severidad de conflicto de record, de peor a menos grave (para el tipo del par).
_SEVERITY_ORDER = ("critical", "warning", "info")


def pair_record_conflicts(report: ConflictReport) -> list[ConflictPair]:
    """Convierte el análisis profundo de xEdit en pares de plugins en disputa.

    Un par por cada ``PluginConflictPair`` con conflictos, tipado con la
    severidad PEOR del par (``record:critical`` > ``record:warning`` >
    ``record:info``) — al usuario le importa "estos dos plugins chocan a nivel
    record, lo peor es crítico", no cada FormID. Los pares sin conflictos se
    ignoran.
    """
    pairs: list[ConflictPair] = []
    for pp in report.plugin_pairs:
        if not pp.conflicts:
            continue
        severities = {c.severity for c in pp.conflicts}
        worst = next((s for s in _SEVERITY_ORDER if s in severities), "info")
        pairs.append((pp.plugin_a, pp.plugin_b, f"record:{worst}"))
    return pairs


async def persist_asset_conflicts(reports: list[AssetConflictReport], db: DatabaseAgent) -> int:
    """Persiste los conflictos de assets en la DB GUI; devuelve cuántos son nuevos."""
    return await _persist_pairs(pair_asset_conflicts(reports), db)


async def persist_record_conflicts(report: ConflictReport, db: DatabaseAgent) -> int:
    """Persiste los conflictos de records (xEdit) en la DB GUI; devuelve cuántos son nuevos."""
    return await _persist_pairs(pair_record_conflicts(report), db)


async def _persist_pairs(pairs: list[ConflictPair], db: DatabaseAgent) -> int:
    """Persiste pares de disputa en la tabla ``conflicts``; devuelve cuántos son nuevos.

    Núcleo compartido por los productores de conflictos (assets y records):
    los nombres de mod se resuelven a ids SIN pisar metadatos (``add_mod`` con
    defaults haría UPSERT de version/size/source a NULL/0 sobre mods ya
    registrados — review Copilot #223; solo se crea el mod si no existe), y la
    deduplicación compara contra las disputas PENDIENTES sin importar el orden
    del par (una disputa resuelta que reaparece se registra de nuevo).
    """
    if not pairs:
        return 0

    existing_ids = {str(m.get("name")): int(m["id"]) for m in await db.get_mods() if m.get("id") is not None}
    mod_ids: dict[str, int] = {}
    for winner, loser, _ in pairs:
        for name in (winner, loser):
            if name not in mod_ids:
                known = existing_ids.get(name)
                mod_ids[name] = known if known is not None else await db.add_mod(name)

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
