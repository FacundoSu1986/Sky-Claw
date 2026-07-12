"""Tests for ChainPreviewService — the dry-run of the LOOT->xEdit->DynDOLOD chain.

The headline guarantee is **zero permanent mutation**: the chain runs for real
inside a single ``SnapshotTransactionLock(force_rollback=True)`` and every target
file is byte-identical afterwards.  We also verify rollback on a mid-stage crash
and clean teardown (no orphan lock) on cancellation.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.orchestrator.preview.chain_preview_service import ChainPreviewService
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
from sky_claw.local.xedit.conflict_analyzer import (
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
)
from sky_claw.local.xedit.flag_rules import FlagAlert

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolver_without_paths() -> MagicMock:
    """Path resolver with no configured tool paths (services' dry_run needs no binary)."""
    resolver = MagicMock()
    for getter in (
        "get_skyrim_path",
        "get_mo2_path",
        "get_mo2_mods_path",
        "get_dyndolod_exe",
        "get_texgen_exe",
        "get_xedit_path",
    ):
        setattr(resolver, getter, MagicMock(return_value=None))
    return resolver


def _critical_report() -> ConflictReport:
    return ConflictReport(
        total_conflicts=1,
        critical_conflicts=1,
        plugin_pairs=[
            PluginConflictPair(
                plugin_a="A.esm",
                plugin_b="B.esp",
                conflicts=[
                    RecordConflict(
                        form_id="00000001",
                        editor_id="NpcX",
                        record_type="NPC_",
                        winner="A.esm",
                        losers=["B.esp"],
                        severity="critical",
                    ),
                ],
            ),
        ],
    )


async def _build_service(
    tmp_path: pathlib.Path,
    *,
    loot_sort: Callable[[], Awaitable[LOOTResult]],
    analyze: Callable[..., Awaitable[ConflictReport]],
    event_bus: AsyncMock,
) -> tuple[ChainPreviewService, DistributedLockManager]:
    lock_mgr = DistributedLockManager(tmp_path / "locks.db", default_ttl=10.0)
    await lock_mgr.initialize()
    snap_mgr = FileSnapshotManager(snapshot_dir=tmp_path / "snaps")
    await snap_mgr.initialize()

    loot_runner = MagicMock()
    loot_runner.sort = AsyncMock(side_effect=loot_sort)

    analyzer = MagicMock()
    analyzer.validate_load_order_limit = MagicMock(return_value=None)
    analyzer.analyze = AsyncMock(side_effect=analyze)

    journal = AsyncMock()

    service = ChainPreviewService(
        lock_manager=lock_mgr,
        snapshot_manager=snap_mgr,
        journal=journal,
        path_resolver=_resolver_without_paths(),
        path_validator=PathValidator(roots=[tmp_path]),
        event_bus=event_bus,
        loot_runner=loot_runner,
        xedit_runner=MagicMock(),
        conflict_analyzer=analyzer,
    )
    return service, lock_mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_chain_no_mutation_and_full_manifest(tmp_path: pathlib.Path) -> None:
    """The star test: the chain runs for real but every target file is reverted.

    LOOT rewrites plugins.txt inside the transaction; on exit force_rollback
    restores it byte-for-byte.  The manifest still captures the would-be diff,
    conflicts, and LOD plan.
    """
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*B.esp\n*A.esm\n", encoding="utf-8")
    original_bytes = plugins_txt.read_bytes()

    async def loot_sort() -> LOOTResult:
        # LOOT reorders AND writes the file (the mutation we must revert).
        plugins_txt.write_text("*A.esm\n*B.esp\n", encoding="utf-8")
        return LOOTResult(
            return_code=0,
            sorted_plugins=["A.esm", "B.esp"],
            warnings=["LOOT moved 2 plugins"],
        )

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        manifest = await service.preview_chain(
            workflow_id="wf-1",
            load_order_file=plugins_txt,
            target_plugin=pathlib.Path("SkyClaw_Patch.esp"),
            dyndolod_preset="High",
            run_texgen=True,
        )

        # --- ZERO permanent mutation: byte-identical after the preview. ---
        assert plugins_txt.read_bytes() == original_bytes

        # --- Manifest captured the full chain. ---
        assert manifest.workflow_id == "wf-1"
        assert manifest.stage_names() == ["loot", "xedit", "dyndolod"]

        loot_stage = manifest.stages[0]
        assert loot_stage.executed_for_real is True
        assert loot_stage.load_order_diff is not None
        assert loot_stage.load_order_diff.before == ["B.esp", "A.esm"]
        assert loot_stage.load_order_diff.after == ["A.esm", "B.esp"]

        xedit_stage = manifest.stages[1]
        assert xedit_stage.executed_for_real is False
        assert xedit_stage.conflicts is not None
        assert xedit_stage.conflicts.critical == 1
        # T-20·2: proposed_resolution se reconcilia con la recomendación del
        # asistente (NPC_ crítico -> xedit_manual), no el coarse "execute_xedit_script".
        assert xedit_stage.conflicts.proposed_resolution == "xedit_manual"

        dyndolod_stage = manifest.stages[2]
        assert dyndolod_stage.executed_for_real is False
        assert dyndolod_stage.lod_plan is not None
        assert dyndolod_stage.lod_plan.preset == "High"

        # Lock released, manifest published for the Operations Hub.
        assert await lock_mgr.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None
        event_bus.publish.assert_awaited()
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_reverts_when_a_stage_crashes(tmp_path: pathlib.Path) -> None:
    """If xEdit scan crashes after LOOT wrote the order, the file is restored."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*B.esp\n*A.esm\n", encoding="utf-8")
    original_bytes = plugins_txt.read_bytes()

    async def loot_sort() -> LOOTResult:
        plugins_txt.write_text("*A.esm\n*B.esp\n", encoding="utf-8")
        return LOOTResult(return_code=0, sorted_plugins=["A.esm", "B.esp"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        raise RuntimeError("xEdit scan exploded")

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        with pytest.raises(RuntimeError, match="xEdit scan exploded"):
            await service.preview_chain(workflow_id="wf-2", load_order_file=plugins_txt)

        # Load order restored despite LOOT having rewritten it before the crash.
        assert plugins_txt.read_bytes() == original_bytes
        assert await lock_mgr.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_cancellation_leaves_no_orphan(tmp_path: pathlib.Path) -> None:
    """Cancelling mid-preview reverts the file and leaves no orphan lock."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*B.esp\n*A.esm\n", encoding="utf-8")
    original_bytes = plugins_txt.read_bytes()

    async def loot_sort() -> LOOTResult:
        plugins_txt.write_text("*A.esm\n*B.esp\n", encoding="utf-8")
        return LOOTResult(return_code=0, sorted_plugins=["A.esm", "B.esp"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        await asyncio.sleep(10)  # cancelled here, after LOOT mutated the file
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        task = asyncio.create_task(service.preview_chain(workflow_id="wf-3", load_order_file=plugins_txt))
        await asyncio.sleep(0.1)  # let LOOT run and the scan begin
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert plugins_txt.read_bytes() == original_bytes
        assert await lock_mgr.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_fails_fast_when_load_order_missing(tmp_path: pathlib.Path) -> None:
    """If the load-order file does not exist, preview must fail fast.

    Otherwise the transaction can't snapshot it, LOOT could create it, and
    force_rollback would not remove the new file → no-mutation violated.
    """
    missing = tmp_path / "does_not_exist.txt"

    async def loot_sort() -> LOOTResult:
        return LOOTResult(return_code=0, sorted_plugins=[])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        with pytest.raises(FileNotFoundError):
            await service.preview_chain(workflow_id="wf-missing", load_order_file=missing)

        # Nothing ran: no LOOT, no event, no orphan lock.
        service._loot_runner.sort.assert_not_awaited()
        event_bus.publish.assert_not_awaited()
        assert await lock_mgr.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_chain_event_payload_has_id(tmp_path: pathlib.Path) -> None:
    """The published ops.hitl.preview event must carry an `id` so the Operations
    Hub router enqueues it (it drops HITL events lacking id/conflict_id)."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*A.esm\n", encoding="utf-8")

    async def loot_sort() -> LOOTResult:
        return LOOTResult(return_code=0, sorted_plugins=["A.esm"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return _critical_report()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        await service.preview_chain(workflow_id="wf-id", load_order_file=plugins_txt)

        event_bus.publish.assert_awaited()
        published = event_bus.publish.await_args.args[0]
        assert published.payload.get("id") == "wf-id"
        assert published.payload.get("workflow_id") == "wf-id"
    finally:
        await lock_mgr.close()


# ---------------------------------------------------------------------------
# T-18: el check de límites del preview usa flags reales cuando hay rutas
# ---------------------------------------------------------------------------


class TestLimitesConFlagsReales:
    def _service(self, tmp_path: pathlib.Path, resolver: MagicMock, analyzer: MagicMock) -> ChainPreviewService:
        return ChainPreviewService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=AsyncMock(),
            path_resolver=resolver,
            path_validator=PathValidator(roots=[tmp_path]),
            event_bus=AsyncMock(),
            loot_runner=MagicMock(),
            xedit_runner=MagicMock(),
            conflict_analyzer=analyzer,
        )

    async def test_con_rutas_resolubles_pasa_plugin_dirs(self, tmp_path: pathlib.Path) -> None:
        """T-18: con Skyrim/MO2 resolubles, el validador recibe los dirs y
        cuenta con flags reales (un ESPFE deja de inflar el pool full)."""
        skyrim = tmp_path / "Skyrim"
        (skyrim / "Data").mkdir(parents=True)
        mo2 = tmp_path / "MO2"
        (mo2 / "mods" / "ModA").mkdir(parents=True)
        resolver = _resolver_without_paths()
        resolver.get_skyrim_path = MagicMock(return_value=skyrim)
        resolver.get_mo2_path = MagicMock(return_value=mo2)
        analyzer = MagicMock()
        analyzer.validate_load_order_limit = MagicMock(return_value=None)

        svc = self._service(tmp_path, resolver, analyzer)
        avisos = await svc._plugin_limit_warnings(["A.esp"])

        assert avisos == []
        kwargs = analyzer.validate_load_order_limit.call_args.kwargs
        dirs = {d.name for d in kwargs["plugin_dirs"]}
        assert "Data" in dirs
        assert "ModA" in dirs

    async def test_mo2_autodetectado_no_degrada(self, tmp_path: pathlib.Path) -> None:
        """review Codex #267: sin MO2_PATH pero con MO2 auto-detectable, se
        siguen leyendo headers reales (no se cae a la heurística)."""
        skyrim = tmp_path / "Skyrim"
        (skyrim / "Data").mkdir(parents=True)
        mo2 = tmp_path / "MO2"
        (mo2 / "mods" / "ModDetectado").mkdir(parents=True)
        resolver = _resolver_without_paths()
        resolver.get_skyrim_path = MagicMock(return_value=skyrim)
        resolver.get_mo2_path = MagicMock(return_value=None)  # MO2_PATH sin setear
        resolver.detect_mo2_path = MagicMock(return_value=mo2)  # pero detectable
        analyzer = MagicMock()
        analyzer.validate_load_order_limit = MagicMock(return_value=None)

        svc = self._service(tmp_path, resolver, analyzer)
        await svc._plugin_limit_warnings(["A.esp"])

        dirs = {d.name for d in analyzer.validate_load_order_limit.call_args.kwargs["plugin_dirs"]}
        assert "ModDetectado" in dirs  # se leyó del MO2 auto-detectado

    async def test_sin_rutas_degrada_a_none(self, tmp_path: pathlib.Path) -> None:
        """Sin entorno resoluble, el validador recibe plugin_dirs=None y
        degrada solo a la heurística (con su warning honesto)."""
        resolver = _resolver_without_paths()
        resolver.detect_mo2_path = MagicMock(return_value=None)  # tampoco auto-detectable
        analyzer = MagicMock()
        analyzer.validate_load_order_limit = MagicMock(return_value=None)

        svc = self._service(tmp_path, resolver, analyzer)
        await svc._plugin_limit_warnings(["A.esp"])

        assert analyzer.validate_load_order_limit.call_args.kwargs["plugin_dirs"] is None

    def test_dir_fuera_del_sandbox_se_descarta(self, tmp_path: pathlib.Path) -> None:
        """review Copilot #267: zero-trust — un dir de plugins que cae fuera
        del sandbox del PathValidator se descarta (no se lee ahí), best-effort."""
        sandbox = tmp_path / "sandbox"
        (sandbox / "Skyrim" / "Data").mkdir(parents=True)
        # MO2 vive FUERA del sandbox → sus mods deben descartarse.
        afuera = tmp_path / "afuera"
        (afuera / "mods" / "ModX").mkdir(parents=True)
        resolver = _resolver_without_paths()
        resolver.get_skyrim_path = MagicMock(return_value=sandbox / "Skyrim")
        resolver.get_mo2_path = MagicMock(return_value=afuera)
        analyzer = MagicMock()
        analyzer.validate_load_order_limit = MagicMock(return_value=None)

        svc = ChainPreviewService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=AsyncMock(),
            path_resolver=resolver,
            path_validator=PathValidator(roots=[sandbox]),  # solo sandbox/ es válido
            event_bus=AsyncMock(),
            loot_runner=MagicMock(),
            xedit_runner=MagicMock(),
            conflict_analyzer=analyzer,
        )

        dirs = svc._plugin_dirs()
        nombres = {d.name for d in dirs}
        assert "Data" in nombres  # dentro del sandbox: OK
        assert "ModX" not in nombres  # fuera: descartado


# ---------------------------------------------------------------------------
# T-20·2: el asistente de parcheo (recommend()) viaja al operador vía el preview
# ---------------------------------------------------------------------------


def _report_con_recomendaciones() -> ConflictReport:
    """Report con una lista nivelada (dedup entre dos losers) y un SPEL cuyo
    ganador pierde un flag crítico (escala a xedit_manual, T-19b)."""
    lvli = RecordConflict(
        form_id="00000010",
        editor_id="LListX",
        record_type="LVLI",
        winner="A.esm",
        losers=["B.esp"],
        severity="warning",
    )
    # Mismo record LVLI perdido por OTRO loser: al aplanar plugin_pairs aparece
    # dos veces — recommend() debe deduplicarlo y contarlo UNA sola vez.
    lvli_dup = RecordConflict(
        form_id="00000010",
        editor_id="LListX",
        record_type="LVLI",
        winner="A.esm",
        losers=["C.esp"],
        severity="warning",
    )
    spel = RecordConflict(
        form_id="00000020",
        editor_id="SpellX",
        record_type="SPEL",
        winner="A.esm",
        losers=["B.esp"],
        severity="critical",
        flag_alerts=(
            FlagAlert(
                form_id="00000020",
                editor_id="SpellX",
                record_type="SPEL",
                flag="Persistent",
                winner="A.esm",
                defined_by=("B.esp",),
                explanation="el ganador no preserva el flag",
                severity="critical",
            ),
        ),
    )
    return ConflictReport(
        total_conflicts=2,
        critical_conflicts=1,
        plugin_pairs=[
            PluginConflictPair(plugin_a="A.esm", plugin_b="B.esp", conflicts=[lvli, spel]),
            PluginConflictPair(plugin_a="A.esm", plugin_b="C.esp", conflicts=[lvli_dup]),
        ],
    )


@pytest.mark.asyncio
async def test_preview_xedit_adjunta_recomendaciones(tmp_path: pathlib.Path) -> None:
    """La etapa xEdit del manifiesto lleva las recomendaciones del asistente:
    LVLI→bashed_patch (deduplicado), SPEL→xedit_manual (escalada por flag), con
    FormIDs y alertas trazadas — el asistente ya no queda huérfano."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*A.esm\n", encoding="utf-8")

    async def loot_sort() -> LOOTResult:
        return LOOTResult(return_code=0, sorted_plugins=["A.esm"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return _report_con_recomendaciones()

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        manifest = await service.preview_chain(workflow_id="wf-rec", load_order_file=plugins_txt)

        xedit_stage = manifest.stages[1]
        assert xedit_stage.conflicts is not None
        por_tipo = {r.record_type: r for r in xedit_stage.conflicts.recommendations}
        assert set(por_tipo) == {"LVLI", "SPEL"}

        lvli = por_tipo["LVLI"]
        assert lvli.approach == "bashed_patch"
        assert lvli.conflict_count == 1  # dedup end-to-end: dos plugin_pairs, un record
        assert lvli.form_ids == ["00000010"]

        spel = por_tipo["SPEL"]
        assert spel.approach == "xedit_manual"  # escalada por flag crítico perdido
        assert "00000020" in spel.form_ids
        assert len(spel.flag_alerts) == 1
        assert spel.flag_alerts[0]["flag"] == "Persistent"

        # proposed_resolution se reconcilia con las recomendaciones (enfoques
        # distintos en orden de severidad: crítico SPEL primero, luego LVLI).
        assert xedit_stage.conflicts.proposed_resolution == "xedit_manual, bashed_patch"
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_xedit_reconcilia_proposed_resolution_listas_niveladas(tmp_path: pathlib.Path) -> None:
    """review Codex #272: un report con solo listas niveladas no-críticas ya no
    propone create_merged_patch (rechazado por ADR 0001) contradiciendo la
    recomendación bashed_patch — proposed_resolution deriva de las recomendaciones."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*A.esm\n", encoding="utf-8")

    async def loot_sort() -> LOOTResult:
        return LOOTResult(return_code=0, sorted_plugins=["A.esm"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        lvli = RecordConflict(
            form_id="00000010",
            editor_id="LListX",
            record_type="LVLI",
            winner="A.esm",
            losers=["B.esp"],
            severity="warning",
        )
        return ConflictReport(
            total_conflicts=1,
            critical_conflicts=0,  # no crítico: xedit_service propondría create_merged_patch
            plugin_pairs=[PluginConflictPair(plugin_a="A.esm", plugin_b="B.esp", conflicts=[lvli])],
        )

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        manifest = await service.preview_chain(workflow_id="wf-lvli", load_order_file=plugins_txt)

        conflicts = manifest.stages[1].conflicts
        assert conflicts is not None
        assert conflicts.proposed_resolution == "bashed_patch"  # NO "create_merged_patch"
        assert [r.approach for r in conflicts.recommendations] == ["bashed_patch"]
    finally:
        await lock_mgr.close()


@pytest.mark.asyncio
async def test_preview_xedit_sin_conflictos_recomendaciones_vacias(tmp_path: pathlib.Path) -> None:
    """Sin conflictos no hay recomendaciones — el manifiesto es honesto (lista
    vacía), no inventa un enfoque."""
    plugins_txt = tmp_path / "plugins.txt"
    plugins_txt.write_text("*A.esm\n", encoding="utf-8")

    async def loot_sort() -> LOOTResult:
        return LOOTResult(return_code=0, sorted_plugins=["A.esm"])

    async def analyze(_plugins: list[str], _runner: object) -> ConflictReport:
        return ConflictReport(total_conflicts=0, critical_conflicts=0, plugin_pairs=[])

    event_bus = AsyncMock(spec=CoreEventBus)
    event_bus.publish = AsyncMock()

    service, lock_mgr = await _build_service(tmp_path, loot_sort=loot_sort, analyze=analyze, event_bus=event_bus)
    try:
        manifest = await service.preview_chain(workflow_id="wf-sin", load_order_file=plugins_txt)

        xedit_stage = manifest.stages[1]
        assert xedit_stage.conflicts is not None
        assert xedit_stage.conflicts.recommendations == []
    finally:
        await lock_mgr.close()
