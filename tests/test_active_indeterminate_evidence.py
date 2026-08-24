"""POST493_ACTIVE_INDETERMINATE_EVIDENCE_REUSE — absorción en el boundary de reemplazo.

Hallazgo confirmado conductualmente (triage post-#493): una regeneración fallida
durante un handoff activo deja una TX PENDING que nombra el artifact; el oracle
la ve, pero el reconciler retorna el owner activo ANTES de materializar la
evidencia (handoffs.py, short-circuit S1) y ningún boundary crea su absorción.
El reemplazo posterior (`_escribir_handoff_de_deployment`) sólo obsoleta
receipts UNRESOLVED — las PENDING quedan intactas — así que tras un ciclo
regen→deployment→resume 100% exitoso, la TX_FAIL histórica sigue siendo
evidencia vigente y el próximo resume/startup fabrica un INDETERMINATE falso
(`HandoffIndeterminate`, DynDOLOD no ejecuta).

Invariante nuevo (§3): cuando una generación autorizada reemplaza durablemente
un handoff activo, TODA evidencia histórica vigente PARA ESE ARTIFACT queda
absorbida EN EL MISMO boundary, referenciando `viejo.handoff_id`:

- NO modifica ``transactions.status`` (LIVE_SAFE_BY_CONSTRUCTION);
- NO deshabilita el stale sweep;
- es per-artifact: identidad ``(transaction_id, artifact_path)``;
- incluye TODAS las candidatas (no sólo source_tx_id);
- sobrevive SUPERSEDED/COMPLETED;
- no toca evidencia de otro artifact.

Fault injection único permitido: el proceso TexGen falla (rc=1) DESPUÉS de que
TX + ActionManifest existen — mismo patrón productivo de los tests de #493.
"""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.handoffs import (
    DeploymentHandoff,
    HandoffState,
    SweepReceiptState,
    clave_de_artifact,
    reconciliar_handoffs_de_deployment,
    reconciliar_orphan_de_artifact,
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
# Fixtures y helpers (espejo de test_pending_evidence_absorption.py)
# =============================================================================

# Los escenarios de este archivo abren journals por cuenta propia (ciclos de
# vida completos sobre la misma DB); el registro garantiza su cierre aunque un
# assert corte a mitad — los worker threads de aiosqlite son non-daemon y un
# sólo leak cuelga el fail-fast de fin de sesión.
_JOURNALS: list[OperationJournal] = []


@pytest.fixture(autouse=True)
async def _cerrar_journals_de_escenario():  # noqa: ANN201
    yield
    for j in list(_JOURNALS):
        try:
            await j.close()
        finally:
            _JOURNALS.remove(j)


def _registrar(j: OperationJournal) -> OperationJournal:
    _JOURNALS.append(j)
    return j


@pytest.fixture
async def journal_tmp(tmp_path: pathlib.Path):  # noqa: ANN201
    j = OperationJournal(tmp_path / "journal.db")
    await j.open()
    yield j, tmp_path / "journal.db"
    await j.close()


def _entorno(tmp_path: pathlib.Path) -> tuple[DynDOLODConfig, DynDOLODRunner]:
    game = tmp_path / "game"
    game.mkdir(parents=True, exist_ok=True)
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir(parents=True, exist_ok=True)
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
    """Fake de ``DynDOLODRunner._execute_process`` (patrón de los tests #493)."""

    def __init__(self, return_code: int = 0, al_ejecutar=None) -> None:  # noqa: ANN001
        self.return_code = return_code
        self.al_ejecutar = al_ejecutar

    async def __call__(self, *args: object, **kwargs: object):  # noqa: ANN002, ANN003, ANN202
        if self.al_ejecutar is not None:
            self.al_ejecutar()
        return "", "", self.return_code, 1.0


def _texgen_genera(config: DynDOLODConfig, marca: bytes):  # noqa: ANN202
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME

    def _escribir() -> None:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "a.dds").write_bytes(marca)
        (staging / "sub").mkdir(parents=True, exist_ok=True)
        (staging / "sub" / "b.dds").write_bytes(b"CURRENT-SUB")
        log_path = config.dyndolod_exe.parent / "Logs" / "TexGen_SSE_log.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "[00:00:01] Using Output Path: C:\\out\\\n[00:10:00] TexGen completed successfully\n",
            encoding="utf-8",
        )

    return _escribir


def _dyn_ok(staging: pathlib.Path):  # noqa: ANN202
    async def _ok(**kw: object) -> ToolExecutionResult:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "DynDOLOD.esp").write_bytes(b"esp")
        return ToolExecutionResult(True, "DynDOLOD", 0, "", "", output_path=staging)

    return AsyncMock(side_effect=_ok)


def _dyn_falla() -> AsyncMock:
    """DynDOLOD reporta fallo de herramienta (patrón T-D22 de #493). Con TexGen
    caído el pipeline sigue hasta acá (el gate de visibilidad sólo corta por
    despliegue); un resultado fallido mantiene el camino en el handler de
    dominio, que es el estado productivo que deja la TX PENDING."""

    async def _falla(**kw: object) -> ToolExecutionResult:
        return ToolExecutionResult(False, "DynDOLOD", 1, "", "", errors=["DynDOLOD roto"])

    return AsyncMock(side_effect=_falla)


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


async def _evidencia(journal: OperationJournal, artifacts: list[pathlib.Path], descripcion: str = "corrida") -> int:
    """TX PENDING cuyo ActionManifest REAL nombra los artifacts (vía productiva)."""
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


async def _reconciliar(journal: OperationJournal, config: DynDOLODConfig):  # noqa: ANN202
    return await reconciliar_orphan_de_artifact(
        journal=journal,
        mod_texgen=config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )


async def _absorciones(db: pathlib.Path) -> list[tuple[int, str, int]]:
    async with (
        aiosqlite.connect(str(db)) as conn,
        conn.execute(
            "SELECT transaction_id, artifact_path, handoff_id FROM orphan_evidence_absorptions "
            "ORDER BY transaction_id, artifact_path"
        ) as cur,
    ):
        return [(int(r[0]), str(r[1]), int(r[2])) for r in await cur.fetchall()]


async def _receipt(journal: OperationJournal, tx_id: int) -> str | None:
    async with journal._db.execute(  # noqa: SLF001
        "SELECT state FROM stale_pending_sweep_receipts WHERE transaction_id = ?",
        (tx_id,),
    ) as cur:
        fila = await cur.fetchone()
    return fila[0] if fila else None


async def _estado_tx(journal: OperationJournal, tx_id: int) -> str | None:
    t = await journal.get_transaction(tx_id)
    return None if t is None else t.status.value


async def _ultima_tx_de_pipeline(journal: OperationJournal, *, solo_pending: bool = False) -> int:
    corridas = [
        t
        for t in await journal.list_recent_transactions(limit=100)
        if t.description == "DynDOLOD pipeline (preset=Medium, texgen=True)"
        and (t.status is TransactionStatus.PENDING if solo_pending else True)
    ]
    assert corridas, "no hay corrida del pipeline registrada"
    return max(t.transaction_id for t in corridas)


async def _max_tx_id(journal: OperationJournal) -> int:
    """Mayor transaction_id sin filtrar por descripción.

    El boundary de reemplazo ESTRECHA la descripción de la TX exitosa ("…
    esperando deployment"), así que un filtro por descripción devolvería la
    TX_FAIL anterior — exactamente el falso tx_ok que hace parecer
    self-absorption.
    """
    corridas = await journal.list_recent_transactions(limit=100)
    assert corridas, "no hay transacciones"
    return max(t.transaction_id for t in corridas)


async def _regen_fallida(journal: OperationJournal, runner: DynDOLODRunner) -> int:
    """run_texgen=True que falla DESPUÉS de TX+ActionManifest (fault injection
    única: proceso TexGen rc=1). Devuelve el id de la TX_FAIL quedada PENDING."""
    svc = _svc(journal, runner)
    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(return_code=1)),
        patch.object(runner, "run_dyndolod", _dyn_falla()),
    ):
        res = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)
    assert res["success"] is False
    assert res.get("needs_deployment") is not True
    return await _ultima_tx_de_pipeline(journal, solo_pending=True)


async def _regen_exitosa_needs_deployment(
    journal: OperationJournal, runner: DynDOLODRunner, config: DynDOLODConfig
) -> int:
    svc = _svc(journal, runner)
    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_texgen_genera(config, b"GEN-OK"))),
        patch.object(runner, "run_dyndolod", AsyncMock()),
    ):
        res = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)
    assert res.get("needs_deployment") is True, res
    return await _max_tx_id(journal)


async def _resume_exitoso(journal: OperationJournal, runner: DynDOLODRunner, config: DynDOLODConfig) -> dict:
    _mirror_a_data(config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME / "textures", config.data_dir)
    dyndolod_staging = config.output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME
    svc = _svc(journal, runner)
    run_dyndolod = _dyn_ok(dyndolod_staging)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        res = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)
    return {"resultado": res, "run_dyndolod": run_dyndolod}


async def _lifecycle_completo(tmp_path: pathlib.Path, *, fallos_previos: int = 1) -> dict:
    """H1 INDETERMINATE → N regens fallidas → regen exitosa → deployment+resume.
    Deja el journal ABIERTO y devuelve el contexto."""
    db_path = tmp_path / "journal.db"
    config, runner = _entorno(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(mod_texgen)

    journal = _registrar(OperationJournal(db_path))
    await journal.open()

    # H1 productivamente: evidencia real + primitiva del reconciler.
    _escribir_mod(mod_texgen, contenido=b"H1-EVIDENCE")
    tx_init = await _evidencia(journal, [mod_texgen], "H1-TX-inicial")
    await _reconciliar(journal, config)
    h1 = await journal.consultar_handoff_activo(clave)
    assert h1 is not None and h1.state is HandoffState.INDETERMINATE

    tx_fails = [await _regen_fallida(journal, runner) for _ in range(fallos_previos)]

    tx_ok = await _regen_exitosa_needs_deployment(journal, runner, config)
    h2 = await journal.consultar_handoff_activo(clave)
    assert h2 is not None and h2.state is HandoffState.AWAITING_DEPLOYMENT

    resume = await _resume_exitoso(journal, runner, config)
    assert resume["resultado"]["success"] is True, resume["resultado"]
    assert await journal.consultar_handoff_activo(clave) is None, "H2 debió quedar COMPLETED"

    return {
        "db_path": db_path,
        "config": config,
        "runner": runner,
        "journal": journal,
        "clave": clave,
        "h1": h1,
        "tx_init": tx_init,
        "tx_fails": tx_fails,
        "tx_ok": tx_ok,
    }


# =============================================================================
# RED principal (§4) — el finding completo end-to-end
# =============================================================================


async def test_regen_fallida_durante_indeterminate_no_reaparece_tras_reemplazo_exitoso(
    tmp_path: pathlib.Path,
) -> None:
    """Secuencia completa del finding: regen fallida durante H1 INDETERMINATE,
    reemplazo exitoso, deployment+resume OK → la TX_FAIL histórica NO vuelve a
    fabricar un INDETERMINATE en el segundo resume."""
    ctx = await _lifecycle_completo(tmp_path, fallos_previos=1)
    journal, clave, runner = ctx["journal"], ctx["clave"], ctx["runner"]
    tx_fail = ctx["tx_fails"][0]

    # Estado intermedio congelado: TX_FAIL vive como PENDING sin absorción
    # mientras H1 era el owner (el lifecycle no toca statuses ajenos).
    assert await _estado_tx(journal, tx_fail) == TransactionStatus.PENDING.value, (
        "absorber evidencia jamás debe tocar el lifecycle de la TX"
    )

    # §4.8: el oracle ya NO devuelve la TX_FAIL histórica para este artifact.
    candidatas = await journal.transacciones_que_nombran(clave)
    assert tx_fail not in candidatas, (
        f"POST493_ACTIVE_INDETERMINATE_EVIDENCE_REUSE: TX_FAIL {tx_fail} sigue siendo "
        f"evidencia tras un reemplazo exitoso — oracle={candidatas}"
    )

    # §4.9: el segundo resume NO puede fallar por HandoffIndeterminate.
    run_dyndolod = _dyn_ok(ctx["config"].output_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME)
    svc = _svc(journal, runner)
    with patch.object(runner, "run_dyndolod", run_dyndolod):
        res = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    assert res.get("reason") != "HandoffIndeterminate", res
    assert res["success"] is True, res
    run_dyndolod.assert_awaited_once()


async def test_la_absorcion_del_reemplazo_referencia_el_viejo_handoff(tmp_path: pathlib.Path) -> None:
    """§9 provenance: absorption(TX_FAIL, artifact).handoff_id == viejo.handoff_id;
    la fila sobrevive a H1 SUPERSEDED y H2 COMPLETED (FK sin ON DELETE)."""
    ctx = await _lifecycle_completo(tmp_path, fallos_previos=1)
    journal, db_path, clave = ctx["journal"], ctx["db_path"], ctx["clave"]
    tx_fail = ctx["tx_fails"][0]

    filas = [a for a in await _absorciones(db_path) if a[0] == tx_fail]
    assert filas, "la TX_FAIL histórica quedó sin absorción en el reemplazo"
    assert filas[0][1] == clave
    assert filas[0][2] == ctx["h1"].handoff_id, (
        "la absorción debe referenciar al viejo INDETERMINATE (dueño del recovery context "
        "cuando la evidencia se acumuló), no al AWAITING/COMPLETED que lo reemplazó"
    )
    # La fila permanece después de que ambos handoffs sean historia terminal.
    async with journal._db.execute(  # noqa: SLF001
        "SELECT state FROM deployment_handoffs WHERE handoff_id = ?", (ctx["h1"].handoff_id,)
    ) as cur:
        fila_h1 = await cur.fetchone()
    assert fila_h1 is not None and fila_h1[0] == HandoffState.SUPERSEDED.value


# =============================================================================
# Startup (§5) — A fresh / B stale
# =============================================================================


async def test_startup_no_refabrica_desde_tx_fail_fresh_tras_lifecycle(tmp_path: pathlib.Path) -> None:
    """§5A: lifecycle exitoso → cerrar → reabrir → startup reconciliation NO
    fabrica INDETERMINATE desde la TX_FAIL histórica (todavía PENDING/fresh)."""
    tmp_startup = tmp_path / "startup-fresh"
    tmp_startup.mkdir()
    ctx = await _lifecycle_completo(tmp_startup, fallos_previos=1)
    cfg, clave = ctx["config"], ctx["clave"]
    tx_fail = ctx["tx_fails"][0]
    await ctx["journal"].close()

    j2 = _registrar(OperationJournal(ctx["db_path"]))
    await j2.open()  # sweep de arranque: TX_FAIL fresca no debe ser barrida
    try:
        assert tx_fail not in j2.swept_pending_transaction_ids_this_open
        await reconciliar_handoffs_de_deployment(
            journal=j2,
            mod_texgen=cfg.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME,
            game_key=clave_de_artifact(cfg.game_path),
            mods_root_key=clave_de_artifact(cfg.mo2_mods_path),
            data_key=clave_de_artifact(cfg.data_dir),
            expected_profile="Perfil-A",
            digest_arbol=digest_arbol,
        )
        activo = await j2.consultar_handoff_activo(clave)
        assert activo is None, f"STARTUP fabricó INDETERMINATE falso desde TX_FAIL histórica: {activo}"
    finally:
        await j2.close()


async def test_startup_no_refabrica_con_tx_fail_stale_rolled_back(tmp_path: pathlib.Path) -> None:
    """§5B: igual que A pero la TX_FAIL pasó stale sweep DESPUÉS del lifecycle
    (ROLLED_BACK + receipt UNRESOLVED): la absorción existente sigue excluyéndola."""
    tmp_stale = tmp_path / "startup-stale"
    tmp_stale.mkdir()
    ctx = await _lifecycle_completo(tmp_stale, fallos_previos=1)
    cfg, clave = ctx["config"], ctx["clave"]
    tx_fail = ctx["tx_fails"][0]
    await ctx["journal"].close()

    async with aiosqlite.connect(str(ctx["db_path"])) as conn:
        await conn.execute(
            "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
            (tx_fail,),
        )
        await conn.commit()

    j2 = _registrar(OperationJournal(ctx["db_path"]))
    await j2.open()  # el sweep convierte la TXFAIL vieja → ROLLED_BACK + receipt UNRESOLVED
    try:
        assert tx_fail in j2.swept_pending_transaction_ids_this_open
        assert await _estado_tx(j2, tx_fail) == TransactionStatus.ROLLED_BACK.value
        assert await _receipt(j2, tx_fail) == SweepReceiptState.UNRESOLVED.value

        await reconciliar_handoffs_de_deployment(
            journal=j2,
            mod_texgen=cfg.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME,
            game_key=clave_de_artifact(cfg.game_path),
            mods_root_key=clave_de_artifact(cfg.mo2_mods_path),
            data_key=clave_de_artifact(cfg.data_dir),
            expected_profile="Perfil-A",
            digest_arbol=digest_arbol,
        )
        assert await j2.consultar_handoff_activo(clave) is None, (
            "la absorción hecha en el reemplazo debió excluir la evidencia también en su forma ROLLED_BACK+UNRESOLVED"
        )
        assert await _receipt(j2, tx_fail) == SweepReceiptState.UNRESOLVED.value, (
            "el fix no debe cerrar receipts por fuera del contrato (SUPERSEDED lo hace "
            "el boundary de reemplazo; acá la evidencia ya estaba absorbida)"
        )
    finally:
        await j2.close()


# =============================================================================
# Múltiples regens fallidas (§6) y multi-artifact (§7)
# =============================================================================


async def test_dos_regens_fallidas_quedan_absorbidas_en_el_reemplazo(tmp_path: pathlib.Path) -> None:
    """§6: ambas TX_FAIL reciben absorption(TX_FAIL_n, artifact, viejo) y el
    oracle no devuelve ninguna. Una solución basada sólo en source_tx_id es
    insuficiente."""
    ctx = await _lifecycle_completo(tmp_path, fallos_previos=2)
    journal, db_path, clave = ctx["journal"], ctx["db_path"], ctx["clave"]
    f1, f2 = ctx["tx_fails"]

    filas = {(a[0], a[2]) for a in await _absorciones(db_path) if a[1] == clave}
    assert (f1, ctx["h1"].handoff_id) in filas, f"TX_FAIL_1={f1} sin absorción"
    assert (f2, ctx["h1"].handoff_id) in filas, f"TX_FAIL_2={f2} sin absorción"

    candidatas = await journal.transacciones_que_nombran(clave)
    assert f1 not in candidatas and f2 not in candidatas, candidatas


async def test_la_absorcion_del_reemplazo_es_per_artifact(tmp_path: pathlib.Path) -> None:
    """§7: la TX_FAIL nombra A (TexGen Output) y B (DynDOLOD Output); el
    reemplazo del handoff de A absorbe SOLO (TX_FAIL, A): oracle(A) la excluye y
    oracle(B) sigue pudiendo devolverla. Prohibida la absorción global por tx."""
    ctx = await _lifecycle_completo(tmp_path, fallos_previos=1)
    journal, db_path, clave_a = ctx["journal"], ctx["db_path"], ctx["clave"]
    cfg = ctx["config"]
    tx_fail = ctx["tx_fails"][0]
    clave_b = clave_de_artifact(cfg.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME)

    # Precondición del escenario: el manifest productivo nombró ambos artifacts.
    assert tx_fail in await journal.transacciones_que_nombran(clave_b), (
        "el escenario requiere una TX_FAIL cuyo manifest nombre también el artifact B"
    )

    absorbidas_a = {a[0] for a in await _absorciones(db_path) if a[1] == clave_a}
    assert tx_fail in absorbidas_a

    assert tx_fail not in await journal.transacciones_que_nombran(clave_a), "oracle(A) debe excluirla"
    assert tx_fail in await journal.transacciones_que_nombran(clave_b), (
        "absorber A NO debe silenciar la evidencia legítima de B (prohibido absorber por tx global)"
    )


# =============================================================================
# Self-absorption (§11) y live-safety (M-F6 guard)
# =============================================================================


async def test_current_generation_no_recibe_absorption(tmp_path: pathlib.Path) -> None:
    """§11: la TX_OK del reemplazo jamás recibe absorption por el artifact que
    acaba de producir (CURRENT_GENERATION_SELF_ABSORPTION = blocker)."""
    ctx = await _lifecycle_completo(tmp_path, fallos_previos=1)
    db_path, clave = ctx["db_path"], ctx["clave"]
    tx_ok = ctx["tx_ok"]

    absorbidas = {a[0] for a in await _absorciones(db_path) if a[1] == clave}
    assert tx_ok not in absorbidas, "la generación actual fue absorbida por sí misma"


async def test_replacement_no_convierte_pending_en_rolled_back(tmp_path: pathlib.Path) -> None:
    """§12 LIVE_SAFE_BY_CONSTRUCTION: el boundary de reemplazo consume EVIDENCIA,
    nunca lifecycle. La TX_FAIL absorbida sigue PENDING y elegible para el stale
    sweep futuro."""
    ctx = await _lifecycle_completo(tmp_path, fallos_previos=1)
    journal = ctx["journal"]
    tx_fail = ctx["tx_fails"][0]
    assert await _estado_tx(journal, tx_fail) == TransactionStatus.PENDING.value
    assert await _receipt(journal, tx_fail) is None, "una PENDING fresca jamás recibe receipt en el reemplazo"


# =============================================================================
# §21 — el mecanismo es del OWNER ACTIVO, no sólo de INDETERMINATE
# =============================================================================


async def test_regens_fallidas_durante_awaiting_tambien_quedan_absorbidas(tmp_path: pathlib.Path) -> None:
    """Razonamiento de máquina de estados (§21): el short-circuit S1 del
    reconciler retorna el owner activo ANTES de materializar evidencia sea
    cual sea su estado — AWAITING_DEPLOYMENT también es un recovery context
    donde la evidencia se acumula sin absorción. El reemplazo de un AWAITING
    debe absorber igual (mismo invariant, cero casos especiales)."""
    tmp_awaiting = tmp_path / "awaiting-viejo"
    tmp_awaiting.mkdir()
    db_path = tmp_awaiting / "journal.db"
    config, runner = _entorno(tmp_awaiting)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(mod_texgen)

    journal = _registrar(OperationJournal(db_path))
    await journal.open()

    # H1 = AWAITING_DEPLOYMENT legítimo (receta T-D11: TexGen entregado).
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    tx1 = await journal.begin_transaction("texgen entregado", agent_id="test")
    d1 = digest_arbol(mod_texgen / "textures")
    registro = DeploymentHandoff(
        handoff_id=0,
        source_tx_id=tx1,
        state=HandoffState.AWAITING_DEPLOYMENT,
        artifact_path=clave,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        expected_digest=d1.digest,
        expected_files=d1.files,
        expected_bytes=d1.bytes,
        observed_digest=None,
        observed_files=None,
        observed_bytes=None,
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )
    h1_id = await journal.crear_handoff_de_deployment(tx1, descripcion=None, registro=registro, viejo=None)

    # Regen fallida durante AWAITING: clase A devuelve el handoff a AWAITING y
    # la TX_FAIL queda PENDING (mutation_started + cobertura incompleta).
    tx_fail = await _regen_fallida(journal, runner)
    activo = await journal.consultar_handoff_activo(clave)
    assert activo is not None and activo.handoff_id == h1_id
    assert activo.state is HandoffState.AWAITING_DEPLOYMENT

    # Reemplazo exitoso del AWAITING: debe absorber la evidencia acumulada.
    tx_ok = await _regen_exitosa_needs_deployment(journal, runner, config)
    h2 = await journal.consultar_handoff_activo(clave)
    assert h2 is not None and h2.state is HandoffState.AWAITING_DEPLOYMENT and h2.handoff_id != h1_id

    filas = {(a[0], a[2]) for a in await _absorciones(db_path) if a[1] == clave}
    assert (tx_fail, h1_id) in filas, "el reemplazo de un AWAITING dejó la TX_FAIL sin absorber"
    assert tx_ok not in {a[0] for a in filas}, "CURRENT_GENERATION_SELF_ABSORPTION"
    assert tx_fail not in await journal.transacciones_que_nombran(clave)
    assert await _estado_tx(journal, tx_fail) == TransactionStatus.PENDING.value


async def test_resume_que_completa_el_handoff_tambien_absorbe_la_evidencia(tmp_path: pathlib.Path) -> None:
    """HERMANO del boundary de reemplazo (AGENTS.md, defecto dominante del repo).

    La propiedad autorizada de un artifact se extingue por DOS caminos, no uno:
    reemplazándola (supersede) y CONSUMIÉNDOLA (resume completado). El
    short-circuit S1 del reconciler acumula evidencia sin absorber mientras el
    owner está activo, así que si el resume no sella lo acumulado, el siguiente
    startup ya no encuentra owner, relee la TX_FAIL y fabrica el INDETERMINATE
    falso que bloquea DynDOLOD sin una corrida nueva.

    Sin regen exitosa intermedia: el usuario despliega lo que ya tenía y
    retoma. Es el camino que el fix de reemplazo NO cubre.
    """
    tmp_resume = tmp_path / "resume-sin-reemplazo"
    tmp_resume.mkdir()
    db_path = tmp_resume / "journal.db"
    config, runner = _entorno(tmp_resume)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(mod_texgen)

    journal = _registrar(OperationJournal(db_path))
    await journal.open()

    # H1 = AWAITING_DEPLOYMENT legítimo (misma receta T-D11 que el hermano).
    _escribir_mod(mod_texgen, contenido=b"GEN-1")
    tx1 = await journal.begin_transaction("texgen entregado", agent_id="test")
    d1 = digest_arbol(mod_texgen / "textures")
    registro = DeploymentHandoff(
        handoff_id=0,
        source_tx_id=tx1,
        state=HandoffState.AWAITING_DEPLOYMENT,
        artifact_path=clave,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        expected_digest=d1.digest,
        expected_files=d1.files,
        expected_bytes=d1.bytes,
        observed_digest=None,
        observed_files=None,
        observed_bytes=None,
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )
    h1_id = await journal.crear_handoff_de_deployment(tx1, descripcion=None, registro=registro, viejo=None)

    # Regen fallida bajo el owner activo: TX_FAIL queda PENDING sin absorber.
    tx_fail = await _regen_fallida(journal, runner)
    assert tx_fail in await journal.transacciones_que_nombran(clave), (
        "precondición: la TX_FAIL es evidencia vigente mientras el owner está activo"
    )

    # Resume SIN regen exitosa previa: consume el handoff (COMPLETED).
    resume = await _resume_exitoso(journal, runner, config)
    assert resume["resultado"]["success"] is True, resume["resultado"]
    assert await journal.consultar_handoff_activo(clave) is None, "H1 debió quedar COMPLETED"

    # El sello ocurrió en ESE boundary, con la provenance del handoff consumido.
    filas = {(a[0], a[2]) for a in await _absorciones(db_path) if a[1] == clave}
    assert (tx_fail, h1_id) in filas, "el resume completado dejó la TX_FAIL sin absorber"
    assert tx_fail not in await journal.transacciones_que_nombran(clave)
    # La absorción consume EVIDENCIA, nunca lifecycle.
    assert await _estado_tx(journal, tx_fail) == TransactionStatus.PENDING.value

    # El bug visible: el startup siguiente no debe fabricar INDETERMINATE.
    assert await _reconciliar(journal, config) is None, (
        "startup re-fabricó un INDETERMINATE falso desde evidencia ya consumida por el resume"
    )
    assert await journal.consultar_handoff_activo(clave) is None


async def test_resume_no_silencia_la_evidencia_de_otro_artifact(tmp_path: pathlib.Path) -> None:
    """REGRESIÓN (hallazgo del revisor adversarial sobre este mismo PR).

    El receipt de stale sweep es UNA FILA POR TX; la absorción, per-artifact.
    Si el boundary del resume obsoletara receipts, sellar el artifact A dejaría
    a la TX barrida sin poder contar como evidencia de B —que nadie absorbió—
    porque el oracle exige receipt UNRESOLVED para admitir una ROLLED_BACK.

    Orden deliberado: la TX_FAIL se barre DESPUÉS de crear el handoff, así el
    único boundary que corre tras el sweep es el resume. Con la obsoletización
    dentro del sello, este test veía `oracle(B) == []`; en main, `[tx_fail]`.
    """
    tmp_multi = tmp_path / "resume-multi-artifact"
    tmp_multi.mkdir()
    db_path = tmp_multi / "journal.db"
    config, runner = _entorno(tmp_multi)
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    art_a = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    art_b = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME
    clave_a, clave_b = clave_de_artifact(art_a), clave_de_artifact(art_b)

    journal = _registrar(OperationJournal(db_path))
    await journal.open()
    _escribir_mod(art_a, contenido=b"GEN-1")
    tx_h = await journal.begin_transaction("texgen entregado", agent_id="test")
    d1 = digest_arbol(art_a / "textures")
    registro = DeploymentHandoff(
        handoff_id=0,
        source_tx_id=tx_h,
        state=HandoffState.AWAITING_DEPLOYMENT,
        artifact_path=clave_a,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        expected_digest=d1.digest,
        expected_files=d1.files,
        expected_bytes=d1.bytes,
        observed_digest=None,
        observed_files=None,
        observed_bytes=None,
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )
    h_id = await journal.crear_handoff_de_deployment(tx_h, descripcion=None, registro=registro, viejo=None)
    # La TX_FAIL nace DESPUÉS del handoff: su receipt no pasa por el boundary
    # de creación, sólo puede tocarlo el resume.
    tx_fail = await _evidencia(journal, [art_a, art_b], "fallida-multi-artifact")
    await journal.close()

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
            (tx_fail,),
        )
        await conn.commit()

    j2 = _registrar(OperationJournal(db_path))
    await j2.open()  # el sweep la deja ROLLED_BACK + receipt UNRESOLVED
    try:
        assert await _receipt(j2, tx_fail) == SweepReceiptState.UNRESOLVED.value
        assert tx_fail in await j2.transacciones_que_nombran(clave_a)
        assert tx_fail in await j2.transacciones_que_nombran(clave_b), (
            "precondición: la TX barrida es evidencia de AMBOS artifacts"
        )

        tx_resume = await j2.begin_transaction("resume", agent_id="test")
        await j2.completar_handoff_de_resume(tx_resume, h_id)

        assert tx_fail not in await j2.transacciones_que_nombran(clave_a), (
            "el resume debe absorber la evidencia del artifact que sella"
        )
        assert tx_fail in await j2.transacciones_que_nombran(clave_b), (
            "sellar A NO puede silenciar la evidencia de B: el receipt es global por TX, la absorción es per-artifact"
        )
        assert await _receipt(j2, tx_fail) == SweepReceiptState.UNRESOLVED.value, (
            "el resume no emite provenance autorizada nueva: no obsoleta receipts (F-001)"
        )
    finally:
        await j2.close()


# =============================================================================
# Fault injection — all-or-nothing del boundary extendido (§16–§19)
# =============================================================================


async def _sembrar_escenario_para_faults(tmp_path: pathlib.Path) -> dict:
    """AWAITING H1 + TX_FAIL PENDING nombrando el artifact (evidencia a absorber)."""
    db_path = tmp_path / "journal.db"
    config, runner = _entorno(tmp_path)
    assert config.data_dir is not None and config.output_root is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(mod_texgen)

    journal = _registrar(OperationJournal(db_path))
    await journal.open()
    _escribir_mod(mod_texgen)
    tx1 = await journal.begin_transaction("texgen entregado", agent_id="test")
    d1 = digest_arbol(mod_texgen / "textures")
    registro = DeploymentHandoff(
        handoff_id=0,
        source_tx_id=tx1,
        state=HandoffState.AWAITING_DEPLOYMENT,
        artifact_path=clave,
        game_key=clave_de_artifact(config.game_path),
        mods_root_key=clave_de_artifact(config.mo2_mods_path),
        data_key=clave_de_artifact(config.data_dir),
        expected_profile="Perfil-A",
        expected_digest=d1.digest,
        expected_files=d1.files,
        expected_bytes=d1.bytes,
        observed_digest=None,
        observed_files=None,
        observed_bytes=None,
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )
    h1_id = await journal.crear_handoff_de_deployment(tx1, descripcion=None, registro=registro, viejo=None)
    h1 = await journal.consultar_handoff_activo(clave)
    assert h1 is not None and h1.handoff_id == h1_id
    tx_fail = await _evidencia(journal, [mod_texgen], "TX_FAIL-historica")
    return {
        "db_path": db_path,
        "config": config,
        "journal": journal,
        "clave": clave,
        "h1_id": h1_id,
        "h1": h1,
        "tx1": tx1,
        "tx_fail": tx_fail,
        "registro": registro,
    }


async def _estado_handoff(journal: OperationJournal, handoff_id: int) -> tuple[str, int | None]:
    async with journal._db.execute(  # noqa: SLF001
        "SELECT state, superseded_by FROM deployment_handoffs WHERE handoff_id = ?", (handoff_id,)
    ) as cur:
        fila = await cur.fetchone()
    assert fila is not None
    return str(fila[0]), (int(fila[1]) if fila[1] is not None else None)


async def test_old_handoff_pierde_ownership_antes_del_supersede_todo_o_nada(tmp_path: pathlib.Path) -> None:
    """§16 (C4): si el viejo ya no está activo cuando corre el guard del
    supersede, TODO el boundary revierte: ni handoff nuevo, ni absorciones, ni
    receipts tocados, ni TX actual committeada."""
    esc = await _sembrar_escenario_para_faults(tmp_path / "race")
    journal, clave = esc["journal"], esc["clave"]

    # Pérdida de ownership externa entre la lectura del caller y el boundary.
    assert await journal.transicionar_handoff(
        esc["h1_id"], desde=HandoffState.AWAITING_DEPLOYMENT, hacia=HandoffState.SUPERSEDED
    )

    tx2 = await journal.begin_transaction("reemplazo", agent_id="test")
    with pytest.raises(JournalTransactionError):
        await journal.crear_handoff_de_deployment(tx2, descripcion=None, registro=esc["registro"], viejo=esc["h1"])

    estado, superseded_by = await _estado_handoff(journal, esc["h1_id"])
    assert estado == HandoffState.SUPERSEDED.value and superseded_by is None, (
        "el handoff viejo no debe ganar un supersede_by del boundary fallido"
    )
    assert await _estado_tx(journal, tx2) == TransactionStatus.PENDING.value, "la TX actual quedó COMMITTED"
    assert await journal.consultar_handoff_activo(clave) is None, (
        "el owner externo pasó a SUPERSEDED: no puede quedar un handoff activo del artifact"
    )
    assert await _absorciones(esc["db_path"]) == [], "quedaron absorciones de un boundary revertido"
    assert len(await journal.list_recent_transactions(limit=10)) >= 3  # sanidad: la DB sigue viva


async def test_fallo_del_insert_del_nuevo_handoff_revierte_todo(tmp_path: pathlib.Path) -> None:
    """§17 (C1/C2): fallo después del supersede UPDATE y antes/durante el INSERT
    → rollback completo; el viejo conserva su estado previo y la TX no se
    commitea."""
    esc = await _sembrar_escenario_para_faults(tmp_path / "insert-fail")
    journal = esc["journal"]
    tx_fail = esc["tx_fail"]

    import sky_claw.app.db.journal as journal_mod

    original = journal_mod.fila_de_registro

    def _fila_rota(registro: DeploymentHandoff) -> tuple[object, ...]:
        raise sqlite3.OperationalError("insert boom inyectado")

    tx2 = await journal.begin_transaction("reemplazo", agent_id="test")
    with (
        patch.object(journal_mod, "fila_de_registro", side_effect=_fila_rota),
        pytest.raises(JournalTransactionError),
    ):
        await journal.crear_handoff_de_deployment(tx2, descripcion=None, registro=esc["registro"], viejo=esc["h1"])
    assert journal_mod.fila_de_registro is original

    estado, _ = await _estado_handoff(journal, esc["h1_id"])
    assert estado == HandoffState.AWAITING_DEPLOYMENT.value, "el viejo fue superseded sin handoff durable detrás"
    assert await _estado_tx(journal, tx2) == TransactionStatus.PENDING.value
    assert await _estado_tx(journal, tx_fail) == TransactionStatus.PENDING.value
    assert await _absorciones(esc["db_path"]) == []
    assert await _receipt(journal, tx_fail) is None


async def test_fallo_insertando_absorpcion_revierte_todo(tmp_path: pathlib.Path) -> None:
    """§18 (C2/C3): fallo DURANTE el lote de absorciones (tras el INSERT del
    nuevo handoff) → rollback total: viejo intacto, nuevo ausente, cero
    absorciones parciales, receipts intactos, TX no committeada."""
    esc = await _sembrar_escenario_para_faults(tmp_path / "absorption-fail")
    journal, clave = esc["journal"], esc["clave"]
    tx_fail = esc["tx_fail"]

    import sky_claw.app.db.journal as journal_mod

    tx2 = await journal.begin_transaction("reemplazo", agent_id="test")
    sql_roto = "INSERT INTO tabla_inexistente_por_fault_injection (a) VALUES (?)"
    with (
        patch.object(journal_mod, "_INSERT_ABSORCION_EVIDENCIA_SQL", sql_roto),
        pytest.raises(JournalTransactionError),
    ):
        await journal.crear_handoff_de_deployment(tx2, descripcion=None, registro=esc["registro"], viejo=esc["h1"])

    estado, superseded_by = await _estado_handoff(journal, esc["h1_id"])
    assert estado == HandoffState.AWAITING_DEPLOYMENT.value
    assert superseded_by is None
    assert await journal.consultar_handoff_activo(clave) is not None and (
        (await journal.consultar_handoff_activo(clave)).handoff_id == esc["h1_id"]  # type: ignore[union-attr]
    )
    assert await _estado_tx(journal, tx2) == TransactionStatus.PENDING.value
    assert await _absorciones(esc["db_path"]) == [], "quedó una absorción parcial"
    assert await _receipt(journal, tx_fail) is None


async def test_fallo_de_commit_no_deja_cambios_y_es_reintentable(tmp_path: pathlib.Path) -> None:
    """§19 (C3): commit roto → cero cambios durables del replacement; un retry
    limpio completa el reemplazo CON sus absorciones (idempotencia operativa)."""
    esc = await _sembrar_escenario_para_faults(tmp_path / "commit-fail")
    journal, db_path, cfg = esc["journal"], esc["db_path"], esc["config"]

    async def _commit_roto() -> None:
        raise sqlite3.OperationalError("commit roto inyectado")

    tx2 = await journal.begin_transaction("reemplazo", agent_id="test")
    with (
        patch.object(journal._db, "commit", _commit_roto),  # noqa: SLF001
        pytest.raises(JournalTransactionError),
    ):
        await journal.crear_handoff_de_deployment(tx2, descripcion=None, registro=esc["registro"], viejo=esc["h1"])

    # Post-fallo: nada afirmado en esta conexión…
    estado, _ = await _estado_handoff(journal, esc["h1_id"])
    assert estado == HandoffState.AWAITING_DEPLOYMENT.value
    # …ni en disco (otra instancia ve el MISMO estado previo).
    await journal.close()
    j2 = _registrar(OperationJournal(db_path))
    await j2.open()
    try:
        estado_disco, _ = await _estado_handoff(j2, esc["h1_id"])
        assert estado_disco == HandoffState.AWAITING_DEPLOYMENT.value
        assert await _estado_tx(j2, tx2) == TransactionStatus.PENDING.value
        assert await _absorciones(db_path) == []

        # Retry limpio: reemplazo completo + absorción de la evidencia histórica.
        d_nuevo = digest_arbol(cfg.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME / "textures")
        registro_nuevo = DeploymentHandoff(
            handoff_id=0,
            source_tx_id=tx2,
            state=HandoffState.AWAITING_DEPLOYMENT,
            artifact_path=esc["clave"],
            game_key=clave_de_artifact(cfg.game_path),
            mods_root_key=clave_de_artifact(cfg.mo2_mods_path),
            data_key=clave_de_artifact(cfg.data_dir),
            expected_profile="Perfil-A",
            expected_digest=d_nuevo.digest,
            expected_files=d_nuevo.files,
            expected_bytes=d_nuevo.bytes,
            observed_digest=None,
            observed_files=None,
            observed_bytes=None,
            created_at="",
            updated_at="",
            completed_at=None,
            superseded_at=None,
            superseded_by=None,
        )
        nuevo_id = await j2.crear_handoff_de_deployment(tx2, descripcion=None, registro=registro_nuevo, viejo=esc["h1"])
        assert nuevo_id != esc["h1_id"]
        filas = {(a[0], a[2]) for a in await _absorciones(db_path)}
        assert (esc["tx_fail"], esc["h1_id"]) in filas, "el retry no absorbió la evidencia histórica"
        assert esc["tx_fail"] not in await j2.transacciones_que_nombran(esc["clave"])
    finally:
        await j2.close()


# =============================================================================
# §13/§22 — receipt ROLLED_BACK+UNRESOLVD presente AL MOMENTO del reemplazo
# =============================================================================


async def test_reemplazo_con_receipt_unresolved_lo_marca_superseded(tmp_path: pathlib.Path) -> None:
    """Si la evidencia ya pasó stale sweep ANTES de la regeneración exitosa
    (ROLLED_BACK + UNRESOLVED), el reemplazo la absorbe Y marca su receipt
    SUPERSEDED en el MISMO boundary — nunca CONSUMED (no creó INDETERMINATE)."""
    tmp_rc = tmp_path / "receipt-unresolved"
    tmp_rc.mkdir()
    esc = await _sembrar_escenario_para_faults(tmp_rc)
    journal, clave = esc["journal"], esc["clave"]
    tx_fail = esc["tx_fail"]

    # Envejecer + sweepear: TX_FAIL → ROLLED_BACK + receipt UNRESOLVED.
    async with aiosqlite.connect(str(esc["db_path"])) as conn:
        await conn.execute(
            "UPDATE transactions SET created_at = datetime('now', '-48 hours') WHERE transaction_id = ?",
            (tx_fail,),
        )
        await conn.commit()
    barridas = await journal.sweep_stale_pending(max_age_hours=24.0)
    assert barridas == 1
    assert await _estado_tx(journal, tx_fail) == TransactionStatus.ROLLED_BACK.value
    assert await _receipt(journal, tx_fail) == SweepReceiptState.UNRESOLVED.value

    tx2 = await journal.begin_transaction("reemplazo", agent_id="test")
    await journal.crear_handoff_de_deployment(tx2, descripcion=None, registro=esc["registro"], viejo=esc["h1"])

    filas = {(a[0], a[2]) for a in await _absorciones(esc["db_path"]) if a[1] == clave}
    assert (tx_fail, esc["h1_id"]) in filas, "la evidencia ROLLED_BACK+UNRESOLVED no fue absorbida en el reemplazo"
    assert await _receipt(journal, tx_fail) == SweepReceiptState.SUPERSEDED.value, (
        "el receipt UNRESOLVED consumido por el reemplazo debe quedar SUPERSEDED (no CONSUMED)"
    )
    assert tx_fail not in await journal.transacciones_que_nombran(clave)


# =============================================================================
# FINDING A — NON_OBJECT_JSON_CAN_ESCAPE_RECOVERY_BOUNDARY
#   parser durable a prueba de corrupción + atomicidad del boundary standalone
# =============================================================================


async def _tx_pending_con_metadata_cruda(tmp_path: pathlib.Path, metadata_cruda: str) -> tuple[pathlib.Path, str, int]:
    """Deja una TX PENDING cuya ÚNICA entrada con metadata lleva ``metadata_cruda``
    (JSON crudo posiblemente corrupto). Devuelve (db_path, clave_A, tx)."""
    db_path = tmp_path / "journal.db"
    config, _ = _entorno(tmp_path)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(mod_texgen)
    journal = _registrar(OperationJournal(db_path))
    await journal.open()
    try:
        # Entrada real con metadata (para tener una fila que el oracle escanee),
        # luego se sobrescribe con la forma cruda bajo prueba.
        tx = await _evidencia(journal, [mod_texgen], "metadata-cruda")
        async with journal._db.execute(  # noqa: SLF001
            "UPDATE journal_entries SET metadata = ? WHERE transaction_id = ? AND metadata IS NOT NULL",
            (metadata_cruda, tx),
        ):
            pass
        await journal._db.commit()  # noqa: SLF001
    finally:
        await journal.close()
    return db_path, clave, tx


# (id, metadata cruda, ¿debe el oracle reportar la TX para el artifact A?)
_CASOS_METADATA = [
    ("A1_lista", "[]", False),
    ("A2_string", '"texto"', False),
    ("A3_int", "42", False),
    ("A4_null", "null", False),
    ("A5_invalido", "{no es json", False),
    ("A6_ft_null", '{"files_touched": null}', False),
    ("A7_ft_string", '{"files_touched": "__CLAVE__"}', False),
    ("A8_ft_int", '{"files_touched": 123}', False),
    ("A9_ft_dict_vacio", '{"files_touched": {}}', False),
    ("A9b_ft_dict", '{"files_touched": {"a": 1}}', False),
    ("A10_ft_lista_valida", '{"files_touched": ["__CLAVE__"]}', True),
]


@pytest.mark.parametrize(("caso", "cruda", "debe_reportar"), _CASOS_METADATA, ids=[c[0] for c in _CASOS_METADATA])
async def test_metadata_corrupta_no_escapa_ni_fabrica_evidencia(
    tmp_path: pathlib.Path, caso: str, cruda: str, debe_reportar: bool
) -> None:
    """El oracle durable (``transacciones_que_nombran`` → ``_candidatas_orphan_en_conn``)
    NUNCA lanza por la forma del metadata, y sólo cuenta como evidencia un
    ActionManifest válido (objeto JSON + ``files_touched`` lista de strings).

    JSON no-objeto (``[]``/``"str"``/``42``/``null``), ``files_touched`` no-lista
    (string, int, dict) y JSON inválido: la fila se ignora, no se interpreta
    carácter por carácter, y no crashea el boundary de recovery.
    """
    sub = tmp_path / caso
    sub.mkdir()
    # el placeholder se resuelve a la clave física real del artifact A
    config, _ = _entorno(sub)
    clave = clave_de_artifact(config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME)
    db_path, clave, tx = await _tx_pending_con_metadata_cruda(
        sub, cruda.replace("__CLAVE__", clave.replace("\\", "\\\\"))
    )

    journal = _registrar(OperationJournal(db_path))
    await journal.open()
    try:
        # NO debe lanzar (ni AttributeError ni TypeError) para ninguna forma.
        reportadas = await journal.transacciones_que_nombran(clave)
    finally:
        await journal.close()

    if debe_reportar:
        assert tx in reportadas, f"{caso}: un manifest válido nombrando A debe reportarse"
    else:
        assert tx not in reportadas, f"{caso}: metadata corrupta no puede fabricarse como evidencia de A"


def test_rutas_de_metadata_contrato_exhaustivo() -> None:
    """Contrato del primitivo compartido de parsing durable (igualdad literal).

    Un mutante que acepte string como iterable de ``files_touched`` (interpretación
    carácter-por-carácter) o que no filtre elementos no-string rompe este ancla.
    """
    from sky_claw.app.db.journal import _rutas_de_metadata

    assert _rutas_de_metadata("[]") == ()
    assert _rutas_de_metadata('"texto"') == ()
    assert _rutas_de_metadata("42") == ()
    assert _rutas_de_metadata("null") == ()
    assert _rutas_de_metadata("{no es json") == ()
    assert _rutas_de_metadata('{"files_touched": null}') == ()
    assert _rutas_de_metadata('{"files_touched": "C:\\\\foo"}') == ()  # string NO se itera como chars
    assert _rutas_de_metadata('{"files_touched": 123}') == ()
    assert _rutas_de_metadata('{"files_touched": {}}') == ()
    assert _rutas_de_metadata('{"files_touched": {"a": 1}}') == ()  # dict NO se itera como keys
    assert _rutas_de_metadata('{"files_touched": ["C:\\\\artifact"]}') == ("C:\\artifact",)
    # elementos no-string dentro de una lista válida se descartan (sin coerción):
    assert _rutas_de_metadata('{"files_touched": ["ok", 123, null, {"x": 1}]}') == ("ok",)
    # tipos no-str en la columna (SQLite dinámico) tampoco lanzan:
    assert _rutas_de_metadata(None) == ()
    assert _rutas_de_metadata(42) == ()


async def _inyectar_fallo_inesperado_en_candidatas():  # noqa: ANN202
    """Parcha ``_candidatas_orphan_en_conn`` para lanzar un fallo inesperado
    DESPUÉS de que el boundary ya empezó a mutar (se llama tras el INSERT del
    handoff nuevo, dentro del sello)."""

    async def _boom(*_a: object, **_k: object) -> set[int]:
        raise RuntimeError("fallo inesperado en candidate processing")

    return patch("sky_claw.app.db.journal._candidatas_orphan_en_conn", _boom)


async def test_standalone_fallo_inesperado_no_deja_mutacion_parcial_durable(tmp_path: pathlib.Path) -> None:
    """ATOMICIDAD (standalone): un fallo inesperado dentro del write boundary
    debe revertir TODO y re-lanzar; un escritor posterior sobre la MISMA conexión
    no puede committear el trabajo parcial del boundary roto."""
    esc = await _sembrar_escenario_para_faults(tmp_path / "atomic-standalone")
    journal, db_path = esc["journal"], esc["db_path"]
    assert journal._lifecycle is None  # noqa: SLF001  — este escenario es standalone

    tx2 = await journal.begin_transaction("reemplazo", agent_id="test")
    with await _inyectar_fallo_inesperado_en_candidatas(), pytest.raises(RuntimeError):
        await journal.crear_handoff_de_deployment(tx2, descripcion=None, registro=esc["registro"], viejo=esc["h1"])

    # Escritor posterior sobre la MISMA conexión: si el boundary roto quedó con
    # la transacción implícita abierta, este commit la volvería durable.
    tx3 = await journal.begin_transaction("escritor-posterior", agent_id="test")
    await journal.commit_transaction(tx3)
    await journal.close()

    # Reabrir fresco: nada del boundary roto puede haberse vuelto durable.
    j2 = _registrar(OperationJournal(db_path))
    await j2.open()
    try:
        estado_h1, superseded_by = await _estado_handoff(j2, esc["h1_id"])
        assert estado_h1 == HandoffState.AWAITING_DEPLOYMENT.value, "el viejo handoff no debió cambiar"
        assert superseded_by is None
        assert await _estado_tx(j2, tx2) != TransactionStatus.COMMITTED.value, "la TX del reemplazo no debió committear"
        assert await _absorciones(db_path) == [], "no debió quedar ninguna absorción"
        async with j2._db.execute("SELECT COUNT(*) FROM deployment_handoffs") as cur:  # noqa: SLF001
            (n_handoffs,) = await cur.fetchone()
        assert n_handoffs == 1, "no debió crearse el handoff nuevo (sólo H1)"
    finally:
        await j2.close()


async def test_lifecycle_fallo_inesperado_revierte_igual_que_standalone(tmp_path: pathlib.Path) -> None:
    """PARIDAD lifecycle: el mismo fallo inesperado bajo el boundary lifecycle
    ya revierte (el CM de ``transaction`` captura ``BaseException``). Congela la
    paridad: standalone debe comportarse igual."""
    from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager

    db_path = tmp_path / "journal.db"
    config, _ = _entorno(tmp_path)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(mod_texgen)
    lifecycle = DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    journal = _registrar(OperationJournal(db_path, lifecycle=lifecycle))
    await journal.open()
    try:
        _escribir_mod(mod_texgen)
        tx1 = await journal.begin_transaction("texgen entregado", agent_id="test")
        d1 = digest_arbol(mod_texgen / "textures")
        registro = DeploymentHandoff(
            handoff_id=0,
            source_tx_id=tx1,
            state=HandoffState.AWAITING_DEPLOYMENT,
            artifact_path=clave,
            game_key=clave_de_artifact(config.game_path),
            mods_root_key=clave_de_artifact(config.mo2_mods_path),
            data_key=clave_de_artifact(config.data_dir),
            expected_profile="Perfil-A",
            expected_digest=d1.digest,
            expected_files=d1.files,
            expected_bytes=d1.bytes,
            observed_digest=None,
            observed_files=None,
            observed_bytes=None,
            created_at="",
            updated_at="",
            completed_at=None,
            superseded_at=None,
            superseded_by=None,
        )
        h1_id = await journal.crear_handoff_de_deployment(tx1, descripcion=None, registro=registro, viejo=None)
        h1 = await journal.consultar_handoff_activo(clave)
        assert h1 is not None

        tx2 = await journal.begin_transaction("reemplazo", agent_id="test")
        with await _inyectar_fallo_inesperado_en_candidatas(), pytest.raises(RuntimeError):
            await journal.crear_handoff_de_deployment(tx2, descripcion=None, registro=registro, viejo=h1)

        # El boundary lifecycle ya revirtió: estado intacto sin escritor posterior.
        estado_h1, superseded_by = await _estado_handoff(journal, h1_id)
        assert estado_h1 == HandoffState.AWAITING_DEPLOYMENT.value
        assert superseded_by is None
        assert await _estado_tx(journal, tx2) != TransactionStatus.COMMITTED.value
        assert await _absorciones(db_path) == []
    finally:
        await journal.close()
        await lifecycle.shutdown_all()
