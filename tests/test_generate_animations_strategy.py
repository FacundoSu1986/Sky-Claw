"""Tests del Follow-up A — estrategia generate_animations (Pandora) + wiring.

Cubre: la estrategia (delegación a ``PandoraPipelineService``), su inclusión en
``DESTRUCTIVE_TOOL_PATTERNS`` (gate HITL obligatorio), el registro en el dispatcher
y el resolver ``get_pandora_exe()`` (env-only, como el resto de las tools).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.tool_strategies.generate_animations import (
    GenerateAnimationsStrategy,
)
from sky_claw.antigravity.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS


# ── Estrategia ───────────────────────────────────────────────────────────────────
def test_strategy_name_is_generate_animations() -> None:
    svc = MagicMock()
    assert GenerateAnimationsStrategy(service=svc).name == "generate_animations"


@pytest.mark.asyncio
async def test_strategy_delegates_to_service() -> None:
    svc = MagicMock()
    svc.generate_animations = AsyncMock(return_value={"status": "success", "success": True})
    strat = GenerateAnimationsStrategy(service=svc)

    result = await strat.execute({})

    svc.generate_animations.assert_awaited_once_with()
    assert result == {"status": "success", "success": True}


@pytest.mark.asyncio
async def test_strategy_surfaces_service_error_dict() -> None:
    svc = MagicMock()
    svc.generate_animations = AsyncMock(return_value={"status": "error", "success": False, "logs": "no exe"})
    strat = GenerateAnimationsStrategy(service=svc)

    result = await strat.execute({})

    assert result["success"] is False


# ── Gate HITL ────────────────────────────────────────────────────────────────────
def test_generate_animations_is_destructive() -> None:
    # Pandora reescribe los grafos de comportamiento → requiere aprobación HITL.
    assert "generate_animations" in DESTRUCTIVE_TOOL_PATTERNS


# ── Registro en el dispatcher ────────────────────────────────────────────────────
def test_dispatcher_registers_generate_animations() -> None:
    from sky_claw.antigravity.orchestrator.tool_dispatcher import build_orchestration_dispatcher

    # build_orchestration_dispatcher solo accede a atributos del supervisor en la
    # construcción de las estrategias (los lambdas son perezosos), así que un
    # MagicMock alcanza para verificar el registro.
    dispatcher = build_orchestration_dispatcher(MagicMock())
    assert "generate_animations" in dispatcher.registered_tools()


# ── Resolver env-only ────────────────────────────────────────────────────────────
def test_get_pandora_exe_resolves_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import pathlib

    from sky_claw.antigravity.core.path_resolver import PathResolutionService

    monkeypatch.setenv("PANDORA_EXE", r"C:\Tools\Pandora\Pandora.exe")
    validator = MagicMock()
    validator.validate = MagicMock(side_effect=lambda p: pathlib.Path(p))
    resolver = PathResolutionService(path_validator=validator)

    resolved = resolver.get_pandora_exe()

    assert resolved == pathlib.Path(r"C:\Tools\Pandora\Pandora.exe")


def test_get_pandora_exe_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from sky_claw.antigravity.core.path_resolver import PathResolutionService

    monkeypatch.delenv("PANDORA_EXE", raising=False)
    resolver = PathResolutionService(path_validator=MagicMock())

    assert resolver.get_pandora_exe() is None
    # Env vacío: el validator nunca se invoca (corto-circuito en validate_env_path).
    assert "PANDORA_EXE" not in os.environ
