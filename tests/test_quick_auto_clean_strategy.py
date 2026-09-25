"""Tests del Follow-up B — estrategia quick_auto_clean + wiring.

Cubre: la estrategia (delegación a ``XEditPipelineService.quick_auto_clean``), su
inclusión en ``DESTRUCTIVE_TOOL_PATTERNS`` (gate HITL), el registro en el dispatcher,
el mapeo del Ritual (``xedit`` → ``quick_auto_clean``) y la hidratación de ``XEDIT_PATH``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.gui.controllers.ritual_runner import RITUAL_TOOL_MAP, ritual_tool_name
from sky_claw.app.orchestrator.tool_strategies.middleware import DESTRUCTIVE_TOOL_PATTERNS
from sky_claw.app.orchestrator.tool_strategies.quick_auto_clean import (
    QuickAutoCleanStrategy,
)
from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher


# ── Estrategia ───────────────────────────────────────────────────────────────────
def test_strategy_name_is_quick_auto_clean() -> None:
    assert QuickAutoCleanStrategy(service=MagicMock()).name == "quick_auto_clean"


@pytest.mark.asyncio
async def test_strategy_delegates_to_service() -> None:
    svc = MagicMock()
    svc.quick_auto_clean = AsyncMock(return_value={"status": "success", "success": True, "cleaned": ["Update.esm"]})
    strat = QuickAutoCleanStrategy(service=svc)

    result = await strat.execute({})

    svc.quick_auto_clean.assert_awaited_once_with()
    assert result["success"] is True


@pytest.mark.asyncio
async def test_strategy_reenvia_el_resultado_completo_del_servicio() -> None:
    """Superficie hermana de la deuda manual de Dawnguard.

    ``quick_auto_clean`` se alcanza por dos caminos: el servicio
    ``XEditPipelineService`` (que ahora emite ``manual_pending``) y esta
    estrategia del dispatcher de orquestación/GUI. Si la estrategia
    reconstruyera el dict del servicio en vez de reenviarlo, proyectaría los
    campos que conoce y ``manual_pending`` desaparecería justo en el camino
    donde el operador lee el resumen — el patrón "arreglar un hermano y no al
    otro" (AGENTS.md). El ancla es la identidad del objeto: un pass-through no
    puede filtrar campos.
    """
    esperado: dict[str, object] = {
        "status": "success",
        "success": True,
        "cleaned": ["Dawnguard.esm"],
        "manual_pending": [{"master": "Dawnguard.esm", "cells": ["00016BCF", "0001FA4C", "0006C3B6"]}],
        "logs": "Dawnguard.esm: quedan celdas para limpieza manual (SOP §2.1)",
    }
    svc = MagicMock()
    svc.quick_auto_clean = AsyncMock(return_value=esperado)

    result = await QuickAutoCleanStrategy(service=svc).execute({})

    assert result is esperado
    assert result["manual_pending"] == [{"master": "Dawnguard.esm", "cells": ["00016BCF", "0001FA4C", "0006C3B6"]}]


# ── Aprobación HITL: payload sin parámetros (Codex #3) ───────────────────────────
def test_validate_for_approval_accepts_empty_payload() -> None:
    QuickAutoCleanStrategy(service=MagicMock()).validate_for_approval({})  # no raise


def test_validate_for_approval_rejects_unexpected_keys() -> None:
    # quick_auto_clean no acepta parámetros: aprobar un payload con claves que no se
    # honran (p.ej. dry_run) sería engañoso → se rechaza ANTES de pedir aprobación.
    strat = QuickAutoCleanStrategy(service=MagicMock())
    with pytest.raises(ValueError) as exc:
        strat.validate_for_approval({"dry_run": True})
    assert "dry_run" in str(exc.value)


def test_describe_for_approval_mentions_no_params() -> None:
    desc = QuickAutoCleanStrategy(service=MagicMock()).describe_for_approval({})
    assert isinstance(desc, str) and desc


# ── Gate HITL ────────────────────────────────────────────────────────────────────
def test_quick_auto_clean_is_destructive() -> None:
    # QuickAutoClean reescribe los plugins oficiales en disco → requiere aprobación HITL.
    assert "quick_auto_clean" in DESTRUCTIVE_TOOL_PATTERNS


# ── Registro en el dispatcher ────────────────────────────────────────────────────
def test_dispatcher_registers_quick_auto_clean() -> None:
    from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher

    dispatcher = build_orchestration_dispatcher(crear_dependencias_dispatcher())
    assert "quick_auto_clean" in dispatcher.registered_tools()


# ── Mapeo del Ritual ─────────────────────────────────────────────────────────────
def test_ritual_xedit_maps_to_quick_auto_clean() -> None:
    assert RITUAL_TOOL_MAP["xedit"] == "quick_auto_clean"
    assert ritual_tool_name("xedit") == "quick_auto_clean"
