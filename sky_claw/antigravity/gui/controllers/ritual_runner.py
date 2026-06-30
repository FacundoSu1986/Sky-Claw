"""Fase 2 — wire the Panel's Rituales to the real destructive-tool dispatcher.

The Ritual cards (Ordenar Mods / Crear Parche / Optimizar Gráficos) dispatch the
matching tool through :meth:`SupervisorAgent.dispatch_tool`, reusing the existing
HITL gate, load-order locks and sandbox. Approval is routed to the GUI via
:func:`make_gui_hitl_notify`: a "Modo local" toggle auto-approves
``tool_execution`` requests when the operator is at the PC, otherwise the bridge
parks the request in the store so the page can show an Aprobar/Denegar modal.

This module deliberately imports no NiceGUI so the logic stays unit-testable; the
view/bootloader own the actual element wiring and the store keys.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sky_claw.antigravity.gui.state.reactive_store import ReactiveStore

logger = logging.getLogger(__name__)

# Store key the bridge parks a pending tool_execution approval under, and the key
# the run flow publishes its result feedback under (both consumed by refreshable
# panels in forge_dashboard so the chat input is never reset).
STORE_KEY_PENDING_HITL = "pending_hitl"
STORE_KEY_RITUAL_FEEDBACK = "ritual_feedback"
#: Per-client "Modo local" toggle, stored in ``app.storage.client`` (server-side,
#: one entry per browser connection, auto-cleared on disconnect) — NOT the global
#: store. So one window's choice never enables auto-approval for another client
#: (Codex review on #211).
CLIENT_KEY_AUTO_APPROVE = "modo_local"
#: Auto-approve armed for the CURRENT in-flight ritual only. ``run_ritual`` sets
#: this from the launching client's toggle right before dispatch and disarms it
#: after; the HITL bridge reads it instead of any global flag. Combined with the
#: single-flight guard this scopes auto-approval to exactly the ritual the operator
#: launched — never another client's, never an agent-initiated tool_execution.
STORE_KEY_PENDING_AUTO_APPROVE = "pending_auto_approve"
#: Single-flight guard: a ritual is dispatching or awaiting approval right now.
STORE_KEY_RITUAL_IN_FLIGHT = "ritual_in_flight"

# Rituales con estrategia HITL-gated en el dispatcher. SSEEdit-clean (xedit) sigue sin
# estrategia → queda como interino hasta el follow-up B.
RITUAL_TOOL_MAP: dict[str, str] = {
    "loot": "execute_loot_sorting",
    "wrye_bash": "generate_bashed_patch",
    "dyndolod": "generate_lods",
    "pandora": "generate_animations",
}


def ritual_tool_name(tool_key: str) -> str | None:
    """Map a Ritual's scanner tool key to its dispatcher tool name, or ``None``."""
    return RITUAL_TOOL_MAP.get(tool_key)


def summarize_ritual_result(tool_key: str, result: dict[str, Any]) -> tuple[str, str]:
    """Build a (message, kind) pair from a dispatcher result dict.

    ``kind`` is one of NiceGUI's notify types ("positive"/"negative"/"warning").
    Denied/timed-out HITL approvals get a friendly Spanish hint instead of the
    raw reason code.
    """
    # Dispatcher results are inconsistent: execute_loot_sorting returns both
    # ``status`` and ``success``, while generate_bashed_patch / generate_lods
    # return only ``success=True`` (no ``status``). Treat either signal as
    # completion so a real success never shows a failure toast (Codex on #211).
    status = str(result.get("status", "")).lower()
    if status == "success" or result.get("success") is True:
        return (f"Ritual «{tool_key}» completado.", "positive")

    reason = str(result.get("reason", "") or "")
    if reason in {"HITLApprovalDenied", "HITLGateUnavailable"}:
        return (
            "Ejecución no aprobada. Activá «Modo local» o aprobá la acción para continuar.",
            "negative",
        )
    detail = str(result.get("details") or result.get("error") or reason or "error desconocido")
    return (f"El ritual «{tool_key}» falló: {detail}", "negative")


def make_gui_hitl_notify(
    *,
    respond: Callable[[str, bool], Awaitable[None]],
    set_pending: Callable[[dict[str, Any]], None],
    auto_approve_getter: Callable[[], bool],
    delegate: Callable[[Any], Awaitable[None]] | None,
) -> Callable[[Any], Awaitable[None]]:
    """Build the GUI's HITL ``notify_fn`` (composes over the original closure).

    For ``category == "tool_execution"`` the GUI owns the decision:
    auto-approve when the toggle is on, otherwise park the request via
    ``set_pending`` so the page renders an Aprobar/Denegar modal. Every other
    category falls through to ``delegate`` (the original Telegram closure), so
    download/scope approvals keep their existing behaviour.
    """

    async def _notify(req: Any) -> None:
        if getattr(req, "category", "") == "tool_execution":
            if auto_approve_getter():
                logger.info("HITL(GUI): auto-approving %s (Modo local ON)", req.request_id)
                await respond(req.request_id, True)
            else:
                set_pending(
                    {
                        "request_id": req.request_id,
                        "reason": getattr(req, "reason", ""),
                        "detail": getattr(req, "detail", ""),
                    }
                )
            return
        if delegate is not None:
            await delegate(req)

    return _notify


async def run_ritual(
    tool_key: str,
    *,
    supervisor: Any,
    store: ReactiveStore,
    auto_approve: bool = False,
) -> None:
    """Dispatch a Ritual's tool and publish a feedback message to the store.

    ``auto_approve`` is the launching client's "Modo local" preference, read in
    the click handler (the only place with client context). It is armed in the
    store for this single dispatch so the HITL bridge can auto-grant *this*
    request — and disarmed afterwards — instead of consulting a process-global
    flag that would also affect other clients/agent calls.

    Never raises: dispatch failures and a missing supervisor are converted into
    a ``ritual_feedback`` entry so the click handler (a fire-and-forget task)
    cannot crash the loop. The HITL gate inside ``dispatch_tool`` is what asks
    for approval — see :func:`make_gui_hitl_notify`.
    """
    tool_name = ritual_tool_name(tool_key)
    if tool_name is None:
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": f"El ritual «{tool_key}» aún no está cableado.", "type": "info"},
        )
        return
    if supervisor is None:
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": "El daemon todavía no está listo. Probá de nuevo en un momento.", "type": "negative"},
        )
        return
    # Single-flight: refuse a second launch while one is dispatching or awaiting
    # approval. Otherwise a second request would overwrite the single pending_hitl
    # entry and orphan the first prompt until its fail-closed timeout (Codex #211).
    if store.get(STORE_KEY_RITUAL_IN_FLIGHT):
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": "Ya hay un ritual en curso o esperando aprobación. Esperá a que termine.", "type": "warning"},
        )
        return
    store.set(STORE_KEY_RITUAL_IN_FLIGHT, True)
    # Arm auto-approve for THIS dispatch only (the launching client's choice).
    store.set(STORE_KEY_PENDING_AUTO_APPROVE, bool(auto_approve))
    try:
        result = await supervisor.dispatch_tool(tool_name, {})
    except Exception as exc:  # noqa: BLE001 — fire-and-forget task must not crash the loop
        logger.exception("Ritual %s (%s) dispatch failed", tool_key, tool_name)
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": f"El ritual «{tool_key}» falló: {type(exc).__name__}", "type": "negative"},
        )
        return
    finally:
        store.set(STORE_KEY_RITUAL_IN_FLIGHT, False)
        store.set(STORE_KEY_PENDING_AUTO_APPROVE, False)  # disarm
        # Drop the approval prompt tied to this run so no stale modal lingers on
        # the timeout/denied path where the operator never clicked (Codex #211).
        store.set(STORE_KEY_PENDING_HITL, None)
    text, kind = summarize_ritual_result(tool_key, result if isinstance(result, dict) else {})
    store.set(STORE_KEY_RITUAL_FEEDBACK, {"text": text, "type": kind})
