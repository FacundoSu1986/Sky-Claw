"""POST_RELEASE_ARTIFACT_PROVENANCE_RACE (PR #503, segunda carrera).

El boundary de preservación ``needs_deployment`` certifica el artifact con
``digest_arbol(mod_textures)`` y firma ``expected_digest`` en el handoff
durable. Hoy ese bloque corre DESPUÉS de soltar ``dyndolod-pipeline``, así
que los bytes que certifica pueden pertenecer a la generación SIGUIENTE:

    A produce CONTENT_A → preserva → RELEASE
    B acquire → regenera productivamente CONTENT_B sobre el mismo mod
    A [sin ownership]: digest(=bytes de B) → handoff(source_tx_id=TX_A)

INVARIANTE bajo prueba (vale en cualquier topología):

    SI handoff.source_tx_id == TX_A
    ENTONCES handoff.expected_digest == DIGEST_A  (los bytes que A produjo)

    y el orden productivo es:
        a-handoff  <  a-released  <  b-mutated

Pre-fix esto reproduce CONFIRMED (source=A, digest=B, orden invertido);
post-fix (certificación dentro del lease) debe quedar GREEN sin ningún
watermark causal.

Los únicos fakes: proceso del runner, contenido del artifact, barreras y el
hook de empaquetado de B para observar el instante exacto. Journal, locks,
boundaries y lifecycle son REALES.
"""

from __future__ import annotations

import asyncio
import itertools
import pathlib
import shutil
import threading
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.handoffs import DeploymentHandoff, HandoffState, clave_de_artifact
from sky_claw.app.db.journal import OperationJournal
from sky_claw.app.db.locks import DistributedLockManager, LockInfo
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner, ToolExecutionResult
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

CONTENIDO_A = (b"GEN-A-MARKER", b"GEN-A-SUB")
# Estrictamente MÁS LARGO que A: el validador de TexGen exige CRECIMIENTO del
# log entre corridas (bytes nuevos); mismo tamaño + mtime distinto = fallo.
CONTENIDO_B = (b"GEN-B-MARKER-QUE-CRECE", b"GEN-B-SUB")


def _entorno(tmp_path: pathlib.Path) -> DynDOLODConfig:
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
    return config


def _proceso_texgen(config: DynDOLODConfig, marcadores: tuple[bytes, bytes]):  # noqa: ANN202
    staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    logs = config.dyndolod_exe.parent / "Logs"

    async def _run(*args: object, **kwargs: object) -> tuple[str, str, int, float]:
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "a.dds").write_bytes(marcadores[0])
        (staging / "sub").mkdir(parents=True, exist_ok=True)
        (staging / "sub" / "b.dds").write_bytes(marcadores[1])
        log_path = logs / "TexGen_SSE_log.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # APPEND puro (el validador exige prefijo preservado + crecimiento):
        # la primera corrida crea el log; las siguientes agregan su línea.
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[00:10:00] TexGen completed successfully {marcadores[0].decode('ascii')}\n")
        return "", "", 0, 1.0

    return _run


def _digest_de_contenido(tmp_path: pathlib.Path, marcadores: tuple[bytes, bytes]) -> object:
    t = tmp_path / f"digest-{marcadores[0].decode('ascii')}" / "textures"
    t.mkdir(parents=True, exist_ok=True)
    (t / "a.dds").write_bytes(marcadores[0])
    (t / "sub").mkdir(exist_ok=True)
    (t / "sub" / "b.dds").write_bytes(marcadores[1])
    return digest_arbol(t)


class _LockManagerObservador(DistributedLockManager):
    """Passthrough EXACTO; registra y señala liberaciones del recurso."""

    def __init__(
        self,
        db_path: pathlib.Path,
        ev_liberado: asyncio.Event,
        eventos: list[str],
        contador: itertools.count,
        liberado_flag: list[bool],
    ) -> None:
        super().__init__(db_path)
        self._ev_liberado = ev_liberado
        self._eventos = eventos
        self._contador = contador
        self._liberado_flag = liberado_flag

    async def acquire_lock(self, resource_id: str, agent_id: str, ttl: int) -> LockInfo:  # type: ignore[override]
        return await super().acquire_lock(resource_id=resource_id, agent_id=agent_id, ttl=ttl)

    async def release_lock(self, resource_id: str, agent_id: str) -> bool:  # type: ignore[override]
        resultado = await super().release_lock(resource_id=resource_id, agent_id=agent_id)
        if resource_id == "dyndolod-pipeline":
            self._eventos.append(f"a-released#{next(self._contador)}")
            self._liberado_flag[0] = True
            self._ev_liberado.set()
        return resultado


async def test_needs_deployment_no_puede_firmar_bytes_de_la_siguiente_corrida(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eventos: list[str] = []
    contador = itertools.count()

    def ev(etiqueta: str) -> None:
        eventos.append(f"{etiqueta}#{next(contador)}")

    config = _entorno(tmp_path)
    artifact = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(artifact)
    digest_a = _digest_de_contenido(tmp_path, CONTENIDO_A)
    digest_b = _digest_de_contenido(tmp_path, CONTENIDO_B)

    ev_a_released = asyncio.Event()
    ev_b_mutated = asyncio.Event()
    ev_dyn_gate_b = asyncio.Event()
    liberado_flag = [False]
    b_bytes_ready = threading.Event()

    lock_mgr = _LockManagerObservador(tmp_path / "locks.db", ev_a_released, eventos, contador, liberado_flag)
    await lock_mgr.initialize()
    db_path = tmp_path / "journal.db"
    journal_a = OperationJournal(db_path)
    journal_b = OperationJournal(db_path)
    await journal_a.open()
    await journal_b.open()
    try:
        # --- ownership previo (variante P2: AWAITING → SUPERSEDING) ----------
        (artifact / "textures" / "sub").mkdir(parents=True, exist_ok=True)
        (artifact / "textures" / "a.dds").write_bytes(b"PREVIO")
        d_previo = digest_arbol(artifact / "textures")
        tx_old = await journal_a.begin_transaction("corrida previa", agent_id="dyndolod-pipeline-service")
        await journal_a.crear_handoff_de_deployment(
            tx_old,
            descripcion="previa",
            registro=DeploymentHandoff(
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
            ),
        )

        # --- instrumentación con orden total ----------------------------------
        tx_ids: dict[str, int] = {}

        original_begin_a = journal_a.begin_transaction

        async def _begin_a(*a: object, **k: object) -> int:
            resultado = await original_begin_a(*a, **k)  # type: ignore[arg-type]
            tx_ids.setdefault("a", resultado)
            return resultado

        journal_a.begin_transaction = _begin_a  # type: ignore[method-assign]

        original_crear_a = journal_a.crear_handoff_de_deployment
        probe: dict[str, bool] = {}
        mgr_probe = DistributedLockManager(tmp_path / "locks.db")
        await mgr_probe.initialize()

        async def _crear_a(*a: object, **k: object) -> int:
            # §17 anti-deadlock: DENTRO de la ventana certificadora (lease aún
            # en manos de A) un tercero NO puede adquirir.
            try:
                await mgr_probe.acquire_lock(resource_id="dyndolod-pipeline", agent_id="sonda-deadlock", ttl=10)
                probe["durante"] = True
                await mgr_probe.release_lock(resource_id="dyndolod-pipeline", agent_id="sonda-deadlock")
            except Exception:  # noqa: BLE001 — esperado: excluido por A
                probe["durante"] = False
            resultado = await original_crear_a(*a, **k)  # type: ignore[arg-type]
            ev("a-handoff")
            return resultado

        journal_a.crear_handoff_de_deployment = _crear_a  # type: ignore[method-assign]

        original_persist_b = journal_b.persist_action_manifest

        async def _persist_b(*a: object, **k: object) -> int:
            resultado = await original_persist_b(*a, **k)  # type: ignore[arg-type]
            if k.get("transaction_id") is not None:
                tx_ids["b"] = int(k["transaction_id"])  # type: ignore[arg-type]
            ev("b-manifest")
            return resultado

        journal_b.persist_action_manifest = _persist_b  # type: ignore[method-assign]

        # Hook del GATE DE VISIBILIDAD (corre SIEMPRE tras el empaquetado):
        # 1ª invocación = corrida A (pasa-through: su veredicto es el corte);
        # 2ª = corrida B: re-espeja el Data ANTES del gate (para que pase con
        # CONTENT_B ya empaquetado) y registra el instante exacto de la
        # mutación productiva. El orden 1-2 está garantizado por las barreras
        # (B no nace hasta después del release de A).
        import sky_claw.local.tools.dyndolod_runner as dyndolod_runner_mod

        gate_original = dyndolod_runner_mod.verificar_visibilidad_de_texgen
        llamadas_gate = itertools.count()
        loop_principal = asyncio.get_running_loop()

        def _gate_observado(**kw: object) -> object:
            llamada = next(llamadas_gate)
            if llamada == 0:
                return gate_original(**kw)
            # La MUTACIÓN es el aterrizaje de los bytes de B en el mod (el
            # empaquetado ya terminó al llegar acá): se registra ANTES del
            # veredicto del gate, que es irrelevante para la carrera.
            shutil.copytree(
                artifact / "textures",
                config.data_dir / "textures",
                dirs_exist_ok=True,
            )
            ev("b-mutated")
            b_bytes_ready.set()
            loop_principal.call_soon_threadsafe(ev_b_mutated.set)
            return gate_original(**kw)

        monkeypatch.setattr(dyndolod_runner_mod, "verificar_visibilidad_de_texgen", _gate_observado)

        # Determinismo del interleaving dañino: si A ya LIBERÓ y los bytes de
        # B aún no aterrizaron, el digest del handler de A ESPERA (bloqueo de
        # thread, permitido: corre en worker de to_thread). Post-fix el digest
        # ocurre ANTES del release y este wrapper es passthrough puro.
        import sky_claw.local.tools.dyndolod_service as dyndolod_service_mod

        digest_original = dyndolod_service_mod.digest_arbol

        def _digest_observado(ruta: object) -> object:
            if liberado_flag[0] and not b_bytes_ready.is_set():
                b_bytes_ready.wait(timeout=30)
            return digest_original(ruta)

        monkeypatch.setattr(dyndolod_service_mod, "digest_arbol", _digest_observado)

        def _servicio(
            journal: OperationJournal,
            etiqueta: str,
            marcadores: tuple[bytes, bytes],
            *,
            dyn_gate: asyncio.Event | None,
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
            svc._runner._execute_process = _proceso_texgen(config, marcadores)  # type: ignore[attr-defined]

            async def _dyn(**kw: object) -> ToolExecutionResult:
                if dyn_gate is not None:
                    await asyncio.wait_for(dyn_gate.wait(), timeout=60)
                return ToolExecutionResult(False, "DynDOLOD", 1, "", "", errors=["DynDOLOD roto"])

            svc._runner.run_dyndolod = AsyncMock(side_effect=_dyn)  # type: ignore[attr-defined]
            return svc

        # --- RUN A: produce CONTENT_A, corta por visibilidad, preserva --------
        svc_a = _servicio(journal_a, "A", CONTENIDO_A, dyn_gate=None)
        tarea_a = asyncio.create_task(svc_a.execute(preset="Medium", run_texgen=True, create_snapshot=True))
        await asyncio.wait_for(ev_a_released.wait(), timeout=30)

        # --- RUN B: dueña LEGÍTIMA del lease; regenera CONTENT_B -------------
        svc_b = _servicio(journal_b, "B", CONTENIDO_B, dyn_gate=ev_dyn_gate_b)
        tarea_b = asyncio.create_task(svc_b.execute(preset="Medium", run_texgen=True, create_snapshot=True))
        await asyncio.wait_for(ev_b_mutated.wait(), timeout=30)

        res_a = await asyncio.wait_for(tarea_a, timeout=60)
        assert res_a.get("needs_deployment") is True, res_a
        ev_dyn_gate_b.set()
        await asyncio.wait_for(tarea_b, timeout=60)

        # --- lectura durable --------------------------------------------------
        async with journal_a._db.execute(  # noqa: SLF001
            "SELECT source_tx_id, expected_digest FROM deployment_handoffs "
            "WHERE artifact_path = ? ORDER BY handoff_id DESC LIMIT 1",
            (clave,),
        ) as cur:
            fila = await cur.fetchone()
        assert fila is not None, "no hay handoff durable para el artifact"
        source_tx = int(fila[0])
        digest_firmado = str(fila[1])

        def orden(prefijo: str) -> int:
            for i, e in enumerate(eventos):
                if e.startswith(prefijo):
                    return i
            raise AssertionError(f"falta evento {prefijo}: {eventos}")

        # --- INVARIANTES ------------------------------------------------------
        assert source_tx == tx_ids["a"], (
            f"el handoff de preservación debe ser de A: source={source_tx}, esperado={tx_ids['a']}"
        )
        assert digest_firmado != digest_b.digest, (
            "POST_RELEASE_ARTIFACT_PROVENANCE_RACE_CONFIRMED: el handoff de A "
            f"firma bytes de B (firmado={digest_firmado} == DIGEST_B={digest_b.digest})"
        )
        assert digest_firmado == digest_a.digest, (
            f"el digest certificado debe ser el de A: {digest_firmado} != DIGEST_A={digest_a.digest} "
            f"(eventos={eventos})"
        )
        assert orden("a-handoff") < orden("a-released"), (
            f"la certificación durable ocurrió tras soltar el lease: {eventos}"
        )
        assert orden("a-released") < orden("b-mutated"), f"B mutó antes de que A liberara: {eventos}"

        # §17: la sonda fue RECHAZADA dentro de la ventana y aceptada después.
        await asyncio.wait_for(ev_a_released.wait(), timeout=10)
        probe["despues"] = True
        try:
            await mgr_probe.acquire_lock(resource_id="dyndolod-pipeline", agent_id="sonda-deadlock", ttl=10)
            probe["despues"] = True
            await mgr_probe.release_lock(resource_id="dyndolod-pipeline", agent_id="sonda-deadlock")
        except Exception:  # noqa: BLE001 — no esperado post-release
            probe["despues"] = False
        assert probe.get("durante") is False, f"la sonda adquirió DENTRO de la ventana: {probe}"
        assert probe.get("despues") is True, f"deadlock: la sonda no adquirió tras el release: {probe}"
    finally:
        # Teardown robusto: desbloquear y cancelar tareas vivas ANTES de cerrar
        # journals/lock manager (si el test falla a mitad, las corridas siguen
        # desenvolviéndose y un release tardío contra un manager cerrado
        # ensuciaría el error real).
        ev_dyn_gate_b.set()
        for tarea in (locals().get("tarea_a"), locals().get("tarea_b")):
            if tarea is not None and not tarea.done():
                tarea.cancel()
        await asyncio.gather(
            *(t for t in (locals().get("tarea_a"), locals().get("tarea_b")) if t is not None),
            return_exceptions=True,
        )
        await asyncio.gather(journal_a.close(), journal_b.close())
        await lock_mgr.close()
        await mgr_probe.close()


# ---------------------------------------------------------------------------
# F5 — LEASE LOST antes de certificar: fail-closed sin handoff ni seal
# ---------------------------------------------------------------------------


async def test_lease_lost_antes_de_certificar_no_firma_nada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11/F5: si ``lease_lost`` está marcado al entrar a la ventana de
    certificación, NO hay digest autorizado, NO hay handoff durable, NO hay
    seal. La corrida falla closed y deja estado recuperable."""
    import sky_claw.local.tools.dyndolod_service as dsm

    dyndolod_mod = dsm
    config = _entorno(tmp_path)
    artifact = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(artifact)

    ev_liberado = asyncio.Event()

    class _Mgr(DistributedLockManager):
        async def release_lock(self, resource_id: str, agent_id: str) -> bool:  # type: ignore[override]
            ok = await super().release_lock(resource_id=resource_id, agent_id=agent_id)
            if resource_id == "dyndolod-pipeline":
                ev_liberado.set()
            return ok

    mgr = _Mgr(tmp_path / "locks.db")
    await mgr.initialize()
    journal = OperationJournal(tmp_path / "journal.db")
    await journal.open()
    try:
        # Seed del viejo AWAITING (igual que el test principal).
        (artifact / "textures" / "sub").mkdir(parents=True, exist_ok=True)
        (artifact / "textures" / "a.dds").write_bytes(b"PREVIO")
        d_previo = digest_arbol(artifact / "textures")
        tx_old = await journal.begin_transaction("previa", agent_id="dyndolod-pipeline-service")
        await journal.crear_handoff_de_deployment(
            tx_old,
            descripcion="previa",
            registro=DeploymentHandoff(
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
            ),
        )

        # F5: inyectar la pérdida EN LA PRIMITIVA — assert_owned es quien
        # decide (compara token acquired_at contra DB y flippea lease_lost);
        # parchear la property externa ya no alcanza porque el guard vive
        # adentro de ella.
        from sky_claw.app.db.locks import LockLeaseLostError

        async def _assert_roto(self: object) -> None:
            raise LockLeaseLostError("lease perdida inyectada antes de certificar")

        monkeypatch.setattr(
            "sky_claw.local.tools.dyndolod_service.SnapshotTransactionLock.assert_owned",
            _assert_roto,
        )

        # Digest roto NO debe llamarse: assert_owned corta antes.
        def _digest_no_debe_correr(ruta: object) -> object:
            raise AssertionError("digest corrió con lease_lost")

        monkeypatch.setattr(dyndolod_mod, "digest_arbol", _digest_no_debe_correr)

        svc = DynDOLODPipelineService(
            lock_manager=mgr,
            snapshot_manager=AsyncMock(spec=FileSnapshotManager),
            journal=journal,  # type: ignore[arg-type]
            path_resolver=MagicMock(),
            event_bus=AsyncMock(spec=CoreEventBus),
            mo2_profile="Perfil-A",
        )
        runner = DynDOLODRunner(config)
        svc._runner = runner  # type: ignore[attr-defined]

        async def _proceso(*a: object, **k: object) -> tuple[str, str, int, float]:
            staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "a.dds").write_bytes(b"GEN-A-MARKER")
            log_path = config.dyndolod_exe.parent / "Logs" / "TexGen_SSE_log.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write("[00:10:00] TexGen completed successfully GEN-A\n")
            return "", "", 0, 1.0

        svc._runner._execute_process = _proceso  # type: ignore[attr-defined]
        svc._runner.run_dyndolod = AsyncMock()  # type: ignore[attr-defined]

        res = await asyncio.wait_for(svc.execute(preset="Medium", run_texgen=True, create_snapshot=True), timeout=60)
        assert res.get("success") is False
        assert res.get("needs_deployment") is True  # el artifact existe

        await asyncio.wait_for(ev_liberado.wait(), timeout=10)  # lock liberado

        filas: list[int] = []
        async with journal._db.execute(  # noqa: SLF001
            "SELECT handoff_id FROM deployment_handoffs WHERE source_tx_id != ?", (tx_old,)
        ) as cur:
            filas = [int(r[0]) for r in await cur.fetchall()]
        assert filas == [], f"F5: se creó handoff con lease perdida: {filas}"
    finally:
        await journal.close()
        await mgr.close()


# ---------------------------------------------------------------------------
# F2/F3 — fallo del digest / fallo del boundary dentro de la ventana
# ---------------------------------------------------------------------------


async def _corrida_con_fallo_en_ventana(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, inyeccion: str
) -> tuple[dict[str, object], asyncio.Event, int]:
    config = _entorno(tmp_path)
    artifact = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(artifact)
    ev_liberado = asyncio.Event()

    class _Mgr(DistributedLockManager):
        async def release_lock(self, resource_id: str, agent_id: str) -> bool:  # type: ignore[override]
            ok = await super().release_lock(resource_id=resource_id, agent_id=agent_id)
            if resource_id == "dyndolod-pipeline":
                ev_liberado.set()
            return ok

    mgr = _Mgr(tmp_path / "locks.db")
    await mgr.initialize()
    journal = OperationJournal(tmp_path / "journal.db")
    await journal.open()
    try:
        (artifact / "textures" / "sub").mkdir(parents=True, exist_ok=True)
        (artifact / "textures" / "a.dds").write_bytes(b"PREVIO")
        d_previo = digest_arbol(artifact / "textures")
        tx_old = await journal.begin_transaction("previa", agent_id="dyndolod-pipeline-service")
        await journal.crear_handoff_de_deployment(
            tx_old,
            descripcion="previa",
            registro=DeploymentHandoff(
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
            ),
        )
        tx_a = await journal.begin_transaction("corrida A", agent_id="dyndolod-pipeline-service")

        import sky_claw.local.tools.dyndolod_service as dsm

        if inyeccion == "digest":

            def _digest_roto(ruta: object) -> object:
                raise OSError("fallo de digest inyectado")

            monkeypatch.setattr(dsm, "digest_arbol", _digest_roto)
        else:

            async def _crear_roto(*a: object, **k: object) -> int:
                from sky_claw.app.db.journal import JournalTransactionError

                raise JournalTransactionError("fallo SQLite inyectado")

            monkeypatch.setattr(journal, "crear_handoff_de_deployment", _crear_roto)

        svc = DynDOLODPipelineService(
            lock_manager=mgr,
            snapshot_manager=AsyncMock(spec=FileSnapshotManager),
            journal=journal,  # type: ignore[arg-type]
            path_resolver=MagicMock(),
            event_bus=AsyncMock(spec=CoreEventBus),
            mo2_profile="Perfil-A",
        )
        runner = DynDOLODRunner(config)
        svc._runner = runner  # type: ignore[attr-defined]

        async def _proceso(*a: object, **k: object) -> tuple[str, str, int, float]:
            staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "a.dds").write_bytes(b"GEN-A-MARKER")
            log_path = config.dyndolod_exe.parent / "Logs" / "TexGen_SSE_log.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write("[00:10:00] TexGen completed successfully GEN-A\n")
            return "", "", 0, 1.0

        svc._runner._execute_process = _proceso  # type: ignore[attr-defined]
        svc._runner.run_dyndolod = AsyncMock()  # type: ignore[attr-defined]

        res = await asyncio.wait_for(svc.execute(preset="Medium", run_texgen=True, create_snapshot=True), timeout=60)
        await asyncio.wait_for(ev_liberado.wait(), timeout=10)

        filas: list[int] = []
        async with journal._db.execute(  # noqa: SLF001
            "SELECT handoff_id FROM deployment_handoffs WHERE source_tx_id = ?", (tx_a,)
        ) as cur:
            filas = [int(r[0]) for r in await cur.fetchall()]
        estado_row = None
        async with journal._db.execute(  # noqa: SLF001
            "SELECT status FROM transactions WHERE transaction_id = ?", (tx_a,)
        ) as cur:
            estado_row = await cur.fetchone()
        return res, filas, (str(estado_row[0]) if estado_row else None)
    finally:
        await journal.close()
        await mgr.close()


@pytest.mark.parametrize("inyeccion", ["digest", "crear"])
async def test_fallo_en_ventana_deja_estado_recuperable_sin_handoff_parcial(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, inyeccion: str
) -> None:
    """F2/F3/F4: un fallo dentro de la ventana certificadora (digest OSError o
    SQLite en el boundary) NO deja handoff parcial; la TX queda recuperable y
    el lease se libera."""
    res, handoffs_tx_a, estado = await _corrida_con_fallo_en_ventana(tmp_path, monkeypatch, inyeccion)
    assert res.get("success") is False
    assert handoffs_tx_a == [], f"handoff parcial tras fallo en ventana: {handoffs_tx_a}"
    assert estado in ("pending", "rolled_back"), f"TX no recuperable: {estado}"


# ---------------------------------------------------------------------------
# H1 (revisión adversarial) — ROBO same-agent_id: flag lease_lost LIMPIO pero
# la fila fue re-adquirida por un hermano (_RENEW_SQL no matchea token). Solo
# assert_owned(verify_db=True) —comparación por acquired_at— lo detecta:
# sin digest, sin handoff, TX PENDING.
# ---------------------------------------------------------------------------


async def test_robo_de_fila_same_agent_id_bloquea_certificacion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite

    import sky_claw.local.tools.dyndolod_service as dsm

    config = _entorno(tmp_path)
    artifact = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    clave = clave_de_artifact(artifact)
    ev_liberado = asyncio.Event()

    class _Mgr(DistributedLockManager):
        async def release_lock(self, resource_id: str, agent_id: str) -> bool:  # type: ignore[override]
            ok = await super().release_lock(resource_id=resource_id, agent_id=agent_id)
            if resource_id == "dyndolod-pipeline":
                ev_liberado.set()
            return ok

    mgr = _Mgr(tmp_path / "locks.db")
    await mgr.initialize()
    journal = OperationJournal(tmp_path / "journal.db")
    await journal.open()
    try:
        # Seed del viejo AWAITING (idéntico a F5).
        (artifact / "textures" / "sub").mkdir(parents=True, exist_ok=True)
        (artifact / "textures" / "a.dds").write_bytes(b"PREVIO")
        d_previo = digest_arbol(artifact / "textures")
        tx_old = await journal.begin_transaction("previa", agent_id="dyndolod-pipeline-service")
        await journal.crear_handoff_de_deployment(
            tx_old,
            descripcion="previa",
            registro=DeploymentHandoff(
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
            ),
        )
        tx_a = await journal.begin_transaction("corrida A", agent_id="dyndolod-pipeline-service")

        svc = DynDOLODPipelineService(
            lock_manager=mgr,
            snapshot_manager=AsyncMock(spec=FileSnapshotManager),
            journal=journal,  # type: ignore[arg-type]
            path_resolver=MagicMock(),
            event_bus=AsyncMock(spec=CoreEventBus),
            mo2_profile="Perfil-A",
        )
        runner = DynDOLODRunner(config)
        svc._runner = runner  # type: ignore[attr-defined]

        async def _proceso(*a: object, **k: object) -> tuple[str, str, int, float]:
            staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "a.dds").write_bytes(b"GEN-A-MARKER")
            log_path = config.dyndolod_exe.parent / "Logs" / "TexGen_SSE_log.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write("[00:10:00] TexGen completed successfully GEN-A\n")
            return "", "", 0, 1.0

        svc._runner._execute_process = _proceso  # type: ignore[attr-defined]
        svc._runner.run_dyndolod = AsyncMock()  # type: ignore[attr-defined]

        # EL ROBO: justo después de preservar el mod y ANTES de la ventana de
        # certificación, un "hermano" (mismo agent_id constante) re-adquiere la
        # fila con OTRO acquired_at. El flag en memoria queda limpio.
        original_preservar = svc._preservar_mod_de_texgen

        async def _preservar_y_robar(
            dir_rollbacks: list[object], objetivo: pathlib.Path, *, tx_id: int | None
        ) -> pathlib.Path | None:
            resultado = await original_preservar(dir_rollbacks, objetivo, tx_id=tx_id)
            async with aiosqlite.connect(tmp_path / "locks.db") as db:
                await db.execute(
                    "UPDATE resource_locks SET acquired_at = acquired_at + 1.0 WHERE resource_id = 'dyndolod-pipeline'"
                )
                await db.commit()
            return resultado

        monkeypatch.setattr(svc, "_preservar_mod_de_texgen", _preservar_y_robar)

        # El digest NO debe correr jamás: assert_owned corta antes.
        digest_llamadas: list[object] = []
        digest_original = dsm.digest_arbol

        def _digest_contado(ruta: object) -> object:
            digest_llamadas.append(ruta)
            return digest_original(ruta)

        monkeypatch.setattr(dsm, "digest_arbol", _digest_contado)

        res = await asyncio.wait_for(svc.execute(preset="Medium", run_texgen=True, create_snapshot=True), timeout=60)
        await asyncio.wait_for(ev_liberado.wait(), timeout=10)

        assert res.get("success") is False
        assert res.get("needs_deployment") is True  # el artifact existe (F5-semántica)
        assert digest_llamadas == [], "el digest corrió pese al robo de la fila"
        filas: list[int] = []
        async with journal._db.execute(  # noqa: SLF001
            "SELECT handoff_id FROM deployment_handoffs WHERE source_tx_id = ?", (tx_a,)
        ) as cur:
            filas = [int(r[0]) for r in await cur.fetchall()]
        assert filas == [], f"se firmó un handoff sobre bytes de otro dueño: {filas}"
        estado_row = None
        async with journal._db.execute(  # noqa: SLF001
            "SELECT status FROM transactions WHERE transaction_id = ?", (tx_a,)
        ) as cur:
            estado_row = await cur.fetchone()
        assert estado_row is not None and str(estado_row[0]) == "pending", (
            f"TX_A debe quedar recuperable (pending), está {estado_row}"
        )
    finally:
        await journal.close()
        await mgr.close()
