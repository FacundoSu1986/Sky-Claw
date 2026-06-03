"""Tests for ChainPreviewApprovalGate — the HITL gate around the real chain.

Flow: dry-run preview (no approval needed) -> show manifest -> HITL approval ->
ONLY on APPROVED run the real chain.  DENIED / TIMEOUT must touch nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.preview.approval_gate import ChainPreviewApprovalGate
from sky_claw.antigravity.orchestrator.preview.manifest import PreviewManifest, StageChangeSet
from sky_claw.antigravity.security.hitl import Decision


def _manifest() -> PreviewManifest:
    return PreviewManifest(
        workflow_id="wf-7",
        stages=[StageChangeSet(stage="loot", executed_for_real=True)],
        summary="preview",
    )


def _gate(decision: Decision) -> tuple[ChainPreviewApprovalGate, AsyncMock, AsyncMock, MagicMock]:
    preview_fn = AsyncMock(return_value=_manifest())
    execute_fn = AsyncMock(return_value={"chain": "ran"})
    hitl = MagicMock()
    hitl.request_approval = AsyncMock(return_value=decision)
    gate = ChainPreviewApprovalGate(hitl_guard=hitl, preview_fn=preview_fn, execute_fn=execute_fn)
    return gate, preview_fn, execute_fn, hitl


@pytest.mark.asyncio
async def test_approved_runs_real_chain() -> None:
    gate, preview_fn, execute_fn, hitl = _gate(Decision.APPROVED)

    result = await gate.preview_then_execute(workflow_id="wf-7", load_order_file="/sandbox/plugins.txt")

    assert result["status"] == "executed"
    assert result["decision"] == "approved"
    assert result["result"] == {"chain": "ran"}
    assert result["manifest"]["workflow_id"] == "wf-7"

    preview_fn.assert_awaited_once()
    execute_fn.assert_awaited_once()
    # The operator was shown the serialized manifest as the approval detail.
    detail = hitl.request_approval.await_args.kwargs["detail"]
    assert "wf-7" in detail


@pytest.mark.asyncio
async def test_denied_executes_nothing() -> None:
    gate, preview_fn, execute_fn, _hitl = _gate(Decision.DENIED)

    result = await gate.preview_then_execute(workflow_id="wf-7", load_order_file="/sandbox/plugins.txt")

    assert result["status"] == "rejected"
    assert result["decision"] == "denied"
    preview_fn.assert_awaited_once()  # preview still runs (it is safe)
    execute_fn.assert_not_awaited()  # but the real chain never runs


@pytest.mark.asyncio
async def test_timeout_executes_nothing() -> None:
    gate, _preview_fn, execute_fn, _hitl = _gate(Decision.TIMEOUT)

    result = await gate.preview_then_execute(workflow_id="wf-7", load_order_file="/sandbox/plugins.txt")

    assert result["status"] == "rejected"
    assert result["decision"] == "timeout"
    execute_fn.assert_not_awaited()
