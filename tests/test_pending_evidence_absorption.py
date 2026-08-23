"""PATCH 0011 — absorción durable de evidencia orphan PER-ARTIFACT (RED first).

Congela el contrato introducido para cerrar PR493_PENDING_EVIDENCE_REUSE:

- Un INDETERMINATE materializado absorbe la EVIDENCIA de TODAS sus candidatas
  para UN artifact físico concreto — identidad ``(transaction_id,
  artifact_path)`` — en el MISMO boundary que el INSERT y el consumo de
  receipts. Jamás modifica ``transactions.status``: una corrida viva puede ser
  candidata espuria (demostrado en el bloqueo previo) y su lifecycle es
  intocable desde la reconciliación.
- El oracle deja de reutilizar evidencia ya absorbida PARA ESE artifact, tanto
  la rama PENDING como la ROLLED_BACK+UNRESOLVED. El stale sweep queda intacto.
- Una TX puede nombrar varios artifacts (el manifiesto del pipeline lista
  DynDOLOD Output, TexGen Output y el staging crudo): absorber A no destruye
  evidencia legítima de B.
"""

from __future__ import annotations

import pathlib
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.handoffs import (
    DeploymentHandoff,
    HandoffState,
    OrphanEvidenceUnresolved,
    SweepReceiptState,
    clave_de_artifact,
)
from sky_claw.app.db.journal import JournalTransactionError, OperationJournal, TransactionStatus
from sky_claw.app.db.locks import DistributedLockManager, LockInfo
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.app.orchestrator.preview.action_manifest import build_action_manifest
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODRunner,
    ToolExecutionResult,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

# =============================================================================
# Fixtures y helpers (patrón espejo de test_dyndolod_handoff_durable.py)
# =============================================================================


@pytest.fixture
async def journal_tmp(tmp_path: pathlib.Path):  # noqa: ANN201
    j = OperationJournal(tmp_path / "journal.db")
    await j.open()
    yield j, tmp_path / "journal.db"
    await j.close()


def _entorno(tmp_path: pathlib.Path) -> tuple[DynDOLODConfig, DynDOLODRunner]:
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir(exist_ok=True)
    (exe_dir / "DynDOLODx64.exe").touch()
    (exe_dir / "TexGenx64.exe").touch()
    config = DynDOLODConfig(
        game_path=game,
        mo2_path=tmp_path / "MO2",
        mo2_mods_path=tmp_path / "MO2" / "mods",
        dyndolod_exe=exe_dir / "DynDOLODx64.exe",
        texgen_exe=exe_dir / "TexGenx64.exe",
    )
    return config, DynDOLODRunner(config)


def _svc(journal: object, runner: DynDOLODRunner, *, perfil: str | None = "Perfil-A") -> DynDOLODPipelineService:
    lock_mgr = AsyncMock(spec=DistributedLockManager)
    lock_mgr.acquire_lock = AsyncMock(
        return_value=LockInfo(
            resource_id="dyndolod-pipeline",
            agent_id="dyndolod-pipeline-service",
            acquired_at=1000.0,
            expires_at=1600.0,
        )
    )
    lock_mgr.release_lock = AsyncMock(return_value=True)

    def _snap() -> SnapshotInfo:
        from datetime import datetime

        return SnapshotInfo(
            snapshot_id="snap",
            original_path="/x",
            snapshot_path="/s",
            checksum="c",
            size_bytes=1,
            created_at=datetime.now().astimezone(),
        )

    snap_mgr = AsyncMock(spec=FileSnapshotManager)
    snap_mgr.create_snapshot = AsyncMock(side_effect=lambda *a, **k: _snap())
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    svc = DynDOLODPipelineService(
        lock_manager=lock_mgr,
        snapshot_manager=snap_mgr,
        journal=journal,  # type: ignore[arg-type]
        path_resolver=MagicMock(),
        event_bus=bus,
        mo2_profile=perfil,
    )
    svc._runner = runner  # type: ignore[attr-defined]
    return svc


class _ProcesoFalso:
    def __init__(self, al_ejecutar) -> None:
        self.al_ejecutar = al_ejecutar

    async def __call__(self, *args: object, **kwargs: object) -> tuple[str, str, int, float]:
        if self.al_ejecutar is not None:
            self.al_ejecutar()
        return "", "", 0, 1.0


def _texgen_genera(config: DynDOLODConfig, marca: bytes):
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME

    def _escribir() -> None:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "a.dds").write_bytes(marca)
        log = config.dyndolod_exe.parent / "Logs" / "TexGen_SSE_log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "[00:00:01] Using Output Path: C:\\out\\\n[00:10:00] TexGen completed successfully\n",
            encoding="utf-8",
        )

    return _escribir


def _dyn_ok(staging: pathlib.Path):
    async def _ok(**kw: object) -> ToolExecutionResult:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "DynDOLOD.esp").write_bytes(b"esp")
        return ToolExecutionResult(True, "DynDOLOD", 0, "", "", output_path=staging)

    return AsyncMock(side_effect=_ok)


def _escribir_mod(mod_texgen: pathlib.Path, contenido: bytes = b"CURRENT!") -> None:
    raiz = mod_texgen / "textures"
    raiz.mkdir(parents=True, exist_ok=True)
    (raiz / "a.dds").write_bytes(contenido)


def _mirror_a_data(origen: pathlib.Path, data_dir: pathlib.Path) -> None:
    destino = data_dir / origen.name
    for archivo in sorted(p for p in origen.rglob("*") if p.is_file()):
        espejo = destino / archivo.relative_to(origen)
        espejo.parent.mkdir(parents=True, exist_ok=True)
        espejo.write_bytes(archivo.read_bytes())


async def _evidencia(journal: OperationJournal, artifacts: list[pathlib.Path], descripcion: str = "corrida") -> int:
    """TX PENDING cuyo ActionManifest REAL nombra uno o VARIOS artifacts."""
    tx = await journal.begin_transaction(f"DynDOLOD pipeline ({descripcion})", agent_id="dyndolod-pipeline-service")
    manifest = build_action_manifest(
        ritual_id=f"dyndolod-pipeline-{tx}",
        tool="DynDOLOD",
        tool_version=None,
        target_files=[str(a) for a in artifacts],
        snapshots=[],
        summary=f"Generar LODs ({descripcion}).",
    )
    await journal.persist_action_manifest(manifest, agent_id="dyndolod-pipeline-service", transaction_id=tx)
    return tx


async def _reconciliar(journal: OperationJournal, config: DynDOLODConfig) -> object:
    from sky_claw.app.db.handoffs import reconciliar_orphan_de_artifact as rec

    return await rec(
        journal=journal,
        mod_texgen=config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )


async def _absorciones(db: pathlib.Path) -> list[tuple[int, str, int]]:
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute(
            "SELECT transaction_id, artifact_path, handoff_id FROM orphan_evidence_absorptions "
            "ORDER BY transaction_id, artifact_path"
        )
        filas = [(int(r[0]), str(r[1]), int(r[2])) for r in await cur.fetchall()]
    return filas


async def _envejecer(db: pathlib.Path, tx_id: int, horas: int) -> None:
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute(
            "UPDATE transactions SET created_at = datetime('now', ?) WHERE transaction_id = ?",
            (f"-{horas} hours", tx_id),
        )
        await conn.commit()


async def _receipt(journal: OperationJournal, tx_id: int) -> str | None:
    async with journal._db.execute(  # noqa: SLF001
        "SELECT state FROM stale_pending_sweep_receipts WHERE transaction_id = ?",
        (tx_id,),
    ) as cur:
        fila = await cur.fetchone()
    return fila[0] if fila else None


def _registro_awaiting(config: DynDOLODConfig, source_tx: int) -> DeploymentHandoff:
    d = digest_arbol(config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME / "textures")
    return DeploymentHandoff(
        handoff_id=0,
        source_tx_id=source_tx,
        state=HandoffState.AWAITING_DEPLOYMENT,
        artifact_path=clave_de_artifact(config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME),
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        expected_digest=d.digest,
        expected_files=d.files,
        expected_bytes=d.bytes,
        observed_digest=None,
        observed_files=None,
        observed_bytes=None,
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )


# =============================================================================
# Anclas de esquema
# =============================================================================


def test_esquema_declara_tabla_absorciones_con_identidad_tx_artifact() -> None:
    """La identidad de la absorción es (transaction_id, artifact_path): una TX
    puede nombrar varios artifacts y la absorción nunca es global por TX."""
    from sky_claw.app.db.handoffs import ORPHAN_ABSORPTIONS_SCHEMA_SQL

    assert "orphan_evidence_absorptions" in ORPHAN_ABSORPTIONS_SCHEMA_SQL
    assert "UNIQUE (transaction_id, artifact_path)" in ORPHAN_ABSORPTIONS_SCHEMA_SQL
    # §15: sin ON DELETE — borrar el handoff referenciado debe ser imposible
    # mientras la relación viva (fail-closed), jamás cascade silencioso.
    assert "ON DELETE CASCADE" not in ORPHAN_ABSORPTIONS_SCHEMA_SQL
    # El schema compuesto del journal incluye la tabla nueva (CREATE IF NOT
    # EXISTS → una DB pre-0011 abre sin migración destructiva).
    assert "orphan_evidence_absorptions" in OperationJournal._SCHEMA_SQL
    assert "uq_absorpcion_tx_artifact" in OperationJournal._SCHEMA_SQL


def test_la_absorcion_solo_se_escribe_en_el_boundary_del_insert() -> None:
    """Ancla M11-4 (estilo AST del repo): ``_INSERT_ABSORCION_EVIDENCIA_SQL``
    tiene EXACTAMENTE DOS sitios de escritura, ambos DENTRO de helpers que
    ASUMEN el boundary del caller:

    - ``_escribir_handoff_indeterminado`` (materialización del INDETERMINATE);
    - ``_escribir_handoff_de_deployment`` (POST493_ACTIVE_INDETERMINATE_
      EVIDENCE: absorción de la evidencia histórica al reemplazar un handoff).

    Un tercer escritor —segunda conexión, post-commit, path paralelo— rompe el
    ancla a propósito: la ventana 'evidencia sin boundary durable' resucitaría
    la evidencia o la consumiría sin handoff detrás."""
    fuente = pathlib.Path(sky_claw_app_db_journal_path()).read_text(encoding="utf-8")
    # Definición + DOS usos como executemany (uno por boundary).
    assert fuente.count("_INSERT_ABSORCION_EVIDENCIA_SQL") == 3
    # Las dos ramas del boundary indeterminado delegan en el MISMO helper con
    # el conjunto.
    assert fuente.count("ids_absorcion=ids_absorcion") >= 2


def sky_claw_app_db_journal_path() -> str:
    import sky_claw.app.db.journal as mod

    return mod.__file__ or ""


async def test_fk_impide_borrar_handoff_referenciado(journal_tmp) -> None:  # noqa: ANN001
    """Borrar historia de handoffs NO puede resucitar evidencia: el FK sin
    ON DELETE convierte ese borrado en IntegrityError (fail-closed)."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    _escribir_mod(config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME)
    tx = await _evidencia(journal, [config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME])
    res = await _reconciliar(journal, config)
    assert isinstance(res, DeploymentHandoff)
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute("DELETE FROM deployment_handoffs WHERE handoff_id = ?", (res.handoff_id,))
        await conn.rollback()
    assert tx > 0


# =============================================================================
# Contrato central: absorción durable per-artifact en el boundary
# =============================================================================


async def test_oracle_no_reutiliza_evidencia_absorbida(journal_tmp) -> None:  # noqa: ANN001
    """RED 11-2: tras materializar el INDETERMINATE, el oracle NO vuelve a
    devolver la candidata absorbida PARA ESE artifact."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    clave = clave_de_artifact(mod)
    tx1 = await _evidencia(journal, [mod])
    assert tx1 in await journal.transacciones_que_nombran(clave)

    res = await _reconciliar(journal, config)
    assert isinstance(res, DeploymentHandoff) and res.source_tx_id == tx1
    assert res.state is HandoffState.INDETERMINATE

    # La TX conserva su lifecycle (intocable: podía ser una corrida viva).
    assert (await journal.get_transaction(tx1)).status is TransactionStatus.PENDING
    # Pero su EVIDENCIA para este artifact quedó consumida durablemente.
    assert tx1 not in await journal.transacciones_que_nombran(clave)
    assert await _absorciones(db) == [(tx1, clave, res.handoff_id)]


async def test_multi_candidatas_todas_absorvidas_mismo_boundary(journal_tmp) -> None:  # noqa: ANN001
    """RED 11-3 / §20: TODAS las candidatas quedan absorbidas — no sólo
    source_tx_id — y ninguna muta su estado de TX."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    clave = clave_de_artifact(mod)
    tx1 = await _evidencia(journal, [mod])
    tx2 = await _evidencia(journal, [mod])

    res = await _reconciliar(journal, config)
    assert isinstance(res, DeploymentHandoff) and res.source_tx_id == tx1
    assert await journal.transacciones_que_nombran(clave) == []
    estados = {t.transaction_id: t.status for t in await journal.list_recent_transactions(50)}
    assert estados[tx1] is TransactionStatus.PENDING
    assert estados[tx2] is TransactionStatus.PENDING
    ordenadas = {(t, a) for t, a, _h in await _absorciones(db)}
    assert ordenadas == {(tx1, clave), (tx2, clave)}
    assert all(h == res.handoff_id for _t, _a, h in await _absorciones(db))


async def test_absorcion_per_artifact_no_destruye_otro_artifact(journal_tmp) -> None:  # noqa: ANN001
    """§10 (BLOQUEANTE): TX1 nombra A y B; absorber (TX1,A) deja intacta la
    evidencia de B — incluso después del stale sweep con receipt UNRESOLVED."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    _escribir_mod(art_a)
    art_b.mkdir(parents=True, exist_ok=True)
    clave_a, clave_b = clave_de_artifact(art_a), clave_de_artifact(art_b)
    tx1 = await _evidencia(journal, [art_a, art_b])
    assert tx1 in await journal.transacciones_que_nombran(clave_a)
    assert tx1 in await journal.transacciones_que_nombran(clave_b)

    await _reconciliar(journal, config)  # absorbe SOLO (TX1, A)
    assert tx1 not in await journal.transacciones_que_nombran(clave_a)
    assert tx1 in await journal.transacciones_que_nombran(clave_b)

    # Stale sweep intacto: la TX envejece, se barre, gana receipt UNRESOLVED…
    await journal.close()
    await _envejecer(db, tx1, 48)
    journal = OperationJournal(db)
    await journal.open()
    try:
        assert (await journal.get_transaction(tx1)).status is TransactionStatus.ROLLED_BACK
        assert await _receipt(journal, tx1) == SweepReceiptState.UNRESOLVED.value
        # …y el oracle per-artifact separa: A consumido, B sigue siendo evidencia.
        assert tx1 not in await journal.transacciones_que_nombran(clave_a)
        assert tx1 in await journal.transacciones_que_nombran(clave_b)
    finally:
        await journal.close()


async def test_sweep_post_absorcion_mantiene_housekeeping_y_cierra_hueco(journal_tmp) -> None:  # noqa: ANN001
    """§9: el sweep NO se salta TXs absorbidas — las convierte a ROLLED_BACK
    (housekeeping histórico) — y aun así el oracle no las readmite vía receipt."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    tx1 = await _evidencia(journal, [mod])
    await _reconciliar(journal, config)

    await journal.close()
    await _envejecer(db, tx1, 48)
    journal = OperationJournal(db)
    await journal.open()
    try:
        # El sweep productivo corre dentro de open() y NO se salta la TX por
        # estar absorbida: housekeeping histórico intacto (§24).
        assert (await journal.get_transaction(tx1)).status is TransactionStatus.ROLLED_BACK
        assert await _receipt(journal, tx1) == SweepReceiptState.UNRESOLVED.value
        # …y el oracle no la readmite para este artifact vía receipt (§9).
        assert tx1 not in await journal.transacciones_que_nombran(clave_de_artifact(mod))
    finally:
        await journal.close()


# =============================================================================
# Atomicidad del boundary (race + commit failure)
# =============================================================================


async def test_race_owner_activo_no_absorbe_ni_consumi(journal_tmp) -> None:  # noqa: ANN001
    """RED 11-4: si el INSERT pierde contra un owner concurrente, tampoco hay
    absorciones durables ni receipts consumidos — todo el boundary revirtió."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    clave = clave_de_artifact(mod)
    tx1 = await _evidencia(journal, [mod])
    await journal.close()
    await _envejecer(db, tx1, 48)
    journal = OperationJournal(db)  # open() corre el sweep → receipt UNRESOLVED

    real = journal.transacciones_que_nombran

    async def con_carrera(objetivo: str) -> list[int]:
        candidatas = await real(objetivo)
        competidor = await journal.begin_transaction("owner concurrente", agent_id="test")
        registro = DeploymentHandoff(
            handoff_id=0,
            source_tx_id=competidor,
            state=HandoffState.INDETERMINATE,
            artifact_path=clave,
            game_key=clave_de_artifact(config.game_path),
            mods_root_key=clave_de_artifact(config.mo2_mods_path),
            data_key=clave_de_artifact(config.data_dir),
            expected_profile="Perfil-A",
            expected_digest=None,
            expected_files=None,
            expected_bytes=None,
            observed_digest=None,
            observed_files=None,
            observed_bytes=None,
            created_at="",
            updated_at="",
            completed_at=None,
            superseded_at=None,
            superseded_by=None,
        )
        await journal.registrar_handoff_indeterminado(registro=registro)
        return candidatas

    journal.transacciones_que_nombran = con_carrera  # type: ignore[method-assign]
    res = await _reconciliar(journal, config)
    journal.transacciones_que_nombran = real  # type: ignore[method-assign]
    try:
        # El handoff devuelto es el del COMPETIDOR (el nuestro perdió), no un
        # INDETERMINATE fabricado desde tx1.
        assert isinstance(res, DeploymentHandoff)
        assert res.source_tx_id != tx1
        assert res.state is HandoffState.INDETERMINATE
        # NADA del boundary perdedor quedó afirmado: receipt intacto, cero
        # absorciones, y la TX conserva su estado previo (ROLLED_BACK: el
        # sweep de open() la barrió al reabrir — su receipt sigue UNRESOLVED).
        assert await _receipt(journal, tx1) == SweepReceiptState.UNRESOLVED.value
        assert (await journal.get_transaction(tx1)).status is TransactionStatus.ROLLED_BACK
        assert await _absorciones(db) == []
        assert await journal.transacciones_que_nombran(clave_de_artifact(mod)) == [tx1]
    finally:
        await journal.close()


async def test_commit_failure_revierte_absorciones_y_consumos(journal_tmp) -> None:  # noqa: ANN001
    """RED 11-5 / §7: fallo del commit tras INSERT+absorpciones+consumos deja
    TODO como estaba (all-or-nothing)."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    tx1 = await _evidencia(journal, [mod])
    await journal.close()
    await _envejecer(db, tx1, 48)
    journal = OperationJournal(db)
    await journal.open()
    receipt_antes = await _receipt(journal, tx1)
    assert receipt_antes == SweepReceiptState.UNRESOLVED.value

    commit_real = journal._db.commit  # noqa: SLF001

    async def commit_roto() -> None:
        raise sqlite3.OperationalError("INYECTADO_C4")

    journal._db.commit = commit_roto  # noqa: SLF001  # type: ignore[method-assign]
    error = None
    try:
        await _reconciliar(journal, config)
    except JournalTransactionError as exc:
        error = exc
    finally:
        journal._db.commit = commit_real  # noqa: SLF001  # type: ignore[method-assign]
        await journal.close()
    assert error is not None

    # Post-reopen: nada del boundary quedó durable (all-or-nothing). El sweep
    # de open() sí barrió la TX vieja (housekeeping intacto, ROLLED_BACK) pero
    # su receipt sigue SIN consumir y cero absorciones sobrevivieron.
    journal = OperationJournal(db)
    await journal.open()
    try:
        assert await _receipt(journal, tx1) == SweepReceiptState.UNRESOLVED.value
        assert (await journal.get_transaction(tx1)).status is TransactionStatus.ROLLED_BACK
        assert await _absorciones(db) == []
        assert await journal.consultar_handoff_activo(clave_de_artifact(mod)) is None
    finally:
        await journal.close()


# =============================================================================
# Tri-state del artifact
# =============================================================================


async def test_unknown_no_crea_handoff_ni_absorbe(journal_tmp) -> None:  # noqa: ANN001
    """§17: evidencia + alcance UNKNOWN → OrphanEvidenceUnresolved SIN handoff
    NI absorción (la conclusión durable todavía no existe); luego reintenta."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME  # padres inexistentes → UNKNOWN
    clave = clave_de_artifact(mod)
    tx1 = await _evidencia(journal, [mod])

    res = await _reconciliar(journal, config)
    assert isinstance(res, OrphanEvidenceUnresolved) and tx1 in res.candidatas
    assert await journal.consultar_handoff_activo(clave) is None
    assert await _absorciones(db) == []
    assert tx1 in await journal.transacciones_que_nombran(clave)

    # Al volverse alcanzable, la misma evidencia materializa el INDETERMINATE.
    _escribir_mod(mod)
    res2 = await _reconciliar(journal, config)
    assert isinstance(res2, DeploymentHandoff) and res2.state is HandoffState.INDETERMINATE
    assert tx1 not in await journal.transacciones_que_nombran(clave)
    assert await _absorciones(db) == [(tx1, clave, res2.handoff_id)]


async def test_absent_preserva_contrato_sin_absorcion(journal_tmp) -> None:  # noqa: ANN001
    """§18: ausencia demostrada → NO_ARTIFACT de receipts, sin INDETERMINATE ni
    absorción (nunca hubo materialización que la justifique)."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    config.mo2_mods_path.mkdir(parents=True, exist_ok=True)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME  # contenedor válido, target ausente
    clave = clave_de_artifact(mod)
    tx1 = await _evidencia(journal, [mod])
    await journal.close()
    await _envejecer(db, tx1, 48)
    journal = OperationJournal(db)
    await journal.open()
    try:
        res = await _reconciliar(journal, config)
        assert res is None
        assert await _receipt(journal, tx1) == SweepReceiptState.NO_ARTIFACT.value
        assert await journal.consultar_handoff_activo(clave) is None
        assert await _absorciones(db) == []
    finally:
        await journal.close()


# =============================================================================
# Idempotencia / vida de la relación
# =============================================================================


async def test_reintentos_no_duplican_absorcion(journal_tmp) -> None:  # noqa: ANN001
    """§21: reentrancia (restart/reconcile repetido) no duplica estado: el
    segundo intento encuentra al owner activo y retorna temprano."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    _tx1 = await _evidencia(journal, [mod])
    primera = await _reconciliar(journal, config)
    segunda = await _reconciliar(journal, config)
    assert isinstance(primera, DeploymentHandoff) and isinstance(segunda, DeploymentHandoff)
    assert segunda.handoff_id == primera.handoff_id
    assert len(await _absorciones(db)) == 1


async def test_insert_duplicado_falla_ruidoso(journal_tmp) -> None:  # noqa: ANN001
    """§21: violar la identidad (transaction_id, artifact_path) levanta — no se
    oculta ni se corrompe el estado."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    clave = clave_de_artifact(mod)
    tx1 = await _evidencia(journal, [mod])
    await _reconciliar(journal, config)
    async with aiosqlite.connect(str(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO orphan_evidence_absorptions (transaction_id, artifact_path, handoff_id) "
                "VALUES (?, ?, 999)",
                (tx1, clave),
            )


# =============================================================================
# Compatibilidad hacia atrás (DB pre-0011)
# =============================================================================


async def test_db_pre_0011_abre_y_conserva_comportamiento(journal_tmp) -> None:  # noqa: ANN001
    """§4/§16: una DB histórica sin tabla de absorciones abre sin migración
    destructiva, conserva handoffs/receipts y su conducta pre-0011."""
    journal, db = journal_tmp
    tmp_path = db.parent
    config, _runner = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    clave = clave_de_artifact(mod)
    tx1 = await _evidencia(journal, [mod])
    await journal.close()

    # Simular DB pre-0011: sin la tabla nueva, CON historia.
    async with aiosqlite.connect(str(db)) as conn:
        await conn.execute("DROP TABLE orphan_evidence_absorptions")
        await conn.commit()

    journal = OperationJournal(db)
    await journal.open()  # CREATE IF NOT EXISTS recrea vacía; nada más cambia
    try:
        assert tx1 in await journal.transacciones_que_nombran(clave)
        res = await _reconciliar(journal, config)
        assert isinstance(res, DeploymentHandoff)
        assert tx1 not in await journal.transacciones_que_nombran(clave)
    finally:
        await journal.close()


# =============================================================================
# Aceptación principal — LIVE_SAFE_BY_CONSTRUCTION y cierre del P1
# =============================================================================


async def test_corrida_viva_candidata_espuria_no_se_corrompe(tmp_path: pathlib.Path) -> None:  # noqa: ANN001
    """§11 (bloqueante previo): una TX PENDING realmente VIVA puede ser
    candidata; la absorción-relación NO toca su lifecycle y la corrida cierra
    normal, supersedendo el INDETERMINATE espurio."""
    config, _runner_base = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    journal = OperationJournal(tmp_path / "journal.db")
    await journal.open()
    try:
        tx_live = await _evidencia(journal, [mod])  # begin_transaction + manifest, corrida en vuelo
        espurio = await _reconciliar(journal, config)
        assert isinstance(espurio, DeploymentHandoff) and espurio.source_tx_id == tx_live

        # La corrida VIVA termina bien por el boundary real (needs_deployment):
        await journal.crear_handoff_de_deployment(
            tx_live,
            descripcion="TexGen generado y empaquetado; esperando deployment",
            registro=_registro_awaiting(config, tx_live),
            viejo=espurio,
        )
        assert (await journal.get_transaction(tx_live)).status is TransactionStatus.COMMITTED
        activo = await journal.consultar_handoff_activo(clave_de_artifact(mod))
        assert activo is not None and activo.state is HandoffState.AWAITING_DEPLOYMENT
    finally:
        await journal.close()


async def test_p1_end_to_end_segundo_resume_no_bloqueado_por_tx1(tmp_path: pathlib.Path) -> None:  # noqa: ANN001
    """§12/§18 acceptance principal: lifecycle completo válido y después el
    segundo resume NO fabrica INDETERMINATE desde la TX1 antigua absorbida."""
    config, runner = _entorno(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod, contenido=b"GEN-1")
    journal = OperationJournal(tmp_path / "journal.db")
    await journal.open()
    svc = _svc(journal, runner)
    try:
        tx1 = await _evidencia(journal, [mod])
        primera = await _reconciliar(journal, config)
        assert isinstance(primera, DeploymentHandoff)

        # Regen explícita autorizada → supersede + AWAITING autorizado.
        with (
            patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_texgen_genera(config, b"GEN-2"))),
            patch.object(runner, "run_dyndolod", AsyncMock()),
        ):
            regen = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)
        assert regen["needs_deployment"] is True

        # Deployment (materializar visibilidad) + Resume exitoso.
        _mirror_a_data(mod / "textures", config.data_dir)
        with patch.object(runner, "run_dyndolod", _dyn_ok(config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME)):
            resume = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)
        assert resume["success"] is True, resume.get("errors")

        clave = clave_de_artifact(mod)
        assert await journal.consultar_handoff_activo(clave) is None
        # La evidencia absorbida NO reaparece tras el lifecycle exitoso (§14).
        assert tx1 not in await journal.transacciones_que_nombran(clave)
        assert len(await _absorciones(tmp_path / "journal.db")) == 1

        # Segundo resume: ya NO lo bloquea la TX1 antigua (DynDOLOD puede correr
        # según el resto de gates — aquí: éxito legacy verbatim).
        dyn2 = _dyn_ok(config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME)
        with patch.object(runner, "run_dyndolod", dyn2):
            segundo = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)
        assert segundo.get("reason") != "HandoffIndeterminate"
        assert segundo["success"] is True, segundo.get("errors")
        assert dyn2.await_count >= 1
    finally:
        await journal.close()


async def test_restart_no_refabrica_tras_absorcion_pre_y_post_threshold(tmp_path: pathlib.Path) -> None:  # noqa: ANN001
    """§13: tras lifecycle completo, restart (antes Y después del umbral stale)
    no fabrica INDETERMINATE desde la TX1 absorbida."""
    config, _r = _entorno(tmp_path)
    mod = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod)
    db = tmp_path / "journal.db"
    journal = OperationJournal(db)
    await journal.open()
    tx1 = await _evidencia(journal, [mod])
    espurio = await _reconciliar(journal, config)
    assert isinstance(espurio, DeploymentHandoff)
    # Cerrar el ciclo completo con TXs PROPIAS de la regen/resume: TX1 absorbida
    # permanece PENDING (era evidencia orphan, no una corrida viva) y el
    # lifecycle termina COMPLETED vía los boundaries reales del journal.
    clave = clave_de_artifact(mod)
    tx_reg = await journal.begin_transaction("regen", agent_id="test")
    nuevo_id = await journal.crear_handoff_de_deployment(
        tx_reg,
        descripcion=None,
        registro=_registro_awaiting(config, tx_reg),
        viejo=espurio,
    )
    _mirror_a_data(mod / "textures", config.data_dir)
    tx_res = await journal.begin_transaction("resume", agent_id="test")
    await journal.completar_handoff_de_resume(tx_res, nuevo_id)
    assert await journal.consultar_handoff_activo(clave) is None
    # Superseder/completar el handoff NO borra la relación (§14).
    absorciones_antes = await _absorciones(db)
    assert absorciones_antes == [(tx1, clave, espurio.handoff_id)]
    await journal.close()

    from sky_claw.app.db.handoffs import reconciliar_handoffs_de_deployment

    # Restart ANTES del umbral: startup no fabrica nada desde TX1 absorbida.
    j2 = OperationJournal(db)
    await j2.open()
    try:
        await reconciliar_handoffs_de_deployment(
            journal=j2,
            mod_texgen=mod,
            game_key=clave_de_artifact(config.game_path),
            mods_root_key=clave_de_artifact(config.mo2_mods_path),
            data_key=clave_de_artifact(config.data_dir),
            expected_profile="Perfil-A",
            digest_arbol=digest_arbol,
        )
        assert await j2.consultar_handoff_activo(clave_de_artifact(mod)) is None
        assert tx1 not in await j2.transacciones_que_nombran(clave_de_artifact(mod))
    finally:
        await j2.close()

    # Restart DESPUÉS del umbral: sweep la convierte a ROLLED_BACK (+receipt)…
    await _envejecer(db, tx1, 48)
    j3 = OperationJournal(db)
    await j3.open()
    try:
        assert (await j3.get_transaction(tx1)).status is TransactionStatus.ROLLED_BACK
        await reconciliar_handoffs_de_deployment(
            journal=j3,
            mod_texgen=mod,
            game_key=clave_de_artifact(config.game_path),
            mods_root_key=clave_de_artifact(config.mo2_mods_path),
            data_key=clave_de_artifact(config.data_dir),
            expected_profile="Perfil-A",
            digest_arbol=digest_arbol,
        )
        # …y aun así el startup NO refabrica desde esa evidencia absorbida.
        assert await j3.consultar_handoff_activo(clave_de_artifact(mod)) is None
        assert await _absorciones(db) == absorciones_antes
    finally:
        await j3.close()
