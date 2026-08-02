"""Follow-up de la review de Codex (#213, P1): cerrar el camino paralelo de Pandora.

El tool ``run_pandora`` del ``AsyncToolRegistry`` (capa LLM) corría
``PandoraRunner.run_pandora()`` directo, sin pasar por el lock ``behavior-graphs``
que sí toma el Ritual de la GUI → un agente podía competir con la GUI sobre los
mismos behavior graphs. Se enruta por ``PandoraPipelineService`` (que toma el lock),
espejando el patrón de ``run_loot_sort`` (Audit #190). Sin ambos managers de
protección, la capa LLM falla cerrada antes de invocar el runner.

También cubre el #3 (P2): ``GenerateAnimationsStrategy`` rechaza payload con claves
no honradas antes de pedir aprobación.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools.system_tools import run_pandora
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.app.orchestrator.tool_strategies.generate_animations import (
    GenerateAnimationsStrategy,
)
from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraResult
from sky_claw.local.tools.pandora_service import BEHAVIOR_GRAPHS_RESOURCE_ID, PandoraPipelineService

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
    return FileSnapshotManager(snapshot_dir=d)


def _runner_result() -> MagicMock:
    return MagicMock(success=True, return_code=0, stdout="ok", stderr="", duration_seconds=1.0)


def _configurar_runner(runner: MagicMock, tmp_path: pathlib.Path) -> pathlib.Path:
    game = tmp_path / "game"
    game.mkdir(parents=True)
    runner.config = PandoraConfig(
        pandora_exe=tmp_path / "Pandora" / "Pandora.exe",
        game_path=game,
    )
    return game


def _materializar_output(game: pathlib.Path) -> None:
    output = game.resolve() / "Pandora_Output"
    output.mkdir(parents=True)
    (output / "generado.hkx").write_bytes(b"pandora")


# ── P1: el camino del agente serializa en el lock behavior-graphs ────────────────
@pytest.mark.asyncio
async def test_run_pandora_serializes_on_behavior_graphs_lock(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    seen: dict[str, object] = {}

    async def on_run() -> MagicMock:
        seen["info"] = await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID)
        _materializar_output(game)
        return _runner_result()

    runner = MagicMock()
    game = _configurar_runner(runner, tmp_path)
    runner.run_pandora = AsyncMock(side_effect=on_run)

    out = json.loads(await run_pandora(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is True
    assert out["message"] == ""
    assert seen["info"] is not None  # el lock se mantuvo durante la corrida
    assert await lock_manager.get_lock_info(BEHAVIOR_GRAPHS_RESOURCE_ID) is None  # liberado al salir


@pytest.mark.asyncio
async def test_run_pandora_blocked_when_gui_ritual_holds_lock(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    # Simula el Ritual de la GUI corriendo: el lock está tomado.
    await lock_manager.acquire_lock(BEHAVIOR_GRAPHS_RESOURCE_ID, "gui-ritual", ttl=30.0)
    runner = MagicMock()
    _configurar_runner(runner, tmp_path)
    runner.run_pandora = AsyncMock(return_value=_runner_result())

    out = json.loads(await run_pandora(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    assert out["message"] == out["error"]
    runner.run_pandora.assert_not_awaited()  # no corrió — serializado contra la GUI


@pytest.mark.parametrize(
    ("lock_manager", "snapshot_manager", "faltantes"),
    [
        (None, MagicMock(), "lock_manager"),
        (MagicMock(), None, "snapshot_manager"),
        (None, None, "lock_manager, snapshot_manager"),
    ],
)
@pytest.mark.asyncio
async def test_run_pandora_falla_cerrado_sin_managers_de_proteccion(
    lock_manager: MagicMock | None,
    snapshot_manager: MagicMock | None,
    faltantes: str,
) -> None:
    runner = MagicMock()
    runner.run_pandora = AsyncMock(return_value=_runner_result())

    out = json.loads(
        await run_pandora(
            runner,
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )
    )

    detail = f"Pandora requiere protección; faltan: {faltantes}"
    assert out == {"success": False, "message": detail, "error": detail}
    runner.run_pandora.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_pandora_none_runner_is_structured_error() -> None:
    out = json.loads(await run_pandora(None))
    detail = "PandoraRunner is not configured. Set pandora_exe in config or install it via setup_tools."
    assert out == {"success": False, "message": detail, "error": detail}


@pytest.mark.asyncio
async def test_run_pandora_excepcion_inesperada_cumple_contrato(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _falla(self: PandoraPipelineService) -> dict[str, object]:
        del self
        raise RuntimeError("boom")

    monkeypatch.setattr(PandoraPipelineService, "generate_animations", _falla)

    out = json.loads(
        await run_pandora(
            MagicMock(),
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )
    )

    assert out == {"success": False, "message": "boom", "error": "boom"}


# ── T-26/T-28 (review Codex #318): el path del agente TAMBIÉN emite la caja negra ──
@pytest.mark.asyncio
async def test_run_pandora_agent_path_emite_caja_negra_con_journal(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """El path del agente, con journal cableado (via app_context), emite el
    ActionManifest + FlightReport igual que run_loot_sort — sin esto un run de
    Pandora por Telegram/LLM mutaría sin caja negra (T-26/T-28 no cubiertos)."""
    from unittest.mock import patch

    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=77)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    journal.persist_action_manifest = AsyncMock()
    journal.persist_flight_report = AsyncMock()
    runner = MagicMock()
    game = _configurar_runner(runner, tmp_path)

    async def _run_exitoso() -> MagicMock:
        _materializar_output(game)
        return _runner_result()

    runner.run_pandora = AsyncMock(side_effect=_run_exitoso)

    with patch(
        "sky_claw.app.orchestrator.preview.flight_report.compose_flight_report_from_journal",
        AsyncMock(return_value=MagicMock()),
    ):
        out = json.loads(
            await run_pandora(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager, journal=journal)
        )

    assert out["success"] is True
    journal.persist_action_manifest.assert_awaited_once()  # T-26
    journal.commit_transaction.assert_awaited_once_with(77)
    journal.persist_flight_report.assert_awaited_once()  # T-28


# ── P2 (#3): aprobación no engañosa para generate_animations ─────────────────────
def test_generate_animations_validate_accepts_empty_payload() -> None:
    GenerateAnimationsStrategy(service=MagicMock()).validate_for_approval({})  # no raise


def test_generate_animations_validate_rejects_unexpected_keys() -> None:
    strat = GenerateAnimationsStrategy(service=MagicMock())
    with pytest.raises(ValueError) as exc:
        strat.validate_for_approval({"dry_run": True})
    assert "dry_run" in str(exc.value)


def test_generate_animations_describe_mentions_no_params() -> None:
    desc = GenerateAnimationsStrategy(service=MagicMock()).describe_for_approval({})
    assert isinstance(desc, str) and desc


@pytest.mark.asyncio
async def test_run_pandora_agent_path_tambien_revierte_la_salida(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
) -> None:
    """U-04 llega también por la capa LLM, no sólo por el Ritual de la GUI.

    Es el defecto #1 del repo aplicado a este cambio: la misma operación mutante se
    alcanza desde dos superficies, y un rollback cableado en una sola deja la otra
    escribiendo behavior graphs parciales sin red. Acá se cumple **por construcción**
    —ambas delegan en ``PandoraPipelineService.generate_animations``— pero el path
    del agente construye el servicio SIN ``path_resolver``, así que las dirs a
    revertir tienen que derivarse del ``config`` del runner. Ese es el detalle que
    podría romperse en silencio, y por eso se afirma en vez de razonarse.
    """
    from sky_claw.local.tools.output_targets import PANDORA_OUTPUT_DIR

    game = tmp_path / "game"
    game.mkdir(parents=True)
    exe = tmp_path / "Pandora" / "Pandora.exe"
    exe.parent.mkdir(parents=True)
    salida = game.resolve() / PANDORA_OUTPUT_DIR
    salida.mkdir()
    (salida / "defaultmale.hkx").write_text("bueno", encoding="utf-8")

    async def _corre_y_falla() -> PandoraResult:
        (salida / "defaultmale.hkx").write_text("corrupto", encoding="utf-8")
        return PandoraResult(success=False, return_code=1, stdout="", stderr="boom", duration_seconds=1.0)

    runner = MagicMock()
    runner.config = PandoraConfig(pandora_exe=exe, game_path=game)
    runner.run_pandora = AsyncMock(side_effect=_corre_y_falla)

    out = json.loads(await run_pandora(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    assert (salida / "defaultmale.hkx").read_text(encoding="utf-8") == "bueno"
