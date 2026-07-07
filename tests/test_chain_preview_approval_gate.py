"""Tests for ChainPreviewApprovalGate — the HITL gate around the real chain.

Flow: dry-run preview (no approval needed) -> show manifest -> HITL approval ->
ONLY on APPROVED run the real chain.  DENIED / TIMEOUT must touch nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.orchestrator.preview.approval_gate import ChainPreviewApprovalGate
from sky_claw.antigravity.orchestrator.preview.guard import MissingManifestError
from sky_claw.antigravity.orchestrator.preview.manifest import (
    ActionManifest,
    PreviewManifest,
    StageChangeSet,
)
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


# ---------------------------------------------------------------------------
# ActionManifest: manifiesto por acción persistido y exigido (T-26a)
# ---------------------------------------------------------------------------


def _action_manifest() -> ActionManifest:
    return ActionManifest(
        workflow_id="wf-act",
        ritual="loot_sort",
        tool="LOOT",
        tool_version="0.29.0",
        files_to_touch=["plugins.txt"],
    )


def _journal_stub(stored: dict | None) -> MagicMock:
    """Journal mock: record_action_manifest no-op; get_action_manifest devuelve ``stored``."""
    journal = MagicMock()
    journal.record_action_manifest = AsyncMock(return_value=1)
    journal.get_action_manifest = AsyncMock(return_value=stored)
    return journal


@pytest.mark.asyncio
async def test_action_aprobada_persiste_manifiesto_y_ejecuta() -> None:
    manifest = _action_manifest()
    journal = _journal_stub(manifest.model_dump(mode="json"))
    execute_fn = AsyncMock(return_value={"ritual": "ran"})
    hitl = MagicMock()
    hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)
    gate = ChainPreviewApprovalGate(
        hitl_guard=hitl,
        preview_fn=AsyncMock(),
        execute_fn=execute_fn,
        journal=journal,
    )

    result = await gate.preview_then_execute_action(manifest=manifest)

    assert result["status"] == "executed"
    assert result["result"] == {"ritual": "ran"}
    # El manifiesto se persistió ANTES de pedir aprobación.
    journal.record_action_manifest.assert_awaited_once()
    execute_fn.assert_awaited_once()
    # El operador vio el manifiesto serializado como detalle de la aprobación.
    detail = hitl.request_approval.await_args.kwargs["detail"]
    assert "wf-act" in detail


@pytest.mark.asyncio
async def test_action_aprobada_sin_manifiesto_persistido_no_ejecuta() -> None:
    """Fail-secure: si el manifiesto no quedó persistido, el guard corta el run."""
    manifest = _action_manifest()
    journal = _journal_stub(None)  # get_action_manifest → None
    execute_fn = AsyncMock(return_value={"ritual": "ran"})
    hitl = MagicMock()
    hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)
    gate = ChainPreviewApprovalGate(
        hitl_guard=hitl,
        preview_fn=AsyncMock(),
        execute_fn=execute_fn,
        journal=journal,
    )

    with pytest.raises(MissingManifestError):
        await gate.preview_then_execute_action(manifest=manifest)

    execute_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_denegada_no_ejecuta() -> None:
    manifest = _action_manifest()
    journal = _journal_stub(manifest.model_dump(mode="json"))
    execute_fn = AsyncMock(return_value={"ritual": "ran"})
    hitl = MagicMock()
    hitl.request_approval = AsyncMock(return_value=Decision.DENIED)
    gate = ChainPreviewApprovalGate(
        hitl_guard=hitl,
        preview_fn=AsyncMock(),
        execute_fn=execute_fn,
        journal=journal,
    )

    result = await gate.preview_then_execute_action(manifest=manifest)

    assert result["status"] == "rejected"
    assert result["decision"] == "denied"
    execute_fn.assert_not_awaited()
