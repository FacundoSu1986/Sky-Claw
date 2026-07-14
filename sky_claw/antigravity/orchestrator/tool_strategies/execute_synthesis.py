"""Strategy for the `execute_synthesis_pipeline` tool.

T-27b·2 (ADR 0005): el pipeline ya no corre contra el overwrite real — la
strategy construye el ritual apuntado a ``clone.overwrite_copy`` y delega el
ciclo clonar → correr → diff → HITL → promote/discard en
:class:`~sky_claw.antigravity.orchestrator.sandbox_promotion.SandboxPromotionFlow`.
Ambos colaboradores llegan como providers lazy (mismo patrón que
``PreviewChainStrategy``): cablear el dispatcher nunca exige MO2 presente, y
un provider que falla lo convierte ErrorWrappingMiddleware en error dict.

Sin gate HITL pre-ejecución (double-gating, precedente PR #173): la aprobación
post-run sobre el diff real es estrictamente más fuerte que aprobar a ciegas.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from sky_claw.antigravity.orchestrator.sandbox_promotion import SandboxPromotionFlow
    from sky_claw.local.mo2.profile_sandbox import SandboxClone
    from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

logger = logging.getLogger(__name__)


class ExecuteSynthesisPipelineStrategy:
    name = "execute_synthesis_pipeline"

    def __init__(
        self,
        *,
        flow_provider: Callable[[], SandboxPromotionFlow],
        service_factory: Callable[[pathlib.Path, Any], SynthesisPipelineService],
        real_journal_provider: Callable[[], Any],
    ) -> None:
        self._flow_provider = flow_provider
        self._service_factory = service_factory
        self._real_journal_provider = real_journal_provider

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # Filter to only valid parameters — the LLM may inject extra keys
        # (e.g. "tool_name") that would cause TypeError on the service.
        valid_keys = {"patcher_ids", "create_snapshot"}
        filtered = {k: v for k, v in payload_dict.items() if k in valid_keys}
        unexpected = payload_dict.keys() - valid_keys
        if unexpected:
            logger.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)

        flow = self._flow_provider()

        from sky_claw.antigravity.db.journal import StagingJournal

        staging_journal = StagingJournal(self._real_journal_provider())

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            # Servicio fresco por run, con la salida redirigida a la copia del
            # overwrite (T-27b: el servicio deshabilita su propio snapshot en
            # modo sandbox — el clon ES el rollback).
            service = self._service_factory(clone.overwrite_copy, staging_journal)
            return await service.execute_pipeline(**filtered)

        result = await flow.run(ritual_name="synthesis", ritual=ritual)

        sandbox_info = result.get("sandbox", {}) if isinstance(result, dict) else {}
        if sandbox_info.get("promoted"):
            await staging_journal.commit_staged()
        else:
            await staging_journal.rollback_staged()

        return result
