"""LECCIÓN APRENDIDA (PR503, revisión adversarial cross-run) — registrada como
propiedad verificable, no como recuerdo de proceso:

    Una prueba de traza sobre UN solo ``execute()`` es estructuralmente
    incapaz de falsificar un claim de serialización CROSS-RUN. Toda
    conclusión del tipo ``REFUTED_BY_SERIALIZATION`` exige su test de
    intercalado de dos corridas con barreras. La primera lectura del autor
    de este archivo fue "el sello post-release no afecta la evidencia":
    equivocada; este test existe para que esa clase de salto no vuelva a
    pasar sin RED.

CARRERA: el boundary de preservación del desenlace ``needs_deployment``
corre DESPUÉS de liberar ``dyndolod-pipeline`` y, con ``viejo != None``,
sella evidencia vía el oracle compartido. Entre el release y el sello hay
ventana productiva (incluye ``digest_arbol`` en hilo aparte): otra corrida B
puede adquirir el lease, abrir su TX y emitir su manifest —legítimamente,
bajo el lock correcto— y el sello de A igual absorbe esa TX VIVA. Si B
falla después, su evidencia queda silenciada (FALSE GREEN): exactamente la
clase de fallo que #493/#503 combate.

INVARIANTE bajo prueba (hoy VALIDA: el fix movió la certificación dentro
del lease, así que TX_B nace después del sello de A y jamás es absorbida):

    El boundary que sella evidencia de un artifact gestionado NO puede
    absorber una TX PENDING viva de otra generación; la evidencia de una
  corrida fallida debe seguir vigente para el reconciler.

Historia del fix: la certificación vive ahora dentro de la región
serializada (sentinela _CertificacionPreservadaError en dyndolod_service);
el watermark ``hasta_transaction_id`` que antes mitigaba este RED fue
ELIMINADO por redundante. Este test quedó congelado como guarda permanente
del orden que hace imposible la absorción.
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.handoffs import DeploymentHandoff, HandoffState, clave_de_artifact
from sky_claw.app.db.journal import OperationJournal
from sky_claw.app.db.locks import DistributedLockManager, LockInfo
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner, ToolExecutionResult
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService


def _entorno(tmp_path: pathlib.Path) -> tuple[DynDOLODConfig, pathlib.Path]:
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
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    return config, game


def _proceso_texgen_ok(config: DynDOLODConfig):  # noqa: ANN202
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    logs = config.dyndolod_exe.parent / "Logs"

    async def _run(*args: object, **kwargs: object) -> tuple[str, str, int, float]:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "a.dds").write_bytes(b"GEN-OK")
        log_path = logs / "TexGen_SSE_log.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "[00:00:01] Using Output Path: C:\\out\\\n[00:10:00] TexGen completed successfully\n",
            encoding="utf-8",
        )
        return "", "", 0, 1.0

    return _run


class _LockManagerObservador(DistributedLockManager):
    """Lock manager REAL que SOLO observa: passthrough exacto de argumentos.

    NUNCA se mutan los identificadores del protocolo (agent_id/resource_id):
    el heartbeat y el release internos usan el agent_id original, y una
    diferencia de un carácter rompe la propiedad del lease en silencio.

    Con ``esperar_release`` la ADQUISICIÓN espera (determinista, sin polling)
    a que la corrida previa libere — B nace temprano pero toma el lease sólo
    cuando es legítimamente libre.
    """

    def __init__(
        self,
        db_path: pathlib.Path,
        eventos: dict[str, asyncio.Event],
        *,
        esperar_release: asyncio.Event | None = None,
    ) -> None:
        super().__init__(db_path)
        self._eventos = eventos
        self._esperar_release = esperar_release

    async def acquire_lock(self, resource_id: str, agent_id: str, ttl: int) -> LockInfo:  # type: ignore[override]
        if self._esperar_release is not None:
            await asyncio.wait_for(self._esperar_release.wait(), timeout=30)
        return await super().acquire_lock(resource_id=resource_id, agent_id=agent_id, ttl=ttl)

    async def release_lock(self, resource_id: str, agent_id: str) -> bool:  # type: ignore[override]
        resultado = await super().release_lock(resource_id=resource_id, agent_id=agent_id)
        if resource_id == "dyndolod-pipeline":
            self._eventos["liberado"].set()
        return resultado


def _servicio(
    config: DynDOLODConfig,
    game: pathlib.Path,
    journal: OperationJournal,
    lock_mgr: DistributedLockManager,
    *,
    proceso: object,
    dyn_falla: bool = False,
    dyn_gate: asyncio.Event | None = None,
) -> DynDOLODPipelineService:
    snap_mgr = AsyncMock(spec=FileSnapshotManager)
    snap_mgr.create_snapshot = AsyncMock(
        side_effect=lambda *a, **k: SnapshotInfo(
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
        mo2_profile="Perfil-A",
    )
    runner = DynDOLODRunner(config)
    svc._runner = runner  # type: ignore[attr-defined]
    # Ambas corridas comparten el MISMO config frozen: las claves físicas del
    # artifact son idénticas por construcción, que es justo la condición de
    # solape del oracle entre A y B.
    svc._runner._execute_process = proceso  # type: ignore[attr-defined]
    if dyn_falla:
        # Camino productivo #493: DynDOLOD (post-spawn) reporta fallo de
        # herramienta. Con ``dyn_gate`` la corrida queda VIVA y PENDING dentro
        # del stage (equivalente determinista de un crash del runner) hasta
        # que el test la libere para el teardown.
        async def _dyn_falla(**kw: object) -> ToolExecutionResult:
            if dyn_gate is not None:
                await asyncio.wait_for(dyn_gate.wait(), timeout=60)
            return ToolExecutionResult(False, "DynDOLOD", 1, "", "", errors=["DynDOLOD roto"])

        svc._runner.run_dyndolod = AsyncMock(side_effect=_dyn_falla)  # type: ignore[attr-defined]
    else:
        svc._runner.run_dyndolod = AsyncMock()  # type: ignore[attr-defined]
    return svc


async def test_sello_post_release_no_debe_absorber_tx_viva_de_otra_corrida(
    tmp_path: pathlib.Path,
) -> None:
    """Intercalado A/B con barreras (receta de la revisión adversarial):

    A: acquire → manifest → needs_deployment → RELEASE ── barrera ──┐
    B: acquire → begin TX_B → manifest ──────────── barrera ──┐     │
                                                              ▼     │
    A: crear_handoff_de_deployment(viejo != None) → sella ──► ¿absorbió TX_B?

    INVARIANTE: la evidencia de TX_B debe seguir VIGENTE (oracle la sigue
    reportando) aunque A haya reemplazado el ownership previo. Hoy el sello
    post-release se la come: RED.
    """
    eventos = {"liberado": asyncio.Event(), "manifestada_b": asyncio.Event(), "sello_hecho": asyncio.Event()}
    db_path = tmp_path / "journal.db"
    config, game = _entorno(tmp_path)
    artifact = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(artifact)

    # Los managers por-corrida (con sus políticas de gating) se crean dentro
    # del try, junto al intercalado.

    # Aperturas SECUENCIALES: tres `open()` concurrentes sobre el mismo archivo
    # colisionan en el setup de PRAGMA (database is locked) de forma
    # nondeterminística. La conexión de seed se cierra ANTES del intercalado
    # para dejar sólo a las dos corridas como contendientes.
    journal_seed = OperationJournal(db_path)
    await journal_seed.open()
    journal_a = OperationJournal(db_path)
    await journal_a.open()
    journal_b = OperationJournal(db_path)
    await journal_b.open()
    try:
        # --- ownership previo: AWAITING viejo para que el handler de A tenga
        #     viejo != None (camino SUPERSEDING del handler post-release) ----
        (artifact / "textures").mkdir(parents=True, exist_ok=True)
        (artifact / "textures" / "previo.dds").write_bytes(b"PREVIO")
        d_previo = digest_arbol(artifact / "textures")
        tx_old = await journal_seed.begin_transaction("corrida previa", agent_id="dyndolod-pipeline-service")
        registro_viejo = DeploymentHandoff(
            handoff_id=0,
            source_tx_id=tx_old,
            state=HandoffState.AWAITING_DEPLOYMENT,
            artifact_path=clave,
            game_key="g",
            mods_root_key="m",
            data_key="d",
            expected_profile="Perfil-A",
            expected_digest=d_previo.digest,
            expected_files=d_previo.files,
            expected_bytes=d_previo.bytes,
            observed_digest=None,
            observed_files=None,
            observed_bytes=None,
            created_at="",
            updated_at="",
            completed_at=None,
            superseded_at=None,
            superseded_by=None,
        )
        await journal_seed.crear_handoff_de_deployment(tx_old, descripcion="previa", registro=registro_viejo)
        await journal_seed.close()

        # --- RUN A: instrumentar el sello post-release con barrera ----------
        original_crear_a = journal_a.crear_handoff_de_deployment

        async def _crear_a(*a: object, **k: object) -> int:
            # Post-fix la certificación corre BAJO lease: sólo se registra.
            resultado = await original_crear_a(*a, **k)  # type: ignore[arg-type]
            eventos["sello_hecho"].set()
            return resultado

        journal_a.crear_handoff_de_deployment = _crear_a  # type: ignore[method-assign]

        # --- RUN B: manifest grabado con barrera; luego B se congela viva
        #     dentro del proceso hasta que el sello de A haya ocurrido -------
        original_persist_b = journal_b.persist_action_manifest
        tx_ids_b: list[int] = []

        async def _persist_b(*a: object, **k: object) -> int:
            resultado = await original_persist_b(*a, **k)  # type: ignore[arg-type]
            if k.get("transaction_id") is not None:
                tx_ids_b.append(int(k["transaction_id"]))  # type: ignore[arg-type]
                print(f"[DIAG] B manifestó tx={tx_ids_b[-1]}")
            eventos["manifestada_b"].set()
            return resultado

        journal_b.persist_action_manifest = _persist_b  # type: ignore[method-assign]

        # --- intercalado (topología post-fix, §13) ----------------------------
        # A corre completo: certificación BAJO lease → release. B nace en
        # paralelo pero su ADQUISICIÓN queda gated hasta el release de A, así
        # que cuando A sella, TX_B todavía NO existe — y jamás es absorbida.
        mgr_a = _LockManagerObservador(tmp_path / "locks.db", eventos)
        await mgr_a.initialize()
        mgr_b = _LockManagerObservador(tmp_path / "locks.db", eventos, esperar_release=eventos["liberado"])
        await mgr_b.initialize()

        svc_a = _servicio(config, game, journal_a, mgr_a, proceso=_proceso_texgen_ok(config))
        tarea_a = asyncio.create_task(svc_a.execute(preset="Medium", run_texgen=True, create_snapshot=True))
        await asyncio.wait_for(eventos["sello_hecho"].wait(), timeout=30)

        # El gate de visibilidad de A ya cortó y preservó el mod: materializar
        # el Data hace que el gate de B pase (si no, B caería en
        # needs_deployment y su TX no quedaría PENDING).
        data_textures = config.data_dir / "textures"
        data_textures.mkdir(parents=True, exist_ok=True)
        for archivo in (artifact / "textures").iterdir():
            shutil.copy2(archivo, data_textures / archivo.name)

        ev_dyn_gate_b = asyncio.Event()
        svc_b = _servicio(
            config,
            game,
            journal_b,
            mgr_b,
            proceso=_proceso_texgen_ok(config),
            dyn_falla=True,
            dyn_gate=ev_dyn_gate_b,
        )

        def _al_terminar_b(t: asyncio.Task) -> None:
            if t.cancelled():
                print("[DIAG] tarea B CANCELADA")
            elif t.exception() is not None:
                print(f"[DIAG] tarea B EXCEPCIÓN: {t.exception()!r}")
            else:
                print(
                    "[DIAG] tarea B OK: success="
                    f"{t.result().get('success')} needs_deployment={t.result().get('needs_deployment')}"
                )

        tarea_b = asyncio.create_task(svc_b.execute(preset="Medium", run_texgen=True, create_snapshot=True))
        tarea_b.add_done_callback(_al_terminar_b)
        await asyncio.wait_for(eventos["manifestada_b"].wait(), timeout=30)

        # Esperar el release real de A antes de leer estado durable.
        await asyncio.wait_for(eventos["liberado"].wait(), timeout=30)

        # --- hecho: el sello de A ya corrió mientras B sigue VIVA, dueña del
        #     lease, bloqueada dentro del stage DynDOLOD -----------------------
        async with journal_b._db.execute(  # noqa: SLF001
            "SELECT transaction_id, artifact_path FROM orphan_evidence_absorptions ORDER BY transaction_id"
        ) as cur:
            absorciones_antes = [(int(r[0]), str(r[1])) for r in await cur.fetchall()]

        assert tx_ids_b, "B nunca emitió su manifest"
        tx_b = tx_ids_b[-1]
        async with journal_b._db.execute(  # noqa: SLF001
            "SELECT status FROM transactions WHERE transaction_id = ?",
            (tx_b,),
        ) as cur:
            fila = await cur.fetchone()
        estado_b = str(fila[0]) if fila else None
        assert estado_b == "pending", f"precondición rota: TX_B debería estar VIVA, está {estado_b}"

        # --- INVARIANTE (el corazón del finding), evaluada CON B VIVA: la
        #     evidencia de la generación en curso debe seguir VIGENTE pese al
        #     reemplazo que otra corrida hizo del ownership previo -------------
        reportadas = await journal_b.transacciones_que_nombran(clave)
        assert tx_b in reportadas, (
            "F2 CROSS-RUN FALSE: el sello post-release de A absorbió la TX_B VIVA "
            f"de B (absorpciones={absorciones_antes}); si B crashea, su evidencia "
            "queda silenciada y el reconciler fabricará un false green."
        )

        # --- teardown controlado: dejar que ambas corridas terminen ----------
        res_a = await asyncio.wait_for(tarea_a, timeout=60)
        ev_dyn_gate_b.set()
        await asyncio.wait_for(tarea_b, timeout=60)

        # Sanity del escenario A (válida hoy y tras el fix):
        assert res_a.get("needs_deployment") is True, res_a
    finally:
        await asyncio.gather(journal_a.close(), journal_b.close())
        await asyncio.gather(mgr_a.close(), mgr_b.close())
