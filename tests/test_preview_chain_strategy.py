"""Tests for PreviewChainStrategy — the dispatcher seam for the chain dry-run.

The strategy is read-only: it wraps ChainPreviewService.preview_chain via a lazy
provider so wiring the dispatcher never needs the tool binaries.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.preview.manifest import PreviewManifest, StageChangeSet
from sky_claw.antigravity.orchestrator.tool_strategies.preview_chain import PreviewChainStrategy


def _manifest() -> PreviewManifest:
    return PreviewManifest(
        workflow_id="wf",
        stages=[StageChangeSet(stage="loot", executed_for_real=True)],
        summary="preview",
    )


def test_strategy_name() -> None:
    strategy = PreviewChainStrategy(service_provider=lambda: MagicMock())
    assert strategy.name == "preview_chain"


@pytest.mark.asyncio
async def test_execute_returns_serialized_manifest() -> None:
    fake_service = MagicMock()
    fake_service.preview_chain = AsyncMock(return_value=_manifest())

    strategy = PreviewChainStrategy(service_provider=lambda: fake_service)

    result = await strategy.execute(
        {
            "workflow_id": "wf",
            "load_order_file": "/sandbox/plugins.txt",
            "dyndolod_preset": "High",
            "run_texgen": False,
        }
    )

    assert result["status"] == "preview_ready"
    assert result["manifest"]["workflow_id"] == "wf"

    fake_service.preview_chain.assert_awaited_once()
    kwargs = fake_service.preview_chain.await_args.kwargs
    assert kwargs["workflow_id"] == "wf"
    assert kwargs["load_order_file"] == pathlib.Path("/sandbox/plugins.txt")
    assert kwargs["dyndolod_preset"] == "High"
    assert kwargs["run_texgen"] is False


@pytest.mark.asyncio
async def test_execute_requires_load_order_file() -> None:
    strategy = PreviewChainStrategy(service_provider=lambda: MagicMock())

    result = await strategy.execute({"workflow_id": "wf"})

    assert result["status"] == "error"
    assert "load_order_file" in result["reason"]


@pytest.mark.asyncio
async def test_provider_is_lazy_not_called_until_execute() -> None:
    calls = {"n": 0}

    def provider() -> MagicMock:
        calls["n"] += 1
        svc = MagicMock()
        svc.preview_chain = AsyncMock(return_value=_manifest())
        return svc

    strategy = PreviewChainStrategy(service_provider=provider)
    assert calls["n"] == 0  # not built at construction time

    await strategy.execute({"load_order_file": "/sandbox/plugins.txt"})
    assert calls["n"] == 1  # built only on dispatch
