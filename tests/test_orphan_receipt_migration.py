"""Pruebas de migración SQLite, backfill y compatibilidad legacy para Issue #506.

Verifica experimentalmente:
- La inviabilidad de D1 (CREATE TABLE IF NOT EXISTS no altera esquemas existentes en SQLite).
- La validez, idempotencia y corrección de D4 (nueva tabla artifact_evidence_resolutions + backfill).
- El comportamiento del oráculo futuro bajo la política FAIL_CLOSED_REACTIVATION para cada estado legacy.
- La ausencia de resurrección de transacciones ROLLED_BACK sin receipt.

Convención del repositorio: tests y comentarios en español.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3

import aiosqlite
import pytest

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleManager
from sky_claw.app.db.handoffs import (
    ARTIFACT_RESOLUTIONS_SCHEMA_SQL,
    clave_de_artifact,
)
from sky_claw.app.db.journal import (
    JournalConnectionError,
    JournalTransactionError,
    OperationJournal,
    TransactionStatus,
    _canonica_y_prefijo,
    _nombra_objetivo,
    _registrar_resoluciones_de_artifact_en_conn,
    _rutas_de_metadata,
)

# =============================================================================
# ESQUEMA ACTUAL DE MAIN (LEGACY BASELINE)
# =============================================================================

LEGACY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mod_id INTEGER,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    committed_at TEXT,
    rolled_back_at TEXT
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    agent_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    target_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    snapshot_path TEXT,
    checksum TEXT,
    metadata TEXT,
    rolled_back INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deployment_handoffs (
    handoff_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_tx_id       INTEGER NOT NULL REFERENCES transactions(transaction_id),
    state              TEXT NOT NULL DEFAULT 'awaiting_deployment',
    artifact_path      TEXT NOT NULL,
    game_key           TEXT NOT NULL,
    mods_root_key      TEXT NOT NULL,
    data_key           TEXT NOT NULL,
    expected_profile   TEXT NOT NULL,
    expected_digest    TEXT,
    expected_files     INTEGER,
    expected_bytes     INTEGER,
    observed_digest    TEXT,
    observed_files     INTEGER,
    observed_bytes     INTEGER,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at       TEXT,
    superseded_at      TEXT,
    superseded_by      INTEGER REFERENCES deployment_handoffs(handoff_id)
);

CREATE TABLE IF NOT EXISTS stale_pending_sweep_receipts (
    receipt_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(transaction_id),
    state          TEXT NOT NULL DEFAULT 'unresolved',
    swept_at       TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT
);

CREATE TABLE IF NOT EXISTS orphan_evidence_absorptions (
    absorption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(transaction_id),
    artifact_path TEXT NOT NULL,
    handoff_id INTEGER NOT NULL REFERENCES deployment_handoffs(handoff_id),
    absorbed_at TEXT NOT NULL DEFAULT (datetime('now')),
    CONSTRAINT uq_absorpcion_tx_artifact UNIQUE (transaction_id, artifact_path)
);
"""

# =============================================================================
# MODELO TEÓRICO ORÁCULO D4 (Referencia formal para comparar contra producción)
# =============================================================================


async def oracle_futuro_d4_modelo(conn: aiosqlite.Connection, objetivo_path: str) -> set[int]:
    """Evaluación teórica del oráculo propuesto bajo D4:

    Candidate(tx, A) =
        (status(tx) == PENDING OR (status(tx) == ROLLED_BACK AND EXISTS receipt(tx)))
        AND manifest_names(tx, A)
        AND NOT EXISTS resolution(tx, A)
    """
    objetivo_fisico, prefijo = _canonica_y_prefijo(objetivo_path)

    sql = """
        SELECT je.transaction_id, t.status, je.metadata, r.receipt_id
        FROM journal_entries je
        JOIN transactions t ON t.transaction_id = je.transaction_id
        LEFT JOIN stale_pending_sweep_receipts r ON r.transaction_id = je.transaction_id
        WHERE je.metadata IS NOT NULL AND t.status IN ('pending', 'rolled_back')
    """
    sql_resueltas = "SELECT transaction_id FROM artifact_evidence_resolutions WHERE artifact_path = ?"

    async with conn.execute(sql) as cursor:
        filas = [tuple(f) async for f in cursor]
    async with conn.execute(sql_resueltas, (objetivo_fisico,)) as cursor:
        resueltas = {int(f[0]) async for f in cursor}

    candidatas: set[int] = set()
    for tx_id, status, metadata, receipt_id in filas:
        es_candidata_causal = (status == TransactionStatus.PENDING.value) or (
            status == TransactionStatus.ROLLED_BACK.value and receipt_id is not None
        )
        if not es_candidata_causal:
            continue

        for ruta in _rutas_de_metadata(metadata):
            if _nombra_objetivo(ruta, objetivo_fisico, prefijo):
                candidatas.add(int(tx_id))
                break

    return candidatas - resueltas


# =============================================================================
# TESTS DE AUDITORÍA DE MIGRACIÓN (MIG506)
# =============================================================================


def test_d1_create_if_not_exists_no_altera_tabla_existente() -> None:
    """Demostración empírica: en SQLite, CREATE TABLE IF NOT EXISTS sobre una
    tabla existente con `handoff_id NOT NULL` NO altera la columna a NULLABLE."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(LEGACY_SCHEMA_SQL)

    conn.execute("INSERT INTO transactions (description) VALUES ('test')")
    conn.execute(
        "INSERT INTO deployment_handoffs (source_tx_id, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
        "VALUES (1, 'path/a', 'g', 'm', 'd', 'p')"
    )
    conn.execute(
        "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (1, 'path/a', 1)"
    )
    conn.commit()

    schema_d1_ingenuo = """
    CREATE TABLE IF NOT EXISTS orphan_evidence_absorptions (
        absorption_id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id INTEGER NOT NULL REFERENCES transactions(transaction_id),
        artifact_path TEXT NOT NULL,
        handoff_id INTEGER NULL REFERENCES deployment_handoffs(handoff_id),
        reason TEXT NOT NULL DEFAULT 'absorbed',
        absorbed_at TEXT NOT NULL DEFAULT (datetime('now')),
        CONSTRAINT uq_absorpcion_tx_artifact UNIQUE (transaction_id, artifact_path)
    );
    """
    conn.executescript(schema_d1_ingenuo)

    cur = conn.execute("PRAGMA table_info(orphan_evidence_absorptions)")
    cols = {row[1]: {"type": row[2], "notnull": row[3]} for row in cur.fetchall()}

    assert cols["handoff_id"]["notnull"] == 1, "CREATE TABLE IF NOT EXISTS no modificó la columna"
    assert "reason" not in cols, "CREATE TABLE IF NOT EXISTS no añadió la columna nueva"

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (2, 'path/b', NULL)"
        )


@pytest.mark.asyncio
async def test_mig506_01_db_actual_preserva_absorciones(tmp_path: pathlib.Path) -> None:
    """MIG506-01: DB actual con absorciones históricas se migra a D4 sin pérdida vía OperationJournal.open()."""
    db_file = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (description) VALUES ('tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (1, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id, absorbed_at) "
            "VALUES (1, 'c:/mods/texgen', 1, '2026-08-20 12:00:00')"
        )
        await conn.commit()

    j = OperationJournal(db_file)
    await j.open()
    await j.close()

    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute(
            "SELECT transaction_id, artifact_path, resolution_kind, handoff_id, resolved_at "
            "FROM artifact_evidence_resolutions"
        ) as cur,
    ):
        filas = await cur.fetchall()

    assert len(filas) == 1
    tx_id, art_path, kind, h_id, res_at = filas[0]
    assert tx_id == 1
    assert art_path == "c:/mods/texgen"
    assert kind == "absorbed_by_handoff"
    assert h_id == 1
    assert res_at == "2026-08-20 12:00:00"


@pytest.mark.asyncio
async def test_mig506_02_migracion_idempotente(tmp_path: pathlib.Path) -> None:
    """MIG506-02: Reabrir el OperationJournal múltiples veces no duplica filas ni produce errores."""
    db_file = tmp_path / "idempotent.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (description) VALUES ('tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (1, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 1)"
        )
        await conn.commit()

    for _ in range(3):
        j = OperationJournal(db_file)
        await j.open()
        await j.close()

    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur,
    ):
        (conteo,) = await cur.fetchone()
    assert conteo == 1


@pytest.mark.asyncio
async def test_mig506_03_consumed_con_absorption_a_preserva_b_activo(tmp_path: pathlib.Path) -> None:
    """MIG506-03: LEGACY CONSUMED con absorción de A -> A queda resuelto y B activo en el oráculo de producción."""
    db_file = tmp_path / "consumed.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")
    path_b = str(tmp_path / "mods" / "DynDOLOD Output")
    clave_a = clave_de_artifact(pathlib.Path(path_a))

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO transactions (transaction_id, description, status) VALUES (1, 'tx1', 'rolled_back')"
        )
        await conn.execute("INSERT INTO stale_pending_sweep_receipts (transaction_id, state) VALUES (1, 'consumed')")
        manifest = json.dumps({"files_touched": [path_a, path_b]})
        await conn.execute(
            "INSERT INTO journal_entries (transaction_id, agent_id, operation_type, target_path, metadata) "
            "VALUES (1, 'agent', 'mod_install', ?, ?)",
            (path_a, manifest),
        )
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (10, 1, 'awaiting_deployment', ?, 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)",
            (clave_a,),
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (1, ?, 10)",
            (clave_a,),
        )
        await conn.commit()

    j = OperationJournal(db_file)
    await j.open()
    try:
        # Evaluar oráculo de producción
        candidatas_prod_a = await j.transacciones_que_nombran(path_a)
        candidatas_prod_b = await j.transacciones_que_nombran(path_b)

        assert candidatas_prod_a == [], "A debe estar resuelto (excluido)"
        assert candidatas_prod_b == [1], "B debe seguir ACTIVO como evidencia (no resuelto)"

        # Comparar contra modelo teórico
        async with aiosqlite.connect(str(db_file)) as conn:
            cand_mod_a = await oracle_futuro_d4_modelo(conn, path_a)
            cand_mod_b = await oracle_futuro_d4_modelo(conn, path_b)
            assert set(candidatas_prod_a) == cand_mod_a
            assert set(candidatas_prod_b) == cand_mod_b
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_mig506_04_superseded_run1_fail_closed_reactivation(tmp_path: pathlib.Path) -> None:
    """MIG506-04: LEGACY SUPERSEDED RUN1 sin absorción -> reactivación fail-closed para A y B."""
    db_file = tmp_path / "superseded_run1.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")
    path_b = str(tmp_path / "mods" / "DynDOLOD Output")

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO transactions (transaction_id, description, status) VALUES (1, 'tx1', 'rolled_back')"
        )
        await conn.execute("INSERT INTO stale_pending_sweep_receipts (transaction_id, state) VALUES (1, 'superseded')")
        manifest = json.dumps({"files_touched": [path_a, path_b]})
        await conn.execute(
            "INSERT INTO journal_entries (transaction_id, agent_id, operation_type, target_path, metadata) "
            "VALUES (1, 'agent', 'mod_install', ?, ?)",
            (path_a, manifest),
        )
        await conn.commit()

    j = OperationJournal(db_file)
    await j.open()
    try:
        candidatas_a = await j.transacciones_que_nombran(path_a)
        candidatas_b = await j.transacciones_que_nombran(path_b)
        assert candidatas_a == [1], "A es evidencia activa (fail-closed)"
        assert candidatas_b == [1], "B es evidencia activa (fail-closed)"
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_mig506_05_no_artifact_global_no_inventa_resolucion(tmp_path: pathlib.Path) -> None:
    """MIG506-05: LEGACY NO_ARTIFACT global -> no inventa resolución para A ni B."""
    db_file = tmp_path / "no_artifact.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")
    path_b = str(tmp_path / "mods" / "DynDOLOD Output")

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO transactions (transaction_id, description, status) VALUES (1, 'tx1', 'rolled_back')"
        )
        await conn.execute("INSERT INTO stale_pending_sweep_receipts (transaction_id, state) VALUES (1, 'no_artifact')")
        manifest = json.dumps({"files_touched": [path_a, path_b]})
        await conn.execute(
            "INSERT INTO journal_entries (transaction_id, agent_id, operation_type, target_path, metadata) "
            "VALUES (1, 'agent', 'mod_install', ?, ?)",
            (path_a, manifest),
        )
        await conn.commit()

    j = OperationJournal(db_file)
    await j.open()
    try:
        candidatas_a = await j.transacciones_que_nombran(path_a)
        candidatas_b = await j.transacciones_que_nombran(path_b)
        assert candidatas_a == [1]
        assert candidatas_b == [1]
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_mig506_06_rolled_back_sin_receipt_no_resucita(tmp_path: pathlib.Path) -> None:
    """MIG506-06: TX ROLLED_BACK sin receipt (rollback manual/operacional) NO resucita."""
    db_file = tmp_path / "normal_rollback.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO transactions (transaction_id, description, status) VALUES (1, 'tx1', 'rolled_back')"
        )
        manifest = json.dumps({"files_touched": [path_a]})
        await conn.execute(
            "INSERT INTO journal_entries (transaction_id, agent_id, operation_type, target_path, metadata) "
            "VALUES (1, 'agent', 'mod_install', ?, ?)",
            (path_a, manifest),
        )
        await conn.commit()

    j = OperationJournal(db_file)
    await j.open()
    try:
        candidatas_a = await j.transacciones_que_nombran(path_a)
        assert candidatas_a == [], "TX rolled back sin receipt jamás debe considerarse evidencia"
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_mig506_07_unresolved_continua_siendo_causalidad_valida(tmp_path: pathlib.Path) -> None:
    """MIG506-07: LEGACY UNRESOLVED continúa siendo evidencia válida para todos sus artifacts."""
    db_file = tmp_path / "unresolved.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")
    path_b = str(tmp_path / "mods" / "DynDOLOD Output")

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO transactions (transaction_id, description, status) VALUES (1, 'tx1', 'rolled_back')"
        )
        await conn.execute("INSERT INTO stale_pending_sweep_receipts (transaction_id, state) VALUES (1, 'unresolved')")
        manifest = json.dumps({"files_touched": [path_a, path_b]})
        await conn.execute(
            "INSERT INTO journal_entries (transaction_id, agent_id, operation_type, target_path, metadata) "
            "VALUES (1, 'agent', 'mod_install', ?, ?)",
            (path_a, manifest),
        )
        await conn.commit()

    j = OperationJournal(db_file)
    await j.open()
    try:
        candidatas_a = await j.transacciones_que_nombran(path_a)
        candidatas_b = await j.transacciones_que_nombran(path_b)
        assert candidatas_a == [1]
        assert candidatas_b == [1]
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_mig506_08_failure_durante_backfill_revierte_totalmente(tmp_path: pathlib.Path) -> None:
    """MIG506-08: Fallo durante backfill en OperationJournal.open() revierte integralmente filas D4 y deja DB limpia."""
    db_file = tmp_path / "fail_boundary.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (2, 'tx2')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (10, 1, 'superseded', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (20, 2, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:def', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (1, 'c:/mods/texgen', 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (2, 'c:/mods/texgen', 10)"
        )
        # Pre-poblar resolución conflictiva para tx2 que obligue fallo por inconsistencia
        await conn.executescript(ARTIFACT_RESOLUTIONS_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
            "VALUES (2, 'c:/mods/texgen', 'absorbed_by_handoff', 20)"
        )
        await conn.commit()

    j = OperationJournal(db_file)
    with pytest.raises(JournalConnectionError, match="Corrupción detectada durante backfill"):
        await j.open()

    # Segunda conexión independiente a disco verifica:
    # 1. tx1 no fue insertada (cero mutación residual de D4)
    # 2. La DB permanece consultable tras el rollback fallido
    async with aiosqlite.connect(str(db_file)) as conn:
        async with conn.execute(
            "SELECT transaction_id FROM artifact_evidence_resolutions WHERE transaction_id = 1"
        ) as cur:
            fila_tx1 = await cur.fetchone()
        assert fila_tx1 is None, "tx1 no debe haber quedado backfilleada tras rollback de migración"


@pytest.mark.asyncio
async def test_mig506_09_db_ya_migrada_reopen_idempotente(tmp_path: pathlib.Path) -> None:
    """MIG506-09: Reabrir una DB ya migrada no borra ni corrompe resoluciones preexistentes."""
    db_file = tmp_path / "migrated.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO transactions (transaction_id, description, status) VALUES (1, 'tx1', 'rolled_back')"
        )
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (1, 1, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id, absorbed_at) "
            "VALUES (1, 'c:/mods/texgen', 1, '2026-08-26 10:00:00')"
        )
        await conn.execute("INSERT INTO stale_pending_sweep_receipts (transaction_id, state) VALUES (1, 'unresolved')")
        await conn.commit()

    # 1. Primera migración
    j1 = OperationJournal(db_file)
    await j1.open()
    await j1.resolver_evidencia_como_no_artifact("c:/mods/dyndolod", [1])
    await j1.close()

    # 2. Reopen
    j2 = OperationJournal(db_file)
    await j2.open()
    await j2.close()

    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur,
    ):
        (conteo,) = await cur.fetchone()
    assert conteo == 2


@pytest.mark.asyncio
async def test_mig506_10_conflicting_backfill_fails_closed(tmp_path: pathlib.Path) -> None:
    """MIG506-10: Discrepancia en backfill detectada por validación posterior falla cerrado."""
    db_file = tmp_path / "corrupt_backfill.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (10, 1, 'superseded', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (20, 1, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:def', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 10)"
        )
        await conn.executescript(ARTIFACT_RESOLUTIONS_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 'absorbed_by_handoff', 20)"
        )
        await conn.commit()

    j = OperationJournal(db_file)
    with pytest.raises(JournalConnectionError, match="Corrupción detectada durante backfill de resoluciones"):
        await j.open()


@pytest.mark.asyncio
async def test_mig506_12_timestamp_conflict_fails_closed(tmp_path: pathlib.Path) -> None:
    """MIG506-TIMESTAMP-CONFLICT: D4 preexistente con resolved_at != absorbed_at
    debe fallar cerrado en open(). La validación del backfill compara TAMBIÉN el
    timestamp histórico (F-04 #506), no sólo resolution_kind y handoff_id: una
    fila D4 con la misma semántica pero distinto resolved_at NO es equivalente."""
    db_file = tmp_path / "timestamp_conflict.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (10, 1, 'superseded', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id, absorbed_at) "
            "VALUES (1, 'c:/mods/texgen', 10, '2026-08-20 12:00:00')"
        )
        await conn.executescript(ARTIFACT_RESOLUTIONS_SCHEMA_SQL)
        # Misma semántica (kind + handoff) pero resolved_at histórico distinto:
        # la validación debe rechazarla como "migración equivalente".
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id, resolved_at) "
            "VALUES (1, 'c:/mods/texgen', 'absorbed_by_handoff', 10, '2099-01-01 00:00:00')"
        )
        await conn.commit()

    j = OperationJournal(db_file)
    with pytest.raises(JournalConnectionError, match="Corrupción detectada durante backfill de resoluciones"):
        await j.open()

    # La fila D4 preexistente no fue mutada ni duplicada por el intento de backfill.
    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute(
            "SELECT COUNT(*), SUM(resolved_at = '2099-01-01 00:00:00') FROM artifact_evidence_resolutions"
        ) as cur,
    ):
        fila = await cur.fetchone()
    assert fila[0] == 1, "No debe haber filas extra tras el fail-closed"
    assert fila[1] == 1, "La fila D4 preexistente no debe ser sobrescrita"


@pytest.mark.asyncio
async def test_mig506_11_concurrent_open_legacy_db(tmp_path: pathlib.Path) -> None:
    """MIG506-11 / F506-MIG4: Múltiples instancias independientes abriendo concurrentemente una DB legacy migran limpiamente."""
    db_file = tmp_path / "concurrent_open.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (1, 1, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 1)"
        )
        await conn.commit()

    mgr1 = DatabaseLifecycleManager()
    mgr2 = DatabaseLifecycleManager()
    j1 = OperationJournal(db_file, lifecycle=mgr1)
    j2 = OperationJournal(db_file, lifecycle=mgr2)

    try:
        await asyncio.gather(j1.open(), j2.open())
        async with aiosqlite.connect(str(db_file)) as conn:
            async with conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur:
                (conteo,) = await cur.fetchone()
            assert conteo == 1, "D4 debe aparecer una sola vez con backfill completo"
    finally:
        await asyncio.gather(mgr1.shutdown_all(), mgr2.shutdown_all())

    # Reopen posterior limpio
    j3 = OperationJournal(db_file)
    await j3.open()
    await j3.close()


@pytest.mark.asyncio
async def test_res506_conflict_raises_journal_transaction_error(tmp_path: pathlib.Path) -> None:
    """RES506-CONFLICT: Registrar una resolución en conflicto con diferente kind/handoff falla cerrado."""
    db_file = tmp_path / "conflict_res.db"
    j = OperationJournal(db_file)
    await j.open()
    try:
        async with aiosqlite.connect(str(db_file)) as conn:
            # 1. Registrar primera resolución
            await _registrar_resoluciones_de_artifact_en_conn(
                conn,
                transaction_ids=[100],
                artifact_path="c:/mods/texgen",
                resolution_kind="no_artifact_demonstrated",
                handoff_id=None,
            )
            await conn.commit()

            # 2. Registrar idéntica (debe ser un no-op idempotente)
            await _registrar_resoluciones_de_artifact_en_conn(
                conn,
                transaction_ids=[100],
                artifact_path="c:/mods/texgen",
                resolution_kind="no_artifact_demonstrated",
                handoff_id=None,
            )
            await conn.commit()

            # 3. Registrar resolución conflictiva (mismo tx/artifact, distinta provenance) -> debe fallar
            with pytest.raises(JournalTransactionError, match="Conflicto de resolución"):
                await _registrar_resoluciones_de_artifact_en_conn(
                    conn,
                    transaction_ids=[100],
                    artifact_path="c:/mods/texgen",
                    resolution_kind="absorbed_by_handoff",
                    handoff_id=5,
                )
    finally:
        await j.close()


# =============================================================================
# CONCURRENCIA REAL DEL WRITER RUNTIME D4 (F-03 #506)
# =============================================================================


@pytest.mark.asyncio
async def test_res506_concurrent_exact_dos_conexiones(tmp_path: pathlib.Path) -> None:
    """RES506-CONCURRENT-EXACT: dos conexiones SQLite reales e independientes
    (dos managers, WAL) registran EXACTAMENTE la misma resolución de forma
    concurrente → CALLER_1=SUCCESS, CALLER_2=SUCCESS, ROW_COUNT=1.
    EXACT_DUPLICATE == IDEMPOTENT_SUCCESS, sin IntegrityError."""
    db_file = tmp_path / "concurrent_exact.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO transactions (transaction_id, description, status) VALUES (1, 'tx1', 'rolled_back')"
        )
        await conn.execute("INSERT INTO stale_pending_sweep_receipts (transaction_id, state) VALUES (1, 'unresolved')")
        await conn.commit()

    mgr1 = DatabaseLifecycleManager()
    mgr2 = DatabaseLifecycleManager()
    j1 = OperationJournal(db_file, lifecycle=mgr1)
    j2 = OperationJournal(db_file, lifecycle=mgr2)
    try:
        await j1.open()
        await j2.open()
        # Carrera real: cada journal usa la conexión de SU manager (conexiones
        # físicas distintas al mismo archivo, WAL). Ni mock ni serialización:
        # el arbitraje del (tx, artifact) lo hace el UNIQUE de SQLite.
        await asyncio.gather(
            j1.resolver_evidencia_como_no_artifact("c:/mods/texgen", [1]),
            j2.resolver_evidencia_como_no_artifact("c:/mods/texgen", [1]),
        )
    finally:
        await j1.close()
        await j2.close()
        await asyncio.gather(mgr1.shutdown_all(), mgr2.shutdown_all())

    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur,
    ):
        (conteo,) = await cur.fetchone()
    assert conteo == 1, "El duplicado exacto concurrente no debe crear una segunda fila"

    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute(
            "SELECT transaction_id, artifact_path, resolution_kind, handoff_id FROM artifact_evidence_resolutions"
        ) as cur,
    ):
        fila = await cur.fetchone()
    objetivo_esperado, _ = _canonica_y_prefijo("c:/mods/texgen")
    assert fila == (1, objetivo_esperado, "no_artifact_demonstrated", None), "Fila exacta"


@pytest.mark.asyncio
async def test_res506_concurrent_conflict_dos_conexiones(tmp_path: pathlib.Path) -> None:
    """RES506-CONCURRENT-CONFLICT: dos conexiones reales registran semánticas
    DISTINTAS para el mismo (tx, artifact) → exactamente una queda durable, la
    otra falla cerrado con JournalTransactionError (nunca overwrite, nunca dos
    filas, nunca IntegrityError crudo como resultado de contrato)."""
    db_file = tmp_path / "concurrent_conflict.db"
    # WAL: lectores no bloquean escritores y el perdedor espera el commit del
    # ganador en el busy handler — carrera real sin deadlock del journal mode
    # rollback ni BUSY espurio.
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.executescript(ARTIFACT_RESOLUTIONS_SCHEMA_SQL)
        await conn.commit()

    # Timeout generoso: el perdedor sólo tiene que esperar el commit del ganador.
    conn1 = await aiosqlite.connect(str(db_file), timeout=30.0)
    conn2 = await aiosqlite.connect(str(db_file), timeout=30.0)
    try:

        async def _registrar(conn: aiosqlite.Connection, kind: str, handoff: int | None) -> None:
            await _registrar_resoluciones_de_artifact_en_conn(
                conn,
                transaction_ids=[1],
                artifact_path="c:/mods/texgen",
                resolution_kind=kind,
                handoff_id=handoff,
            )
            await conn.commit()

        resultado1, resultado2 = await asyncio.gather(
            _registrar(conn1, "no_artifact_demonstrated", None),
            _registrar(conn2, "absorbed_by_handoff", 10),
            return_exceptions=True,
        )

        # Exactamente un caller gana; el perdedor falla cerrado con el tipo de
        # contrato — un IntegrityError crudo sería exactamente el defecto F-03.
        errores = [r for r in (resultado1, resultado2) if isinstance(r, BaseException)]
        assert len(errores) == 1, f"Debe fallar exactamente un caller: {(resultado1, resultado2)}"
        assert isinstance(errores[0], JournalTransactionError), f"Tipo de contrato: {errores[0]!r}"
    finally:
        await conn1.close()
        await conn2.close()

    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur,
    ):
        (conteo,) = await cur.fetchone()
    assert conteo == 1, "Nunca dos filas ni mutación parcial"

    async with (
        aiosqlite.connect(str(db_file)) as conn,
        conn.execute("SELECT resolution_kind, handoff_id FROM artifact_evidence_resolutions") as cur,
    ):
        fila = await cur.fetchone()
    ganador = ("no_artifact_demonstrated", None) if resultado1 is None else ("absorbed_by_handoff", 10)
    assert fila == ganador, "La semántica durable debe ser la del caller ganador"


# =============================================================================
# TESTS DE DURABILIDAD Y ROLLBACK DE MIGRACIÓN (F506-MIG1..4)
# =============================================================================


@pytest.mark.asyncio
async def test_f506_mig1_migracion_durable_antes_del_sweep(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F506-MIG1: La migración D4 es durable incluso si el sweep de arranque falla."""
    db_file = tmp_path / "mig1.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (10, 1, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id, absorbed_at) "
            "VALUES (1, 'c:/mods/texgen', 10, '2026-08-20 12:00:00')"
        )
        await conn.commit()

    # Inyectar fallo en sweep_stale_pending
    async def _fail_sweep(*args, **kwargs):  # noqa: ANN002, ANN003
        raise sqlite3.OperationalError("Simulated sweep failure")

    monkeypatch.setattr(OperationJournal, "sweep_stale_pending", _fail_sweep)

    j = OperationJournal(db_file)
    await j.open()
    await j.close()

    # Abrir una SEGUNDA conexión independiente a disco y verificar que D4 está guardado
    async with aiosqlite.connect(str(db_file)) as conn:
        async with conn.execute(
            "SELECT transaction_id, artifact_path, resolution_kind, handoff_id FROM artifact_evidence_resolutions"
        ) as cur:
            filas = await cur.fetchall()
        assert len(filas) == 1, "La resolución debe estar persistida duraderamente a pesar del fallo del sweep"
        assert filas[0] == (1, "c:/mods/texgen", "absorbed_by_handoff", 10)


@pytest.mark.asyncio
async def test_f506_mig2_migration_conflict_lifecycle_rollback(tmp_path: pathlib.Path) -> None:
    """F506-MIG2: Conflicto en migración bajo DatabaseLifecycleManager revierte y deja conexión limpia."""
    from sky_claw.app.db.handoffs import ARTIFACT_RESOLUTIONS_SCHEMA_SQL
    from sky_claw.app.db.journal import JournalConnectionError

    db_file = tmp_path / "mig2.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (2, 'tx2')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (10, 1, 'superseded', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (20, 2, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:def', 1, 10)"
        )
        # Absorción legacy para tx1 compatible (apunta a 10)
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 10)"
        )
        # Absorción legacy para tx2 (apunta a 10)
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (2, 'c:/mods/texgen', 10)"
        )
        # Pre-poblar tabla D4 con conflicto para tx2 (apunta a handoff 20)
        await conn.executescript(ARTIFACT_RESOLUTIONS_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
            "VALUES (2, 'c:/mods/texgen', 'absorbed_by_handoff', 20)"
        )
        await conn.commit()

    mgr = DatabaseLifecycleManager()
    j = OperationJournal(db_file, lifecycle=mgr)
    try:
        with pytest.raises(JournalConnectionError, match="Corrupción detectada durante backfill"):
            await j.open()

        # Comprobar con la conexión administrada que no quedó backfill parcial de tx1
        async with mgr.operation(db_file) as conn:
            async with conn.execute(
                "SELECT transaction_id FROM artifact_evidence_resolutions WHERE transaction_id = 1"
            ) as cur:
                fila = await cur.fetchone()
            assert fila is None, "tx1 no debe haber quedado backfilleada"
            assert not conn.in_transaction, "Conexión compartida no debe quedar in_transaction"
    finally:
        await mgr.shutdown_all()


@pytest.mark.asyncio
async def test_f506_mig3_migration_conflict_standalone_rollback(tmp_path: pathlib.Path) -> None:
    """F506-MIG3: Conflicto en migración standalone revierte y deja DB limpia."""
    from sky_claw.app.db.handoffs import ARTIFACT_RESOLUTIONS_SCHEMA_SQL
    from sky_claw.app.db.journal import JournalConnectionError

    db_file = tmp_path / "mig3.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (2, 'tx2')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (10, 1, 'superseded', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:abc', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (20, 2, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p', 'sha256:def', 1, 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 10)"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (2, 'c:/mods/texgen', 10)"
        )
        await conn.executescript(ARTIFACT_RESOLUTIONS_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
            "VALUES (2, 'c:/mods/texgen', 'absorbed_by_handoff', 20)"
        )
        await conn.commit()

    j = OperationJournal(db_file)
    with pytest.raises(JournalConnectionError, match="Corrupción detectada durante backfill"):
        await j.open()

    # Segunda conexión verifica que tx1 no quedó insertada
    async with aiosqlite.connect(str(db_file)) as conn:
        async with conn.execute(
            "SELECT transaction_id FROM artifact_evidence_resolutions WHERE transaction_id = 1"
        ) as cur:
            fila = await cur.fetchone()
        assert fila is None, "tx1 no debe existir tras rollback"
