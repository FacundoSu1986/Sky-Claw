"""Pruebas de regresión, caracterización y contrato durable para Issue #506.

Problema resuelto:
El receipt de stale sweep (``stale_pending_sweep_receipts``) es global por
transacción (clave única ``transaction_id``) y representa estrictamente la
causalidad del sweep (``swept(tx) == True``).

La autoridad sobre la resolución de evidencia orphan reside exclusivamente a nivel
per-artifact en la tabla ``artifact_evidence_resolutions`` (identidad
``(transaction_id, artifact_path)``).

Cualquier operación sobre un artifact A (RUN 1, replacement, INDETERMINATE, NO_ARTIFACT)
resuelve la evidencia de A sin mutar el receipt global ni neutralizar la evidencia
de otro artifact B nombrado en la misma transacción.

Convención del repositorio: tests y comentarios en español.
"""

from __future__ import annotations

import ast
import pathlib
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.handoffs import (
    ArtifactResolutionKind,
    DeploymentHandoff,
    HandoffState,
    SweepReceiptState,
    clave_de_artifact,
    reconciliar_orphan_de_artifact,
)
from sky_claw.app.db.journal import (
    OperationJournal,
    TransactionStatus,
)
from sky_claw.app.db.locks import DistributedLockManager, LockInfo
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.app.orchestrator.preview.action_manifest import build_action_manifest
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

# =============================================================================
# SNAPSHOT DE EVIDENCIA
# =============================================================================


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """Snapshot atómico del estado causal y oráculo para dos artifacts."""

    transaction_status: str | None
    receipt_state: str | None
    resolutions: list[tuple[int, str, str, int | None]]
    oracle_a: list[int]
    oracle_b: list[int]


async def tomar_snapshot_evidencia(
    journal: OperationJournal,
    *,
    tx_id: int,
    clave_a: str,
    clave_b: str,
    db_path: pathlib.Path,
) -> EvidenceSnapshot:
    """Captura el estado completo de la transacción, receipt, resoluciones y oráculos."""
    tx = await journal.get_transaction(tx_id)
    tx_status = tx.status.value if tx is not None else None

    async with aiosqlite.connect(str(db_path)) as conn:
        # Estado del receipt
        async with conn.execute(
            "SELECT state FROM stale_pending_sweep_receipts WHERE transaction_id = ?",
            (tx_id,),
        ) as cur:
            fila_r = await cur.fetchone()
            receipt_state = str(fila_r[0]) if fila_r is not None else None

        # Resoluciones durables per-artifact (D4)
        async with conn.execute(
            "SELECT transaction_id, artifact_path, resolution_kind, handoff_id "
            "FROM artifact_evidence_resolutions "
            "WHERE transaction_id = ? ORDER BY artifact_path",
            (tx_id,),
        ) as cur:
            resoluciones = [
                (int(r[0]), str(r[1]), str(r[2]), int(r[3]) if r[3] is not None else None) for r in await cur.fetchall()
            ]

    oracle_a = await journal.transacciones_que_nombran(clave_a)
    oracle_b = await journal.transacciones_que_nombran(clave_b)

    return EvidenceSnapshot(
        transaction_status=tx_status,
        receipt_state=receipt_state,
        resolutions=resoluciones,
        oracle_a=oracle_a,
        oracle_b=oracle_b,
    )


# =============================================================================
# FIXTURES Y HELPERS
# =============================================================================


@pytest.fixture
async def journal_tmp(tmp_path: pathlib.Path):  # noqa: ANN201
    """Crea y abre una instancia de OperationJournal en un directorio temporal."""
    db_path = tmp_path / "journal.db"
    j = OperationJournal(db_path)
    await j.open()
    yield j, db_path
    await j.close()


def _entorno(tmp_path: pathlib.Path) -> tuple[DynDOLODConfig, DynDOLODRunner]:
    """Configuración básica de entorno para pruebas con dos artifacts."""
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir(exist_ok=True)
    (exe_dir / "DynDOLODx64.exe").touch()
    (exe_dir / "TexGenx64.exe").touch()
    mo2 = tmp_path / "MO2"
    mods = mo2 / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    config = DynDOLODConfig(
        game_path=game,
        mo2_path=mo2,
        mo2_mods_path=mods,
        dyndolod_exe=exe_dir / "DynDOLODx64.exe",
        texgen_exe=exe_dir / "TexGenx64.exe",
    )
    return config, DynDOLODRunner(config)


def _svc(journal: object, runner: DynDOLODRunner, *, perfil: str | None = "Perfil-A") -> DynDOLODPipelineService:
    """Instancia de DynDOLODPipelineService con mocks controlados."""
    lock_mgr = AsyncMock(spec=DistributedLockManager)
    ahora = time.time()
    lock_info = LockInfo(
        resource_id="dyndolod-pipeline",
        agent_id="dyndolod-pipeline-service",
        acquired_at=ahora,
        expires_at=ahora + 3600.0,
    )
    lock_mgr.acquire_lock = AsyncMock(return_value=lock_info)
    lock_mgr.get_lock_info = AsyncMock(return_value=lock_info)
    lock_mgr.release_lock = AsyncMock(return_value=True)

    snap_mgr = AsyncMock(spec=FileSnapshotManager)
    from datetime import datetime

    snap_mgr.create_snapshot = AsyncMock(
        return_value=SnapshotInfo(
            snapshot_id="snap",
            original_path="/x",
            snapshot_path="/s",
            checksum="c",
            size_bytes=1,
            created_at=datetime.now().astimezone(),
        )
    )
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


async def _crear_tx_multi_artifact(
    journal: OperationJournal,
    artifacts: list[pathlib.Path],
    *,
    descripcion: str = "corrida multi-artifact",
) -> int:
    """Crea una transacción PENDING cuyo ActionManifest nombra múltiples artifacts."""
    tx = await journal.begin_transaction(
        f"DynDOLOD pipeline ({descripcion})",
        agent_id="dyndolod-pipeline-service",
    )
    manifest = build_action_manifest(
        ritual_id=f"dyndolod-pipeline-{tx}",
        tool="DynDOLOD",
        tool_version=None,
        target_files=[str(a) for a in artifacts],
        snapshots=[],
        summary=f"Generar LODs ({descripcion}).",
    )
    await journal.persist_action_manifest(
        manifest,
        agent_id="dyndolod-pipeline-service",
        transaction_id=tx,
    )
    return tx


async def _envejecer_tx(db_path: pathlib.Path, tx_id: int, horas: int = 48) -> None:
    """Envejece la fecha de creación de una transacción para simular stale pending."""
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "UPDATE transactions SET created_at = datetime('now', ?) WHERE transaction_id = ?",
            (f"-{horas} hours", tx_id),
        )
        await conn.commit()


def _escribir_artifact_dir(art_path: pathlib.Path, contenido: bytes = b"TEST_DATA") -> None:
    """Crea la estructura física de un artifact directorio."""
    texturas = art_path / "textures"
    texturas.mkdir(parents=True, exist_ok=True)
    (texturas / "diffuse.dds").write_bytes(contenido)


def _crear_registro_awaiting(
    art_path: pathlib.Path,
    source_tx: int,
    config: DynDOLODConfig,
) -> DeploymentHandoff:
    """Crea un registro DeploymentHandoff en estado AWAITING_DEPLOYMENT con digest válido."""
    d = digest_arbol(art_path / "textures")
    return DeploymentHandoff(
        handoff_id=0,
        source_tx_id=source_tx,
        state=HandoffState.AWAITING_DEPLOYMENT,
        artifact_path=clave_de_artifact(art_path),
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


def _sky_claw_journal_path() -> pathlib.Path:
    """Ruta al archivo sky_claw/app/db/journal.py."""
    import sky_claw.app.db.journal as j_mod

    return pathlib.Path(j_mod.__file__ or "").resolve()


def _funciones_que_mencionan_ast(modulo_path: pathlib.Path, aguja: str) -> set[str]:
    """Retorna los nombres de funciones cuyo cuerpo fuente contiene `aguja`."""
    fuente = modulo_path.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    encontradas: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seg = ast.get_source_segment(fuente, nodo) or ""
        if aguja in seg:
            encontradas.add(nodo.name)
    return encontradas


def _funciones_que_llaman_ast(modulo_path: pathlib.Path, callee_name: str) -> set[str]:
    """Retorna los nombres de funciones que contienen una llamada explícita a `callee_name`."""
    fuente = modulo_path.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    llamadores: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for subnodo in ast.walk(nodo):
            if isinstance(subnodo, ast.Call):
                func = subnodo.func
                nombre = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if nombre == callee_name:
                    llamadores.add(nodo.name)
    return llamadores


# =============================================================================
# T506-01: SUPERSEDED / RUN 1
# =============================================================================


async def test_t506_01_superseded_run1_preserva_evidencia_de_b(journal_tmp) -> None:  # noqa: ANN001
    """T506-01: RUN 1 sobre Artifact A resuelve A pero preserva la evidencia de B."""
    journal, db_path = journal_tmp
    tmp_path = db_path.parent
    config, _runner = _entorno(tmp_path)

    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a = clave_de_artifact(art_a)
    clave_b = clave_de_artifact(art_b)

    _escribir_artifact_dir(art_a, b"TEXGEN_DATA")
    _escribir_artifact_dir(art_b, b"DYNDOLOD_DATA")

    # 1. TX swept a ROLLED_BACK con receipt UNRESOLVED que nombra A y B
    tx_stale = await _crear_tx_multi_artifact(journal, [art_a, art_b], descripcion="stale multi")
    await _envejecer_tx(db_path, tx_stale, 48)
    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)
    assert barridas == 1

    # Verificar PRE
    snap_pre = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )
    assert snap_pre.transaction_status == TransactionStatus.ROLLED_BACK.value
    assert snap_pre.receipt_state == SweepReceiptState.UNRESOLVED.value
    assert snap_pre.resolutions == []
    assert snap_pre.oracle_a == [tx_stale]
    assert snap_pre.oracle_b == [tx_stale]

    # 2. Ejecutar RUN 1 para A vía API real (crear_handoff_de_deployment con viejo=None)
    tx_run1 = await journal.begin_transaction("RUN 1 TexGen", agent_id="test")
    reg_a = _crear_registro_awaiting(art_a, tx_run1, config)
    h_a_id = await journal.crear_handoff_de_deployment(
        tx_run1,
        descripcion="TexGen RUN 1",
        registro=reg_a,
        viejo=None,
    )
    assert h_a_id > 0

    # 3. Snapshot POST
    snap_post = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )

    # El receipt de sweep conserva su estado causal (inmutable por TX)
    assert snap_post.receipt_state == SweepReceiptState.UNRESOLVED.value

    # Resolución per-artifact registrada exactamente para A (SUPERSEDED_BY_RUN)
    resolucion_a = [r for r in snap_post.resolutions if r[1] == clave_a]
    resolucion_b = [r for r in snap_post.resolutions if r[1] == clave_b]
    assert resolucion_a == [(tx_stale, clave_a, ArtifactResolutionKind.SUPERSEDED_BY_RUN.value, h_a_id)]
    assert resolucion_b == [], "no debe existir resolución para B"

    # Oráculos: A queda resuelto (excluido) y B PRESERVA su evidencia
    assert tx_stale not in snap_post.oracle_a
    assert tx_stale in snap_post.oracle_b


# =============================================================================
# T506-02: SUPERSEDED / REPLACEMENT
# =============================================================================


async def test_t506_02_superseded_replacement_preserva_evidencia_de_b(journal_tmp) -> None:  # noqa: ANN001
    """T506-02: Replacement de handoff de A resuelve A pero preserva la evidencia de B."""
    journal, db_path = journal_tmp
    tmp_path = db_path.parent
    config, _runner = _entorno(tmp_path)

    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a = clave_de_artifact(art_a)
    clave_b = clave_de_artifact(art_b)

    _escribir_artifact_dir(art_a, b"TEXGEN_V1")
    _escribir_artifact_dir(art_b, b"DYNDOLOD_V1")

    # 1. Handoff viejo para A
    tx_vieja_a = await journal.begin_transaction("TexGen v1", agent_id="test")
    reg_viejo_a = _crear_registro_awaiting(art_a, tx_vieja_a, config)
    h_viejo_a_id = await journal.crear_handoff_de_deployment(
        tx_vieja_a,
        descripcion="TexGen v1 entregado",
        registro=reg_viejo_a,
        viejo=None,
    )
    h_viejo_a = await journal.consultar_handoff_activo(clave_a)
    assert h_viejo_a is not None and h_viejo_a.handoff_id == h_viejo_a_id

    # 2. TX swept a ROLLED_BACK + UNRESOLVED nombrando A y B
    tx_stale = await _crear_tx_multi_artifact(journal, [art_a, art_b], descripcion="stale multi")
    await _envejecer_tx(db_path, tx_stale, 48)
    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)
    assert barridas == 1

    # Verificar PRE
    snap_pre = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )
    assert snap_pre.oracle_a == [tx_stale]
    assert snap_pre.oracle_b == [tx_stale]

    # 3. Ejecutar replacement real para A
    _escribir_artifact_dir(art_a, b"TEXGEN_V2")
    tx_rep_a = await journal.begin_transaction("TexGen v2 replacement", agent_id="test")
    reg_nuevo_a = _crear_registro_awaiting(art_a, tx_rep_a, config)
    await journal.crear_handoff_de_deployment(
        tx_rep_a,
        descripcion="TexGen v2 reemplazo",
        registro=reg_nuevo_a,
        viejo=h_viejo_a,
    )

    # 4. Snapshot POST
    snap_post = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )

    # Resolución per-artifact registrada exactamente para A (ABSORBED_BY_HANDOFF referenciando viejo)
    resolucion_a = [r for r in snap_post.resolutions if r[1] == clave_a]
    resolucion_b = [r for r in snap_post.resolutions if r[1] == clave_b]
    assert resolucion_a == [(tx_stale, clave_a, ArtifactResolutionKind.ABSORBED_BY_HANDOFF.value, h_viejo_a_id)]
    assert resolucion_b == [], "no debe existir resolución para B"

    # Oráculos: A queda resuelto (excluido) y B PRESERVA su evidencia
    assert tx_stale not in snap_post.oracle_a
    assert tx_stale in snap_post.oracle_b


# =============================================================================
# T506-03: CONSUMED / INDETERMINATE
# =============================================================================


async def test_t506_03_consumed_indeterminate_preserva_evidencia_de_b(journal_tmp) -> None:  # noqa: ANN001
    """T506-03: Materializar INDETERMINATE para A resuelve A pero preserva la evidencia de B."""
    journal, db_path = journal_tmp
    tmp_path = db_path.parent
    config, _runner = _entorno(tmp_path)

    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a = clave_de_artifact(art_a)
    clave_b = clave_de_artifact(art_b)

    _escribir_artifact_dir(art_a, b"TEXGEN_INDETERMINATE_DATA")
    _escribir_artifact_dir(art_b, b"DYNDOLOD_DATA")

    # 1. TX swept a ROLLED_BACK + UNRESOLVED nombrando A y B
    tx_stale = await _crear_tx_multi_artifact(journal, [art_a, art_b], descripcion="stale multi")
    await _envejecer_tx(db_path, tx_stale, 48)
    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)
    assert barridas == 1

    # Verificar PRE
    snap_pre = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )
    assert snap_pre.oracle_a == [tx_stale]
    assert snap_pre.oracle_b == [tx_stale]

    # 2. Reconciliar A con artifact PRESENT -> se crea INDETERMINATE
    resultado_a = await reconciliar_orphan_de_artifact(
        journal=journal,
        mod_texgen=art_a,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )
    assert isinstance(resultado_a, DeploymentHandoff)
    assert resultado_a.state is HandoffState.INDETERMINATE

    # 3. Snapshot POST
    snap_post = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )

    resolucion_a = [r for r in snap_post.resolutions if r[1] == clave_a]
    resolucion_b = [r for r in snap_post.resolutions if r[1] == clave_b]
    assert resolucion_a == [
        (tx_stale, clave_a, ArtifactResolutionKind.ABSORBED_BY_HANDOFF.value, resultado_a.handoff_id)
    ]
    assert resolucion_b == [], "no debe existir resolución para B"

    # Oráculos: A resuelto, B preservado
    assert tx_stale not in snap_post.oracle_a
    assert tx_stale in snap_post.oracle_b


# =============================================================================
# T506-04: NO_ARTIFACT
# =============================================================================


async def test_t506_04_no_artifact_absent_preserva_evidencia_de_b(journal_tmp) -> None:  # noqa: ANN001
    """T506-04: Ausencia demostrada de A resuelve A pero preserva la evidencia de B."""
    journal, db_path = journal_tmp
    tmp_path = db_path.parent
    config, _runner = _entorno(tmp_path)

    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a = clave_de_artifact(art_a)
    clave_b = clave_de_artifact(art_b)

    # A está PROBADAMENTE AUSENTE
    assert not art_a.exists()
    assert config.mo2_mods_path.is_dir()

    # B sí existe en disco
    _escribir_artifact_dir(art_b, b"DYNDOLOD_DATA")

    # 1. TX swept a ROLLED_BACK + UNRESOLVED nombrando A y B
    tx_stale = await _crear_tx_multi_artifact(journal, [art_a, art_b], descripcion="stale multi")
    await _envejecer_tx(db_path, tx_stale, 48)
    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)
    assert barridas == 1

    # Verificar PRE
    snap_pre = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )
    assert snap_pre.oracle_a == [tx_stale]
    assert snap_pre.oracle_b == [tx_stale]

    # 2. Reconciliar A -> reachability es ABSENT -> resuelve A como NO_ARTIFACT_DEMONSTRATED
    resultado_a = await reconciliar_orphan_de_artifact(
        journal=journal,
        mod_texgen=art_a,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )
    assert resultado_a is None

    # 3. Snapshot POST
    snap_post = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )

    resolucion_a = [r for r in snap_post.resolutions if r[1] == clave_a]
    resolucion_b = [r for r in snap_post.resolutions if r[1] == clave_b]
    assert resolucion_a == [(tx_stale, clave_a, ArtifactResolutionKind.NO_ARTIFACT_DEMONSTRATED.value, None)]
    assert resolucion_b == [], "no debe existir resolución para B"

    # Oráculos: A resuelto, B preservado
    assert tx_stale not in snap_post.oracle_a
    assert tx_stale in snap_post.oracle_b


# =============================================================================
# T506-05: RESUME CONTROL NEGATIVO
# =============================================================================


async def test_t506_05_resume_control_negativo(journal_tmp) -> None:  # noqa: ANN001
    """T506-05 (Control Negativo): Resume completado sobre A no destruye B."""
    journal, db_path = journal_tmp
    tmp_path = db_path.parent
    config, _runner = _entorno(tmp_path)

    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a = clave_de_artifact(art_a)
    clave_b = clave_de_artifact(art_b)

    _escribir_artifact_dir(art_a, b"TEXGEN_RESUME_DATA")
    _escribir_artifact_dir(art_b, b"DYNDOLOD_DATA")

    # 1. Handoff de A en AWAITING_DEPLOYMENT
    tx_a = await journal.begin_transaction("TexGen para resume", agent_id="test")
    reg_a = _crear_registro_awaiting(art_a, tx_a, config)
    h_a_id = await journal.crear_handoff_de_deployment(
        tx_a,
        descripcion="TexGen entregado",
        registro=reg_a,
        viejo=None,
    )

    # 2. TX swept a ROLLED_BACK + UNRESOLVED nombrando A y B (posterior al handoff)
    tx_stale = await _crear_tx_multi_artifact(journal, [art_a, art_b], descripcion="stale multi")
    await _envejecer_tx(db_path, tx_stale, 48)
    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)
    assert barridas == 1

    # Verificar PRE
    snap_pre = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )
    assert snap_pre.oracle_a == [tx_stale]
    assert snap_pre.oracle_b == [tx_stale]

    # 3. Completar Resume sobre A
    tx_resume = await journal.begin_transaction("resume execution", agent_id="test")
    await journal.completar_handoff_de_resume(tx_resume, h_a_id)

    # 4. Snapshot POST
    snap_post = await tomar_snapshot_evidencia(
        journal, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )

    # Resolución per-artifact registrada exactamente para A
    resolucion_a = [r for r in snap_post.resolutions if r[1] == clave_a]
    resolucion_b = [r for r in snap_post.resolutions if r[1] == clave_b]
    assert resolucion_a == [(tx_stale, clave_a, ArtifactResolutionKind.ABSORBED_BY_HANDOFF.value, h_a_id)]
    assert resolucion_b == []

    # Oráculos: A resuelto, B preservado
    assert tx_stale not in snap_post.oracle_a
    assert tx_stale in snap_post.oracle_b


# =============================================================================
# T506-06: PENDING CONTROL
# =============================================================================


async def test_t506_06_pending_control_retiene_semantica_actual(journal_tmp) -> None:  # noqa: ANN001
    """T506-06: Una TX PENDING multi-artifact conserva el aislamiento per-artifact."""
    journal, db_path = journal_tmp
    tmp_path = db_path.parent
    config, _runner = _entorno(tmp_path)

    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a = clave_de_artifact(art_a)
    clave_b = clave_de_artifact(art_b)

    _escribir_artifact_dir(art_a, b"TEXGEN_PENDING")
    _escribir_artifact_dir(art_b, b"DYNDOLOD_PENDING")

    # TX PENDING viva (sin envejecer, sin sweep)
    tx_pending = await _crear_tx_multi_artifact(journal, [art_a, art_b], descripcion="pending live")

    snap_pre = await tomar_snapshot_evidencia(
        journal, tx_id=tx_pending, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )
    assert snap_pre.transaction_status == TransactionStatus.PENDING.value
    assert snap_pre.receipt_state is None
    assert snap_pre.oracle_a == [tx_pending]
    assert snap_pre.oracle_b == [tx_pending]

    # Reconciliar A (PRESENT) -> INDETERMINATE para A + resolución (tx_pending, A)
    res_a = await reconciliar_orphan_de_artifact(
        journal=journal,
        mod_texgen=art_a,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )
    assert isinstance(res_a, DeploymentHandoff)

    snap_post = await tomar_snapshot_evidencia(
        journal, tx_id=tx_pending, clave_a=clave_a, clave_b=clave_b, db_path=db_path
    )

    assert snap_post.transaction_status == TransactionStatus.PENDING.value
    assert (
        tx_pending,
        clave_a,
        ArtifactResolutionKind.ABSORBED_BY_HANDOFF.value,
        res_a.handoff_id,
    ) in snap_post.resolutions
    assert tx_pending not in snap_post.oracle_a
    assert tx_pending in snap_post.oracle_b


# =============================================================================
# T506-07: RESTART Y DURABILIDAD TRAS RESOLUCIÓN PARCIAL
# =============================================================================


async def test_t506_07_restart_preserva_evidencia_de_b(journal_tmp) -> None:  # noqa: ANN001
    """T506-07: Tras resolver únicamente A, reiniciar el proceso preserva B."""
    journal, db_path = journal_tmp
    tmp_path = db_path.parent
    config, _runner = _entorno(tmp_path)

    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a = clave_de_artifact(art_a)
    clave_b = clave_de_artifact(art_b)

    _escribir_artifact_dir(art_a, b"TEXGEN_RESTART")
    _escribir_artifact_dir(art_b, b"DYNDOLOD_RESTART")

    # 1. TX swept a ROLLED_BACK + UNRESOLVED nombrando A y B
    tx_stale = await _crear_tx_multi_artifact(journal, [art_a, art_b], descripcion="stale restart")
    await _envejecer_tx(db_path, tx_stale, 48)
    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)
    assert barridas == 1

    # 2. Resolver únicamente A (vía RUN 1)
    tx_run1 = await journal.begin_transaction("RUN 1 TexGen", agent_id="test")
    reg_a = _crear_registro_awaiting(art_a, tx_run1, config)
    await journal.crear_handoff_de_deployment(
        tx_run1,
        descripcion="TexGen RUN 1",
        registro=reg_a,
        viejo=None,
    )

    # 3. Cerrar journal (simulando fin de sesión / reinicio)
    await journal.close()

    # 4. Reabrir DB con nueva instancia de OperationJournal
    j_restart = OperationJournal(db_path)
    await j_restart.open()
    try:
        snap_restart = await tomar_snapshot_evidencia(
            j_restart, tx_id=tx_stale, clave_a=clave_a, clave_b=clave_b, db_path=db_path
        )

        assert tx_stale not in snap_restart.oracle_a
        assert tx_stale in snap_restart.oracle_b
    finally:
        await j_restart.close()


# =============================================================================
# ANCLAS ESTRUCTURALES AST
# =============================================================================


def test_ast_writers_de_receipts_en_journal() -> None:
    """Ancla estructural: _escribir_receipts_de_sweep es el único escritor
    productivo de stale_pending_sweep_receipts en el contrato moderno.
    """
    j_path = _sky_claw_journal_path()
    writers = _funciones_que_mencionan_ast(j_path, "_INSERT_SWEEP_RECEIPT_SQL")
    assert writers == {"_escribir_receipts_de_sweep"}


def test_ast_writers_de_resoluciones_en_journal() -> None:
    """Ancla estructural: _registrar_resoluciones_de_artifact_en_conn es el
    único helper de escritura en artifact_evidence_resolutions.
    """
    j_path = _sky_claw_journal_path()
    writers = _funciones_que_mencionan_ast(j_path, "_INSERT_RESOLUCION_EVIDENCIA_SQL")
    assert writers == {"_registrar_resoluciones_de_artifact_en_conn"}


def test_ast_resume_no_obsoleta_receipts_globales() -> None:
    """Ancla estructural: _escribir_resume_completado NO invoca _obsoletar_receipts_de_artifact."""
    j_path = _sky_claw_journal_path()
    llamadores = _funciones_que_llaman_ast(j_path, "_obsoletar_receipts_de_artifact")
    assert "_escribir_resume_completado" not in llamadores
    assert "_escribir_handoff_de_deployment" not in llamadores
