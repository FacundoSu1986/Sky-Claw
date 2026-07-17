"""Contrato ToolResult anclado por servicio (deuda #5 de CLAUDE.md).

Cada servicio debe emitir ``message`` canónico junto a ``success`` en sus
retornos, y ``normalize_tool_result`` debe extraer ese detalle sin adivinar.
Se ejercitan los retornos tempranos de error (runner no disponible / path sin
configurar), que no requieren subprocesos ni locks reales.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sky_claw.local.tools.tool_result import normalize_tool_result


def _assert_contract_error(result: dict, expected_fragment: str) -> None:
    """El dict cumple el contrato y el normalizador extrae el detalle."""
    assert result["success"] is False
    assert expected_fragment in result["message"]
    normalized = normalize_tool_result(result)
    assert normalized["success"] is False
    assert expected_fragment in normalized["message"]
    assert normalized["message"] != "error desconocido"


async def test_loot_service_runner_no_disponible_cumple_contrato() -> None:
    from sky_claw.local.loot.cli import LOOTNotFoundError
    from sky_claw.local.tools.loot_service import LootSortingService

    service = LootSortingService(lock_manager=MagicMock(), snapshot_manager=MagicMock())
    with patch.object(service, "_ensure_loot_runner", side_effect=LOOTNotFoundError("LOOT.exe no encontrado")):
        result = await service.sort_load_order()

    _assert_contract_error(result, "LOOT.exe no encontrado")


async def test_loot_fallo_con_solo_raw_stderr_no_es_error_desconocido() -> None:
    """LOOT non-zero con el error solo en stderr no estructurado (errors=[] y
    stdout vacío) debe surfear el stderr en message (review Codex #222)."""
    from sky_claw.local.loot.parser import LOOTResult
    from sky_claw.local.tools.loot_service import LootSortingService

    service = LootSortingService(lock_manager=MagicMock(), snapshot_manager=MagicMock())
    failed = LOOTResult(return_code=1, sorted_plugins=[], errors=[], raw_stdout="", raw_stderr="boost::bad_alloc")
    runner = MagicMock()
    runner.sort = AsyncMock(return_value=failed)
    with (
        patch.object(service, "_ensure_loot_runner", return_value=runner),
        patch("sky_claw.local.tools.loot_service.SnapshotTransactionLock") as lock_cls,
    ):
        lock_cls.return_value.__aenter__ = AsyncMock()
        lock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await service.sort_load_order()

    _assert_contract_error(result, "boost::bad_alloc")


async def test_pandora_service_runner_no_disponible_cumple_contrato() -> None:
    from sky_claw.local.tools.pandora_runner import PandoraExecutionError
    from sky_claw.local.tools.pandora_service import PandoraPipelineService

    service = PandoraPipelineService(lock_manager=MagicMock(), snapshot_manager=MagicMock())
    with patch.object(service, "_ensure_runner", side_effect=PandoraExecutionError("Pandora.exe no encontrado")):
        result = await service.generate_animations()

    _assert_contract_error(result, "Pandora.exe no encontrado")


async def test_dyndolod_service_runner_no_disponible_cumple_contrato() -> None:
    from sky_claw.local.tools.dyndolod_runner import DynDOLODExecutionError
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    service = DynDOLODPipelineService.__new__(DynDOLODPipelineService)
    with (
        patch.object(DynDOLODPipelineService, "_publish_started", new=AsyncMock()),
        patch.object(DynDOLODPipelineService, "_publish_completed", new=AsyncMock()),
        # El objeto se crea con __new__ (sin __init__): el gate de preflight
        # (T-16c·3) se saltea porque este test aísla el contrato de error del
        # runner no disponible, ortogonal al preflight.
        patch.object(DynDOLODPipelineService, "_ensure_preflight", return_value=None),
        patch.object(
            DynDOLODPipelineService,
            "_ensure_runner",
            side_effect=DynDOLODExecutionError("DYNDLOD_EXE no configurado"),
        ),
    ):
        result = await service.execute()

    _assert_contract_error(result, "DYNDLOD_EXE no configurado")


async def test_xedit_quick_auto_clean_sin_masters_es_exito_con_message_vacio(tmp_path) -> None:
    """Éxito sin DLC para limpiar: message vacío (contrato); el detalle va en logs."""
    from sky_claw.local.tools.xedit_service import XEditPipelineService

    game_path = tmp_path / "Skyrim"
    (game_path / "Data").mkdir(parents=True)  # sin masters presentes
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game_path
    service = XEditPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
    )
    result = await service.quick_auto_clean()

    assert result["success"] is True
    assert result["message"] == ""
    assert "No se encontraron DLC oficiales" in result["logs"]
    assert normalize_tool_result(result)["message"] == ""


async def test_xedit_quick_auto_clean_sin_skyrim_path_cumple_contrato() -> None:
    from sky_claw.local.tools.xedit_service import XEditPipelineService

    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = None
    service = XEditPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
    )
    result = await service.quick_auto_clean()

    _assert_contract_error(result, "SKYRIM_PATH no está configurado")


def test_synthesis_error_dict_cumple_contrato() -> None:
    from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

    result = SynthesisPipelineService._error_dict("SYNTHESIS_EXE no configurado")
    _assert_contract_error(result, "SYNTHESIS_EXE no configurado")


def test_xedit_error_dict_cumple_contrato() -> None:
    from sky_claw.local.tools.xedit_service import XEditPipelineService

    result = XEditPipelineService._error_dict("XEDIT_PATH no configurado")
    _assert_contract_error(result, "XEDIT_PATH no configurado")
