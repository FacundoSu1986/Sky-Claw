"""Tests de las strategies de grass cache (PR-5): registro, gate HITL y adaptación.

- ``analyze_grass_prerequisites``: read-only, SIN gate (como preview_chain).
- ``generate_grass_cache``: destructiva — vive en ``DESTRUCTIVE_TOOL_PATTERNS``,
  el gate la deniega fail-closed sin ``HITLGuard``, y expone
  ``validate_for_approval``/``describe_for_approval`` para el prompt HITL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sky_claw.antigravity.orchestrator.tool_strategies.analyze_grass_prerequisites import (
    AnalyzeGrassPrerequisitesStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.base import (
    ApprovalPayloadDescriber,
    ApprovalPayloadValidator,
)
from sky_claw.antigravity.orchestrator.tool_strategies.generate_grass_cache import (
    GenerateGrassCacheStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import (
    DESTRUCTIVE_TOOL_PATTERNS,
    HitlGateMiddleware,
)

_PAYLOAD = {"worldspaces": ["Tamriel"], "conflicting_mods": ["ENB Helper"]}


def test_generate_esta_en_los_patrones_destructivos() -> None:
    assert GenerateGrassCacheStrategy.name in DESTRUCTIVE_TOOL_PATTERNS


def test_analyze_no_esta_en_los_patrones_destructivos() -> None:
    assert AnalyzeGrassPrerequisitesStrategy.name not in DESTRUCTIVE_TOOL_PATTERNS


def test_generate_implementa_las_capacidades_hitl() -> None:
    strategy = GenerateGrassCacheStrategy(service=AsyncMock())
    assert isinstance(strategy, ApprovalPayloadValidator)
    assert isinstance(strategy, ApprovalPayloadDescriber)


async def test_gate_deniega_generate_sin_guard_fail_closed() -> None:
    service = AsyncMock()
    strategy = GenerateGrassCacheStrategy(service=service)
    gate = HitlGateMiddleware()  # sin guard, sin allow_unattended

    resultado = await gate(strategy, _PAYLOAD, AsyncMock())

    assert resultado["status"] == "error"
    assert resultado["reason"] == "HITLGateUnavailable"
    service.generate.assert_not_awaited(), "rechazo HITL = cero mutaciones"


def test_validate_for_approval_rechaza_sin_worldspaces() -> None:
    strategy = GenerateGrassCacheStrategy(service=AsyncMock())

    with pytest.raises(Exception, match="worldspaces"):
        strategy.validate_for_approval({"worldspaces": []})


def test_describe_for_approval_es_operador_legible_y_sin_secretos() -> None:
    strategy = GenerateGrassCacheStrategy(service=AsyncMock())

    descripcion = strategy.describe_for_approval(
        {
            "worldspaces": ["Tamriel", "DLC2SolstheimWorld"],
            "conflicting_mods": ["ENB Helper"],
            "max_runtime_s": 7200,
            "force_stage_guard": True,
        }
    )

    assert "Tamriel" in descripcion
    assert "ENB Helper" in descripcion
    assert "force_stage_guard" in descripcion, "un bypass del guard debe ser visible al operador"
    for prohibido in ("token", "password", "api_key"):
        assert prohibido not in descripcion.lower()


async def test_generate_execute_delega_al_servicio() -> None:
    service = AsyncMock()
    service.generate.return_value = {"success": True, "message": ""}
    strategy = GenerateGrassCacheStrategy(service=service)

    resultado = await strategy.execute(dict(_PAYLOAD))

    assert resultado == {"success": True, "message": ""}
    service.generate.assert_awaited_once_with(dict(_PAYLOAD))


async def test_analyze_execute_delega_al_servicio() -> None:
    service = AsyncMock()
    service.analyze_prerequisites.return_value = {"success": True, "message": "", "ready": True}
    strategy = AnalyzeGrassPrerequisitesStrategy(service=service)

    resultado = await strategy.execute({"plugins": ["Skyrim.esm"], "timeout": 1800})

    assert resultado["ready"] is True
    service.analyze_prerequisites.assert_awaited_once_with(["Skyrim.esm"], timeout=1800)


async def test_analyze_execute_payload_invalido_lanza() -> None:
    strategy = AnalyzeGrassPrerequisitesStrategy(service=AsyncMock())

    with pytest.raises(Exception, match="plugins"):
        await strategy.execute({"plugins": []})


def test_dispatcher_registra_generate_gateada_y_analyze_libre() -> None:
    # Espejo del harness de tests/test_hitl_destructive_gate.py.
    from unittest.mock import MagicMock

    from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
    from sky_claw.antigravity.orchestrator.tool_dispatcher import build_orchestration_dispatcher

    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup.scraper = MagicMock()
    sup.tools = MagicMock()
    sup.interface = MagicMock()
    sup._loot_service = MagicMock()
    sup._synthesis_service = MagicMock()
    sup._xedit_service = MagicMock()
    sup._dyndolod_service = MagicMock()
    sup._pandora_service = MagicMock()
    sup._grass_cache_service = MagicMock()
    sup.profile_name = "TestProfile"
    gate = HitlGateMiddleware(allow_unattended=True)

    dispatcher = build_orchestration_dispatcher(sup, hitl_gate=gate)

    gated = {name for name, chain in dispatcher._middleware.items() if any(mw is gate for mw in chain)}
    assert GenerateGrassCacheStrategy.name in gated
    assert AnalyzeGrassPrerequisitesStrategy.name in dispatcher._middleware, "analyze debe registrarse (con wrapping)"
    assert AnalyzeGrassPrerequisitesStrategy.name not in gated
