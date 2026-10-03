"""#655 / H2 (#592): un artifact TexGen RESTAURADO byte-exact no debe envenenar
la evidencia durable del resume — sin cerrar la TX global.

Contrato (aceptación de #655, refinado tras el P1 de #656):

- La TX global del pipeline SÍ puede quedar correctamente ``PENDING`` después de
  una mutación iniciada: ``Logs/`` del ejecutable, INI persistentes y ``temp_dir``
  son superficies mutables que NO están cubiertas por el inventario de
  ``DirectoryRollback``, así que ``mutation_coverage_complete`` queda en
  ``False`` y ``mark_transaction_rolled_back`` jamás corre por esa vía (I1).
- Lo que SÍ puede afirmarse es por ARTIFACT: si el ``DirectoryRollback`` del
  mod ``TexGen Output`` de ESTA transacción confirma su restauración
  (``rollback_completed=True``) con las leases intactas y sin preservación
  deliberada, la pareja ``(tx_id, artifact)`` recibe una resolución durable
  ``restored_byte_exact`` en ``artifact_evidence_resolutions`` (I2, I3, I6, I7).
- El oracle ``transacciones_que_nombran(<artifact>)`` excluye únicamente esa
  pareja resuelta: la TX sigue siendo evidencia VIVA para cualquier otro target
  que nombre su ActionManifest (I10), y ``reconciliar_orphan_de_artifact``
  deja de fabricar un ``HandoffIndeterminate`` por esa evidencia ya resuelta.

TODO el escenario corre con ``OperationJournal`` REAL sobre SQLite temporal y
rollback move-aside REAL de filesystem: no hay mock en la parte cuya semántica
durable cambia (el journal). Lo único fakeado es el proceso externo
(TexGen/DynDOLOD), que no es el contrato bajo prueba.

Reutiliza el harness de ``tests.test_dyndolod_handoff_durable`` (journal real,
lock/snapshot managers mocked, runner real con ``_execute_process`` fakeado).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from sky_claw.app.db.handoffs import (
    DeploymentHandoff,
    HandoffState,
    clave_de_artifact,
    reconciliar_orphan_de_artifact,
)
from sky_claw.app.db.journal import JournalTransactionError, OperationJournal, OperationType, TransactionStatus
from sky_claw.app.db.locks import SnapshotTransactionLock
from sky_claw.local.tools._dir_rollback import DirectoryRollback
from sky_claw.local.tools.artifact_digest import digest_arbol
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODPipelineResult,
    DynDOLODRunner,
    ReadinessMode,
    ToolExecutionResult,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService, _ResumeBloqueado
from sky_claw.local.tools.texgen_handoff import TexGenHandoffApproval, TexGenHandoffResult
from tests.test_dyndolod_handoff_durable import (
    _dyndolod_falla,
    _escribir_mod,
    _ProcesoFalso,
    _runner_real,
    _svc,
    _texgen_que_genera,
)
from tests.test_orphan_receipt_migration import _crear_db_655_histórica


@pytest.fixture
async def journal_tmp(tmp_path: pathlib.Path):  # noqa: ANN201
    """OperationJournal REAL sobre una DB temporal (standalone, sin lifecycle).

    Misma definición que la del resto de suites (test_dyndolod_handoff_durable,
    test_active_indeterminate_evidence, ...): la importa el fixture por nombre
    y el test la recibe como parámetro.
    """
    j = OperationJournal(tmp_path / "journal.db")
    await j.open()
    yield j, tmp_path / "journal.db"
    await j.close()


# =============================================================================
# Helpers de escenario
# =============================================================================


class _GateVerificado:
    """Spawn strategy de test: el gate VERIFICA el handoff TexGen→DynDOLOD (el
    artifact se considera visible en el namespace del consumidor), de modo que
    el pipeline REAL llega hasta la fase DynDOLOD —que el test reemplaza por
    un fallo—. ``spawn`` jamás se ejecuta: ``run_dyndolod`` es el fake.

    Mismo patrón que ``_GateQueBloquea`` de ``test_dyndolod_handoff_durable``.
    """

    async def verify_texgen_handoff(self, request: Any) -> TexGenHandoffResult:
        digest = await asyncio.to_thread(digest_arbol, request.staging)
        return TexGenHandoffResult.aprobado(
            "artifact visible en el namespace del consumidor (test)",
            approval=TexGenHandoffApproval(
                mod_name=request.mod_name,
                artifact_root=request.staging,
                artifact=digest,
                data_dir=request.data_dir,
            ),
        )

    async def spawn(self, **kwargs: Any) -> Any:
        raise AssertionError("spawn no debe ejecutarse: run_dyndolod es el fake del test")


def _runner_con_gate_verificado(config: Any) -> DynDOLODRunner:
    return DynDOLODRunner(
        config,
        readiness=ReadinessMode.DISABLED_FOR_TEST,
        spawn_strategy=_GateVerificado(),  # type: ignore[arg-type]
    )


async def _estado_de_tx(journal: OperationJournal, tx_id: int) -> TransactionStatus:
    tx = await journal.get_transaction(tx_id)
    assert tx is not None, f"TX {tx_id} debe existir"
    return tx.status


async def _txs_de(db_path: pathlib.Path) -> list[int]:
    async with (
        aiosqlite.connect(str(db_path)) as conn,
        conn.execute("SELECT transaction_id FROM transactions") as cur,
    ):
        return [int(fila[0]) async for fila in cur]


async def _tx_unica_de(db_path: pathlib.Path) -> int:
    """La corrida del service dejó EXACTAMENTE una TX en la DB del test."""
    txs = await _txs_de(db_path)
    assert len(txs) == 1, f"esperaba 1 TX en la corrida del servicio, hay {txs}"
    return txs[0]


async def _resolucion_de(
    journal: OperationJournal, tx_id: int, ruta: pathlib.Path | str
) -> tuple[str, int | None] | None:
    clave = clave_de_artifact(pathlib.Path(str(ruta)))
    async with (
        aiosqlite.connect(str(journal._db_path)) as conn,  # noqa: SLF001
        conn.execute(
            "SELECT resolution_kind, handoff_id FROM artifact_evidence_resolutions "
            "WHERE transaction_id = ? AND artifact_path = ?",
            (tx_id, clave),
        ) as cur,
    ):
        fila = await cur.fetchone()
    return None if fila is None else (str(fila[0]), int(fila[1]) if fila[1] is not None else None)


async def _resoluciones_de_tx(journal: OperationJournal, tx_id: int) -> list[tuple[str, str, int | None]]:
    async with (
        aiosqlite.connect(str(journal._db_path)) as conn,  # noqa: SLF001
        conn.execute(
            "SELECT artifact_path, resolution_kind, handoff_id "
            "FROM artifact_evidence_resolutions WHERE transaction_id = ? ORDER BY artifact_path",
            (tx_id,),
        ) as cur,
    ):
        return [(str(f[0]), str(f[1]), int(f[2]) if f[2] is not None else None) async for f in cur]


async def _correr_fallo_con_restauracion(
    tmp_path: pathlib.Path,
    journal: OperationJournal,
    *,
    create_snapshot: bool = True,
    runner: DynDOLODRunner | None = None,
) -> tuple[dict, DynDOLODPipelineService, pathlib.Path, DynDOLODRunner]:
    """Corrida canónica de #655: el mod ``TexGen Output`` EXISTE antes de la
    corrida (estado previo real que el move-aside debe restaurar), TexGen
    genera staging (empaquetado REAL), el gate de handoff verifica y
    DynDOLOD falla después de iniciada la mutación.

    ``runner`` puede preexistir (tests que lo parchean antes de la corrida);
    si no, se construye con el gate verificado sobre el config del harness.

    Devuelve (payload, service, mod_texgen, runner).
    """
    if runner is None:
        config, _runner_base = _runner_real(tmp_path)
        runner = _runner_con_gate_verificado(config)
    else:
        config = runner._config  # noqa: SLF001
    assert config.data_dir is not None and config.output_layout is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen, contenido=b"PRE-RUN")
    staging = config.output_layout.texgen_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
    svc = _svc(journal, runner=runner)
    with (
        patch.object(
            runner,
            "_execute_process",
            _ProcesoFalso(al_ejecutar=_texgen_que_genera(tmp_path, staging, marca=b"GEN-2")),
        ),
        patch.object(runner, "run_dyndolod", _dyndolod_falla()),
    ):
        result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=create_snapshot)
    return result, svc, mod_texgen, runner


def _clave_texgen(mod_texgen: pathlib.Path) -> str:
    return clave_de_artifact(mod_texgen)


# =============================================================================
# A — happy rollback específico: la evidencia del artifact restaurado se
#      resuelve durable, la TX global sigue PENDING y el resume no recibe un
#      HandoffIndeterminate fabricado por ESA evidencia.
# =============================================================================


@pytest.mark.asyncio
async def test_restauracion_byte_exacto_resuelve_evidencia_y_deja_tx_pendiente(
    tmp_path: pathlib.Path, journal_tmp
) -> None:  # noqa: ANN001
    """A (rojo antes del fix): fallo tras mutación + restore byte-exact del
    ``TexGen Output`` ⇒ resolución durable ``(tx, artifact)`` =
    ``restored_byte_exact``; TX PENDING (cobertura global incompleta); el oracle
    del artifact ya no ve a la TX; el resume NO fabrica HandoffIndeterminate."""
    journal, db_path = journal_tmp
    result, svc, mod_texgen, _runner = await _correr_fallo_con_restauracion(tmp_path, journal)

    assert result["success"] is False, "la corrida falla (DynDOLOD roto)"
    # I1 — la TX global NO puede marcarse ROLLED_BACK: Logs/INI/temp no están
    # inventariados por DirectoryRollback, aunque el restore del artifact cupiera.
    tx_id = await _tx_unica_de(db_path)
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING, (
        "la TX global debe permanecer PENDING: el restore del artifact no demuestra "
        "rollback de TODAS las superficies mutables (I1)"
    )
    # I3 — el restore fue byte-exact de verdad (move-aside real restauró el
    # estado previo, no el contenido empaquetado por la corrida fallida).
    assert (mod_texgen / "textures" / "a.dds").read_bytes() == b"PRE-RUN"

    # La resolución durable existe SOLO para la pareja (tx, TexGen Output).
    assert await _resolucion_de(journal, tx_id, mod_texgen) == ("restored_byte_exact", None), (
        "el artifact restaurado byte-exact debe recibir su resolución durable "
        "(#655): sin ella el oracle sigue viéndolo como evidencia viva"
    )

    # I10 — el oracle del artifact resuelto ya no reporta la TX…
    assert await journal.transacciones_que_nombran(_clave_texgen(mod_texgen)) == [], (
        "una resolución durable vigente debe excluir la pareja (tx, artifact) del oracle"
    )
    # …pero la TX SÍ sigue nombrando el mod en su manifiesto (la evidencia global
    # no se borra: solo cambia su clasificación per-artifact).
    cur = await journal._db.execute(  # noqa: SLF001
        "SELECT metadata FROM journal_entries WHERE transaction_id = ? AND metadata IS NOT NULL",
        (tx_id,),
    )
    filas = [fila async for fila in cur]
    clave_texgen = _clave_texgen(mod_texgen)
    manifests = [json.loads(str(fila[0])) for fila in filas]
    assert any(
        isinstance(metadata, dict)
        and any(
            isinstance(ruta, str) and clave_de_artifact(pathlib.Path(ruta)) == clave_texgen
            for ruta in metadata.get("files_touched", [])
        )
        for metadata in manifests
    ), "el ActionManifest sigue nombrando semánticamente el mod: la resolución no reescribe historia"

    # I10 — el resume (run_texgen=False) no recibe HandoffIndeterminate fabricado
    # por esa evidencia: sin handoff activo y sin evidencia vigente → legacy.
    consulta = await svc._consultar_resume(_runner)  # noqa: SLF001
    assert consulta is None, f"el resume no debe bloquearse por evidencia ya resuelta; obtuvo: {consulta!r}"


@pytest.mark.asyncio
async def test_evidencia_hermana_sigue_viva_en_el_mismo_tx(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """G: en la MISMA TX, ``TexGen Output`` resuelto + ``DynDOLOD Output`` (otro
    target del mismo ActionManifest) SIN resolver. Consultar ``TexGen Output``
    ignora solo la pareja resuelta; consultar ``DynDOLOD Output`` sigue viendo
    la TX. La resolución de un artifact jamás resuelve la TX entera ni a sus
    hermanos (I2)."""
    journal, _db_path = journal_tmp
    _result, _svc, mod_texgen, runner = await _correr_fallo_con_restauracion(tmp_path, journal)
    config = runner._config  # noqa: SLF001
    mod_dyn = config.mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME

    tx_id = await _tx_unica_de(journal._db_path)  # noqa: SLF001
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING

    assert await _resolucion_de(journal, tx_id, mod_texgen) == ("restored_byte_exact", None)
    # J: la resolución de TexGen Output NO resuelve al artifact hermana.
    assert await _resolucion_de(journal, tx_id, mod_dyn) is None, (
        "resolver (tx, TexGen Output) no puede resolver (tx, DynDOLOD Output)"
    )
    # Consultar TexGen Output → la pareja resuelta queda excluida…
    assert tx_id not in await journal.transacciones_que_nombran(_clave_texgen(mod_texgen))
    # …consultar DynDOLOD Output → la TX sigue siendo evidencia viva.
    assert tx_id in await journal.transacciones_que_nombran(clave_de_artifact(mod_dyn)), (
        "la evidencia del target hermana (sin resolver) debe seguir visible"
    )
    # Y el staging crudo de TexGen (otro target nombrado, sin resolución) igual.
    assert tx_id in await journal.transacciones_que_nombran(clave_de_artifact(config.output_layout.texgen_root))


# =============================================================================
# B — restart real: la resolución sobrevive a cerrar y reabrir la DB.
# =============================================================================


@pytest.mark.asyncio
async def test_resolucion_durable_sobrevive_a_restart_real(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """I9: cerrar la instancia del journal y abrir UNA NUEVA sobre la misma DB
    debe seguir mostrando (a) la TX PENDING, (b) la resolución durable y
    (c) el resume sin HandoffIndeterminate fabricado."""
    journal, db_path = journal_tmp
    _result, _service, mod_texgen, runner = await _correr_fallo_con_restauracion(tmp_path, journal)
    tx_id = await _tx_unica_de(db_path)
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING
    assert await _resolucion_de(journal, tx_id, mod_texgen) == ("restored_byte_exact", None)

    # Cierre REAL de la conexión/instancia.
    await journal.close()

    j2 = OperationJournal(db_path)
    await j2.open()
    try:
        assert await _estado_de_tx(j2, tx_id) is TransactionStatus.PENDING, (
            "el restart no debe cambiar el estado de la TX"
        )
        assert await _resolucion_de(j2, tx_id, mod_texgen) == ("restored_byte_exact", None), (
            "la resolución debe ser DURABLE: sobrevive a cerrar y reabrir el journal"
        )
        assert tx_id not in await j2.transacciones_que_nombran(_clave_texgen(mod_texgen))
        svc2 = _svc(j2, runner=runner)
        assert await svc2._consultar_resume(runner) is None  # noqa: SLF001
    finally:
        await j2.close()


# =============================================================================
# C — restore fallido: rollback_completed=False ⇒ SIN resolución; la evidencia
#      sigue viva y el resume falla cerrado.
# =============================================================================


@pytest.mark.asyncio
async def test_restore_fallido_no_resuelve_evidencia(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """I5: si el ``DirectoryRollback`` del artifact NO confirma la restauración
    (``rollback_completed=False``), no hay resolución: el oracle sigue
    reportando la TX y el resume falla cerrado (fail-closed, no false-green)."""
    journal, db_path = journal_tmp
    config, _runner_base = _runner_real(tmp_path)
    runner = _runner_con_gate_verificado(config)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME

    restore_original = DirectoryRollback._restore_backup

    async def _restore_roto(self: DirectoryRollback) -> None:
        if self.target == mod_texgen:
            raise OSError("restore simulado fallido del TexGen Output")
        await restore_original(self)

    with patch.object(DirectoryRollback, "_restore_backup", _restore_roto):
        _result, svc, _mod, _ = await _correr_fallo_con_restauracion(tmp_path, journal, runner=runner)

    tx_id = await _tx_unica_de(db_path)
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING
    assert await _resolucion_de(journal, tx_id, mod_texgen) is None, (
        "rollback_completed=False no autoriza NINGUNA resolución del artifact"
    )
    assert tx_id in await journal.transacciones_que_nombran(_clave_texgen(mod_texgen)), (
        "con el restore fallido la evidencia debe seguir viva"
    )
    consulta = await svc._consultar_resume(runner)  # noqa: SLF001
    assert isinstance(consulta, _ResumeBloqueado) and consulta.reason == "HandoffIndeterminate", (
        f"restore fallido ⇒ resume fail-closed; obtuvo: {consulta!r}"
    )


# =============================================================================
# D — lease perdida: el veto omite el restore ⇒ rollback_completed=False ⇒
#      SIN resolución; evidencia viva; resume fail-closed.
# =============================================================================


@pytest.mark.asyncio
async def test_lease_perdida_no_resuelve_evidencia(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """I4: con la lease del lock perdida, el ``DirectoryRollback`` omite el
    restore (no pisar al dueño concurrente) y la corrida no puede afirmar
    restauración: SIN resolución, evidencia viva, resume fail-closed."""
    journal, db_path = journal_tmp
    config, _runner_base = _runner_real(tmp_path)
    runner = _runner_con_gate_verificado(config)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME

    with patch.object(SnapshotTransactionLock, "lease_lost", property(lambda self: True)):
        _result, svc, _mod, _ = await _correr_fallo_con_restauracion(tmp_path, journal, runner=runner)

    tx_id = await _tx_unica_de(db_path)
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING
    assert await _resolucion_de(journal, tx_id, mod_texgen) is None, (
        "lease perdida ⇒ el restore se omite ⇒ no hay nada que afirmar"
    )
    assert tx_id in await journal.transacciones_que_nombran(_clave_texgen(mod_texgen))
    consulta = await svc._consultar_resume(runner)  # noqa: SLF001
    assert isinstance(consulta, _ResumeBloqueado) and consulta.reason == "HandoffIndeterminate", (
        f"lease perdida ⇒ resume fail-closed; obtuvo: {consulta!r}"
    )


# =============================================================================
# E — artifact preservado para deployment: SIN resolución de restore; la TX
#      queda PENDING y la evidencia/handoff sigue viva.
# =============================================================================


@pytest.mark.asyncio
async def test_artifact_preservado_no_recibe_resolucion(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """I6: con ``needs_deployment`` el protector del mod se CONFIRMA a
    propósito (``commit()`` lo sella; ``rollback_completed`` queda False). Si
    además la certificación durable falla (F-2), la TX queda PENDING con el
    artifact VIVO en disco: no debe existir NINGUNA resolución de restore para
    esa pareja, y el reconciler debe seguir pudiendo materializar el
    INDETERMINATE (evidencia viva)."""
    journal, db_path = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_layout is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME

    def _pipeline_que_empaqueta_y_corta(*_args: object, **_kwargs: object) -> DynDOLODPipelineResult:
        raiz = mod_texgen / "textures"
        raiz.mkdir(parents=True, exist_ok=True)
        (raiz / "a.dds").write_bytes(b"GEN-PRESERVADO")
        return DynDOLODPipelineResult(
            success=False,
            texgen_result=ToolExecutionResult(True, "TexGen", 0, "", ""),
            dyndolod_result=ToolExecutionResult(False, "DynDOLOD", 1, "", "", errors=["corte"]),
            errors=["DynDOLOD pipeline failed: corte"],
            needs_deployment=True,
            handoff_action="physical_deployment",
            texgen_packaging_attempted=True,
        )

    svc = _svc(journal, runner=runner)
    with (
        patch.object(runner, "run_full_pipeline", AsyncMock(side_effect=_pipeline_que_empaqueta_y_corta)),
        patch.object(
            journal,
            "crear_handoff_de_deployment",
            AsyncMock(side_effect=JournalTransactionError("certificación simulada fallida")),
        ),
    ):
        _result = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)

    tx_id = await _tx_unica_de(db_path)
    # I6 — la mutación preservada y viva mantiene la TX PENDING…
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING
    # …el artifact sigue en disco (no fue restaurado: se preservó a propósito)…
    assert (mod_texgen / "textures" / "a.dds").read_bytes() == b"GEN-PRESERVADO"
    # …y NINGUNA resolución de restore existe para esa pareja.
    assert await _resolucion_de(journal, tx_id, mod_texgen) is None, (
        "un artifact PRESERVADO a propósito jamás se marca RESTORED"
    )
    # La evidencia sigue viva: reconciliar materializa el INDETERMINATE y el
    # resume falla cerrado.
    game_key, mods_root_key, data_key = (
        clave_de_artifact(config.game_path),
        clave_de_artifact(config.mo2_mods_path),
        clave_de_artifact(config.data_dir),
    )
    await reconciliar_orphan_de_artifact(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key=game_key,
        mods_root_key=mods_root_key,
        data_key=data_key,
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )
    activo = await journal.consultar_handoff_activo(_clave_texgen(mod_texgen))
    assert activo is not None and activo.state is HandoffState.INDETERMINATE, (
        "artifact preservado vivo + TX PENDING ⇒ el reconciler debe poder "
        "materializar el INDETERMINATE (evidencia viva)"
    )
    consulta = await svc._consultar_resume(runner)  # noqa: SLF001
    assert isinstance(consulta, _ResumeBloqueado) and consulta.reason == "HandoffIndeterminate"


# =============================================================================
# F — create_snapshot=False: no existe move-aside del mod ⇒ no hay prueba de
#      restauración ⇒ SIN resolución inventada.
# =============================================================================


@pytest.mark.asyncio
async def test_create_snapshot_false_no_fabrica_resolucion(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """I7: con ``create_snapshot=False`` el mod ``TexGen Output`` nunca entró al
    inventario de ``DirectoryRollback``: la corrida fallida puede haberlo
    dejado empaquetado/sucio y nadie puede afirmar restauración. La resolución
    no se inventa; la evidencia sigue viva y el resume falla cerrado."""
    journal, db_path = journal_tmp
    config, _runner_base = _runner_real(tmp_path)
    runner = _runner_con_gate_verificado(config)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _result, svc, _mod, _ = await _correr_fallo_con_restauracion(
        tmp_path, journal, create_snapshot=False, runner=runner
    )

    tx_id = await _tx_unica_de(db_path)
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING
    assert await _resolucion_de(journal, tx_id, mod_texgen) is None, (
        "create_snapshot=False ⇒ sin move-aside del mod ⇒ sin prueba de restore"
    )
    assert tx_id in await journal.transacciones_que_nombran(_clave_texgen(mod_texgen)), (
        "sin snapshot no hay nada que resolver: la evidencia sigue viva"
    )
    consulta = await svc._consultar_resume(runner)  # noqa: SLF001
    assert isinstance(consulta, _ResumeBloqueado) and consulta.reason == "HandoffIndeterminate", (
        f"sin snapshot el resume debe fallar cerrado; obtuvo: {consulta!r}"
    )


# =============================================================================
# H — un INDETERMINATE legítimo ANTERIOR sigue bloqueando: resolver la
#      evidencia de una TX posterior fallida no borra el handoff viejo.
# =============================================================================


@pytest.mark.asyncio
async def test_indeterminate_legitimo_anterior_sigue_bloqueando(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """I10: un INDETERMINATE legítimo materializado por el reconciler a partir
    de evidencia anterior NO lo toca la resolución de una corrida posterior
    fallida: el resume sigue fallando cerrado por ese handoff."""
    journal, db_path = journal_tmp
    config, _runner_base = _runner_real(tmp_path)
    runner = _runner_con_gate_verificado(config)
    assert config.data_dir is not None and config.output_layout is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME

    # Sembrar evidencia legítima de una corrida previa muerta (TX PENDING que
    # nombra el mod + mod vivo) y dejar que el reconciler de arranque
    # materialice el INDETERMINATE — exactamente como lo haría en producción.
    tx_previa = await journal.begin_transaction("corrida previa muerta", agent_id="test")
    await journal.begin_operation(
        agent_id="test",
        operation_type=OperationType.FILE_MODIFY,
        target_path=str(mod_texgen),
        transaction_id=tx_previa,
        metadata={"files_touched": [str(mod_texgen)]},
    )
    _escribir_mod(mod_texgen, contenido=b"LEGITIMA!")
    game_key, mods_root_key, data_key = (
        clave_de_artifact(config.game_path),
        clave_de_artifact(config.mo2_mods_path),
        clave_de_artifact(config.data_dir),
    )
    previo = await reconciliar_orphan_de_artifact(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key=game_key,
        mods_root_key=mods_root_key,
        data_key=data_key,
        expected_profile="Perfil-A",
        digest_arbol=digest_arbol,
    )
    assert previo is not None and isinstance(previo, DeploymentHandoff)
    assert previo.state is HandoffState.INDETERMINATE, "la evidencia legítima materializa el INDETERMINATE"
    handoff_previo = previo

    # Corrida posterior que falla con restauración byte-exact del mod.
    _result, svc, _mod, _runner_post = await _correr_fallo_con_restauracion(tmp_path, journal, runner=runner)
    txs = await _txs_de(db_path)
    assert tx_previa in txs and len(txs) == 2, f"esperaba la TX previa + la de la corrida: {txs}"
    tx_nueva = max(t for t in txs if t != tx_previa)
    # La corrida posterior resuelve su pareja, pero el handoff previo NO se toca.
    assert await _resolucion_de(journal, tx_nueva, mod_texgen) == ("restored_byte_exact", None)

    consulta = await svc._consultar_resume(_runner_post)  # noqa: SLF001
    assert isinstance(consulta, _ResumeBloqueado) and consulta.reason == "HandoffIndeterminate", (
        f"el INDETERMINATE legítimo anterior sigue bloqueando; obtuvo: {consulta!r}"
    )
    activo = await journal.consultar_handoff_activo(_clave_texgen(mod_texgen))
    assert activo is not None and activo.state is HandoffState.INDETERMINATE
    assert activo.handoff_id == handoff_previo.handoff_id, (
        "resolviendo la evidencia de la TX posterior NO se tocó el handoff previo"
    )


# =============================================================================
# I — idempotencia: dos escrituras exactas de la misma resolución no duplican
#      evidencia ni rompen la DB; un conflicto de provenance sí falla cerrado.
# =============================================================================


@pytest.mark.asyncio
async def test_idempotencia_y_conflicto_de_resolucion(journal_tmp) -> None:  # noqa: ANN001
    """I: re-registrar la MISMA (tx, artifact, restored_byte_exact) es
    idempotente (1 fila); registrar una provenance DISTINTA para la misma
    pareja levanta JournalTransactionError y no toca la fila existente."""
    journal, _db_path = journal_tmp
    mod = pathlib.Path("X:/MO2/mods/TexGen Output")
    tx = await journal.begin_transaction("idempotencia", agent_id="test")

    await journal.registrar_resolucion_de_restauracion_de_artifact(transaction_id=tx, artifact_path=str(mod))
    await journal.registrar_resolucion_de_restauracion_de_artifact(transaction_id=tx, artifact_path=str(mod))
    filas = await _resoluciones_de_tx(journal, tx)
    assert len(filas) == 1, f"la doble escritura no debe duplicar evidencia: {filas}"
    assert filas[0][1] == "restored_byte_exact"
    assert filas[0][2] is None

    # Conflicto de provenance semántica: sembrar en crudo OTRA resolución para
    # la misma pareja (absorbed_by_handoff + handoff) y reintentar el kind
    # nuevo: el helper canónico debe fallar cerrado y no tocar la fila ajena.
    clave = clave_de_artifact(mod)
    tx2 = await journal.begin_transaction("conflicto", agent_id="test")
    async with aiosqlite.connect(str(journal._db_path)) as conn:  # noqa: SLF001
        await conn.execute(
            "INSERT INTO deployment_handoffs (handoff_id, source_tx_id, state, artifact_path, "
            "game_key, mods_root_key, data_key, expected_profile, expected_digest, expected_files, expected_bytes) "
            "VALUES (99, ?, 'awaiting_deployment', ?, 'g', 'm', 'd', 'p', 'sha256:x', 1, 1)",
            (tx2, clave),
        )
        await conn.execute(
            "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
            "VALUES (?, ?, 'absorbed_by_handoff', 99)",
            (tx2, clave),
        )
        await conn.commit()
    with pytest.raises(JournalTransactionError, match="Conflicto de resolución"):
        await journal.registrar_resolucion_de_restauracion_de_artifact(transaction_id=tx2, artifact_path=str(mod))
    filas2 = await _resoluciones_de_tx(journal, tx2)
    assert filas2 == [(clave, "absorbed_by_handoff", 99)], (
        "el conflicto no debe sobrescribir la resolución existente: " + repr(filas2)
    )


# =============================================================================
# API/esquema del journal para el kind nuevo
# =============================================================================


@pytest.mark.asyncio
async def test_esquema_admite_restored_byte_exact_con_handoff_nulo(journal_tmp) -> None:  # noqa: ANN001
    """El kind nuevo exige ``handoff_id IS NULL`` (provenance pipeline, no
    handoff): la CHECK de provenance lo acepta sin handoff y lo rechaza con él,
    y el writer público (que nunca expone handoff_id) lo escribe y lo persiste."""
    journal, _db_path = journal_tmp
    tx = await journal.begin_transaction("esquema", agent_id="test")
    clave = clave_de_artifact(pathlib.Path("X:/MO2/mods/TexGen Output"))

    # Provenance inválida: handoff_id no nulo ⇒ CHECK fail-closed en el schema.
    async with aiosqlite.connect(str(journal._db_path)) as conn:  # noqa: SLF001
        with pytest.raises(sqlite3.IntegrityError, match="chk_resolution_provenance"):
            await conn.execute(
                "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind, handoff_id) "
                "VALUES (?, ?, 'restored_byte_exact', 7)",
                (tx, clave),
            )

    await journal.registrar_resolucion_de_restauracion_de_artifact(
        transaction_id=tx, artifact_path="X:/MO2/mods/TexGen Output"
    )
    assert await _resolucion_de(journal, tx, "X:/MO2/mods/TexGen Output") == ("restored_byte_exact", None)


@pytest.mark.asyncio
async def test_db_antigua_migra_y_admite_restored_byte_exact(tmp_path: pathlib.Path) -> None:
    """Upgrade durable (transformación del test fail-closed original): una DB
    creada por el schema inmediatamente anterior a este PR (CHECK histórica de
    3 kinds + filas previas) se MIGRA en el open() y la escritura del kind
    nuevo pasa a funcionar — el objetivo funcional de #655 para upgrades: la
    evidencia restaurada deja de ser fabricadora de INDETERMINATE. Las filas
    históricas no se tocan y no queda remanente de la reconstrucción.

    (El comportamiento fail-closed original —DB histórica sin migración— quedó
    cubierto por los tests MIG655 de test_orphan_receipt_migration.py: forma
    desconocida y kinds desconocidos siguen fallando cerrado.)
    """
    db_file = tmp_path / "vieja.db"
    # DB real de la release anterior: schema completo + tabla con la CHECK
    # histórica de 3 kinds y dos filas previas (provenance históricas).
    await _crear_db_655_histórica(
        db_file,
        filas=[
            (3, "C:/MO2/mods/Antiguo", "absorbed_by_handoff", 31, "2026-01-01 10:00:00"),
            (5, "C:/MO2/mods/Otro", "no_artifact_demonstrated", None, "2026-02-01 11:30:00"),
        ],
    )

    j = OperationJournal(db_file)
    await j.open()
    try:
        tx = await j.begin_transaction("db vieja", agent_id="test")
        await j.registrar_resolucion_de_restauracion_de_artifact(
            transaction_id=tx, artifact_path="X:/MO2/mods/TexGen Output"
        )
        # La escritura nueva convivió con la historia sin corromperla.
        async with (
            aiosqlite.connect(str(db_file)) as conn,
            conn.execute(
                "SELECT resolution_id, transaction_id, artifact_path, resolution_kind, handoff_id, resolved_at "
                "FROM artifact_evidence_resolutions ORDER BY resolution_id"
            ) as cur,
        ):
            filas = [tuple(f) async for f in cur]
        assert filas[:2] == [
            (3, 3, "C:/MO2/mods/Antiguo", "absorbed_by_handoff", 31, "2026-01-01 10:00:00"),
            (5, 5, "C:/MO2/mods/Otro", "no_artifact_demonstrated", None, "2026-02-01 11:30:00"),
        ], "la migración mutó la historia durable"
        assert len(filas) == 3
        assert filas[2][3] == "restored_byte_exact" and filas[2][4] is None
        # El kind nuevo sigue siendo único por pareja (UNIQUE sobreviviente).
        # El path va canonicalizado: el writer guardó la pareja con la misma
        # identidad física del oracle (F-002), no el literal del caller.
        clave_nueva = clave_de_artifact(pathlib.Path("X:/MO2/mods/TexGen Output"))
        async with aiosqlite.connect(str(db_file)) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
                await conn.execute(
                    "INSERT INTO artifact_evidence_resolutions (transaction_id, artifact_path, resolution_kind) "
                    "VALUES (?, ?, 'no_artifact_demonstrated')",
                    (tx, clave_nueva),
                )
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_upgrade_db_vieja_scenario_655_completo(tmp_path: pathlib.Path) -> None:
    """Upgrade + escenario #655 completo: sobre una DB de la release anterior,
    una corrida canónica que restaura ``TexGen Output`` byte-exact deja la
    resolución durable en la DB MIGRADA; tras close/reopen real el oracle deja
    de ver a la TX y el resume NO recibe HandoffIndeterminate fabricado — sin
    que el usuario tenga que borrar su journal."""
    db_file = tmp_path / "upgrade.db"
    await _crear_db_655_histórica(
        db_file,
        filas=[(11, "C:/MO2/mods/Historico", "superseded_by_run", 101, "2025-06-01 00:00:00")],
    )
    config, _runner_base = _runner_real(tmp_path)
    runner = _runner_con_gate_verificado(config)

    j = OperationJournal(db_file)
    await j.open()  # ← aquí corre la migración del esquema #655
    try:
        result, svc, mod_texgen, _r = await _correr_fallo_con_restauracion(tmp_path, j, runner=runner)
        assert result["success"] is False, "la corrida falla (DynDOLOD roto)"
        # La DB trae historia (TX 11): la TX de la corrida es la más nueva.
        tx_id = max(await _txs_de(db_file))
        assert await _estado_de_tx(j, tx_id) is TransactionStatus.PENDING, (
            "tras el upgrade la TX global sigue PENDING (I1)"
        )
        assert await _resolucion_de(j, tx_id, mod_texgen) == ("restored_byte_exact", None), (
            "la corrida post-upgrade debe dejar la resolución durable del restore"
        )
    finally:
        await j.close()

    # Restart real sobre la DB migrada: la resolución sobrevive, el oracle la
    # excluye y el resume no fabrica INDETERMINATE.
    j2 = OperationJournal(db_file)
    await j2.open()
    try:
        assert await j2.transacciones_que_nombran(_clave_texgen(mod_texgen)) == [], (
            "post-restart la pareja resuelta debe seguir excluida del oracle"
        )
        # La historia pre-upgrade también sigue viva e íntegra (fila durable).
        async with (
            aiosqlite.connect(str(db_file)) as conn,
            conn.execute(
                "SELECT resolution_id, transaction_id, artifact_path, resolution_kind, handoff_id, resolved_at "
                "FROM artifact_evidence_resolutions WHERE transaction_id = 11"
            ) as cur,
        ):
            filas_historicas = [tuple(f) async for f in cur]
        assert filas_historicas == [
            (11, 11, "C:/MO2/mods/Historico", "superseded_by_run", 101, "2025-06-01 00:00:00")
        ], "la migración del upgrade mutó la historia durable"
        svc2 = _svc(j2, runner=runner)
        consulta = await svc2._consultar_resume(runner)  # noqa: SLF001
        assert consulta is None, (
            f"el resume post-upgrade/restart no debe bloquearse por la evidencia ya resuelta; obtuvo: {consulta!r}"
        )
    finally:
        await j2.close()


# =============================================================================
# Guard: el kind nuevo no debe filtrar la evidencia de OTRA TX de la misma
# corrida ni de un run_texgen=False (donde el mod no se muta ni se nombra).
# =============================================================================


@pytest.mark.asyncio
async def test_run_texgen_false_no_resuelve_ni_nombrea_el_mod(tmp_path: pathlib.Path, journal_tmp) -> None:  # noqa: ANN001
    """Con ``run_texgen=False`` el ActionManifest no nombra ``TexGen Output`` y
    su mod ni siquiera entra al inventario de rollback: una corrida fallida de
    DynDOLOD-solo no puede fabricar (ni necesita) resolución de ese artifact."""
    journal, db_path = journal_tmp
    config, runner = _runner_real(tmp_path)
    assert config.data_dir is not None and config.output_layout is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    mod_texgen = config.mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
    _escribir_mod(mod_texgen, contenido=b"PRE-RUN")

    staging_dyn = config.output_layout.dyndolod_root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME

    def _dyndolod_solo_falla() -> None:
        staging_dyn.mkdir(parents=True, exist_ok=True)
        (staging_dyn / "parcial.esp").write_bytes(b"PARCIAL")

    svc = _svc(journal, runner=runner)
    with (
        patch.object(runner, "_execute_process", _ProcesoFalso(al_ejecutar=_dyndolod_solo_falla)),
        patch.object(runner, "run_dyndolod", _dyndolod_falla()),
    ):
        _result = await svc.execute(preset="Medium", run_texgen=False, create_snapshot=True)

    tx_id = await _tx_unica_de(db_path)
    assert await _estado_de_tx(journal, tx_id) is TransactionStatus.PENDING
    assert await _resolucion_de(journal, tx_id, mod_texgen) is None, (
        "run_texgen=False no muta el mod TexGen Output: no hay resolución que fabricar"
    )
    # El manifiesto de esta corrida no nombra al mod.
    assert tx_id not in await journal.transacciones_que_nombran(_clave_texgen(mod_texgen))
