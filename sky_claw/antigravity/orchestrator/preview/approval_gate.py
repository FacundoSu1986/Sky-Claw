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
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.orchestrator.preview.guard import require_manifest
from sky_claw.antigravity.orchestrator.preview.manifest import ActionManifest, PreviewManifest
from sky_claw.antigravity.security.hitl import Decision, HITLGuard

if TYPE_CHECKING:
    from sky_claw.antigravity.db.journal import OperationJournal

logger = logging.getLogger(__name__)

#: Produces the dry-run manifest for the given keyword arguments.
PreviewFn = Callable[..., Awaitable[PreviewManifest]]
#: Runs the real chain for the (same) keyword arguments; returns a result dict.
ExecuteFn = Callable[..., Awaitable[dict[str, Any]]]


class ChainPreviewApprovalGate:
    """Gate the real LOOT->xEdit->DynDOLOD chain behind a previewed HITL approval."""

    AGENT_ID = "chain-preview-approval-gate"

    def __init__(
        self,
        *,
        hitl_guard: HITLGuard,
        preview_fn: PreviewFn,
        execute_fn: ExecuteFn,
        journal: OperationJournal | None = None,
    ) -> None:
        self._hitl = hitl_guard
        self._preview_fn = preview_fn
        self._execute_fn = execute_fn
        # Requerido solo por preview_then_execute_action (manifiesto por acción).
        self._journal = journal

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

    async def preview_then_execute_action(
        self,
        *,
        manifest: ActionManifest,
        execute_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persiste el manifiesto por acción, pide aprobación y ejecuta si aprueban.

        Flujo "caja negra de vuelo" para UN Ritual mutante:
            1. Persistir el :class:`ActionManifest` en el journal ANTES de nada
               (deja la evidencia registrada aunque el operador rechace).
            2. Mostrar el manifiesto serializado como detalle del HITL.
            3. Solo en ``APPROVED``: reexigir el manifiesto persistido
               (``require_manifest``, fail-secure) y recién ahí ejecutar.

        Requiere que el gate se haya construido con ``journal``.

        Raises:
            ValueError: si no se inyectó un journal.
            MissingManifestError: si al ejecutar el manifiesto no está persistido.
        """
        if self._journal is None:
            raise ValueError("preview_then_execute_action requiere un journal inyectado.")

        manifest_dict = manifest.model_dump(mode="json")
        await self._journal.record_action_manifest(
            workflow_id=manifest.workflow_id,
            manifest=manifest_dict,
            agent_id=self.AGENT_ID,
        )

        decision = await self._hitl.request_approval(
            reason=f"¿Ejecutar la acción previsualizada? {manifest.describe()}",
            detail=manifest.model_dump_json(),
        )

        if decision != Decision.APPROVED:
            logger.info(
                "Action %s (workflow=%s) — nothing executed",
                decision.value,
                manifest.workflow_id,
            )
            return {
                "status": "rejected",
                "decision": decision.value,
                "manifest": manifest_dict,
            }

        # Fail-secure: el manifiesto debe seguir persistido para poder ejecutar.
        await require_manifest(self._journal, manifest.workflow_id)

        logger.info(
            "Action APPROVED (workflow=%s, ritual=%s) — executing",
            manifest.workflow_id,
            manifest.ritual,
        )
        result = await self._execute_fn(**(execute_kwargs or {}))
        return {
            "status": "executed",
            "decision": decision.value,
            "manifest": manifest_dict,
            "result": result,
        }
