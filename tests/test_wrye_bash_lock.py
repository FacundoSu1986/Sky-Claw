"""§2.1 auditoría — ``system_tools.generate_bashed_patch`` bajo lock distribuido.

Wrye Bash era el ÚNICO mutador de plugins fuera de la disciplina lock+snapshot.
#315 (PR A) cerró el path del Ritual GUI/dispatcher extrayendo
``WryeBashPipelineService`` (lock anidado ``Bashed Patch, 0.esp`` + ``load-order``,
serializa tanto contra otra corrida de Wrye Bash como contra un sort de LOOT
concurrente). Este módulo ancla el SEGUNDO path de producción —
``system_tools.generate_bashed_patch`` (registry del agente LLM/Telegram) — que
delega al MISMO servicio en vez de mantener una segunda implementación del lock
(espejando el patrón ya usado por ``run_pandora``/``run_bodyslide_batch``): el
lock cross-process solo protege si TODOS los mutadores participan.

Los tests de la lógica del lock/rollback/M-04 en sí viven en
``test_wrye_bash_service.py`` (ancla del servicio). Acá solo se ancla el
contrato de la delegación: que el handler pasa por el servicio cuando el lock
manager está cableado, que el contrato JSON de la tool se preserva (incluida la
sanitización de stdout/stderr contra prompt injection), y que el path sin lock
manager (legacy/tests) sigue corriendo directo.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.agent.tools.system_tools import generate_bashed_patch
from sky_claw.antigravity.db.locks import DistributedLockManager, LockLeaseLostError
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME, WryeBashResult
from sky_claw.local.tools.wrye_bash_service import BASHED_PATCH_RESOURCE_ID
from sky_claw.local.xedit.patch_orchestrator import DelegateToBashedPatch

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_path / "test_locks.db",
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    d = tmp_path / "snapshots"
    d.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=d)
    await mgr.initialize()
    return mgr


def _resultado(success: bool, stdout: str = "ok", stderr: str = "") -> WryeBashResult:
    return WryeBashResult(
        success=success,
        return_code=0 if success else 1,
        stdout=stdout if success else "",
        stderr=stderr if success else (stderr or "boom"),
        duration_seconds=1.0,
    )


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(return_value=_resultado(True))
    return runner


async def test_handler_serializa_en_el_lock_anidado(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """Con lock manager cableado, el handler delega al servicio y toma el
    MISMO lock (Bashed Patch) que el Ritual GUI/dispatcher — no una copia."""
    seen: dict[str, object] = {}
    runner = _runner()

    async def on_run() -> WryeBashResult:
        seen["info"] = await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID)
        return _resultado(True)

    runner.generate_bashed_patch = AsyncMock(side_effect=on_run)

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is True
    assert seen["info"] is not None  # lock tomado DURANTE la generación
    assert await lock_manager.get_lock_info(BASHED_PATCH_RESOURCE_ID) is None  # liberado


async def test_handler_bloqueado_si_otra_corrida_tiene_el_lock(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    await lock_manager.acquire_lock(BASHED_PATCH_RESOURCE_ID, "other-runner", ttl=30.0)
    runner = _runner()

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    assert "error" in out
    runner.generate_bashed_patch.assert_not_awaited()  # serializado: no corrió


async def test_handler_bloqueado_si_loot_tiene_el_load_order(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """El lock interno del servicio (``load-order``) es el MISMO que usa LOOT."""
    from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID

    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "loot-service", ttl=30.0)
    runner = _runner()

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    runner.generate_bashed_patch.assert_not_awaited()


async def test_handler_fallo_del_runner_reporta_error_saneado(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    runner = _runner()
    runner.generate_bashed_patch = AsyncMock(
        return_value=_resultado(False, stderr="stderr con \x1b[31mcontrol\x1b[0m basura")
    )

    out = json.loads(await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    assert "error" in out
    assert "\x1b" not in out["stderr"]  # sanitize_for_prompt aplicado (strip_control)


async def test_handler_lease_perdida_devuelve_json_de_error(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """El servicio nunca propaga LockLeaseLostError — el handler lo refleja
    como ``success: False`` sin crashear el dispatch del agente."""
    runner = _runner()

    class _LeaseLostLock:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _LeaseLostLock:
            return self

        async def __aexit__(self, *exc: object) -> None:
            raise LockLeaseLostError("lease perdido durante la corrida")

    with patch("sky_claw.local.tools.wrye_bash_service.SnapshotTransactionLock", _LeaseLostLock):
        out = json.loads(
            await generate_bashed_patch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager)
        )

    assert out["success"] is False
    assert "error" in out


async def test_handler_path_directo_sin_lock_preservado() -> None:
    runner = _runner()

    out = json.loads(await generate_bashed_patch(runner))

    assert out["success"] is True
    runner.generate_bashed_patch.assert_awaited_once_with()


async def test_handler_runner_none_es_error_estructurado() -> None:
    out = json.loads(await generate_bashed_patch(None))
    assert "error" in out


async def test_handler_con_journal_emite_caja_negra(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
) -> None:
    """T-26/T-28 (#315 PR C): el path del agente, con journal cableado (via
    app_context), emite el ActionManifest + FlightReport igual que run_loot_sort —
    sin esto un Bashed Patch generado por Telegram/LLM no tendría caja negra."""
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=88)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    journal.persist_action_manifest = AsyncMock()
    journal.persist_flight_report = AsyncMock()
    runner = _runner()

    with patch(
        "sky_claw.antigravity.orchestrator.preview.flight_report.compose_flight_report_from_journal",
        AsyncMock(return_value=MagicMock()),
    ):
        out = json.loads(
            await generate_bashed_patch(
                runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager, journal=journal
            )
        )

    assert out["success"] is True
    journal.persist_action_manifest.assert_awaited_once()  # T-26
    journal.commit_transaction.assert_awaited_once_with(88)
    journal.persist_flight_report.assert_awaited_once()  # T-28


def test_nombre_canonico_del_bashed_patch_sincronizado() -> None:
    """El runner, el servicio (#315) y la estrategia de delegación (ADR 0001)
    nombran el mismo .esp — un drift acá rompería el snapshot/lock de alguno."""
    assert BASHED_PATCH_NAME == DelegateToBashedPatch.BASHED_PATCH_NAME
    assert BASHED_PATCH_NAME == BASHED_PATCH_RESOURCE_ID
