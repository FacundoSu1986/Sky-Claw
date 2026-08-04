"""Follow-up #3 — cerrar el vector ungated de BodySlide en la capa del agente.

Codex (#213) nombró el "mismo vector P1 de pandora/bodyslide": el tool del agente
corría el runner directo, sin el lock que serializa contra otros mutadores. Pandora
ya se cerró (#215); esto cierra BodySlide: ``run_bodyslide_batch`` serializa en el lock
``bodyslide-meshes`` cuando el lock está cableado, espejando ``run_loot_sort`` /
``run_pandora``. Si falta cualquiera de los gestores, la ejecución falla cerrada
y el runner no se invoca.

Nota: hoy no hay Ritual de GUI que compita con BodySlide, así que la carrera es teórica;
esto cierra el patrón abierto de forma consistente.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools.system_tools import (
    BODYSLIDE_MESHES_RESOURCE_ID,
    run_bodyslide_batch,
)
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager


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


@pytest.mark.asyncio
async def test_serializes_on_bodyslide_meshes_lock(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    seen: dict[str, object] = {}

    async def on_run(group: str, output_path: str, **_opciones: object) -> MagicMock:
        seen["info"] = await lock_manager.get_lock_info(BODYSLIDE_MESHES_RESOURCE_ID)
        return _runner_result()

    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=tmp_path / "game")
    runner.run_batch = AsyncMock(side_effect=on_run)

    out = json.loads(await run_bodyslide_batch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is True
    assert out["message"] == ""  # contrato success/message (AGENTS.md): vacío canónico en éxito
    assert seen["info"] is not None  # lock tomado durante la corrida
    assert await lock_manager.get_lock_info(BODYSLIDE_MESHES_RESOURCE_ID) is None  # liberado al salir


@pytest.mark.asyncio
async def test_blocked_when_lock_already_held(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    await lock_manager.acquire_lock(BODYSLIDE_MESHES_RESOURCE_ID, "other", ttl=30.0)
    runner = MagicMock()
    runner.run_batch = AsyncMock(return_value=_runner_result())

    out = json.loads(await run_bodyslide_batch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert "error" in out
    assert out["success"] is False
    assert out["message"]  # contrato success/message (AGENTS.md): no vacío en fallo
    runner.run_batch.assert_not_awaited()  # serializado: no corrió


@pytest.mark.asyncio
async def test_fails_closed_without_lock() -> None:
    runner = MagicMock()
    runner.run_batch = AsyncMock(return_value=_runner_result())

    out = json.loads(await run_bodyslide_batch(runner, group="3BA", output_path="meshes"))

    assert "error" in out
    assert "lock_manager" in out["error"]
    assert "snapshot_manager" in out["error"]
    runner.run_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_error_returns_success_false_with_message(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """CodeRabbit (PR #430): el catch-all no puede volver a la forma legacy
    ``{"error": ...}`` sin ``success``/``message`` — es el mismo contrato que
    ``pandora_service._dict_de_resultado`` ya respeta para su propio catch-all."""
    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=tmp_path / "game")
    runner.run_batch = AsyncMock(side_effect=RuntimeError("boom inesperado"))

    out = json.loads(await run_bodyslide_batch(runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager))

    assert out["success"] is False
    assert out["message"] == "boom inesperado"


async def test_fails_closed_when_one_protection_manager_is_missing(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager
) -> None:
    runner = MagicMock()
    runner.run_batch = AsyncMock(return_value=_runner_result())

    for managers in (
        {"lock_manager": None, "snapshot_manager": snapshot_manager},
        {"lock_manager": lock_manager, "snapshot_manager": None},
    ):
        out = json.loads(await run_bodyslide_batch(runner, **managers))

        assert "error" in out
        runner.run_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_none_runner_is_structured_error() -> None:
    out = json.loads(await run_bodyslide_batch(None))
    assert "error" in out


# ---------------------------------------------------------------------------
# U-04: rollback real de la salida — subárbol propio por grupo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_run_restores_previous_output(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """U-04: un batch fallido no puede dejar mallas parciales pisando las que ya
    había — mismo defecto de directorio-completo que Pandora cerró en #399,
    ahora para el subárbol administrado propio de BodySlide
    (``BodySlide_Output/<group>``, no el ``output_path`` que elige el LLM)."""
    game = tmp_path / "game"
    target = game / "BodySlide_Output" / "CBBE"
    target.mkdir(parents=True)
    (target / "vieja.nif").write_text("estado previo", encoding="utf-8")

    async def on_run(group: str, output_path: str, **_opciones: object) -> MagicMock:
        destino = pathlib.Path(output_path)
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "parcial.nif").write_text("a medio escribir", encoding="utf-8")
        return MagicMock(success=False, return_code=1, stdout="", stderr="boom", duration_seconds=0.5)

    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=game)
    runner.run_batch = AsyncMock(side_effect=on_run)

    out = json.loads(
        await run_bodyslide_batch(runner, group="CBBE", lock_manager=lock_manager, snapshot_manager=snapshot_manager)
    )

    assert out["success"] is False
    assert out["message"] == "boom"  # contrato success/message: el detalle de fallo, no vacío
    assert (target / "vieja.nif").exists(), "el estado previo debe volver tras el rollback"
    assert not (target / "parcial.nif").exists(), "la escritura parcial del run fallido no debe sobrevivir"


@pytest.mark.asyncio
async def test_distintos_grupos_no_se_pisan(
    lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager, tmp_path: pathlib.Path
) -> None:
    """Cada ``group`` obtiene su propio subárbol bajo ``BodySlide_Output``: un
    directorio único compartido entre grupos violaría la propiedad que
    ``DirectoryRollback`` exige (\"la herramienta regenera el target por
    completo\") — un rebuild de ``CBBE`` habría descartado lo que ya existía
    de ``3BA``."""
    game = tmp_path / "game"

    async def on_run(group: str, output_path: str, **_opciones: object) -> MagicMock:
        destino = pathlib.Path(output_path)
        destino.mkdir(parents=True, exist_ok=True)
        (destino / f"{group}.nif").write_text("malla", encoding="utf-8")
        return _runner_result()

    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=game)
    runner.run_batch = AsyncMock(side_effect=on_run)

    await run_bodyslide_batch(runner, group="CBBE", lock_manager=lock_manager, snapshot_manager=snapshot_manager)
    await run_bodyslide_batch(runner, group="3BA", lock_manager=lock_manager, snapshot_manager=snapshot_manager)

    root = game / "BodySlide_Output"
    assert (root / "CBBE" / "CBBE.nif").exists()
    assert (root / "3BA" / "3BA.nif").exists()
