"""Tests del puente detección→persistencia de conflictos (F5).

`AssetConflictDetector` (camino liviano, sin xEdit) produce reportes de assets
pisados entre mods; este puente los convierte en pares y los persiste en la
tabla ``conflicts`` de la DB GUI — el productor que le faltaba a la pantalla
de Conflictos (#220).
"""

from __future__ import annotations

import pathlib

from sky_claw.antigravity.core.conflict_persistence import (
    pair_asset_conflicts,
    persist_asset_conflicts,
)
from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.local.assets.asset_scanner import AssetConflictReport, AssetType


def _report(winner: str, losers: tuple[str, ...], path: str = "meshes/a.nif") -> AssetConflictReport:
    return AssetConflictReport(
        file_path=path,
        winner_mod=winner,
        overwritten_mods=losers,
        asset_type=AssetType.MESH,
    )


# ── pair_asset_conflicts (seam puro) ────────────────────────────────────────────
def test_genera_un_par_por_ganador_y_cada_pisado() -> None:
    pares = pair_asset_conflicts([_report("SMIM", ("Skyrim 202X", "Noble Skyrim"))])
    assert ("SMIM", "Skyrim 202X", "asset:mesh") in pares
    assert ("SMIM", "Noble Skyrim", "asset:mesh") in pares
    assert len(pares) == 2


def test_deduplica_pares_repetidos_entre_archivos() -> None:
    # Dos archivos distintos pisados entre los mismos mods = UN solo par.
    reports = [
        _report("SMIM", ("Skyrim 202X",), path="meshes/a.nif"),
        _report("SMIM", ("Skyrim 202X",), path="meshes/b.nif"),
    ]
    assert len(pair_asset_conflicts(reports)) == 1


def test_lista_vacia_devuelve_vacio() -> None:
    assert pair_asset_conflicts([]) == []


# ── persist_asset_conflicts (DB real en tmp) ────────────────────────────────────
async def test_persiste_pares_y_enriquece_con_nombres(tmp_path: pathlib.Path) -> None:
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()

    nuevos = await persist_asset_conflicts([_report("SMIM", ("Skyrim 202X",))], db)
    assert nuevos == 1

    pendientes = await db.get_conflicts(resolved=False)
    assert len(pendientes) == 1
    assert pendientes[0]["conflict_type"] == "asset:mesh"

    # Los ids apuntan a mods reales de la DB GUI (contrato de enrich_conflicts).
    from sky_claw.antigravity.gui.models.app_state import enrich_conflicts

    mods = await db.get_mods()
    enriquecido = enrich_conflicts(pendientes, mods)[0]
    assert {enriquecido["mod_a"], enriquecido["mod_b"]} == {"SMIM", "Skyrim 202X"}


async def test_es_idempotente_sobre_pendientes(tmp_path: pathlib.Path) -> None:
    # Correr la detección dos veces no debe duplicar disputas sin resolver.
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    reports = [_report("SMIM", ("Skyrim 202X",))]

    assert await persist_asset_conflicts(reports, db) == 1
    assert await persist_asset_conflicts(reports, db) == 0
    assert len(await db.get_conflicts(resolved=False)) == 1


async def test_no_pisa_metadatos_de_mods_existentes(tmp_path: pathlib.Path) -> None:
    """El scan no debe degradar version/size/source de mods ya registrados
    (add_mod con defaults haría UPSERT a NULL/0 — review Copilot #223)."""
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    await db.add_mod("SMIM", "2.08", 1200, "Nexusmods")

    await persist_asset_conflicts([_report("SMIM", ("Skyrim 202X",))], db)

    smim = next(m for m in await db.get_mods() if m["name"] == "SMIM")
    assert smim["version"] == "2.08"
    assert smim["size_mb"] == 1200
    assert smim["source"] == "Nexusmods"


# ── Single-flight del escaneo (guard de doble click) ────────────────────────────
class _StoreFalso:
    def __init__(self) -> None:
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value) -> None:
        self._d[key] = value


def test_claim_scan_slot_es_single_flight() -> None:
    from sky_claw.antigravity.core.conflict_persistence import claim_scan_slot, release_scan_slot

    store = _StoreFalso()
    assert claim_scan_slot(store) is True
    assert claim_scan_slot(store) is False  # segundo click: rechazado
    release_scan_slot(store)
    assert claim_scan_slot(store) is True  # liberado: se puede volver a escanear


async def test_conflicto_resuelto_puede_reaparecer(tmp_path: pathlib.Path) -> None:
    # Si el usuario resolvió la disputa pero la detección la vuelve a encontrar,
    # se registra de nuevo (el estado real manda sobre el historial).
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    reports = [_report("SMIM", ("Skyrim 202X",))]

    await persist_asset_conflicts(reports, db)
    cid = (await db.get_conflicts(resolved=False))[0]["id"]
    await db.resolve_conflict(cid, resolution="orden ajustado")

    assert await persist_asset_conflicts(reports, db) == 1
    assert len(await db.get_conflicts(resolved=False)) == 1
