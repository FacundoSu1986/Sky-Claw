"""Tests for the Fase 2 GUI Ritual → dispatcher wiring + HITL GUI bridge.

The Panel's Ritual buttons (Ordenar Mods / Crear Parche / Optimizar Gráficos)
dispatch the real destructive tools through ``SupervisorAgent.dispatch_tool`` —
reusing the existing HITL gate, load-order locks and sandbox. Approval is routed
to the GUI: a "Modo local" toggle auto-approves ``tool_execution`` requests when
the operator is at the PC; otherwise a modal asks. These tests cover the pure
seams (mapping, result summary, the bridge decision, the dispatch flow) without
a live NiceGUI client.
"""

from __future__ import annotations

from dataclasses import dataclass

from sky_claw.antigravity.gui.controllers.ritual_runner import (
    RITUAL_TOOL_MAP,
    make_gui_hitl_notify,
    ritual_tool_name,
    run_ritual,
    summarize_ritual_result,
)
from sky_claw.antigravity.gui.state.reactive_store import ReactiveStore


@dataclass
class _FakeReq:
    request_id: str
    category: str = "tool_execution"
    reason: str = "Tool requires approval"
    detail: str = "payload: <empty>"


class _FakeSupervisor:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._result = result if result is not None else {"status": "success"}

    async def dispatch_tool(self, tool_name: str, payload: dict) -> dict:
        self.calls.append((tool_name, payload))
        return self._result


# ── Ritual → dispatcher tool mapping ─────────────────────────────────────────────
def test_ritual_tool_map_covers_the_wired_rituals() -> None:
    # Los 5 Rituales del Panel ya tienen estrategia HITL-gated en el dispatcher.
    assert RITUAL_TOOL_MAP == {
        "loot": "execute_loot_sorting",
        "wrye_bash": "generate_bashed_patch",
        "dyndolod": "generate_lods",
        "pandora": "generate_animations",
        "xedit": "quick_auto_clean",
    }


def test_ritual_tool_name_known_and_unmapped() -> None:
    assert ritual_tool_name("loot") == "execute_loot_sorting"
    assert ritual_tool_name("wrye_bash") == "generate_bashed_patch"
    assert ritual_tool_name("dyndolod") == "generate_lods"
    assert ritual_tool_name("pandora") == "generate_animations"
    assert ritual_tool_name("xedit") == "quick_auto_clean"
    # Una clave desconocida no tiene destino de dispatch.
    assert ritual_tool_name("bodyslide") is None


# ── Result summary ───────────────────────────────────────────────────────────────
def test_summarize_success_is_positive() -> None:
    msg, kind = summarize_ritual_result("loot", {"status": "success"})
    assert kind == "positive"
    assert msg


def test_summarize_denied_is_negative_and_mentions_approval() -> None:
    msg, kind = summarize_ritual_result("loot", {"status": "error", "reason": "HITLApprovalDenied"})
    assert kind == "negative"
    assert "aprob" in msg.lower()


def test_summarize_generic_error_is_negative() -> None:
    msg, kind = summarize_ritual_result("dyndolod", {"status": "error", "reason": "Boom"})
    assert kind == "negative"
    assert "Boom" in msg


def test_summarize_surfaces_logs_field() -> None:
    # Codex #1: LOOT/Pandora/quick_auto_clean devuelven el detalle bajo "logs" —
    # el summarizer debe mostrarlo en vez de "error desconocido".
    result = {"status": "error", "success": False, "logs": "PANDORA_EXE no configurado"}
    msg, kind = summarize_ritual_result("pandora", result)
    assert kind == "negative"
    assert "PANDORA_EXE no configurado" in msg
    assert "desconocido" not in msg


def test_summarize_surfaces_stderr_field() -> None:
    msg, kind = summarize_ritual_result("xedit", {"status": "error", "success": False, "stderr": "xEdit crashed"})
    assert kind == "negative"
    assert "xEdit crashed" in msg


def test_summarize_success_key_without_status_is_positive() -> None:
    # generate_bashed_patch / generate_lods return success=True with no status.
    _, kind = summarize_ritual_result("dyndolod", {"success": True})
    assert kind == "positive"


def test_summarize_success_false_with_error_is_negative() -> None:
    msg, kind = summarize_ritual_result("wrye_bash", {"success": False, "error": "no path"})
    assert kind == "negative"
    assert "no path" in msg


# ── HITL GUI bridge decision ─────────────────────────────────────────────────────
async def test_bridge_auto_approves_tool_execution_when_toggle_on() -> None:
    responded: list[tuple[str, bool]] = []
    pending: list[dict] = []

    async def _respond(rid: str, approved: bool) -> None:
        responded.append((rid, approved))

    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=pending.append,
        auto_approve_getter=lambda: True,
        delegate=None,
    )
    await notify(_FakeReq("r1"))
    assert responded == [("r1", True)]
    assert pending == []


async def test_bridge_opens_modal_when_toggle_off() -> None:
    responded: list[tuple[str, bool]] = []
    pending: list[dict] = []

    async def _respond(rid: str, approved: bool) -> None:
        responded.append((rid, approved))

    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=pending.append,
        auto_approve_getter=lambda: False,
        delegate=None,
    )
    await notify(_FakeReq("r2", reason="Tool 'execute_loot_sorting'…", detail="payload: <empty>"))
    assert responded == []
    assert pending == [{"request_id": "r2", "reason": "Tool 'execute_loot_sorting'…", "detail": "payload: <empty>"}]


async def test_bridge_delegates_non_tool_execution_to_original() -> None:
    delegated: list[str] = []
    pending: list[dict] = []

    async def _respond(rid: str, approved: bool) -> None:  # pragma: no cover - must not run
        raise AssertionError("respond must not be called for non-tool_execution")

    async def _delegate(req: _FakeReq) -> None:
        delegated.append(req.request_id)

    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=pending.append,
        auto_approve_getter=lambda: True,
        delegate=_delegate,
    )
    await notify(_FakeReq("d1", category="scope"))
    assert delegated == ["d1"]
    assert pending == []


# ── run_ritual dispatch flow ─────────────────────────────────────────────────────
async def test_run_ritual_dispatches_mapped_tool_and_publishes_feedback() -> None:
    store = ReactiveStore()
    sup = _FakeSupervisor({"status": "success"})
    await run_ritual("loot", supervisor=sup, store=store)
    assert sup.calls == [("execute_loot_sorting", {})]
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "positive"


async def test_run_ritual_unmapped_ritual_does_not_dispatch() -> None:
    store = ReactiveStore()
    sup = _FakeSupervisor()
    await run_ritual("bodyslide", supervisor=sup, store=store)  # clave sin estrategia en el dispatcher
    assert sup.calls == []
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] in {"info", "warning"}


async def test_run_ritual_without_supervisor_reports_error() -> None:
    store = ReactiveStore()
    await run_ritual("loot", supervisor=None, store=store)
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"


async def test_run_ritual_surfaces_denied_as_negative_feedback() -> None:
    store = ReactiveStore()
    sup = _FakeSupervisor({"status": "error", "reason": "HITLApprovalDenied"})
    await run_ritual("dyndolod", supervisor=sup, store=store)
    assert sup.calls == [("generate_lods", {})]
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"


async def test_run_ritual_single_flight_refuses_while_one_is_in_flight() -> None:
    store = ReactiveStore()
    store.set("ritual_in_flight", True)  # simulate an outstanding ritual
    sup = _FakeSupervisor()
    await run_ritual("loot", supervisor=sup, store=store)
    assert sup.calls == []  # second launch must not dispatch
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "warning"


async def test_run_ritual_clears_pending_prompt_and_inflight_on_finish() -> None:
    store = ReactiveStore()
    store.set("pending_hitl", {"request_id": "tool-x-1"})  # a stale prompt for this run
    sup = _FakeSupervisor({"success": True})
    await run_ritual("dyndolod", supervisor=sup, store=store)
    assert store.get("pending_hitl") is None
    assert not store.get("ritual_in_flight")
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "positive"


class _ArmCapturingSupervisor:
    """Records the armed auto-approve flag visible during dispatch."""

    def __init__(self, store: ReactiveStore) -> None:
        self._store = store
        self.armed_during_dispatch: object = "unset"

    async def dispatch_tool(self, tool_name: str, payload: dict) -> dict:
        self.armed_during_dispatch = self._store.get("pending_auto_approve")
        return {"status": "success"}


async def test_run_ritual_arms_auto_approve_for_this_dispatch_only() -> None:
    # The launching client's Modo local choice must be armed for THIS dispatch
    # (so the HITL bridge auto-grants it) and disarmed right after.
    store = ReactiveStore()
    sup = _ArmCapturingSupervisor(store)
    await run_ritual("loot", supervisor=sup, store=store, auto_approve=True)
    assert sup.armed_during_dispatch is True
    assert store.get("pending_auto_approve") is False  # disarmed on finish


async def test_run_ritual_does_not_arm_when_auto_approve_off() -> None:
    store = ReactiveStore()
    sup = _ArmCapturingSupervisor(store)
    await run_ritual("dyndolod", supervisor=sup, store=store)  # default False
    assert sup.armed_during_dispatch is False
    assert store.get("pending_auto_approve") is False
