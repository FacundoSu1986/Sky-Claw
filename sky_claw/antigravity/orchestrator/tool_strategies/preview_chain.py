"""Strategy for `preview_chain` — dry-run of the LOOT->xEdit->DynDOLOD chain.

Read-only: it produces a :class:`PreviewManifest` of everything the chain WOULD
change without mutating a single file, so it carries NO HitlGateMiddleware — the
preview needs no approval.  Approval is requested separately, on the manifest,
before the real chain runs.

The :class:`ChainPreviewService` is supplied through a lazy ``service_provider``
callable so wiring the dispatcher never requires the LOOT/xEdit binaries; the
service (and its runners) is built only when ``preview_chain`` is dispatched.
"""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.antigravity.orchestrator.preview.chain_preview_service import ChainPreviewService

logger = logging.getLogger(__name__)


class PreviewChainStrategy:
    """Dispatchable wrapper around ``ChainPreviewService.preview_chain``."""

    name = "preview_chain"

    def __init__(self, service_provider: Callable[[], ChainPreviewService]) -> None:
        self._service_provider = service_provider

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        load_order_file = payload_dict.get("load_order_file")
        if not load_order_file:
            return {
                "status": "error",
                "reason": "preview_chain requires 'load_order_file' in the payload",
            }

        kwargs: dict[str, Any] = {
            "workflow_id": str(payload_dict.get("workflow_id", "preview")),
            "load_order_file": pathlib.Path(load_order_file),
            "dyndolod_preset": payload_dict.get("dyndolod_preset", "Medium"),
            "run_texgen": bool(payload_dict.get("run_texgen", True)),
        }
        target_plugin = payload_dict.get("target_plugin")
        if target_plugin:
            kwargs["target_plugin"] = pathlib.Path(target_plugin)
        if payload_dict.get("plugins_for_scan") is not None:
            kwargs["plugins_for_scan"] = list(payload_dict["plugins_for_scan"])

        service = self._service_provider()
        manifest = await service.preview_chain(**kwargs)
        logger.info("preview_chain ready for workflow=%s", kwargs["workflow_id"])
        return {
            "status": "preview_ready",
            "manifest": manifest.model_dump(mode="json"),
        }
