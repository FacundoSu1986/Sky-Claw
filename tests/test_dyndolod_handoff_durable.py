"""D2 (PR #493): handoff durable de deployment TexGen → DynDOLOD — RED first.

Estos tests congelan el contrato durable del artifact empaquetado
``mods/TexGen Output``: identidad (digest completo del árbol final), ownership
físico único, estados con semántica propia, cierre atómico TX+handoff con el
estado Python del journal sincronizado, resume por estado durable (nunca
``exists()``), supersede con matriz de fallos A/B/C, crash recovery conservador
y orden FS-seal → DB-boundary.

Fase RED: contra BASE+0001+0002 (sin la implementación D2) todo lo conductual
es ROJO — los métodos del journal no existen y el servicio no consulta handoff.
Los únicos que pueden pasar por construcción son los anclas de contrato puro
(esquema declarado y digest determinista), que no dependen del wiring.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.handoffs import (
    DeploymentHandoff,
    HandoffState,
    clave_de_artifact,
    reconciliar_handoffs_de_deployment,
)
from sky_claw.app.db.journal import OperationJournal, OperationType, TransactionStatus
from sky_claw.app.db.locks import DistributedLockManager, LockInfo
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools.artifact_digest import TreeDigest, digest_arbol
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODRunner,
    DynDOLODValidationError,
    ToolExecutionResult,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

# =============================================================================
# Fixtures y helpers
# =============================================================================


@pytest.fixture
async def journal_tmp(tmp_path: pathlib.Path):  # noqa: ANN201
    """OperationJournal REAL sobre una DB temporal (standalone, sin lifecycle)."""
    j = OperationJournal(tmp_path / "journal.db")
    await j.open()
    yield j, tmp_path / "journal.db"
    await j.close()


def _lock_manager() -> AsyncMock:
    mgr = AsyncMock(spec=DistributedLockManager)
    mgr.acquire_lock = AsyncMock(
        return_value=LockInfo(
            resource_id="dyndolod-pipeline",
            agent_id="dyndolod-pipeline-service",
            acquired_at=1000.0,
            expires_at=1600.0,
        )
    )
    mgr.release_lock = AsyncMock(return_value=True)
    return mgr


def _snapshot_manager() -> AsyncMock:
    from datetime import datetime

    mgr = AsyncMock(spec=FileSnapshotManager)
    mgr.create_snapshot = AsyncMock(
        return_value=SnapshotInfo(
            snapshot_id="snap-001",
            original_path="/x",
            snapshot_path="/snapshots/snap-001",
            checksum="abc",
            size_bytes=1,
            created_at=datetime.now().astimezone(),
        )
    )
    return mgr


def _event_bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


def _runner_real(tmp_path: pathlib.Path) -> tuple[DynDOLODConfig, DynDOLODRunner]:
    game = tmp_path / "game"
    game.mkdir()
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir()
    dyndolod_exe = exe_dir / "DynDOLODx64.exe"
    dyndolod_exe.touch()
    texgen_exe = exe_dir / "TexGenx64.exe"
    texgen_exe.touch()
    config = DynDOLODConfig(
        game_path=game,
        mo2_path=tmp_path / "MO2",
        mo2_mods_path=tmp_path / "MO2" / "mods",
        dyndolod_exe=dyndolod_exe,
        texgen_exe=texgen_exe,
    )
    return config, DynDOLODRunner(config)


def _svc(
    journal: object, *, profile: str | None = "Perfil-A", runner: DynDOLODRunner | None = None
) -> DynDOLODPipelineService:
    svc = DynDOLODPipelineService(
        lock_manager=_lock_manager(),
        snapshot_manager=_snapshot_manager(),
        journal=journal,  # type: ignore[arg-type]
        path_resolver=MagicMock(),
        event_bus=_event_bus(),
        mo2_profile=profile,
    )
    if runner is not None:
        svc._runner = runner  # type: ignore[attr-defined]
    return svc


class _ProcesoFalso:
    """Fake de ``DynDOLODRunner._execute_process`` (espejo del patrón de T3)."""

    def __init__(self, return_code: int = 0, al_ejecutar: Callable[[], None] | None = None) -> None:
        self.return_code = return_code
        self.al_ejecutar = al_ejecutar

    async def __call__(self, *args: object, **kwargs: object) -> tuple[str, str, int, float]:
        if self.al_ejecutar is not None:
            self.al_ejecutar()
        return "", "", self.return_code, 1.0


def _texgen_que_genera(
    tmp_path: pathlib.Path, staging: pathlib.Path, *, marca: bytes = b"CURRENT!"
) -> Callable[[], None]:
    """TexGen escribe el staging Y su log de completitud durante la corrida."""

    def _escribir() -> None:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "a.dds").write_bytes(marca)
        (staging / "sub").mkdir(parents=True, exist_ok=True)
        (staging / "sub" / "b.dds").write_bytes(b"CURRENT-SUB")
        log = tmp_path / "DynDOLOD" / "Logs" / "TexGen_SSE_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "[00:00:01] Using Output Path: C:\\out\\\n[00:10:00] TexGen completed successfully\n",
            encoding="utf-8",
        )

    return _escribir


def _dyndolod_falla() -> AsyncMock:
    async def _falla(**kw: object) -> ToolExecutionResult:
        return ToolExecutionResult(False, "DynDOLOD", 1, "", "", errors=["DynDOLOD roto"])

    return AsyncMock(side_effect=_falla)


def _dyndolod_ok(dyndolod_staging: pathlib.Path) -> AsyncMock:
    async def _ok(**kw: object) -> ToolExecutionResult:
        dyndolod_staging.mkdir(parents=True, exist_ok=True)
        (dyndolod_staging / "DynDOLOD.esp").write_bytes(b"esp")
        return ToolExecutionResult(True, "DynDOLOD", 0, "", "", output_path=dyndolod_staging)

    return AsyncMock(side_effect=_ok)


def _registro_awaiting(
    *,
    source_tx_id: int,
    mod_texgen: pathlib.Path,
    game_key: str,
    mods_root_key: str,
    data_key: str,
    profile: str,
    digest: TreeDigest,
) -> DeploymentHandoff:
    return DeploymentHandoff(
        handoff_id=0,
        source_tx_id=source_tx_id,
        state=HandoffState.AWAITING_DEPLOYMENT,
        artifact_path=clave_de_artifact(mod_texgen),
        game_key=game_key,
        mods_root_key=mods_root_key,
        data_key=data_key,
        expected_profile=profile,
        expected_digest=digest.digest,
        expected_files=digest.files,
        expected_bytes=digest.bytes,
        observed_digest=None,
        observed_files=None,
        observed_bytes=None,
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )


async def _sembrar_awaiting(
    journal: OperationJournal,
    *,
    mod_texgen: pathlib.Path,
    game: pathlib.Path,
    mods: pathlib.Path,
    data: pathlib.Path,
    profile: str = "Perfil-A",
    digest: TreeDigest | None = None,
) -> tuple[int, DeploymentHandoff]:
    tx = await journal.begin_transaction("texgen entregado", agent_id="test")
    digest_final = digest or digest_arbol(mod_texgen / "textures")
    registro = _registro_awaiting(
        source_tx_id=tx,
        mod_texgen=mod_texgen,
        game_key=clave_de_artifact(game),
        mods_root_key=clave_de_artifact(mods),
        data_key=clave_de_artifact(data),
        profile=profile,
        digest=digest_final,
    )
    handoff_id = await journal.crear_handoff_de_deployment(
        tx,
        descripcion="TexGen generado y empaquetado; esperando deployment (test)",
        registro=registro,
        viejo=None,
    )
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.handoff_id == handoff_id
    return tx, activo


def _escribir_mod(mod_texgen: pathlib.Path, contenido: bytes = b"CURRENT!") -> None:
    raiz = mod_texgen / "textures"
    raiz.mkdir(parents=True, exist_ok=True)
    (raiz / "a.dds").write_bytes(contenido)
    (raiz / "sub").mkdir(parents=True, exist_ok=True)
    (raiz / "sub" / "b.dds").write_bytes(b"CURRENT-SUB")


def _mirror_a_data(origen: pathlib.Path, data_dir: pathlib.Path) -> None:
    destino = data_dir / origen.name
    for archivo in sorted(p for p in origen.rglob("*") if p.is_file()):
        espejo = destino / archivo.relative_to(origen)
        espejo.parent.mkdir(parents=True, exist_ok=True)
        espejo.write_bytes(archivo.read_bytes())


# =============================================================================
# CONTRATO PURO — esquema declarado (anclas de contrato, no de wiring)
# =============================================================================


async def _insert_raw(db_path: pathlib.Path, sql: str, params: tuple[object, ...]) -> int:
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return int(cur.lastrowid or 0)


@pytest.mark.asyncio
async def test_esquema_acepta_indeterminate_con_expected_null(journal_tmp) -> None:  # noqa: ANN001
    """T-D14: INDETERMINATE con identidad esperada toda NULL es válido en SQL."""
    journal, db_path = journal_tmp
    tx = await journal.begin_transaction("evidencia", agent_id="test")
    handoff_id = await _insert_raw(
        db_path,
        """INSERT INTO deployment_handoffs
           (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile,
            observed_digest, observed_files, observed_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tx, "indeterminate", "A", "G", "M", "D", "Perfil-A", "d", 1, 1),
    )
    assert handoff_id > 0


@pytest.mark.asyncio
async def test_esquema_acepta_superseded_sin_expected_como_historia_de_indeterminate(
    journal_tmp,  # noqa: ANN001
) -> None:
    """R-002B: una fila INDETERMINATE reemplazada termina SUPERSEDED con
    expected_* NULL — historia de auditoría, no una reclamación de identidad.
    El CHECK lo admite (sólo awaiting/superseding/completed exigen identidad)."""
    journal, db_path = journal_tmp
    tx = await journal.begin_transaction("evidencia", agent_id="test")
    handoff_id = await _insert_raw(
        db_path,
        """INSERT INTO deployment_handoffs
           (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile)
           VALUES (?, 'superseded', 'A', 'G', 'M', 'D', 'Perfil-A')""",
        (tx,),
    )
    assert handoff_id > 0


@pytest.mark.asyncio
async def test_esquema_rechaza_awaiting_con_expected_null(journal_tmp) -> None:  # noqa: ANN001
    """T-D15: AWAITING_DEPLOYMENT sin identidad autorizada lo rechaza el CHECK."""
    journal, db_path = journal_tmp
    tx = await journal.begin_transaction("evidencia", agent_id="test")
    with pytest.raises(sqlite3.IntegrityError):
        await _insert_raw(
            db_path,
            """INSERT INTO deployment_handoffs
               (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tx, "awaiting_deployment", "A", "G", "M", "D", "Perfil-A"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql_fragment", "params"),
    [
        # AWAITING con digest suelto: lo rechaza chk_handoff_expected_identity.
        (
            """INSERT INTO deployment_handoffs
               (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile,
                expected_digest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("awaiting_deployment", "A", "G", "M", "D", "Perfil-A", "solo-digest"),
        ),
        # INDETERMINATE con expected_files suelto (digest NULL): SOLO lo rechaza
        # chk_handoff_expected_coherente — es la mitad del mutante M14.
        (
            """INSERT INTO deployment_handoffs
               (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile,
                expected_files)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("indeterminate", "A", "G", "M", "D", "Perfil-A", 5),
        ),
    ],
    ids=["awaiting-digest-suelto", "indeterminate-files-suelto"],
)
async def test_esquema_rechaza_expected_identidad_parcial(journal_tmp, sql_fragment: str, params: tuple) -> None:  # noqa: ANN001
    """T-D25: la tupla esperada es las tres NULL o las tres NOT NULL, en TODO estado."""
    journal, db_path = journal_tmp
    tx = await journal.begin_transaction("evidencia", agent_id="test")
    with pytest.raises(sqlite3.IntegrityError):
        await _insert_raw(db_path, sql_fragment, (tx, *params))


@pytest.mark.asyncio
async def test_esquema_rechaza_observed_identidad_parcial(journal_tmp) -> None:  # noqa: ANN001
    """T-D26: la observación también es una tupla: las tres NULL o las tres NOT NULL."""
    journal, db_path = journal_tmp
    tx = await journal.begin_transaction("evidencia", agent_id="test")
    with pytest.raises(sqlite3.IntegrityError):
        await _insert_raw(
            db_path,
            """INSERT INTO deployment_handoffs
               (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile,
                observed_digest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tx, "indeterminate", "A", "G", "M", "D", "Perfil-A", "solo-observado"),
        )


@pytest.mark.asyncio
async def test_esquema_rechaza_dos_owners_activos_del_mismo_artifact(journal_tmp) -> None:  # noqa: ANN001
    """T-D16: dos handoffs ACTIVOS del mismo artifact físico son imposibles aunque
    el perfil difiera — la exclusividad sigue al recurso, no al dueño."""
    journal, db_path = journal_tmp
    tx_a = await journal.begin_transaction("a", agent_id="test")
    tx_b = await journal.begin_transaction("b", agent_id="test")
    await _insert_raw(
        db_path,
        """INSERT INTO deployment_handoffs
           (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile,
            expected_digest, expected_files, expected_bytes)
           VALUES (?, 'awaiting_deployment', 'X', 'G', 'M', 'D', 'Perfil-A', 'd1', 1, 1)""",
        (tx_a,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await _insert_raw(
            db_path,
            """INSERT INTO deployment_handoffs
               (source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, expected_profile,
                expected_digest, expected_files, expected_bytes)
               VALUES (?, 'awaiting_deployment', 'X', 'G', 'M', 'D', 'Perfil-B', 'd2', 1, 1)""",
            (tx_b,),
        )


# =============================================================================
# JOURNAL — API atómica y estado Python sincronizado
# =============================================================================


@pytest.mark.asyncio
async def test_cierre_atomico_sincroniza_estado_python_con_sqlite(journal_tmp) -> None:  # noqa: ANN001
    """T-D21: tras el cierre atómico TX+handoff, ``_current_transaction`` de
    Python y el estado durable SQLite cuentan la MISMA historia (principio 5)."""
    journal, _ = journal_tmp
    tx = await journal.begin_transaction("texgen", agent_id="test")
    assert journal._current_transaction == tx  # noqa: SLF001 — el test del invariante lo exige
    registro = _registro_awaiting(
        source_tx_id=tx,
        mod_texgen=pathlib.Path("X:/mods/TexGen Output"),
        game_key="G",
        mods_root_key="M",
        data_key="D",
        profile="Perfil-A",
        digest=TreeDigest(digest="d", files=1, bytes=1),
    )
    await journal.crear_handoff_de_deployment(tx, descripcion="narrowed", registro=registro, viejo=None)

    assert journal._current_transaction is None  # noqa: SLF001
    db = journal._db  # noqa: SLF001
    assert db is not None
    cur = await db.execute("SELECT status FROM transactions WHERE transaction_id = ?", (tx,))
    row = await cur.fetchone()
    assert row is not None and row[0] == TransactionStatus.COMMITTED.value


@pytest.mark.asyncio
async def test_el_sweep_no_altera_handoff_ni_tx_committeada(journal_tmp) -> None:  # noqa: ANN001
    """T-D2: +48h y sweep de arranque NO tocan el handoff ni la TX ya committeada."""
    journal, db_path = journal_tmp
    tx, activo = await _sembrar_awaiting(
        journal,
        mod_texgen=pathlib.Path("X:/mods/TexGen Output"),
        game=pathlib.Path("X:/game"),
        mods=pathlib.Path("X:/mods"),
        data=pathlib.Path("X:/game/Data"),
        digest=TreeDigest(digest="d", files=1, bytes=1),
    )
    await _insert_raw(
        db_path,
        "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
        (tx,),
    )
    await _insert_raw(
        db_path,
        "UPDATE deployment_handoffs SET created_at = datetime('now', '-48 hours') WHERE handoff_id = ?",
        (activo.handoff_id,),
    )

    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)

    assert barridas == 0, "la TX del handoff es COMMITTED: el sweep no la toca"
    despues = await journal.consultar_handoff_activo(activo.artifact_path)
    assert despues is not None and despues.state is HandoffState.AWAITING_DEPLOYMENT
    cur = await journal._db.execute(  # noqa: SLF001
        "SELECT status FROM transactions WHERE transaction_id = ?", (tx,)
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == TransactionStatus.COMMITTED.value


# =============================================================================
# RECONCILER DE ARRANQUE — crash window RUN 1 / huérfanos de supersede
# =============================================================================


async def _evidencia_de_corrida(
    journal: OperationJournal,
    *,
    mod_texgen: pathlib.Path,
    estado_tx: str | None = None,
) -> int:
    tx = await journal.begin_transaction("DynDOLOD pipeline (preset=Medium, texgen=True)", agent_id="test")
    await journal.begin_operation(
        agent_id="test",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(mod_texgen),
        transaction_id=tx,
        metadata={"files_touched": [str(mod_texgen), str(mod_texgen / "textures")]},
    )
    if estado_tx == "rolled_back":
        await journal.mark_transaction_rolled_back(tx)
    return tx


@pytest.mark.asyncio
async def test_reconciliador_crea_indeterminate_ante_corrida_interrumpida(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """T-D13+T-D20: el ActionManifest (pre-mutación) NUNCA autoriza AWAITING —
    con artifact vivo y una TX PENDING que lo nombra, el reconciler crea un
    INDETERMINATE conservador sin identidad esperada, recuperable con
    regeneración explícita."""
    journal, _ = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)
    await _evidencia_de_corrida(journal, mod_texgen=mod_texgen)

    await reconciliar_handoffs_de_deployment(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key=clave_de_artifact(tmp_path / "game"),
        mods_root_key=clave_de_artifact(tmp_path / "MO2" / "mods"),
        data_key=clave_de_artifact(tmp_path / "game" / "Data"),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )

    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None
    assert activo.state is HandoffState.INDETERMINATE, "el manifiesto solo no fabrica identidad autorizada"
    assert activo.expected_digest is None
    esperado = digest_arbol(mod_texgen / "textures")
    assert activo.observed_digest == esperado.digest


@pytest.mark.asyncio
async def test_reconciliador_no_crea_nada_sin_evidencia(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """Un mod suelto en disco SIN transacción que lo nombre no es evidencia de
    corrida interrumpida: el reconciler no inventa provenance."""
    journal, _ = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)

    await reconciliar_handoffs_de_deployment(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key="G",
        mods_root_key="M",
        data_key="D",
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )

    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None


@pytest.mark.asyncio
async def test_reconciliador_superseding_huerfano_vuelve_a_awaiting_si_digest_coincide(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """Muerte dura en pleno supersede: si el árbol sigue siendo el esperado,
    SUPERSEDING vuelve a AWAITING (el artifact no fue tocado)."""
    journal, _ = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)
    tx, activo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=tmp_path / "game",
        mods=tmp_path / "MO2" / "mods",
        data=tmp_path / "game" / "Data",
    )
    assert await journal.transicionar_handoff(
        activo.handoff_id, desde=HandoffState.AWAITING_DEPLOYMENT, hacia=HandoffState.SUPERSEDING
    )

    await reconciliar_handoffs_de_deployment(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key="G",
        mods_root_key="M",
        data_key="D",
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )

    despues = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert despues is not None and despues.state is HandoffState.AWAITING_DEPLOYMENT


@pytest.mark.asyncio
async def test_reconciliador_superseding_huerfano_cae_a_indeterminate_si_no_coincide(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """La contracara: si el árbol ya NO coincide con la identidad esperada, no
    hay AWAITING de cortesía — cae a INDETERMINATE (fail-closed)."""
    journal, _ = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)
    _tx, activo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=tmp_path / "game",
        mods=tmp_path / "MO2" / "mods",
        data=tmp_path / "game" / "Data",
    )
    await journal.transicionar_handoff(
        activo.handoff_id, desde=HandoffState.AWAITING_DEPLOYMENT, hacia=HandoffState.SUPERSEDING
    )
    (mod_texgen / "textures" / "a.dds").write_bytes(b"OTRO-BYTES")

    await reconciliar_handoffs_de_deployment(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key="G",
        mods_root_key="M",
        data_key="D",
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )

    despues = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert despues is not None and despues.state is HandoffState.INDETERMINATE
    assert despues.expected_digest is None


# =============================================================================
# DIGEST — contrato puro
# =============================================================================


def test_digest_es_deterministico(tmp_path: pathlib.Path) -> None:
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    a = digest_arbol(raiz)
    b = digest_arbol(raiz)
    assert a == b
    assert a.files == 2 and a.bytes > 0


def test_digest_distingue_rename(tmp_path: pathlib.Path) -> None:
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    antes = digest_arbol(raiz)
    (raiz / "a.dds").rename(raiz / "a-renombrado.dds")
    despues = digest_arbol(raiz)
    assert antes.digest != despues.digest


def test_digest_distingue_redistribucion_de_bytes(tmp_path: pathlib.Path) -> None:
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    antes = digest_arbol(raiz)
    (raiz / "a.dds").write_bytes(b"CURRENT-S")  # mismos bytes, repartidos distinto
    (raiz / "sub" / "b.dds").write_bytes(b"CURRENT!")
    despues = digest_arbol(raiz)
    assert antes.digest != despues.digest


def test_digest_meta_ini_sibling_sigue_fuera_por_estructura(tmp_path: pathlib.Path) -> None:
    """F-003: ``mods/TexGen Output/meta.ini`` (el que MO2 administra) queda fuera
    del digest POR ESTRUCTURA — el scope es el subárbol ``textures/``, no el mod."""
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    antes = digest_arbol(raiz)
    (tmp_path / "mod" / "meta.ini").write_text("MO2 reescribe esto", encoding="utf-8")
    despues = digest_arbol(raiz)
    assert antes == despues


def test_digest_meta_ini_dentro_de_textures_cambia_el_digest(tmp_path: pathlib.Path) -> None:
    """F-003: ``textures/meta.ini`` NO lo administra MO2 — es un archivo regular
    propio del artifact y DEBE entrar al digest. La exclusión nominal por nombre
    fue removida."""
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    antes = digest_arbol(raiz)
    (raiz / "meta.ini").write_text("propio del artifact", encoding="utf-8")
    despues = digest_arbol(raiz)
    assert antes.digest != despues.digest
    assert despues.files == antes.files + 1


def test_digest_meta_ini_anidado_cambia_el_digest(tmp_path: pathlib.Path) -> None:
    """F-003: ``textures/**/meta.ini`` también es propio y también entra."""
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    antes = digest_arbol(raiz)
    (raiz / "sub" / "meta.ini").write_text("propio anidado", encoding="utf-8")
    despues = digest_arbol(raiz)
    assert antes.digest != despues.digest


def test_digest_symlink_en_el_subtree_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """F-004: un enlace dentro del subtree autorizado NO se sigue NI se ignora —
    el artifact deja de ser autorizable y ``digest_arbol`` falla cerrado."""
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    fuera = tmp_path / "fuera.txt"
    fuera.write_bytes(b"externo")
    try:
        (raiz / "enlace.txt").symlink_to(fuera)
    except (OSError, NotImplementedError):
        pytest.skip("la plataforma no permite crear symlinks acá")
    with pytest.raises(OSError):
        digest_arbol(raiz)


def test_digest_junction_en_el_subtree_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """F-004: misma política para un junction/reparse-point de directorio cuando
    la plataforma permite crearlo."""
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    destino = tmp_path / "otro-dir"
    destino.mkdir()
    try:
        (raiz / "puente").symlink_to(destino, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("la plataforma no permite crear el reparse point acá")
    with pytest.raises(OSError):
        digest_arbol(raiz)


def test_clave_de_artifact_sigue_la_semantica_de_la_plataforma(tmp_path: pathlib.Path) -> None:
    """F-006: ``normcase`` es case-insensitive en Windows y case-preserving en
    POSIX — el test afirma la semántica DE LA PLATAFORMA, no una exigencia
    portátil que Windows cumple y Linux no."""
    import os

    clave_mayus = clave_de_artifact(tmp_path / "MO2" / "mods" / "TexGen Output")
    clave_minus = clave_de_artifact(tmp_path / "MO2" / "mods" / "texgen output")
    if os.name == "nt":
        assert clave_mayus == clave_minus
    else:
        assert clave_mayus != clave_minus


def test_los_estados_activos_del_sql_y_python_estan_alineados() -> None:
    """F-008: el conjunto Python ``HANDOFF_STATES_ACTIVOS``, el ``WHERE`` del
    SELECT activo y el ``WHERE`` del supersede nombran EXACTAMENTE los mismos
    tres estados — una divergencia entre el enum y el SQL rompe acá antes de
    llegar a una corrida."""
    import re

    from sky_claw.app.db import journal as journal_mod
    from sky_claw.app.db.handoffs import (
        _SELECT_HANDOFF_ACTIVO_SQL,
        HANDOFF_STATES_ACTIVOS,
        HandoffState,
    )

    select = _SELECT_HANDOFF_ACTIVO_SQL
    m = re.search(r"state IN \(([^)]*)\)", select)
    assert m is not None
    estados_select = {s.strip().strip("'") for s in m.group(1).split(",")}
    assert estados_select == {s.value for s in HANDOFF_STATES_ACTIVOS}

    fuente_journal = pathlib.Path(journal_mod.__file__).read_text(encoding="utf-8")
    m2 = re.search(
        r"WHERE handoff_id = \? AND state IN \(([^)]*)\)",
        fuente_journal,
    )
    assert m2 is not None, "el supersede debe declarar sus estados activos en SQL"
    estados_supersede = {s.strip().strip("'") for s in m2.group(1).split(",")}
    assert estados_supersede == {s.value for s in HANDOFF_STATES_ACTIVOS}
    assert HandoffState.AWAITING_DEPLOYMENT.value == "awaiting_deployment"


def test_el_literal_del_mod_texgen_solo_vive_en_la_constante_canonica() -> None:
    """F-007: ``"TexGen Output"`` no se hardcodea en el CÓDIGO del flujo durable
    — la fuente canónica es ``DynDOLODRunner.TEXGEN_MOD_NAME``. Los docstrings
    (prosa) no cuentan: el ancla solo mira strings de código, no documentación."""
    import ast

    from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner

    assert DynDOLODRunner.TEXGEN_MOD_NAME == "TexGen Output", "la constante canónica cambió de valor"

    def _es_docstring(nodo: ast.expr, cuerpo: list[ast.stmt]) -> bool:
        return (
            bool(cuerpo)
            and isinstance(cuerpo[0], ast.Expr)
            and isinstance(cuerpo[0].value, ast.Constant)
            and cuerpo[0].value is nodo
        )

    raiz = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"
    modulos_del_flujo = [
        raiz / "app_context.py",
        raiz / "app" / "db" / "handoffs.py",
        raiz / "local" / "tools" / "dyndolod_service.py",
    ]
    infractores: list[str] = []
    for archivo in modulos_del_flujo:
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        docstrings: set[ast.Constant] = set()
        for padre in ast.walk(arbol):
            cuerpo = getattr(padre, "body", None)
            if isinstance(cuerpo, list) and cuerpo and isinstance(cuerpo[0], ast.Expr):
                primer = cuerpo[0].value
                if isinstance(primer, ast.Constant) and isinstance(primer.value, str):
                    docstrings.add(primer)
        for nodo in ast.walk(arbol):
            if nodo in docstrings:
                continue
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and "TexGen Output" in nodo.value:
                infractores.append(f"{archivo.name}:{nodo.lineno}")
                break
    assert infractores == [], f"literal hardcodeado fuera de la constante canónica: {infractores}"


# =============================================================================
# F-002 — ORPHAN SAME-SESSION (hot path)
# =============================================================================


async def _sembrar_tx_terminada(
    journal: OperationJournal,
    *,
    mod_texgen: pathlib.Path,
    estado: str,
) -> int:
    """TX con ActionManifest nombrando el mod, en el estado residual indicado.

    Reproduce EXACTAMENTE el estado que dejan las ventanas A/B (PENDING tras
    digest/DB failure) y C (ROLLED_BACK tras cancelación entre FS seal y DB
    commit) — sin reiniciar el journal, que es la condición del harness."""
    tx = await journal.begin_transaction("DynDOLOD pipeline (preset=Medium, texgen=True)", agent_id="test")
    await journal.begin_operation(
        agent_id="test",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(mod_texgen),
        transaction_id=tx,
        metadata={"files_touched": [str(mod_texgen), str(mod_texgen / "textures")]},
    )
    if estado == "rolled_back":
        await journal.mark_transaction_rolled_back(tx)
    return tx


@pytest.mark.asyncio
async def test_resume_same_session_con_tx_pending_huerfana_no_llama_dyndolod(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """F-002 ventana A/B (T-red-3/T-red-4): TX PENDING huérfana que nombra el
    artifact + mod vivo → el hot path materializa INDETERMINATE y el resume
    falla cerrado. NUNCA legacy."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="pending")

    svc = _svc(journal, runner=runner)
    run_dyndolod = AsyncMock()
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    run_dyndolod.assert_not_awaited()
    assert result.get("reason") == "HandoffIndeterminate"
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.INDETERMINATE
    assert activo.expected_digest is None, "la evidencia pre-mutación NUNCA fabrica identidad autorizada"


@pytest.mark.asyncio
async def test_resume_same_session_con_tx_rolled_back_sin_otra_evidencia_sigue_legacy(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R-002A (contrato revisado de F-002 ventana C): una TX ROLLED_BACK por sí
    sola —sin PENDING relevante y sin IDs barridos en este open— NO es evidencia
    de orphan. La ventana C de cancelación (FS seal → DB) sigue bloqueando en
    producción MEDIANTE SU TX PENDING: ``_cerrar_tx_tras_rollback`` deja la TX
    PENDING cuando la mutación preservada no puede resolverse (ver el test
    PENDING de arriba). Acá el resume sin otra evidencia vigente sigue legacy."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _mirror_a_data(mod_texgen / "textures", config.data_dir)
    await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="rolled_back")

    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    run_dyndolod = _dyndolod_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    run_dyndolod.assert_awaited_once()
    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None


@pytest.mark.asyncio
async def test_resume_con_tx_committed_sin_handoff_no_es_orphan(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """F-002 anti-falso-positivo: una TX COMMITTED (p. ej. un legacy exitoso
    previo) con manifest nombrando el mod NO se convierte en orphan — el hot
    path NO fabrica INDETERMINATE y el camino legacy queda preservado."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _mirror_a_data(mod_texgen / "textures", config.data_dir)
    tx = await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="pending")
    await journal.commit_transaction(tx)  # COMMITTED sin handoff activo

    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    run_dyndolod = _dyndolod_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    run_dyndolod.assert_awaited_once()
    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None


@pytest.mark.asyncio
async def test_resume_legacy_autentico_con_mod_vivo_y_sin_evidencia_sigue_legacy(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """Control (F-002): mod vivo SIN ninguna TX que lo nombre → legacy verbatim
    (el runner gatea Data). El hot path no convierte todo ``activo is None`` en
    fail-closed."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _mirror_a_data(mod_texgen / "textures", config.data_dir)

    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    run_dyndolod = _dyndolod_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    run_dyndolod.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciliador_no_trata_una_tx_committed_como_orphan(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """La MISMA regla aplica al reconciler de arranque: tras un resume exitoso
    (TX COMMITTED + handoff COMPLETED), el siguiente arranque NO degrada el
    artifact a INDETERMINATE."""
    journal, _ = journal_tmp
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)
    tx = await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="pending")
    await journal.commit_transaction(tx)

    await reconciliar_handoffs_de_deployment(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key=clave_de_artifact(tmp_path / "game"),
        mods_root_key=clave_de_artifact(tmp_path / "MO2" / "mods"),
        data_key=clave_de_artifact(tmp_path / "game" / "Data"),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )

    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None


def test_digest_arbol_sin_archivos_propios_falla_cerrado(tmp_path: pathlib.Path) -> None:
    vacio = tmp_path / "vacio"
    vacio.mkdir()
    with pytest.raises(OSError):
        digest_arbol(vacio)


# =============================================================================
# F-002b — PROVENANCE DEL ORPHAN (R-002A): historia vs evidencia vigente
# =============================================================================
#
# El contrato original de F-002 tomaba "TX PENDING o ROLLED_BACK que nombra el
# mod" como evidencia de corrida interrumpida. Demasiado ancho: un ROLLED_BACK
# de hace 90 días —incluso con un COMMITTED posterior— intoxicaba un artifact
# legítimo actual con un INDETERMINATE falso. La evidencia vigente es:
#
# - hot path (run_texgen=False, sin handoff activo): TX PENDING relevante, y
#   NADA de historia ROLLED_BACK;
# - arranque: PENDING relevantes ∪ los IDs EXACTOS que ``sweep_stale_pending``
#   barrió en ESTE open (``swept_pending_transaction_ids_this_open``) — porque
#   una PENDING huérfana de la sesión anterior se convierte a ROLLED_BACK justo
#   en ese open y debe seguir bloqueando, sin reintoxicar para siempre.


async def _envejecer_tx(journal: OperationJournal, tx_id: int, horas: int = 2160) -> None:
    """Corre ``created_at`` de una TX al pasado (2160h = 90 días por defecto)."""
    await journal._db.execute(  # noqa: SLF001
        "UPDATE transactions SET created_at = datetime('now', ?) WHERE transaction_id = ?",
        (f"-{horas} hours", tx_id),
    )
    await journal._db.commit()


@pytest.mark.asyncio
async def test_rolled_back_historica_no_es_orphan_por_si_sola(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R2-1: TX ROLLED_BACK de hace 90 días + manifest que nombra el artifact +
    artifact legítimo vivo + sin handoff → NO INDETERMINATE: el hot path sigue
    legacy. La historia no es evidencia vigente."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _mirror_a_data(mod_texgen / "textures", config.data_dir)
    tx = await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="rolled_back")
    await _envejecer_tx(journal, tx)

    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    run_dyndolod = _dyndolod_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    run_dyndolod.assert_awaited_once()
    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None, (
        "un ROLLED_BACK histórico fabricó un INDETERMINATE falso"
    )


@pytest.mark.asyncio
async def test_multiples_rolled_back_historicas_no_son_evidencia(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R2-2: varias ROLLED_BACK viejas nombrando el artifact — NINGUNA es
    evidencia actual (ni la primera ni la última candidata)."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _mirror_a_data(mod_texgen / "textures", config.data_dir)
    for _ in range(3):
        tx = await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="rolled_back")
        await _envejecer_tx(journal, tx)

    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    run_dyndolod = _dyndolod_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None


@pytest.mark.asyncio
async def test_rolled_back_vieja_mas_committed_nueva_no_es_orphan(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R2-3: ROLLED_BACK histórica + COMMITTED posterior, ambas nombrando el
    mismo artifact → no hay orphan falso (ninguna es evidencia de interrupción)."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _mirror_a_data(mod_texgen / "textures", config.data_dir)
    tx_old = await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="rolled_back")
    await _envejecer_tx(journal, tx_old)
    tx_new = await _sembrar_tx_terminada(journal, mod_texgen=mod_texgen, estado="pending")
    await journal.commit_transaction(tx_new)

    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    run_dyndolod = _dyndolod_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None


@pytest.mark.asyncio
async def test_swept_this_open_es_evidencia_del_arranque_y_no_persiste(
    tmp_path: pathlib.Path,
) -> None:
    """R2-5: una PENDING huérfana >24h se convierte a ROLLED_BACK en el open y
    SU ID exacto queda en ``swept_pending_transaction_ids_this_open`` — el
    reconciler de arranque lo usa como evidencia → INDETERMINATE. Reabrir el
    journal sin PENDING nueva: el ID NO vuelve a aparecer (no intoxica para
    siempre)."""
    db_path = tmp_path / "swept.db"
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)

    j1 = OperationJournal(db_path)
    await j1.open()
    tx = await _sembrar_tx_terminada(j1, mod_texgen=mod_texgen, estado="pending")
    await _envejecer_tx(j1, tx, horas=48)
    await j1.close()

    j2 = OperationJournal(db_path)
    await j2.open()
    assert tx in j2.swept_pending_transaction_ids_this_open, (
        "el sweep no registró el ID exacto de la PENDING barrida en este open"
    )
    barrida = await j2.get_transaction(tx)
    assert barrida is not None and barrida.status is TransactionStatus.ROLLED_BACK

    await reconciliar_handoffs_de_deployment(
        journal=j2,
        mod_texgen=mod_texgen,
        game_key=clave_de_artifact(tmp_path / "game"),
        mods_root_key=clave_de_artifact(tmp_path / "MO2" / "mods"),
        data_key=clave_de_artifact(tmp_path / "game" / "Data"),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )
    activo = await j2.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.INDETERMINATE, (
        "el ID barrido en este open no fue evidencia para el reconciler de arranque"
    )
    await j2.close()

    # Reabrir la MISMA instancia tampoco conserva el ID (M5-4): el conjunto
    # refleja sólo el barrido del open que lo produjo, no historia acumulada.
    await j2.open()
    assert tx not in j2.swept_pending_transaction_ids_this_open, (
        "el ID barrido persiste al reabrir la misma instancia: reintoxicaría el artifact"
    )
    await j2.close()

    j3 = OperationJournal(db_path)
    await j3.open()
    assert tx not in j3.swept_pending_transaction_ids_this_open, (
        "el ID barrido persiste entre opens: reintoxicaría el artifact para siempre"
    )
    await j3.close()


@pytest.mark.asyncio
async def test_rolled_back_previa_al_open_no_entra_al_conjunto_swept_ni_es_candidata(
    tmp_path: pathlib.Path,
) -> None:
    """R2-6: una TX que YA estaba ROLLED_BACK antes del open no aparece en
    ``swept_pending_transaction_ids_this_open`` (el sweep sólo registra lo que
    él convierte) y el reconciler de arranque no la toma como candidata."""
    db_path = tmp_path / "swept-previo.db"
    mod_texgen = tmp_path / "MO2" / "mods" / "TexGen Output"
    _escribir_mod(mod_texgen)

    j1 = OperationJournal(db_path)
    await j1.open()
    tx = await _sembrar_tx_terminada(j1, mod_texgen=mod_texgen, estado="rolled_back")
    await _envejecer_tx(j1, tx, horas=48)
    await j1.close()

    j2 = OperationJournal(db_path)
    await j2.open()
    assert tx not in j2.swept_pending_transaction_ids_this_open, (
        "una ROLLED_BACK previa al open apareció como barrida en este open"
    )
    await reconciliar_handoffs_de_deployment(
        journal=j2,
        mod_texgen=mod_texgen,
        game_key=clave_de_artifact(tmp_path / "game"),
        mods_root_key=clave_de_artifact(tmp_path / "MO2" / "mods"),
        data_key=clave_de_artifact(tmp_path / "game" / "Data"),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )
    assert await j2.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None, (
        "una ROLLED_BACK previa al open se volvió candidata orphan del arranque"
    )
    await j2.close()


# =============================================================================
# SERVICIO — needs_deployment durable (T-D1, T-D9/T-D19, T-D27)
# =============================================================================


@pytest.mark.asyncio
async def test_needs_deployment_crea_handoff_durable_que_sobrevive_restart(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D1: el veredicto needs_deployment deja un handoff AWAITING_DEPLOYMENT
    completo (TX1 COMMITTED con descripción estrechada + identidad autorizada),
    y sobrevive a reabrir el journal desde el mismo archivo."""
    journal, db_path = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    svc = _svc(journal, runner=runner)

    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging))),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["success"] is False
    assert result["needs_deployment"] is True
    assert result["texgen_mod_path"] == str(mod_texgen)

    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.AWAITING_DEPLOYMENT
    esperado = digest_arbol(mod_texgen / "textures")
    assert (activo.expected_digest, activo.expected_files, activo.expected_bytes) == (
        esperado.digest,
        esperado.files,
        esperado.bytes,
    )
    assert activo.expected_profile == "Perfil-A"
    cur = await journal._db.execute(  # noqa: SLF001
        "SELECT status, description FROM transactions WHERE transaction_id = ?", (activo.source_tx_id,)
    )
    fila = await cur.fetchone()
    assert fila is not None and fila[0] == TransactionStatus.COMMITTED.value
    assert "esperando deployment" in fila[1]

    # Restart: una instancia NUEVA del journal sobre el MISMO archivo ve el handoff.
    await journal.close()
    j2 = OperationJournal(db_path)
    await j2.open()
    try:
        reabierto = await j2.consultar_handoff_activo(clave_de_artifact(mod_texgen))
        assert reabierto is not None and reabierto.expected_digest == esperado.digest
    finally:
        await j2.close()


@pytest.mark.asyncio
async def test_snapshot_false_needs_deployment_crea_handoff_completo_y_payload(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D9+T-D19: ``create_snapshot=False`` produce el MISMO handoff durable
    completo — cuelga del veredicto, no de "había rollback que sellar" — y el
    payload lleva ``texgen_mod_path`` aunque el mod nunca entró al move-aside."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    svc = _svc(journal, runner=runner)

    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging))),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["needs_deployment"] is True
    assert result["texgen_mod_path"] == str(mod_texgen)
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.AWAITING_DEPLOYMENT
    esperado = digest_arbol(mod_texgen / "textures")
    assert (activo.expected_digest, activo.expected_files, activo.expected_bytes) == (
        esperado.digest,
        esperado.files,
        esperado.bytes,
    )
    assert activo.artifact_path == clave_de_artifact(mod_texgen)
    assert activo.mods_root_key == clave_de_artifact(config.mo2_mods_path)
    assert activo.data_key == clave_de_artifact(config.data_dir)


@pytest.mark.asyncio
async def test_el_digest_del_handoff_se_calcula_sobre_el_mod_final(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D27: la identidad autorizada se calcula sobre el artifact FINAL
    empaquetado (``mods/TexGen Output/textures``), nunca sobre el staging crudo."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    svc = _svc(journal, runner=runner)

    raices: list[pathlib.Path] = []

    def _registradora(raiz: pathlib.Path) -> TreeDigest:
        raices.append(raiz)
        return digest_arbol(raiz)

    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging))),
        patch.object(runner, "run_dyndolod", AsyncMock()),
        patch("sky_claw.local.tools.dyndolod_service.digest_arbol", side_effect=_registradora),
    ):
        await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert raices, "nunca se calculó identidad durable"
    assert all(r == mod_texgen / "textures" for r in raices)
    assert config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME not in raices


# =============================================================================
# SERVICIO — resume durable (T-D3…T-D8, T-D10, T-D12, T-D17, T-D18)
# =============================================================================


@pytest.mark.asyncio
async def test_resume_exitoso_sella_fs_antes_del_boundary_db_y_cierra_todo(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D3+T-D18: resume exitoso = FS seal ANTES que el boundary DB, y el
    boundary cierra handoff→COMPLETED y TX2→COMMITTED como una sola unidad."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _mirror_a_data(mod_texgen / "textures", config.data_dir)
    _tx1, _ = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME

    orden: list[str] = []
    import sky_claw.local.tools.dyndolod_service as ds_mod

    _seal_real = ds_mod._commit_directory_rollbacks
    _cierre_real = journal.completar_handoff_de_resume

    async def _seal_grabadora(rollbacks: object) -> None:
        orden.append("fs_seal")
        await _seal_real(rollbacks)

    async def _cierre_grabadora(tx_id: int, handoff_id: int) -> None:
        orden.append("db_boundary")
        await _cierre_real(tx_id, handoff_id)

    journal.completar_handoff_de_resume = _cierre_grabadora  # type: ignore[method-assign]

    with (
        patch("sky_claw.local.tools.dyndolod_service._commit_directory_rollbacks", side_effect=_seal_grabadora),
        patch.object(runner, "run_dyndolod", _dyndolod_ok(dyndolod_staging)),
    ):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    assert orden == ["fs_seal", "db_boundary"], "FS seal DEBE preceder al boundary DB"
    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None  # T-D10: no resucita


@pytest.mark.asyncio
async def test_resume_con_handoff_artifact_missing_no_llama_dyndolod(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D4: handoff AWAITING + artifact desaparecido → ARTIFACT_MISSING,
    DynDOLOD NO se lanza (el falso verde F-D3 queda cerrado)."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    # El actor externo borra el mod (el escenario F-D3).
    import shutil

    shutil.rmtree(mod_texgen)

    svc = _svc(journal, runner=runner)
    run_dyndolod = AsyncMock()
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    run_dyndolod.assert_not_awaited()
    assert result["success"] is False
    assert result.get("reason") == "ArtifactMissing"


@pytest.mark.asyncio
async def test_resume_digest_mismatch_no_llama_dyndolod(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """T-D5: el árbol presente pero distinto del producido por TX1 → DIGEST_MISMATCH."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    (mod_texgen / "textures" / "a.dds").write_bytes(b"CORRUPTO")  # mismo tamaño, bytes distintos

    svc = _svc(journal, runner=runner)
    run_dyndolod = AsyncMock()
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    run_dyndolod.assert_not_awaited()
    assert result.get("reason") == "DigestMismatch"


@pytest.mark.asyncio
async def test_resume_data_stale_no_llama_y_conserva_handoff(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """T-D6: artifact intacto pero Data stale → NEEDS_DEPLOYMENT, no se lanza, y
    el handoff sigue AWAITING (el despliegue sigue pendiente)."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )

    svc = _svc(journal, runner=runner)
    run_dyndolod = AsyncMock()
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    run_dyndolod.assert_not_awaited()
    assert result["needs_deployment"] is True
    assert result["texgen_mod_path"] == str(mod_texgen)
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.AWAITING_DEPLOYMENT


@pytest.mark.asyncio
async def test_sin_handoff_run_texgen_false_es_legacy(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """T-D8: sin handoff activo para el artifact físico, ``run_texgen=False``
    conserva el camino legacy verbatim (DynDOLOD arranca)."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    svc = _svc(journal, runner=runner)

    run_dyndolod = _dyndolod_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    run_dyndolod.assert_awaited_once()
    assert result["success"] is True, result.get("errors")


@pytest.mark.asyncio
async def test_perfil_distinto_mismo_artifact_no_llama_dyndolod(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D12: handoff del perfil A, caller perfil B, MISMO artifact físico →
    HANDOFF_PROFILE_MISMATCH, DynDOLOD NO se lanza."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
        profile="Perfil-A",
    )

    svc = _svc(journal, profile="Perfil-B", runner=runner)
    run_dyndolod = AsyncMock()
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    run_dyndolod.assert_not_awaited()
    assert result.get("reason") == "HandoffProfileMismatch"


@pytest.mark.asyncio
async def test_perfil_desconocido_run_texgen_true_falla_cerrado_antes_de_mutar(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D17: sin perfil resoluble NO se puede crear un handoff resumible — el
    pipeline falla cerrado ANTES de cualquier mutación."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    svc = _svc(journal, profile=None, runner=runner)
    run_pipeline = AsyncMock()
    with patch.object(runner, "run_full_pipeline", run_pipeline):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    run_pipeline.assert_not_awaited()
    assert result["success"] is False
    assert result.get("reason") == "PerfilDesconocido"
    cur = await journal._db.execute("SELECT COUNT(*) FROM transactions")  # noqa: SLF001
    assert (await cur.fetchone())[0] == 0, "fallar cerrado no abre ninguna TX"


# =============================================================================
# SERVICIO — supersede y matriz de fallos (T-D11, T-D22, T-D23, T-D24)
# =============================================================================


@pytest.mark.asyncio
async def test_segundo_texgen_supersede_el_handoff_activo(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """T-D11: handoff AWAITING + nuevo TexGen → old SUPERSEDED y new AWAITING
    con la identidad de la NUEVA generación, enlazados por ``superseded_by``."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    _tx1, viejo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    svc = _svc(journal, runner=runner)

    with (
        patch.object(
            runner,
            "_execute_process",
            _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging, marca=b"GEN-2")),
        ),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["needs_deployment"] is True
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.AWAITING_DEPLOYMENT
    assert activo.handoff_id != viejo.handoff_id
    assert activo.superseded_by is None
    cur = await journal._db.execute(  # noqa: SLF001
        "SELECT state, superseded_by FROM deployment_handoffs WHERE handoff_id = ?", (viejo.handoff_id,)
    )
    fila = await cur.fetchone()
    assert fila is not None and fila[0] == HandoffState.SUPERSEDED.value
    assert fila[1] == activo.handoff_id
    esperado = digest_arbol(mod_texgen / "textures")
    assert activo.expected_digest == esperado.digest
    assert (mod_texgen / "textures" / "a.dds").read_bytes() == b"GEN-2"


@pytest.mark.asyncio
async def test_supersede_fallo_clase_a_vuelve_a_awaiting(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """T-D22: fallo ANTES de tocar el artifact empaquetado (TexGen rojo) →
    SUPERSEDING vuelve a AWAITING y el handoff viejo sigue vigente."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen)
    _tx1, viejo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    svc = _svc(journal, runner=runner)

    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(return_code=1)),
        patch.object(runner, "run_dyndolod", _dyndolod_falla()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["success"] is False
    assert result.get("needs_deployment") is not True
    despues = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert despues is not None and despues.state is HandoffState.AWAITING_DEPLOYMENT
    assert despues.handoff_id == viejo.handoff_id
    assert (mod_texgen / "textures" / "a.dds").read_bytes() == b"CURRENT!"


@pytest.mark.asyncio
async def test_supersede_fallo_clase_b_restore_exacto_vuelve_a_awaiting(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D23: packaging falla pero el DirectoryRollback restauró el artifact
    byte-exacto → SUPERSEDING vuelve a AWAITING (verificado por digest)."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_mod(mod_texgen)
    _tx1, viejo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    svc = _svc(journal, runner=runner)

    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging))),
        patch.object(
            runner,
            "_package_output_as_mod",
            AsyncMock(side_effect=DynDOLODValidationError("packaging roto", output_path=staging)),
        ),
        patch.object(runner, "run_dyndolod", _dyndolod_falla()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["success"] is False
    despues = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert despues is not None and despues.state is HandoffState.AWAITING_DEPLOYMENT
    assert despues.handoff_id == viejo.handoff_id
    assert (mod_texgen / "textures" / "a.dds").read_bytes() == b"CURRENT!", "el restore fue byte-exacto"


@pytest.mark.asyncio
async def test_supersede_fallo_clase_c_sin_snapshot_cae_a_indeterminate_y_resume_falla_cerrado(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """T-D24: packaging falla SIN rollback exacto (create_snapshot=False) y el
    artifact puede quedar a medias → INDETERMINATE, y el resume falla cerrado."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_mod(mod_texgen)
    _tx1, viejo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    svc = _svc(journal, runner=runner)

    def _empaquetado_que_corrompe(*args: object, **kw: object) -> None:
        # El empaquetado empezó a reemplazar el artifact y murió a medias.
        (mod_texgen / "textures" / "a.dds").write_bytes(b"PARCIAL!")
        raise DynDOLODValidationError("packaging roto", output_path=pathlib.Path(str(args[0])))

    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging))),
        patch.object(runner, "_package_output_as_mod", side_effect=_empaquetado_que_corrompe),
        patch.object(runner, "run_dyndolod", _dyndolod_falla()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=False)

    assert result["success"] is False
    despues = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert despues is not None and despues.state is HandoffState.INDETERMINATE
    assert despues.expected_digest is None
    assert despues.observed_digest is not None

    # El resume sobre INDETERMINATE falla cerrado.
    run_dyndolod = AsyncMock()
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        resultado_resume = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)
    run_dyndolod.assert_not_awaited()
    assert resultado_resume.get("reason") == "HandoffIndeterminate"


# =============================================================================
# R-002B — ESCAPE DESDE INDETERMINATE (regeneración explícita)
# =============================================================================
#
# INDETERMINATE + run_texgen=True NO puede transicionar antes a SUPERSEDING:
# el CHECK del esquema exige expected_* NOT NULL fuera de 'indeterminate', así
# que la transición previa levantaba JournalTransactionError y dejaba el estado
# activo sin salida. El contrato nuevo: el old PERMANECE INDETERMINATE durante
# la regeneración; si la corrida produce y empaqueta un artifact autorizado, el
# boundary de reemplazo existente lo cierra (old → SUPERSEDED + INSERT nuevo).
# La identidad nueva sale EXCLUSIVAMENTE del artifact generado.


async def _sembrar_indeterminado(
    journal: OperationJournal,
    *,
    mod_texgen: pathlib.Path,
    game: pathlib.Path,
    mods: pathlib.Path,
    data: pathlib.Path,
    profile: str = "Perfil-A",
    observado: TreeDigest | None = None,
) -> tuple[int, DeploymentHandoff]:
    """Handoff INDETERMINATE activo (sin identidad esperada), como lo deja el
    reconciler de arranque o la clase C del supersede."""
    tx = await journal.begin_transaction("evidencia indeterminada", agent_id="test")
    await journal.commit_transaction(tx)
    registro = DeploymentHandoff(
        handoff_id=0,
        source_tx_id=tx,
        state=HandoffState.INDETERMINATE,
        artifact_path=clave_de_artifact(mod_texgen),
        game_key=clave_de_artifact(game),
        mods_root_key=clave_de_artifact(mods),
        data_key=clave_de_artifact(data),
        expected_profile=profile,
        expected_digest=None,
        expected_files=None,
        expected_bytes=None,
        observed_digest=observado.digest if observado is not None else None,
        observed_files=observado.files if observado is not None else None,
        observed_bytes=observado.bytes if observado is not None else None,
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )
    handoff_id = await journal.registrar_handoff_indeterminado(registro=registro)
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.handoff_id == handoff_id
    return tx, activo


def _preparar_data_para_generacion(data_dir: pathlib.Path, *, marca: bytes) -> None:
    """Espeja en el Data físico los bytes exactos que ``_texgen_que_genera`` va a
    producir, para que el gate de visibilidad deje correr DynDOLOD."""
    destino = data_dir / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    (destino / "a.dds").parent.mkdir(parents=True, exist_ok=True)
    (destino / "a.dds").write_bytes(marca)
    (destino / "sub").mkdir(parents=True, exist_ok=True)
    (destino / "sub" / "b.dds").write_bytes(b"CURRENT-SUB")


@pytest.mark.asyncio
async def test_regen_desde_indeterminate_supersede_en_boundary_de_reemplazo(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R2-7: INDETERMINATE + run_texgen=True → NO hay transición previa a
    SUPERSEDING ni CHECK failure; el boundary de reemplazo (needs_deployment)
    cierra old → SUPERSEDED e INSERTA el nuevo AWAITING autorizado."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    _tx0, viejo = await _sembrar_indeterminado(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
        observado=digest_arbol(mod_texgen / "textures"),
    )
    svc = _svc(journal, runner=runner)

    with (
        patch.object(
            runner,
            "_execute_process",
            _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging, marca=b"GEN-2")),
        ),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["needs_deployment"] is True
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.AWAITING_DEPLOYMENT
    assert activo.handoff_id != viejo.handoff_id
    cur = await journal._db.execute(  # noqa: SLF001
        "SELECT state, superseded_by FROM deployment_handoffs WHERE handoff_id = ?",
        (viejo.handoff_id,),
    )
    fila = await cur.fetchone()
    assert fila is not None and fila[0] == HandoffState.SUPERSEDED.value
    assert fila[1] == activo.handoff_id


@pytest.mark.asyncio
async def test_regen_desde_indeterminate_exitosa_terminaliza_con_completed(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R2-7 (éxito total): el mismo escape por el boundary de éxito completo —
    old INDETERMINATE → SUPERSEDED, nuevo COMPLETED, y el old deja de reclamar
    ownership activo."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    _tx0, viejo = await _sembrar_indeterminado(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
        observado=digest_arbol(mod_texgen / "textures"),
    )
    _preparar_data_para_generacion(config.data_dir, marca=b"GEN-2")
    svc = _svc(journal, runner=runner)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME

    with (
        patch.object(
            runner,
            "_execute_process",
            _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging, marca=b"GEN-2")),
        ),
        patch.object(runner, "run_dyndolod", _dyndolod_ok(dyndolod_staging)),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["success"] is True, result.get("errors")
    assert await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen)) is None, (
        "el old sigue reclamando ownership activo tras el reemplazo"
    )
    cur = await journal._db.execute(  # noqa: SLF001
        "SELECT state, superseded_by FROM deployment_handoffs WHERE handoff_id = ?",
        (viejo.handoff_id,),
    )
    fila = await cur.fetchone()
    assert fila is not None and fila[0] == HandoffState.SUPERSEDED.value


@pytest.mark.asyncio
async def test_regen_desde_indeterminate_falla_y_old_sigue_indeterminate(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R2-8: TexGen falla antes de producir un artifact autorizado → el old
    sigue INDETERMINATE, sin identidad esperada inventada, y run_texgen=False
    continúa fail-closed."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    _tx0, viejo = await _sembrar_indeterminado(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
        observado=digest_arbol(mod_texgen / "textures"),
    )
    svc = _svc(journal, runner=runner)

    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(return_code=1)),
        patch.object(runner, "run_dyndolod", _dyndolod_falla()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["success"] is False
    assert result.get("needs_deployment") is not True
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.INDETERMINATE
    assert activo.handoff_id == viejo.handoff_id, "la regeneración fallida reemplazó al old"
    assert activo.expected_digest is None, "se inventó identidad esperada en un fallo"
    # La TX de la regeneración fallida queda PENDING (la corrida ya había
    # empezado a mutar): es la evidencia vigente que sostiene el fail-closed
    # del próximo arranque. En el bug pre-fix, el CHECK la tiraba al handler
    # genérico con mutation_started=False y quedaba ROLLED_BACK — historia.
    corridas = [
        t
        for t in await journal.list_recent_transactions(limit=10)
        if t.description == "DynDOLOD pipeline (preset=Medium, texgen=True)"
    ]
    assert corridas, "no se encontró la TX de la regeneración fallida"
    assert corridas[0].status is TransactionStatus.PENDING, (
        "la TX de una regeneración fallida post-mutación quedó ROLLED_BACK: pierde la evidencia vigente del fail-closed"
    )

    run_dyndolod = AsyncMock()
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        resultado_resume = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)
    run_dyndolod.assert_not_awaited()
    assert resultado_resume.get("reason") == "HandoffIndeterminate"


@pytest.mark.asyncio
async def test_la_identidad_nueva_nunca_promociona_el_observed_del_viejo(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R2-9: INDETERMINATE con observed_digest=X + regeneración → la identidad
    esperada nueva es la del artifact generado (Y), NUNCA X."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    x = digest_arbol(mod_texgen / "textures")
    _tx0, viejo = await _sembrar_indeterminado(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
        observado=x,
    )
    svc = _svc(journal, runner=runner)

    with (
        patch.object(
            runner,
            "_execute_process",
            _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging, marca=b"GEN-2")),
        ),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["needs_deployment"] is True
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.handoff_id != viejo.handoff_id
    y = digest_arbol(mod_texgen / "textures")
    assert y.digest != x.digest, "la generación no cambió el artifact: el test no prueba nada"
    assert activo.expected_digest == y.digest
    assert activo.expected_digest != x.digest, "la identidad nueva promovió el observed del viejo"


@pytest.mark.asyncio
async def test_regen_desde_superseding_reafirma_y_supersede(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """Receta congelada para SUPERSEDING (ancla R-002B): la regeneración
    reafirma la transición (idempotente) y el boundary de reemplazo cierra
    old → SUPERSEDED con el nuevo autorizado."""
    journal, _ = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    _tx1, viejo = await _sembrar_awaiting(
        journal,
        mod_texgen=mod_texgen,
        game=config.game_path,
        mods=config.mo2_mods_path,
        data=config.data_dir,
    )
    await journal.transicionar_handoff(
        viejo.handoff_id,
        desde=HandoffState.AWAITING_DEPLOYMENT,
        hacia=HandoffState.SUPERSEDING,
    )
    svc = _svc(journal, runner=runner)

    with (
        patch.object(
            runner,
            "_execute_process",
            _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging, marca=b"GEN-2")),
        ),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    assert result["needs_deployment"] is True
    activo = await journal.consultar_handoff_activo(clave_de_artifact(mod_texgen))
    assert activo is not None and activo.state is HandoffState.AWAITING_DEPLOYMENT
    assert activo.handoff_id != viejo.handoff_id


# =============================================================================
# R-006 — CANONICALIZACIÓN DE PATHS DEL ORACLE
# =============================================================================


def test_clave_canonica_de_path_colapsa_variantes_sintacticas() -> None:
    """R-006: la forma canónica de comparación colapsa separador de cola,
    segmentos ``.``/``..`` y mezclas de separadores, sin resolver symlinks."""
    import os

    from sky_claw.app.db.handoffs import clave_canonica_de_path

    base = "C:" + os.sep + os.path.join("MO2", "mods", "TexGen Output")
    assert clave_canonica_de_path(base + os.sep) == clave_canonica_de_path(base)
    assert clave_canonica_de_path(os.path.join(base, "textures", "..", "textures", "a.dds")) == (
        clave_canonica_de_path(os.path.join(base, "textures", "a.dds"))
    )
    assert clave_canonica_de_path("C:/MO2/mods/TexGen Output") == clave_canonica_de_path(base)


@pytest.mark.skipif(os.name != "nt", reason="la semántica de caso de Windows sólo aplica en nt")
@pytest.mark.asyncio
async def test_oracle_alinea_variantes_de_caso_en_windows(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R-006 (Windows): una entrada del manifest con distinto caso sigue
    siendo la MISMA identidad que el lookup del handoff."""
    journal, _ = journal_tmp
    mod = tmp_path / "MO2" / "mods" / "TexGen Output"
    tx = await journal.begin_transaction("caso", agent_id="test")
    await journal.begin_operation(
        agent_id="test",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(mod),
        transaction_id=tx,
        metadata={"files_touched": [str(mod).upper()]},
    )
    assert tx in await journal.transacciones_que_nombran(clave_de_artifact(mod))


@pytest.mark.asyncio
async def test_oracle_reconoce_variante_sintactica_resoluble(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R-006: un entry del manifest con segmentos ``..`` que resuelve al árbol
    del artifact es la MISMA identidad que el lookup del handoff."""
    journal, _ = journal_tmp
    mod = tmp_path / "MO2" / "mods" / "TexGen Output"
    tx = await journal.begin_transaction("variante", agent_id="test")
    await journal.begin_operation(
        agent_id="test",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(mod),
        transaction_id=tx,
        metadata={"files_touched": [str(mod / "textures" / ".." / "textures" / "a.dds")]},
    )
    assert tx in await journal.transacciones_que_nombran(clave_de_artifact(mod))


@pytest.mark.asyncio
async def test_oracle_ignora_separador_de_cola_en_el_entry(
    tmp_path: pathlib.Path,
    journal_tmp,  # noqa: ANN001
) -> None:
    """R-006: un entry con separador de cola nombra el artifact igual."""
    journal, _ = journal_tmp
    mod = tmp_path / "MO2" / "mods" / "TexGen Output"
    tx = await journal.begin_transaction("cola", agent_id="test")
    await journal.begin_operation(
        agent_id="test",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(mod),
        transaction_id=tx,
        metadata={"files_touched": [str(mod) + os.sep]},
    )
    assert tx in await journal.transacciones_que_nombran(clave_de_artifact(mod))


# =============================================================================
# ANCLAS ENUMERATIVAS (D2 §26) — congelan conjuntos, no muestran
# =============================================================================


def test_el_conjunto_de_estados_activos_esta_congelado() -> None:
    """Los tres estados activos no son semánticamente equivalentes y el conjunto
    se congela por igualdad literal: agregar un cuarto (o quitar uno) rompe el
    ancla y obliga a decidir su semántica de resume/supersede explícitamente."""
    from sky_claw.app.db.handoffs import HANDOFF_STATES_ACTIVOS, HandoffState

    assert (
        frozenset(
            {
                HandoffState.AWAITING_DEPLOYMENT,
                HandoffState.INDETERMINATE,
                HandoffState.SUPERSEDING,
            }
        )
        == HANDOFF_STATES_ACTIVOS
    )


def test_los_estados_con_escape_de_regen_tienen_receta_congelada() -> None:
    """Ancla enumerativa (R-002B): cada estado desde el cual una regeneración
    explícita DEBE tener una salida, con su receta congelada por igualdad
    literal. Un docstring que prometa escape sin test conductual no alcanza:
    cada receta tiene su test de comportamiento en este archivo (AWAITING →
    ``test_segundo_texgen_supersede_el_handoff_activo``; INDETERMINATE →
    ``test_regen_desde_indeterminate_*``; SUPERSEDING →
    ``test_regen_desde_superseding_reafirma_y_supersede``)."""
    from sky_claw.app.db.handoffs import HandoffState

    recetas_de_escape = {
        HandoffState.AWAITING_DEPLOYMENT.value: "pre-mutación → SUPERSEDING → boundary de reemplazo",
        HandoffState.INDETERMINATE.value: "sin pre-transición (CHECK) → boundary de reemplazo",
        HandoffState.SUPERSEDING.value: "reafirma SUPERSEDING → boundary de reemplazo",
    }
    assert recetas_de_escape == {
        "awaiting_deployment": "pre-mutación → SUPERSEDING → boundary de reemplazo",
        "indeterminate": "sin pre-transición (CHECK) → boundary de reemplazo",
        "superseding": "reafirma SUPERSEDING → boundary de reemplazo",
    }


def test_el_digest_no_tiene_exclusiones_nominales_dentro_del_scope() -> None:
    """F-003: TODO archivo regular propio dentro de ``textures/`` entra al
    digest, sin excepción por nombre. La única exclusión es ESTRUCTURAL: lo que
    está fuera del subárbol (``mods/TexGen Output/meta.ini``). El ancla se
    reduce a que el módulo de digest no importe/defina ningún conjunto de
    exclusiones nominales."""
    import sky_claw.local.tools.artifact_digest as digest_mod

    assert not hasattr(digest_mod, "EXCLUSIONES_DE_DIGEST"), (
        "F-003: la exclusión nominal por nombre fue removida — todo archivo propio "
        "del subtree entra al digest; reintroducirla debe ser una decisión explícita"
    )


def test_los_escritores_de_la_tabla_deployment_handoffs_estan_congelados() -> None:
    """Quién escribe la tabla durable, enumerado por AST y congelado: hoy sólo
    ``journal.py`` (API atómica) y ``handoffs.py`` (SQL del contrato). Un
    serializador nuevo que toque la tabla por fuera del principio 5 rompe el
    ancla a propósito."""
    import ast

    raiz = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"
    escritores: set[str] = set()
    for archivo in raiz.rglob("*.py"):
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and "deployment_handoffs" in nodo.value:
                escritores.add(archivo.relative_to(raiz).as_posix())
                break

    assert escritores == {
        "app/db/journal.py",
        "app/db/handoffs.py",
    }
