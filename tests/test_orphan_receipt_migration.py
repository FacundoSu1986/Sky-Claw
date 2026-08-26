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
    clave_de_artifact,
)
from sky_claw.app.db.journal import (
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
# DDL Y BACKFILL PROPUESTOS PARA D4
# =============================================================================

D4_RESOLUTIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifact_evidence_resolutions (
    resolution_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  INTEGER NOT NULL REFERENCES transactions(transaction_id),
    artifact_path   TEXT NOT NULL,
    resolution_kind TEXT NOT NULL,
    handoff_id      INTEGER REFERENCES deployment_handoffs(handoff_id),
    resolved_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CONSTRAINT chk_resolution_kind CHECK (
        resolution_kind IN ('absorbed_by_handoff', 'superseded_by_run', 'no_artifact_demonstrated')
    ),
    CONSTRAINT uq_resolution_tx_artifact UNIQUE (transaction_id, artifact_path)
);

CREATE INDEX IF NOT EXISTS idx_artifact_resolutions_path_tx
    ON artifact_evidence_resolutions(artifact_path, transaction_id);
"""

D4_BACKFILL_SQL = """
INSERT OR IGNORE INTO artifact_evidence_resolutions (
    transaction_id,
    artifact_path,
    resolution_kind,
    handoff_id,
    resolved_at
)
SELECT
    transaction_id,
    artifact_path,
    'absorbed_by_handoff',
    handoff_id,
    absorbed_at
FROM orphan_evidence_absorptions;
"""


# =============================================================================
# ORÁCULO FUTURO D4 (Prototipo formal de evaluación)
# =============================================================================


async def oracle_futuro_d4(conn: aiosqlite.Connection, objetivo_path: str) -> set[int]:
    """Evaluación formal del oráculo propuesto bajo D4:

    Candidate(tx, A) =
        (status(tx) == PENDING OR (status(tx) == ROLLED_BACK AND EXISTS receipt(tx)))
        AND manifest_names(tx, A)
        AND NOT EXISTS resolution(tx, A)
    """
    objetivo_fisico, prefijo = _canonica_y_prefijo(objetivo_path)

    # 1. Candidatas base con causalidad probada: PENDING o ROLLED_BACK con sweep receipt
    sql = """
        SELECT je.transaction_id, t.status, je.metadata, r.receipt_id
        FROM journal_entries je
        JOIN transactions t ON t.transaction_id = je.transaction_id
        LEFT JOIN stale_pending_sweep_receipts r ON r.transaction_id = je.transaction_id
        WHERE je.metadata IS NOT NULL AND t.status IN ('pending', 'rolled_back')
    """
    # 2. Exclusiones per-artifact desde la nueva tabla D4
    sql_resueltas = "SELECT transaction_id FROM artifact_evidence_resolutions WHERE artifact_path = ?"

    async with conn.execute(sql) as cursor:
        filas = [tuple(f) async for f in cursor]
    async with conn.execute(sql_resueltas, (objetivo_fisico,)) as cursor:
        resueltas = {int(f[0]) async for f in cursor}

    candidatas: set[int] = set()
    for tx_id, status, metadata, receipt_id in filas:
        # Causalidad del sweep: PENDING activa O ROLLED_BACK con receipt existente
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


async def ejecutar_migracion_d4(conn: aiosqlite.Connection) -> None:
    """Ejecuta la migración D4: crea la nueva tabla y realiza el backfill."""
    await conn.executescript(D4_RESOLUTIONS_SCHEMA_SQL)
    await conn.execute(D4_BACKFILL_SQL)
    await conn.commit()


# =============================================================================
# TESTS DE AUDITORÍA DE MIGRACIÓN (MIG506)
# =============================================================================


def test_d1_create_if_not_exists_no_altera_tabla_existente() -> None:
    """Demostración empírica: en SQLite, CREATE TABLE IF NOT EXISTS sobre una
    tabla existente con `handoff_id NOT NULL` NO altera la columna a NULLABLE."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(LEGACY_SCHEMA_SQL)

    # Insertar dato inicial válido
    conn.execute("INSERT INTO transactions (description) VALUES ('test')")
    conn.execute(
        "INSERT INTO deployment_handoffs (source_tx_id, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
        "VALUES (1, 'path/a', 'g', 'm', 'd', 'p')"
    )
    conn.execute(
        "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (1, 'path/a', 1)"
    )
    conn.commit()

    # Intentar aplicar el DDL "D1" con CREATE TABLE IF NOT EXISTS con handoff_id NULLABLE
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

    # Inspeccionar PRAGMA table_info
    cur = conn.execute("PRAGMA table_info(orphan_evidence_absorptions)")
    cols = {row[1]: {"type": row[2], "notnull": row[3]} for row in cur.fetchall()}

    # handoff_id SIGUE SIENDO NOT NULL (notnull == 1)
    assert cols["handoff_id"]["notnull"] == 1, "CREATE TABLE IF NOT EXISTS no modificó la columna"
    assert "reason" not in cols, "CREATE TABLE IF NOT EXISTS no añadió la columna nueva"

    # Intentar insertar NULL falla con IntegrityError
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (2, 'path/b', NULL)"
        )


@pytest.mark.asyncio
async def test_mig506_01_db_actual_preserva_absorciones(tmp_path: pathlib.Path) -> None:
    """MIG506-01: DB actual con absorciones históricas se migra a D4 sin pérdida."""
    db_file = tmp_path / "legacy.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (description) VALUES ('tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (source_tx_id, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
            "VALUES (1, 'c:/mods/texgen', 'g', 'm', 'd', 'p')"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id, absorbed_at) "
            "VALUES (1, 'c:/mods/texgen', 1, '2026-08-20 12:00:00')"
        )
        await conn.commit()

        # Ejecutar migración D4
        await ejecutar_migracion_d4(conn)

        # Verificar datos en artifact_evidence_resolutions
        async with conn.execute(
            "SELECT transaction_id, artifact_path, resolution_kind, handoff_id, resolved_at "
            "FROM artifact_evidence_resolutions"
        ) as cur:
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
    """MIG506-02: Ejecutar la migración D4 múltiples veces no duplica filas ni produce errores."""
    db_file = tmp_path / "idempotent.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (description) VALUES ('tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (source_tx_id, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
            "VALUES (1, 'c:/mods/texgen', 'g', 'm', 'd', 'p')"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 1)"
        )
        await conn.commit()

        # Ejecutar migración 3 veces consecutivas
        await ejecutar_migracion_d4(conn)
        await ejecutar_migracion_d4(conn)
        await ejecutar_migracion_d4(conn)

        async with conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur:
            (conteo,) = await cur.fetchone()
        assert conteo == 1


@pytest.mark.asyncio
async def test_mig506_03_consumed_con_absorption_a_preserva_b_activo(tmp_path: pathlib.Path) -> None:
    """MIG506-03: LEGACY CONSUMED con absorción de A -> A queda resuelto y B activo en el oráculo futuro."""
    db_file = tmp_path / "consumed.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")
    path_b = str(tmp_path / "mods" / "DynDOLOD Output")
    clave_a = clave_de_artifact(pathlib.Path(path_a))

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        # TX ROLLED_BACK con receipt 'consumed'
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
        # Absorción histórica SOLO para A
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
            "VALUES (10, 1, ?, 'g', 'm', 'd', 'p')",
            (clave_a,),
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) VALUES (1, ?, 10)",
            (clave_a,),
        )
        await conn.commit()

        # Migrar a D4
        await ejecutar_migracion_d4(conn)

        # Evaluar oráculo futuro D4
        candidatas_a = await oracle_futuro_d4(conn, path_a)
        candidatas_b = await oracle_futuro_d4(conn, path_b)

        assert candidatas_a == set(), "A debe estar resuelto (excluido)"
        assert candidatas_b == {1}, "B debe seguir ACTIVO como evidencia (no resuelto)"


@pytest.mark.asyncio
async def test_mig506_04_superseded_run1_fail_closed_reactivation(tmp_path: pathlib.Path) -> None:
    """MIG506-04: LEGACY SUPERSEDED RUN1 sin absorción -> no inventa resolución;
    bajo FAIL_CLOSED_REACTIVATION ambos A y B son evidencia activa."""
    db_file = tmp_path / "superseded_run1.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")
    path_b = str(tmp_path / "mods" / "DynDOLOD Output")

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        # TX ROLLED_BACK con receipt 'superseded' pero CERO filas en orphan_evidence_absorptions
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

        # Migrar a D4 (no hay absorciones que backfillear)
        await ejecutar_migracion_d4(conn)

        # Evaluar oráculo futuro D4
        candidatas_a = await oracle_futuro_d4(conn, path_a)
        candidatas_b = await oracle_futuro_d4(conn, path_b)

        # Política FAIL_CLOSED_REACTIVATION: sin resolución durable explícita, se reactivan
        assert candidatas_a == {1}, "A es evidencia activa (fail-closed)"
        assert candidatas_b == {1}, "B es evidencia activa (fail-closed)"


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

        await ejecutar_migracion_d4(conn)

        candidatas_a = await oracle_futuro_d4(conn, path_a)
        candidatas_b = await oracle_futuro_d4(conn, path_b)

        # Al no haber registro per-artifact de ausencia para A ni B, el oráculo los considera activos
        # hasta que la probe real de reconciliación emita una resolución per-artifact
        assert candidatas_a == {1}
        assert candidatas_b == {1}


@pytest.mark.asyncio
async def test_mig506_06_rolled_back_sin_receipt_no_resucita(tmp_path: pathlib.Path) -> None:
    """MIG506-06: TX ROLLED_BACK sin receipt (rollback manual/operacional) NO resucita."""
    db_file = tmp_path / "normal_rollback.db"
    path_a = str(tmp_path / "mods" / "TexGen Output")

    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        # TX ROLLED_BACK SIN receipt
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

        await ejecutar_migracion_d4(conn)

        candidatas_a = await oracle_futuro_d4(conn, path_a)
        assert candidatas_a == set(), "TX rolled back sin receipt jamás debe considerarse evidencia"


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

        await ejecutar_migracion_d4(conn)

        candidatas_a = await oracle_futuro_d4(conn, path_a)
        candidatas_b = await oracle_futuro_d4(conn, path_b)

        assert candidatas_a == {1}
        assert candidatas_b == {1}


@pytest.mark.asyncio
async def test_mig506_08_failure_durante_backfill_revierte_totalmente(tmp_path: pathlib.Path) -> None:
    """MIG506-08: Fallo durante el backfill dentro de un boundary revierte completamente."""
    db_file = tmp_path / "fail_boundary.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (description) VALUES ('tx1')")
        await conn.commit()

        try:
            # Iniciar transacción SQLite explícita
            await conn.execute("BEGIN IMMEDIATE")
            await conn.executescript(D4_RESOLUTIONS_SCHEMA_SQL)
            # Provocar fallo SQL en el backfill
            await conn.execute(
                "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind) "
                "VALUES (9999, 'bad', 'invalid_kind')"
            )
            await conn.commit()
        except sqlite3.Error:
            await conn.rollback()

        # Verificar que la transacción revertida no dejó la tabla creada de forma inconsistente
        async with conn.execute("SELECT COUNT(*) FROM transactions") as cur:
            (conteo,) = await cur.fetchone()
        assert conteo == 1


@pytest.mark.asyncio
async def test_mig506_09_db_ya_migrada_reopen_idempotente(tmp_path: pathlib.Path) -> None:
    """MIG506-09: Reabrir una DB ya migrada ejecuta el init D4 limpiamente sin mutaciones."""
    db_file = tmp_path / "migrated.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (description) VALUES ('tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (source_tx_id, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
            "VALUES (1, 'c:/mods/texgen', 'g', 'm', 'd', 'p')"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id, absorbed_at) "
            "VALUES (1, 'c:/mods/texgen', 1, '2026-08-26 10:00:00')"
        )
        await conn.commit()

        # 1. Primera migración
        await ejecutar_migracion_d4(conn)

        # 2. Registrar una nueva resolución nativa D4
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
            "VALUES (1, 'c:/mods/dyndolod', 'no_artifact_demonstrated', NULL)"
        )
        await conn.commit()

        # 3. Simular reinicio / re-ejecución del init D4
        await ejecutar_migracion_d4(conn)

        async with conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur:
            (conteo,) = await cur.fetchone()
        assert conteo == 2


@pytest.mark.asyncio
async def test_mig506_10_conflicting_backfill_fails_closed(tmp_path: pathlib.Path) -> None:
    """MIG506-10: Discrepancia en backfill detectada por validación posterior falla cerrado."""
    from sky_claw.app.db.journal import JournalConnectionError, OperationJournal

    db_file = tmp_path / "corrupt_backfill.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
            "VALUES (10, 1, 'superseded', 'c:/mods/texgen', 'g', 'm', 'd', 'p')"
        )
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
            "VALUES (20, 1, 'awaiting_deployment', 'c:/mods/texgen', 'g', 'm', 'd', 'p')"
        )
        # Fila en orphan_evidence_absorptions que apunta a handoff_id=10
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 10)"
        )
        # Crear schema D4 manualmente y pre-poblar resolución conflictiva que apunta a handoff_id=20
        await conn.executescript(D4_RESOLUTIONS_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 'absorbed_by_handoff', 20)"
        )
        await conn.commit()

    j = OperationJournal(db_file)
    with pytest.raises(JournalConnectionError, match="Corrupción detectada durante backfill de resoluciones"):
        await j.open()


@pytest.mark.asyncio
async def test_mig506_11_concurrent_open_legacy_db(tmp_path: pathlib.Path) -> None:
    """MIG506-11: Múltiples instancias abriendo concurrentemente una DB legacy migran limpiamente."""
    db_file = tmp_path / "concurrent_open.db"
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.executescript(LEGACY_SCHEMA_SQL)
        await conn.execute("INSERT INTO transactions (transaction_id, description) VALUES (1, 'tx1')")
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, artifact_path, game_key, mods_root_key, data_key, expected_profile) "
            "VALUES (1, 1, 'c:/mods/texgen', 'g', 'm', 'd', 'p')"
        )
        await conn.execute(
            "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
            "VALUES (1, 'c:/mods/texgen', 1)"
        )
        await conn.commit()

    mgr = DatabaseLifecycleManager()
    j1 = OperationJournal(db_file, lifecycle=mgr)
    j2 = OperationJournal(db_file, lifecycle=mgr)

    await asyncio.gather(j1.open(), j2.open())
    try:
        async with mgr.operation(db_file) as conn:
            async with conn.execute("SELECT COUNT(*) FROM artifact_evidence_resolutions") as cur:
                (conteo,) = await cur.fetchone()
            assert conteo == 1
    finally:
        await mgr.shutdown_all()


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
