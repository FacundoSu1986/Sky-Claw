"""Event streamer — FASE 1.5.4 granular tool events.

``ToolEventStreamer`` emite eventos de ciclo de vida de tools
(``tool_started``, ``tool_progress``, ``tool_completed``, ``tool_failed``,
``tool_requires_approval``) vía :class:`InterfaceAgent`.

Nota (F1b, informe #319): el histórico ``LangGraphEventStreamer`` — que
envolvía ``SupervisorStateGraph.execute()`` — se retiró junto con el StateGraph
muerto. Nada en producción lo invocaba.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.antigravity.comms.interface import InterfaceAgent

logger = logging.getLogger("SkyClaw.WSEventStreamer")

# FASE 1.5.4: Tool event types emitted to the frontend
TOOL_EVENT_STARTED = "tool_started"
TOOL_EVENT_PROGRESS = "tool_progress"
TOOL_EVENT_COMPLETED = "tool_completed"
TOOL_EVENT_FAILED = "tool_failed"
TOOL_EVENT_REQUIRES_APPROVAL = "tool_requires_approval"


# ---------------------------------------------------------------------------
# FASE 1.5.4: Tool Event Streamer
# ---------------------------------------------------------------------------


class ToolEventStreamer:
    """Emits granular tool lifecycle events via InterfaceAgent.

    Each method sends a single event frame to the frontend with a structured
    payload so the UI can render spinners, progress bars, and approval dialogs
    instead of assuming optimistic success.

    Args:
        interface: The InterfaceAgent used to send events to the frontend.
    """

    def __init__(self, interface: InterfaceAgent) -> None:
        self._interface = interface

    async def emit_started(
        self,
        tool_name: str,
        task_id: str,
        payload_keys: list[str] | None = None,
    ) -> None:
        """Emit ``tool_started`` event."""
        await self._safe_emit(
            TOOL_EVENT_STARTED,
            {
                "tool": tool_name,
                "task_id": task_id,
                "payload_keys": payload_keys or [],
            },
        )

    async def emit_progress(
        self,
        tool_name: str,
        task_id: str,
        *,
        progress: float | None = None,
        message: str = "",
    ) -> None:
        """Emit ``tool_progress`` event."""
        await self._safe_emit(
            TOOL_EVENT_PROGRESS,
            {
                "tool": tool_name,
                "task_id": task_id,
                "progress": progress,
                "message": message,
            },
        )

    async def emit_completed(
        self,
        tool_name: str,
        task_id: str,
        *,
        status: str = "success",
    ) -> None:
        """Emit ``tool_completed`` event."""
        await self._safe_emit(
            TOOL_EVENT_COMPLETED,
            {
                "tool": tool_name,
                "task_id": task_id,
                "status": status,
            },
        )

    async def emit_failed(
        self,
        tool_name: str,
        task_id: str,
        *,
        error: str = "",
        error_type: str = "",
    ) -> None:
        """Emit ``tool_failed`` event."""
        await self._safe_emit(
            TOOL_EVENT_FAILED,
            {
                "tool": tool_name,
                "task_id": task_id,
                "error": error,
                "error_type": error_type,
            },
        )

    async def emit_requires_approval(
        self,
        tool_name: str,
        task_id: str,
        *,
        reason: str = "",
        timeout: float = 120.0,
    ) -> None:
        """Emit ``tool_requires_approval`` event for HITL flow."""
        await self._safe_emit(
            TOOL_EVENT_REQUIRES_APPROVAL,
            {
                "tool": tool_name,
                "task_id": task_id,
                "reason": reason,
                "timeout": timeout,
            },
        )

    async def _safe_emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit event, swallowing errors to avoid disrupting tool execution."""
        try:
            await self._interface.send_event(event_type, payload)
        except Exception:
            logger.debug(
                "ToolEventStreamer: failed to emit %s",
                event_type,
                exc_info=True,
            )
