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


def test_digest_excluye_meta_ini_del_scope(tmp_path: pathlib.Path) -> None:
    raiz = tmp_path / "mod" / "textures"
    _escribir_mod(tmp_path / "mod")
    antes = digest_arbol(raiz)
    (raiz / "meta.ini").write_text("MO2 reescribe esto", encoding="utf-8")
    despues = digest_arbol(raiz)
    assert antes == despues


def test_digest_arbol_sin_archivos_propios_falla_cerrado(tmp_path: pathlib.Path) -> None:
    vacio = tmp_path / "vacio"
    vacio.mkdir()
    with pytest.raises(OSError):
        digest_arbol(vacio)


def test_clave_de_artifact_es_normcase(tmp_path: pathlib.Path) -> None:
    assert clave_de_artifact(tmp_path / "MO2" / "mods" / "TexGen Output") == clave_de_artifact(
        tmp_path / "MO2" / "mods" / "texgen output"
    )


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


def test_el_conjunto_de_exclusiones_del_digest_esta_congelado() -> None:
    """La exclusión del digest es exactamente ``meta.ini``: una exclusión ad-hoc
    nueva rompería el ancla y obliga a justificar por qué ese archivo no es
    parte de la identidad del artifact."""
    from sky_claw.local.tools.artifact_digest import EXCLUSIONES_DE_DIGEST

    assert frozenset({"meta.ini"}) == EXCLUSIONES_DE_DIGEST


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
