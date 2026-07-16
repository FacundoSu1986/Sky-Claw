"""Strategy catch-all del advisor de IA para conflictos críticos (Fase 1).

Se registra con la prioridad MÁS BAJA (1): cualquier conflicto crítico que
``ExecuteXEditScript`` no pueda reclamar (script ``.pas`` inexistente — su
``can_handle`` verifica existencia) cae acá en vez de a "no strategy". El plan
resultante es ADVISORY: no nombra ``.esp`` de salida ni script Pascal; el
service layer (``XEditPipelineService._run_ai_advisor``) enruta al
``PatchAdvisorLLM`` y devuelve recomendaciones, jamás una mutación.

Vive en ``local/xedit`` (no en ``local/ai``) porque implementa el contrato
``PatchStrategy`` del orquestador; el paquete ``ai`` no conoce el orquestador.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sky_claw.local.xedit.patch_orchestrator import (
    PatchPlan,
    PatchStrategy,
    PatchStrategyType,
    ScriptGenerationError,
)

if TYPE_CHECKING:
    from sky_claw.local.xedit.conflict_analyzer import RecordConflict

logger = logging.getLogger(__name__)


class AIAssistedPatch(PatchStrategy):
    """Catch-all advisory: conflictos críticos sin script → LLM advisor.

    Priority: 1 (la más baja — solo gana cuando ninguna strategy con script
    real puede manejar el conflicto).
    """

    async def can_handle(self, conflict: RecordConflict) -> bool:
        """True para todo conflicto crítico (catch-all).

        No verifica si hay LLM configurado: la disponibilidad del LLM es del
        service layer (``_run_ai_advisor`` falla closed con mensaje accionable
        si no hay provider). Acá solo se decide el ENRUTADO.
        """
        return conflict.severity == "critical"

    async def create_plan(self, conflicts: list[RecordConflict]) -> PatchPlan:
        """Plan advisory para los conflictos críticos del set.

        Raises:
            ScriptGenerationError: Si el set no trae ningún conflicto crítico.
        """
        critical = [c for c in conflicts if c.severity == "critical"]
        if not critical:
            raise ScriptGenerationError("AIAssistedPatch: no hay conflictos críticos en el set")

        target_plugins: set[str] = set()
        form_ids: list[str] = []
        for conflict in critical:
            target_plugins.add(conflict.winner)
            target_plugins.update(conflict.losers)
            form_ids.append(conflict.form_id)

        logger.info(
            "AIAssistedPatch plan (advisory): %d conflictos críticos, %d plugins — sin mutación.",
            len(critical),
            len(target_plugins),
        )
        return PatchPlan(
            strategy_type=PatchStrategyType.AI_ASSISTED,
            target_plugins=sorted(target_plugins),
            # Advisory puro: no se genera .esp. El service layer produce el
            # PatchResult final con output_path=None.
            output_plugin="",
            form_ids=form_ids,
            estimated_records=len(critical),
            requires_hitl=True,  # el operador SIEMPRE decide (Fase 1)
            script_path=None,
        )

    def get_priority(self) -> int:
        """1 — catch-all: pierde contra cualquier strategy con script real."""
        return 1


__all__ = ["AIAssistedPatch"]
