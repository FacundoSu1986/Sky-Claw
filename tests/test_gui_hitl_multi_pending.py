"""Regresiones C3 para el estado HITL multi-pending de la GUI.

La suite prueba el contenedor y sus seams sin levantar NiceGUI. La autoridad de
la decisión sigue siendo HITLGuard; acá solo se verifica parking, visibilidad,
ordering y limpieza exacta del estado efímero de la GUI.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sky_claw.app.gui.controllers.ritual_runner import (
    HITL_OWNER_TAB,
    PENDING_HITL_ENTRIES,
    PENDING_HITL_ORDER,
    STORE_KEY_PENDING_HITL,
    DuplicatePendingHitlError,
    MalformedPendingHitlError,
    PendingHitlCapacityError,
    clear_answered_hitl,
    clear_owned_hitl,
    compose_gui_hitl_lifecycle,
    enqueue_pending_hitl,
    make_gui_hitl_notify,
    resolve_visible_pending_hitl,
    run_ritual,
    run_ritual_install,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.config import GUI_MAX_PENDING_HITL, _umbral_entero_env


def _payload(request_id: str, owner: str | None = None, *, category: str = "tool_execution") -> dict[str, object]:
    return {
        "request_id": request_id,
        "reason": f"Razón {request_id}",
        "detail": f"Detalle {request_id}",
        "owner_tab": owner,
        "category": category,
    }


def _park(store: ReactiveStore, request_id: str, owner: str | None = None, *, category: str = "tool_execution") -> None:
    enqueue_pending_hitl(store, _payload(request_id, owner, category=category))


class _Req(SimpleNamespace):
    def __init__(self, request_id: str, *, category: str = "tool_execution", url: str | None = None) -> None:
        super().__init__(
            request_id=request_id,
            category=category,
            reason=f"Razón {request_id}",
            detail=f"Detalle {request_id}",
            url=url,
        )


async def test_c3_01_dos_requests_coexisten_en_el_mismo_root_key() -> None:
    store = ReactiveStore()
    notify = make_gui_hitl_notify(
        respond=lambda _rid, _approved: None,  # type: ignore[arg-type]
        set_pending=lambda payload: enqueue_pending_hitl(store, payload),
        auto_approve_getter=lambda: False,
        tab_id_getter=lambda: "tab-A",
        delegate=None,
    )

    await notify(_Req("A"))
    await notify(_Req("B"))

    state = store.get(STORE_KEY_PENDING_HITL)
    assert isinstance(state, dict)
    assert list(state[PENDING_HITL_ORDER]) == ["A", "B"]
    assert set(state[PENDING_HITL_ENTRIES]) == {"A", "B"}


def test_c3_03_fifo_visible_estable_y_owner_scoped() -> None:
    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-A")
    _park(store, "C", "tab-B")
    _park(store, "U", None)

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "A"  # type: ignore[index]
    clear_answered_hitl(store, "A")
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]
    assert resolve_visible_pending_hitl(store, "tab-B")["request_id"] == "C"  # type: ignore[index]


def test_c3_04_clear_exacto_no_toca_las_demas() -> None:
    store = ReactiveStore()
    for request_id in ("A", "B", "C"):
        _park(store, request_id, "tab-A")

    clear_answered_hitl(store, "A")

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]
    state = store.get(STORE_KEY_PENDING_HITL)
    assert list(state[PENDING_HITL_ORDER]) == ["B", "C"]  # type: ignore[index]


def test_c3_09_click_viejo_responde_por_request_id_y_no_mueve_b() -> None:
    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-A")

    # El callback de una vista que capturó A debe seguir apuntando a A aunque B
    # haya llegado después; nunca se consulta un "current" global.
    clear_answered_hitl(store, "A")

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


def test_c3_10_doble_click_stale_no_responde_ni_borra_b() -> None:
    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-A")

    clear_answered_hitl(store, "A")
    clear_answered_hitl(store, "A")

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


def test_c3_12_y_c3_13_cross_tab_no_ve_ni_puede_limpiar() -> None:
    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-B")

    assert resolve_visible_pending_hitl(store, "tab-B")["request_id"] == "B"  # type: ignore[index]
    clear_owned_hitl(store, "tab-B", request_id="A")
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "A"  # type: ignore[index]


def test_c3_15_cleanup_de_a_no_evicta_b_de_la_misma_tab() -> None:
    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-A")

    clear_owned_hitl(store, "tab-A", request_id="A")

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


def test_c3_16_y_c3_17_ownerless_coexiste_y_sigue_visible() -> None:
    store = ReactiveStore()
    _park(store, "U1")
    _park(store, "U2")

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "U1"  # type: ignore[index]
    assert resolve_visible_pending_hitl(store, "tab-B")["request_id"] == "U1"  # type: ignore[index]


def test_c3_18_capacity_fail_closed_sin_eviction() -> None:
    store = ReactiveStore()
    for number in range(GUI_MAX_PENDING_HITL):
        _park(store, f"req-{number}")

    with pytest.raises(PendingHitlCapacityError):
        _park(store, "overflow")

    state = store.get(STORE_KEY_PENDING_HITL)
    assert len(state[PENDING_HITL_ORDER]) == GUI_MAX_PENDING_HITL  # type: ignore[index]
    assert "overflow" not in state[PENDING_HITL_ENTRIES]  # type: ignore[index]


def test_c3_19_duplicate_request_id_no_sobrescribe() -> None:
    store = ReactiveStore()
    _park(store, "same", "tab-A")

    with pytest.raises(DuplicatePendingHitlError):
        enqueue_pending_hitl(store, _payload("same", "tab-B"))

    entry = store.get(STORE_KEY_PENDING_HITL)[PENDING_HITL_ENTRIES]["same"]  # type: ignore[index]
    assert entry[HITL_OWNER_TAB] == "tab-A"


def test_c3_27_malformed_state_fail_closed() -> None:
    store = ReactiveStore()
    store.set(
        STORE_KEY_PENDING_HITL,
        {PENDING_HITL_ENTRIES: {"A": _payload("B")}, PENDING_HITL_ORDER: ["A"]},
    )

    assert resolve_visible_pending_hitl(store, "tab-A") is None
    with pytest.raises(MalformedPendingHitlError):
        enqueue_pending_hitl(store, _payload("C"))


@pytest.mark.asyncio
async def test_external_resolution_cleans_exact_entry_and_preserves_original_observer() -> None:
    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-A")
    observed: list[tuple[str, Decision]] = []

    async def original(req, decision) -> None:
        observed.append((req.request_id, decision))

    guard = SimpleNamespace(_on_terminal=original, _on_cancel=None)
    compose_gui_hitl_lifecycle(guard, store)

    req = SimpleNamespace(request_id="A")
    await guard._on_terminal(req, Decision.APPROVED)

    assert observed == [("A", Decision.APPROVED)]
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


@pytest.mark.asyncio
async def test_external_resolution_real_guard_retires_a_y_preserves_b() -> None:
    store = ReactiveStore()
    _park(store, "B", "tab-A")
    notify = make_gui_hitl_notify(
        respond=lambda _rid, _approved: None,  # type: ignore[arg-type]
        set_pending=lambda payload: enqueue_pending_hitl(store, payload),
        auto_approve_getter=lambda: False,
        tab_id_getter=lambda: "tab-A",
        delegate=None,
    )
    guard = HITLGuard(notify_fn=notify, timeout=1)
    compose_gui_hitl_lifecycle(guard, store)
    waiter = asyncio.create_task(guard.request_approval(request_id="A", category="tool_execution"))
    await asyncio.sleep(0)

    assert await guard.respond("A", True)
    assert await waiter is Decision.APPROVED
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


@pytest.mark.asyncio
async def test_timeout_y_cancelacion_retiran_solo_su_request() -> None:
    store = ReactiveStore()
    _park(store, "B", "tab-A")
    notify = make_gui_hitl_notify(
        respond=lambda _rid, _approved: None,  # type: ignore[arg-type]
        set_pending=lambda payload: enqueue_pending_hitl(store, payload),
        auto_approve_getter=lambda: False,
        tab_id_getter=lambda: "tab-A",
        delegate=None,
    )
    guard = HITLGuard(notify_fn=notify, timeout=0.01)
    compose_gui_hitl_lifecycle(guard, store)
    assert await guard.request_approval(request_id="A", category="tool_execution") is Decision.TIMEOUT
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]

    guard_cancel = HITLGuard(notify_fn=notify, timeout=1)
    compose_gui_hitl_lifecycle(guard_cancel, store)
    waiter = asyncio.create_task(guard_cancel.request_approval(request_id="C", category="tool_execution"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


@pytest.mark.asyncio
async def test_first_writer_del_guard_preserved_en_respuestas_concurrentes() -> None:
    guard = HITLGuard(notify_fn=None, timeout=1)
    waiter = asyncio.create_task(guard.request_approval(request_id="A"))
    await asyncio.sleep(0)

    outcomes = await asyncio.gather(guard.respond("A", True), guard.respond("A", False))
    decision = await waiter

    assert sum(outcomes) == 1
    assert decision in {Decision.APPROVED, Decision.DENIED}


@pytest.mark.asyncio
async def test_download_preserva_url_y_nunca_autoaprueba() -> None:
    store = ReactiveStore()
    responded: list[tuple[str, bool]] = []

    async def respond(request_id: str, approved: bool) -> None:
        responded.append((request_id, approved))

    notify = make_gui_hitl_notify(
        respond=respond,
        set_pending=lambda payload: enqueue_pending_hitl(store, payload),
        auto_approve_getter=lambda: True,
        tab_id_getter=lambda: "tab-A",
        delegate=None,
    )
    await notify(_Req("download-A", category="download", url="https://example.invalid/a.zip"))

    assert responded == []
    pending = resolve_visible_pending_hitl(store, "tab-A", category="download")
    assert pending is not None
    assert pending["request_id"] == "download-A"
    assert pending["url"] == "https://example.invalid/a.zip"
    assert pending["category"] == "download"


@pytest.mark.asyncio
async def test_wiring_callback_responde_id_exacto_y_rechaza_cross_tab() -> None:
    from sky_claw.app.gui.sky_claw_gui import _dispatch_hitl_response

    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-B")
    responded: list[tuple[str, bool]] = []

    class _Guard:
        async def respond(self, request_id: str, approved: bool) -> bool:
            responded.append((request_id, approved))
            return True

    scheduled: list[object] = []

    def scheduler(coro, *, name: str):
        assert name == "gui-hitl-respond"
        scheduled.append(coro)
        return coro

    _dispatch_hitl_response(
        "A",
        True,
        store=store,
        tab_id="tab-A",
        guard=_Guard(),
        scheduler=scheduler,
    )
    assert len(scheduled) == 1
    await scheduled[0]
    assert responded == [("A", True)]

    # Una pestaña ajena no agenda nada y tampoco puede retirar B.
    _dispatch_hitl_response(
        "B",
        False,
        store=store,
        tab_id="tab-A",
        guard=_Guard(),
        scheduler=scheduler,
    )
    assert len(scheduled) == 1
    assert resolve_visible_pending_hitl(store, "tab-B")["request_id"] == "B"  # type: ignore[index]


def test_root_key_recibe_una_mutacion_por_enqueue_y_remove() -> None:
    store = ReactiveStore()
    notifications: list[int] = []
    store.subscribe(STORE_KEY_PENDING_HITL, lambda: notifications.append(1))

    _park(store, "A")
    _park(store, "B")
    clear_answered_hitl(store, "A")
    clear_answered_hitl(store, "B")
    clear_answered_hitl(store, "B")  # stale: no debe publicar otra mutación

    assert len(notifications) == 4


def test_legacy_single_entry_se_adopta_sin_perder_identidad() -> None:
    store = ReactiveStore()
    legacy = _payload("legacy-A", "tab-A")
    store.set(STORE_KEY_PENDING_HITL, legacy)

    enqueue_pending_hitl(store, _payload("B", "tab-B"))

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "legacy-A"  # type: ignore[index]
    state = store.get(STORE_KEY_PENDING_HITL)
    assert list(state[PENDING_HITL_ORDER]) == ["legacy-A", "B"]  # type: ignore[index]


@pytest.mark.parametrize("runner", ["run", "install"])
async def test_tarea_sin_request_propia_no_evicta_request_existente(runner: str) -> None:
    """Una task sin parking propio nunca infiere que la única entrada es suya."""
    store = ReactiveStore()
    _park(store, "B", "tab-A")

    if runner == "run":

        class _Supervisor:
            async def dispatch_tool(self, _name: str, _args: dict) -> dict:
                return {"success": True}

        await run_ritual("loot", supervisor=_Supervisor(), store=store, tab_id="tab-A")
    else:

        class _Installer:
            async def ensure_loot(self, _install_dir: object, _session: object) -> dict:
                return {"success": True}

        class _Context:
            tools_installer = _Installer()
            install_dir = None
            session = None

        await run_ritual_install("loot", app_context=_Context(), store=store, tab_id="tab-A")

    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


@pytest.mark.parametrize("crudo, esperado", [("1", 1), (" 4 ", 4), ("32", 32)])
def test_umbral_entero_env_acepta_positivos(crudo: str, esperado: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKY_CLAW_UMBRAL_ENTERO_DE_PRUEBA", crudo)
    assert _umbral_entero_env("SKY_CLAW_UMBRAL_ENTERO_DE_PRUEBA", 32) == esperado


@pytest.mark.parametrize("crudo", ["0", "-1", "1.5", "no-es-entero"])
def test_umbral_entero_env_rechaza_no_positivos(crudo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKY_CLAW_UMBRAL_ENTERO_DE_PRUEBA", crudo)
    assert _umbral_entero_env("SKY_CLAW_UMBRAL_ENTERO_DE_PRUEBA", 32) == 32


def test_capacidad_configurable_se_prueba_sin_llenar_32(monkeypatch: pytest.MonkeyPatch) -> None:
    import sky_claw.app.gui.controllers.ritual_runner as ritual_runner

    monkeypatch.setattr(ritual_runner, "GUI_MAX_PENDING_HITL", 2)
    store = ReactiveStore()
    _park(store, "A")
    _park(store, "B")

    with pytest.raises(PendingHitlCapacityError):
        _park(store, "C")

    assert list(store.get(STORE_KEY_PENDING_HITL)[PENDING_HITL_ORDER]) == ["A", "B"]  # type: ignore[index]
    assert GUI_MAX_PENDING_HITL > 0


def test_umbral_entero_env_sin_override_usa_default_32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKY_CLAW_UMBRAL_ENTERO_DE_PRUEBA", raising=False)
    assert _umbral_entero_env("SKY_CLAW_UMBRAL_ENTERO_DE_PRUEBA", 32) == 32


def test_dispatch_sin_guard_preserva_la_pendiente() -> None:
    from sky_claw.app.gui.sky_claw_gui import _dispatch_hitl_response

    store = ReactiveStore()
    _park(store, "A", "tab-A")
    scheduled: list[object] = []

    result = _dispatch_hitl_response(
        "A",
        True,
        store=store,
        tab_id="tab-A",
        guard=None,
        scheduler=lambda coro, **kwargs: scheduled.append(coro),
    )

    assert result is None
    assert scheduled == []
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "A"  # type: ignore[index]


@pytest.mark.asyncio
async def test_dispatch_deja_a_hasta_respuesta_autoritativa_y_nunca_b() -> None:
    from sky_claw.app.gui.sky_claw_gui import _dispatch_hitl_response

    store = ReactiveStore()
    _park(store, "A", "tab-A")
    _park(store, "B", "tab-A")
    responded: list[tuple[str, bool]] = []

    class _Guard:
        async def respond(self, request_id: str, approved: bool) -> bool:
            responded.append((request_id, approved))
            return True

    scheduled: list[object] = []

    def scheduler(coro, **kwargs):
        scheduled.append(coro)
        return coro

    _dispatch_hitl_response(
        "A",
        True,
        store=store,
        tab_id="tab-A",
        guard=_Guard(),
        scheduler=scheduler,
    )
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "A"  # type: ignore[index]
    await scheduled[0]
    clear_answered_hitl(store, "A")

    assert responded == [("A", True)]
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]


def test_dispatch_stale_no_afecta_la_siguiente() -> None:
    from sky_claw.app.gui.sky_claw_gui import _dispatch_hitl_response

    store = ReactiveStore()
    _park(store, "B", "tab-A")
    scheduled: list[object] = []

    _dispatch_hitl_response(
        "A",
        True,
        store=store,
        tab_id="tab-A",
        guard=object(),
        scheduler=lambda coro, **kwargs: scheduled.append(coro),
    )

    assert scheduled == []
    assert resolve_visible_pending_hitl(store, "tab-A")["request_id"] == "B"  # type: ignore[index]
