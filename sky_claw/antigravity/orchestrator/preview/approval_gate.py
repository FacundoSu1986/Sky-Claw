"""ChainPreviewApprovalGate — HITL gate between the dry-run and the real chain.

Flow:
    1. Run the dry-run preview (safe, reverts everything → no approval needed).
    2. Show the serialized :class:`PreviewManifest` to the operator and request
       approval via :class:`HITLGuard` (fail-secure: timeout → DENIED).
    3. ONLY on ``Decision.APPROVED`` run the real chain (``execute_fn``).
       ``DENIED`` / ``TIMEOUT`` execute nothing.

``preview_fn`` and ``execute_fn`` are injected so the gate stays decoupled from
*how* the preview and the real chain are produced (and trivially testable).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sky_claw.antigravity.orchestrator.preview.manifest import PreviewManifest
from sky_claw.antigravity.security.hitl import Decision, HITLGuard

logger = logging.getLogger(__name__)

#: Produces the dry-run manifest for the given keyword arguments.
PreviewFn = Callable[..., Awaitable[PreviewManifest]]
#: Runs the real chain for the (same) keyword arguments; returns a result dict.
ExecuteFn = Callable[..., Awaitable[dict[str, Any]]]


class ChainPreviewApprovalGate:
    """Gate the real LOOT->xEdit->DynDOLOD chain behind a previewed HITL approval."""

    def __init__(
        self,
        *,
        hitl_guard: HITLGuard,
        preview_fn: PreviewFn,
        execute_fn: ExecuteFn,
    ) -> None:
        self._hitl = hitl_guard
        self._preview_fn = preview_fn
        self._execute_fn = execute_fn

    async def preview_then_execute(self, **kwargs: Any) -> dict[str, Any]:
        """Preview, ask for approval, and execute the real chain only if approved.

        Returns a dict with ``status`` ``"executed"`` (approved + ran) or
        ``"rejected"`` (denied/timeout), the operator ``decision``, the serialized
        ``manifest``, and — when executed — the real chain ``result``.
        """
        manifest = await self._preview_fn(**kwargs)
        manifest_dict = manifest.model_dump(mode="json")

        decision = await self._hitl.request_approval(
            reason="Apply the previewed LOOT->xEdit->DynDOLOD chain?",
            detail=manifest.model_dump_json(),
        )

        if decision == Decision.APPROVED:
            logger.info(
                "Chain preview APPROVED (workflow=%s) — executing the real chain",
                manifest.workflow_id,
            )
            result = await self._execute_fn(**kwargs)
            return {
                "status": "executed",
                "decision": decision.value,
                "manifest": manifest_dict,
                "result": result,
            }

        logger.info(
            "Chain preview %s (workflow=%s) — nothing executed",
            decision.value,
            manifest.workflow_id,
        )
        return {
            "status": "rejected",
            "decision": decision.value,
            "manifest": manifest_dict,
        }
