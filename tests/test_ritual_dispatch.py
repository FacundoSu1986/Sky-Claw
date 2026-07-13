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

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from sky_claw.antigravity.gui.controllers.ritual_runner import (
    RITUAL_TOOL_MAP,
    STORE_KEY_RITUAL_PREFLIGHT,
    make_gui_hitl_notify,
    preflight_from_result,
    ritual_auto_approve_armed,
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


def test_summarize_surfaces_errors_list_field() -> None:
    # DynDOLOD reporta el detalle bajo "errors" (lista) — debe surfacearse,
    # no caer en "error desconocido".
    result = {"success": False, "errors": ["DynDOLOD output validation failed"]}
    msg, kind = summarize_ritual_result("dyndolod", result)
    assert kind == "negative"
    assert "DynDOLOD output validation failed" in msg
    assert "desconocido" not in msg


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


# ── M-9: auto-approve scoped a la task del ritual ────────────────────────────────
async def test_auto_approve_scoped_to_ritual_task_not_concurrent() -> None:
    """M-9: con Modo local ON, sólo el tool_execution del ritual del GUI se
    auto-aprueba; un tool_execution CONCURRENTE (otra task, p.ej. Telegram/LLM)
    corre con el ContextVar en su default y se parkea para aprobación manual.
    """
    responded: list[tuple[str, bool]] = []
    pending: list[dict] = []

    async def _respond(rid: str, approved: bool) -> None:
        responded.append((rid, approved))

    # Bridge cableado con el getter REAL (ContextVar), como en el bootloader.
    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=pending.append,
        auto_approve_getter=ritual_auto_approve_armed,
        delegate=None,
    )

    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def _dispatch(tool_name: str, payload: dict) -> dict:
        # Corre en la task de run_ritual → ContextVar armado (True).
        await notify(_FakeReq("ritual-req"))
        dispatch_started.set()
        await release_dispatch.wait()  # mantener el ritual "en vuelo"
        return {"status": "success"}

    sup = SimpleNamespace(dispatch_tool=_dispatch)
    store = ReactiveStore()
    ritual_task = asyncio.create_task(run_ritual("loot", supervisor=sup, store=store, auto_approve=True))

    await dispatch_started.wait()
    # Desde el contexto del TEST (default False) — simula un dispatch concurrente
    # de Telegram/LLM mientras el ritual del GUI sigue en vuelo.
    await notify(_FakeReq("foreign-req"))
    release_dispatch.set()
    await ritual_task

    approved_ids = [rid for rid, ok in responded if ok]
    assert "ritual-req" in approved_ids  # el del ritual SÍ se auto-aprobó
    assert "foreign-req" not in approved_ids  # el concurrente NO
    assert any(p["request_id"] == "foreign-req" for p in pending)  # se parkeó a modal


async def test_auto_approve_armed_defaults_false_outside_ritual() -> None:
    """M-9: fuera de un ritual, el getter devuelve False (no auto-aprueba)."""
    assert ritual_auto_approve_armed() is False


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
    """Records the armed auto-approve flag (ContextVar) visible during dispatch."""

    def __init__(self) -> None:
        self.armed_during_dispatch: object = "unset"

    async def dispatch_tool(self, tool_name: str, payload: dict) -> dict:
        # M-9: la señal es el ContextVar scoped a esta task, no un flag del store.
        self.armed_during_dispatch = ritual_auto_approve_armed()
        return {"status": "success"}


async def test_run_ritual_arms_auto_approve_for_this_dispatch_only() -> None:
    # The launching client's Modo local choice must be armed for THIS dispatch
    # (so the HITL bridge auto-grants it) and disarmed right after.
    store = ReactiveStore()
    sup = _ArmCapturingSupervisor()
    await run_ritual("loot", supervisor=sup, store=store, auto_approve=True)
    assert sup.armed_during_dispatch is True
    assert ritual_auto_approve_armed() is False  # disarmed on finish (fuera de la task)


async def test_run_ritual_does_not_arm_when_auto_approve_off() -> None:
    store = ReactiveStore()
    sup = _ArmCapturingSupervisor()
    await run_ritual("dyndolod", supervisor=sup, store=store)  # default False
    assert sup.armed_during_dispatch is False
    assert ritual_auto_approve_armed() is False


# ── T-16b: surface del reporte de preflight al panel vivo ─────────────────────────
def test_preflight_from_result_extracts_the_dict() -> None:
    report = {"status": "red", "blocks_mutations": True, "checks": []}
    assert preflight_from_result({"success": False, "preflight": report}) == report


def test_preflight_from_result_none_when_absent() -> None:
    assert preflight_from_result({"success": True}) is None


def test_preflight_from_result_none_for_non_dict_result() -> None:
    # Un dispatch que devuelve una shape inesperada no debe romper el surface.
    assert preflight_from_result("boom") is None
    assert preflight_from_result(None) is None


def test_preflight_from_result_none_when_preflight_not_a_dict() -> None:
    assert preflight_from_result({"preflight": "red"}) is None


async def test_run_ritual_surfaces_preflight_report_to_store() -> None:
    # Hoy solo LOOT corre preflight y adjunta result["preflight"] = to_dict();
    # run_ritual debe publicarlo al store para que el panel lo renderice.
    store = ReactiveStore()
    report = {
        "status": "yellow",
        "blocks_mutations": False,
        "checks": [{"name": "overwrite", "status": "yellow", "summary": "residuos", "details": []}],
    }
    sup = _FakeSupervisor({"success": True, "preflight": report})
    await run_ritual("loot", supervisor=sup, store=store)
    assert store.get(STORE_KEY_RITUAL_PREFLIGHT) == report


async def test_run_ritual_without_preflight_leaves_the_key_empty() -> None:
    store = ReactiveStore()
    sup = _FakeSupervisor({"success": True})
    await run_ritual("dyndolod", supervisor=sup, store=store)
    assert store.get(STORE_KEY_RITUAL_PREFLIGHT) is None


async def test_run_ritual_blocked_loot_surfaces_red_report_and_negative_feedback() -> None:
    # Un sort bloqueado por preflight rojo: feedback negativo Y el panel muestra
    # el semáforo rojo para que el operador vea POR QUÉ se frenó.
    store = ReactiveStore()
    red = {
        "status": "red",
        "blocks_mutations": True,
        "checks": [{"name": "composition", "status": "red", "summary": "symlinks + LOOT <0.29", "details": []}],
    }
    sup = _FakeSupervisor({"success": False, "reason": "PreflightBlocked", "preflight": red})
    await run_ritual("loot", supervisor=sup, store=store)
    fb = store.get("ritual_feedback")
    assert fb is not None and fb["type"] == "negative"
    assert store.get(STORE_KEY_RITUAL_PREFLIGHT) == red


async def test_run_ritual_clears_stale_preflight_on_new_run() -> None:
    # Un reporte de un run anterior no debe quedar pegado si el nuevo no trae uno.
    store = ReactiveStore()
    store.set(STORE_KEY_RITUAL_PREFLIGHT, {"status": "red", "blocks_mutations": True, "checks": []})
    sup = _FakeSupervisor({"success": True})  # este dispatch no adjunta preflight
    await run_ritual("dyndolod", supervisor=sup, store=store)
    assert store.get(STORE_KEY_RITUAL_PREFLIGHT) is None
