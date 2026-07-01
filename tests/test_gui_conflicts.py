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


async def test_add_mod_upsert_preserva_id_con_conflicto_registrado(tmp_path: pathlib.Path) -> None:
    """Actualizar un mod con conflicto registrado no debe romper la FK.

    ``INSERT OR REPLACE`` borra+reinserta la fila (nuevo id) y, con
    ``foreign_keys=ON``, la fila de ``conflicts`` que apunta al id viejo
    bloquea la operación. El UPSERT debe preservar el id (review Codex #220).
    """
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    m1 = await db.add_mod("Immersive Armors", "1.0")
    m2 = await db.add_mod("Ordinator")
    await db.add_conflict(m1, m2, "record")

    # Refresco normal de versión: no debe lanzar IntegrityError ni cambiar el id.
    m1_again = await db.add_mod("Immersive Armors", "2.0")
    assert m1_again == m1

    mods = await db.get_mods()
    ia = next(m for m in mods if m["name"] == "Immersive Armors")
    assert ia["version"] == "2.0"

    # El conflicto sigue apuntando a un id válido y enriquece al nombre real.
    pending = await db.get_conflicts(resolved=False)
    assert pending[0]["mod_id_1"] == m1
    assert enrich_conflicts(pending, mods)[0]["mod_a"] == "Immersive Armors"


# ── ReactiveState: refresh de conflictos sin pisar stats de mods ────────────────
class _StubDB:
    """DB falsa: dos mods y un conflicto pendiente."""

    async def get_mods(self) -> list[dict]:
        return [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}]

    async def get_conflicts(self, resolved: bool | None = None) -> list[dict]:
        return [{"id": 7, "mod_id_1": 1, "mod_id_2": 2, "conflict_type": "record", "detected_at": "hoy"}]


def _make_state(tmp_path: pathlib.Path) -> tuple:
    from sky_claw.antigravity.gui import sky_claw_gui as gui
    from sky_claw.antigravity.gui.models.app_state import AppState
    from sky_claw.antigravity.gui.state.reactive_store import ReactiveStore

    store = ReactiveStore()
    state = gui.ReactiveState(app_state=AppState(config_path=tmp_path / "c.json"), store=store)
    return gui, state, store


async def test_refresh_conflicts_solo_toca_datos_de_conflictos(tmp_path: pathlib.Path, monkeypatch) -> None:
    """El resolver no debe pisar active_mods (fuente: registry vivo, no la DB GUI)."""
    gui, state, store = _make_state(tmp_path)
    state.active_mods.set(42)  # simula el valor escrito por _gui_mod_update_loop
    monkeypatch.setattr(gui, "get_db_agent", lambda: _StubDB())

    await state.refresh_conflicts()

    assert state.active_mods.get() == 42  # intacto
    assert state.conflicts_count.get() == 1
    lista = store.get("conflicts_list")
    assert lista[0]["mod_a"] == "Alpha"
    assert lista[0]["mod_b"] == "Beta"


async def test_conflict_detected_refresca_la_lista(tmp_path: pathlib.Path, monkeypatch) -> None:
    """El evento CONFLICT_DETECTED debe dejar contador y lista consistentes."""
    import asyncio

    from sky_claw.antigravity.gui import task_tracking
    from sky_claw.antigravity.gui.gui_event_adapter import EventType, SkyClawEvent

    gui, state, store = _make_state(tmp_path)
    monkeypatch.setattr(gui, "get_db_agent", lambda: _StubDB())
    monkeypatch.setattr(state, "notify", lambda *a, **k: None)  # sin contexto UI en tests

    state.handle_conflict_detected(SkyClawEvent(type=EventType.CONFLICT_DETECTED, data={"description": "x"}))
    await asyncio.gather(*list(task_tracking._BACKGROUND_TASKS))

    assert state.conflicts_count.get() == 1
    assert store.get("conflicts_list")[0]["mod_a"] == "Alpha"
