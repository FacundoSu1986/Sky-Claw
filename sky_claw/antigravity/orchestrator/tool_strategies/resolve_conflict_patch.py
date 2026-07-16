"""Strategy for the `resolve_conflict_with_patch` tool.

Replaces supervisor.py:295-322. Builds the kwargs that
XEditPipelineService.execute_patch expects: `target_plugin` coerced to
pathlib.Path and `report` constructed as ConflictReport. The try/except
+ isinstance(dict) guard is provided by middleware.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Any

from sky_claw.local.xedit.conflict_analyzer import ConflictReport

if TYPE_CHECKING:
    from sky_claw.local.tools.xedit_service import XEditPipelineService


class ResolveConflictWithPatchStrategy:
    name = "resolve_conflict_with_patch"

    def __init__(self, service: XEditPipelineService) -> None:
        self.service = service

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        self._parse_payload(payload_dict)

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        target_plugin, report = self._parse_payload(payload_dict)
        parts = [
            f"target_plugin={str(target_plugin)!r}",
            f"total_conflicts={report.total_conflicts!r}",
            f"critical_conflicts={report.critical_conflicts!r}",
        ]
        # Fase 1 AI-assisted: si hay conflictos críticos sin script .pas, van
        # al LLM advisor. Hacerlo visible en el prompt HITL — el operador debe
        # saber que la IA va a RECOMENDAR (advisory, sin mutación) antes de
        # aprobar la tool.
        if report.critical_conflicts > 0:
            parts.append(
                f"critical_conflicts→AI_advisor={report.critical_conflicts!r} "
                "(advisory, sin mutación — el operador decide)"
            )
        return ", ".join(parts)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        target_plugin, report = self._parse_payload(payload_dict)
        return await self.service.execute_patch(
            target_plugin=target_plugin,
            report=report,
        )

    @staticmethod
    def _parse_payload(payload_dict: dict[str, Any]) -> tuple[pathlib.Path, ConflictReport]:
        target_plugin = pathlib.Path(payload_dict["target_plugin"])
        report = ConflictReport(**payload_dict["report"])
        return target_plugin, report
