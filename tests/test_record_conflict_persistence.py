"""Tests del puente análisis→persistencia de conflictos de records (F6).

Espejo del puente de assets (#223), pero para el análisis PROFUNDO de xEdit:
``ConflictAnalyzer`` produce un ``ConflictReport`` con pares de plugins en
disputa a nivel record; este puente los convierte en pares y los persiste en
la tabla ``conflicts`` de la DB GUI, con la severidad peor del par como tipo.
"""

from __future__ import annotations

import pathlib

from sky_claw.antigravity.core.conflict_persistence import (
    pair_record_conflicts,
    persist_record_conflicts,
)
from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)


def _rc(severity: str, form_id: str = "0x001") -> RecordConflict:
    return RecordConflict(
        form_id=form_id,
        editor_id="SomeRecord",
        record_type="ARMO",
        winner="A.esp",
        losers=["B.esp"],
        severity=severity,
    )


def _report(*pairs: PluginConflictPair) -> ConflictReport:
    total = sum(len(p.conflicts) for p in pairs)
    critical = sum(1 for p in pairs for c in p.conflicts if c.severity == "critical")
    return ConflictReport(total_conflicts=total, critical_conflicts=critical, plugin_pairs=list(pairs))


# ── pair_record_conflicts (seam puro) ───────────────────────────────────────────
def test_un_par_por_plugin_pair_con_severidad_peor() -> None:
    report = _report(
        PluginConflictPair("A.esp", "B.esp", conflicts=[_rc("warning"), _rc("critical"), _rc("info")]),
    )
    pares = pair_record_conflicts(report)
    assert pares == [("A.esp", "B.esp", "record:critical")]


def test_severidad_warning_cuando_no_hay_critical() -> None:
    report = _report(PluginConflictPair("A.esp", "B.esp", conflicts=[_rc("info"), _rc("warning")]))
    assert pair_record_conflicts(report) == [("A.esp", "B.esp", "record:warning")]


def test_severidad_info_cuando_solo_hay_info() -> None:
    report = _report(PluginConflictPair("A.esp", "B.esp", conflicts=[_rc("info")]))
    assert pair_record_conflicts(report) == [("A.esp", "B.esp", "record:info")]


def test_ignora_pares_sin_conflictos() -> None:
    report = _report(PluginConflictPair("A.esp", "B.esp", conflicts=[]))
    assert pair_record_conflicts(report) == []


def test_multiples_pares_se_conservan() -> None:
    report = _report(
        PluginConflictPair("A.esp", "B.esp", conflicts=[_rc("critical")]),
        PluginConflictPair("C.esp", "D.esp", conflicts=[_rc("warning")]),
    )
    pares = pair_record_conflicts(report)
    assert ("A.esp", "B.esp", "record:critical") in pares
    assert ("C.esp", "D.esp", "record:warning") in pares
    assert len(pares) == 2


def test_reporte_vacio_devuelve_vacio() -> None:
    assert pair_record_conflicts(_report()) == []


# ── persist_record_conflicts (DB real en tmp) ───────────────────────────────────
async def test_persiste_pares_y_enriquece_con_nombres(tmp_path: pathlib.Path) -> None:
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()

    report = _report(PluginConflictPair("A.esp", "B.esp", conflicts=[_rc("critical")]))
    assert await persist_record_conflicts(report, db) == 1

    pendientes = await db.get_conflicts(resolved=False)
    assert len(pendientes) == 1
    assert pendientes[0]["conflict_type"] == "record:critical"

    from sky_claw.antigravity.gui.models.app_state import enrich_conflicts

    mods = await db.get_mods()
    enriquecido = enrich_conflicts(pendientes, mods)[0]
    assert {enriquecido["mod_a"], enriquecido["mod_b"]} == {"A.esp", "B.esp"}


async def test_es_idempotente_sobre_pendientes(tmp_path: pathlib.Path) -> None:
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    report = _report(PluginConflictPair("A.esp", "B.esp", conflicts=[_rc("critical")]))

    assert await persist_record_conflicts(report, db) == 1
    assert await persist_record_conflicts(report, db) == 0
    assert len(await db.get_conflicts(resolved=False)) == 1


async def test_no_pisa_metadatos_de_mods_existentes(tmp_path: pathlib.Path) -> None:
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    await db.add_mod("A.esp", "3.1", 500, "Nexusmods")

    report = _report(PluginConflictPair("A.esp", "B.esp", conflicts=[_rc("warning")]))
    await persist_record_conflicts(report, db)

    a = next(m for m in await db.get_mods() if m["name"] == "A.esp")
    assert a["version"] == "3.1"
    assert a["size_mb"] == 500
    assert a["source"] == "Nexusmods"


async def test_reporte_vacio_no_persiste_nada(tmp_path: pathlib.Path) -> None:
    db = DatabaseAgent(str(tmp_path / "state.db"))
    await db.init_db()
    assert await persist_record_conflicts(_report(), db) == 0
    assert await db.get_conflicts(resolved=False) == []
