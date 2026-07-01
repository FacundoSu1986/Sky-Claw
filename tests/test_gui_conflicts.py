"""Tests de la sección Conflictos del Forge (GUI estática → funcional).

Cubre:
- `enrich_conflicts`: seam puro que mapea los ``mod_id_1/2`` de la DB a nombres
  para mostrar en la pantalla de Conflictos.
- `DatabaseAgent.add_conflict` / `resolve_conflict`: alta y resolución reales en
  la tabla ``conflicts`` (la lista y el botón "Resolver" se apoyan en esto).
"""

from __future__ import annotations

import pathlib

from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.gui.models.app_state import enrich_conflicts

# ── enrich_conflicts (seam puro) ────────────────────────────────────────────────
_MODS = [
    {"id": 1, "name": "Immersive Armors"},
    {"id": 2, "name": "Ordinator"},
    {"id": 3, "name": "Lux Via"},
]


def test_enrich_conflicts_mapea_ids_a_nombres() -> None:
    conflicts = [{"id": 10, "mod_id_1": 1, "mod_id_2": 2, "conflict_type": "record", "detected_at": "2026-07-01"}]
    out = enrich_conflicts(conflicts, _MODS)
    assert out == [
        {"id": 10, "type": "record", "mod_a": "Immersive Armors", "mod_b": "Ordinator", "detected_at": "2026-07-01"}
    ]


def test_enrich_conflicts_id_desconocido_cae_a_placeholder() -> None:
    conflicts = [{"id": 11, "mod_id_1": 1, "mod_id_2": 99, "conflict_type": "asset"}]
    out = enrich_conflicts(conflicts, _MODS)
    assert out[0]["mod_a"] == "Immersive Armors"
    assert out[0]["mod_b"] == "Mod desconocido"


def test_enrich_conflicts_tipo_ausente_usa_default() -> None:
    out = enrich_conflicts([{"id": 12, "mod_id_1": 1, "mod_id_2": 2}], _MODS)
    assert out[0]["type"] == "Conflicto"


def test_enrich_conflicts_listas_vacias() -> None:
    assert enrich_conflicts([], _MODS) == []
    assert enrich_conflicts(None, None) == []


# ── DatabaseAgent: alta y resolución de conflictos ──────────────────────────────
async def test_add_and_get_conflict(tmp_path: pathlib.Path) -> None:
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    m1 = await db.add_mod("Immersive Armors")
    m2 = await db.add_mod("Ordinator")

    cid = await db.add_conflict(m1, m2, "record")
    assert cid > 0

    pending = await db.get_conflicts(resolved=False)
    assert len(pending) == 1
    assert pending[0]["mod_id_1"] == m1
    assert pending[0]["mod_id_2"] == m2
    assert pending[0]["conflict_type"] == "record"


async def test_resolve_conflict_lo_saca_de_pendientes(tmp_path: pathlib.Path) -> None:
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    m1 = await db.add_mod("Immersive Armors")
    m2 = await db.add_mod("Ordinator")
    cid = await db.add_conflict(m1, m2, "record")

    await db.resolve_conflict(cid, resolution="parcheado")

    assert await db.get_conflicts(resolved=False) == []
    resolved = await db.get_conflicts(resolved=True)
    assert len(resolved) == 1
    assert resolved[0]["resolution"] == "parcheado"
