"""P1-7 — el gate HITL debe ser solo del cliente que lanzó el Ritual.

La aprobación de operaciones DESTRUCTIVAS se parkeaba en una única clave del
store (``pending_hitl``), y ``get_store()`` es un singleton de proceso
(``global _store``). Con dos pestañas abiertas —o un F5 sin cerrar la
anterior— el modal se renderiza y es accionable en AMBAS: cualquier sesión
podía aprobar un Ritual que no inició.

El fix espeja el patrón que este mismo módulo ya usa para el auto-approve
(``_ritual_auto_approve``, M-9): un ContextVar scoped a la task del dispatch
lleva la identidad del cliente que lanzó, y la aprobación se parkea en una
clave derivada de esa identidad.

Alcance deliberado: una solicitud SIN cliente lanzador (originada en el agente
LLM, en Telegram o en el backend) conserva la clave global y sigue visible para
todos. No tiene "dueño" al que scopearla, y rerutearla sería un cambio de
comportamiento con su propio radio de impacto — no un fix de este defecto.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sky_claw.app.gui.controllers.ritual_runner import (
    STORE_KEY_PENDING_HITL,
    make_gui_hitl_notify,
    pending_hitl_key,
    resolve_pending_hitl,
    ritual_client_id,
    run_ritual,
)
from sky_claw.app.gui.state.reactive_store import ReactiveStore


class _Req:
    """Solicitud HITL mínima con la forma que lee el bridge."""

    def __init__(self, category: str = "tool_execution", request_id: str = "req-1") -> None:
        self.category = category
        self.request_id = request_id
        self.reason = "Operación destructiva"
        self.detail = "detalle"


def _bridge_con_store(store: ReactiveStore, client_id_getter, delegate=None):
    """Arma el notify del GUI cableado como lo hace el bootloader."""
    respondidos: list[tuple[str, bool]] = []

    async def _respond(request_id: str, approved: bool) -> None:
        respondidos.append((request_id, approved))

    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=lambda cid, payload: store.set(pending_hitl_key(cid), payload),
        auto_approve_getter=lambda: False,
        client_id_getter=client_id_getter,
        delegate=delegate,
    )
    return notify, respondidos


# ---------------------------------------------------------------------------
# La clave por cliente
# ---------------------------------------------------------------------------


def test_la_clave_de_un_cliente_no_es_la_de_otro() -> None:
    """Dos clientes deben mapear a claves distintas, y None a la global."""
    assert pending_hitl_key("cliente-A") != pending_hitl_key("cliente-B")
    assert pending_hitl_key(None) == STORE_KEY_PENDING_HITL


async def test_el_hitl_de_un_cliente_no_aparece_en_la_clave_de_otro() -> None:
    """El caso del bug: A lanza el Ritual, B no debe poder aprobarlo."""
    store = ReactiveStore()
    notify, _ = _bridge_con_store(store, client_id_getter=lambda: "cliente-A")

    await notify(_Req())

    assert store.get(pending_hitl_key("cliente-A")), "el cliente que lanzó no recibió el prompt"
    assert not store.get(pending_hitl_key("cliente-B")), (
        "un cliente que NO lanzó el Ritual puede aprobar una operación destructiva"
    )
    assert not store.get(STORE_KEY_PENDING_HITL), "quedó también en la clave global: todo cliente lo seguiría viendo"


@pytest.mark.parametrize("categoria", ["tool_execution", "sandbox_promotion", "download"])
async def test_todas_las_categorias_del_modal_se_scopean(categoria: str) -> None:
    """Las tres categorías que parkean modal deben scopearse, no solo una."""
    store = ReactiveStore()
    notify, _ = _bridge_con_store(store, client_id_getter=lambda: "cliente-A")

    await notify(_Req(category=categoria))

    assert store.get(pending_hitl_key("cliente-A")), f"{categoria} no se parkeó en el cliente"
    assert not store.get(STORE_KEY_PENDING_HITL), f"{categoria} quedó en la clave global"


async def test_sin_cliente_lanzador_conserva_la_clave_global() -> None:
    """Una solicitud del agente/backend no tiene dueño: no se descarta ni se reruta."""
    store = ReactiveStore()
    notify, _ = _bridge_con_store(store, client_id_getter=lambda: None)

    await notify(_Req())

    assert store.get(STORE_KEY_PENDING_HITL), (
        "una aprobación sin cliente lanzador se perdió: nadie podría aprobarla nunca"
    )


async def test_las_categorias_ajenas_siguen_delegando() -> None:
    """Guard de no-regresión: el scoping no debe robarle categorías a Telegram."""
    store = ReactiveStore()
    delegados: list[Any] = []

    async def _delegate(req: Any) -> None:
        delegados.append(req)

    notify, _ = _bridge_con_store(store, client_id_getter=lambda: "cliente-A", delegate=_delegate)

    await notify(_Req(category="scope"))

    assert len(delegados) == 1, "una categoría ajena dejó de delegar al closure original"
    assert not store.get(pending_hitl_key("cliente-A"))


# ---------------------------------------------------------------------------
# Qué ve cada cliente al renderizar
# ---------------------------------------------------------------------------


def test_cada_cliente_resuelve_solo_su_propia_aprobacion() -> None:
    """El panel de B no debe ver la aprobación parkeada para A."""
    store = ReactiveStore()
    store.set(pending_hitl_key("cliente-A"), {"request_id": "req-A"})

    propia = resolve_pending_hitl(store, "cliente-A")
    ajena = resolve_pending_hitl(store, "cliente-B")

    assert propia is not None and propia["request_id"] == "req-A"
    assert ajena is None, "el cliente B renderiza el modal de un Ritual que no lanzó"


def test_una_aprobacion_sin_dueno_la_ven_todos() -> None:
    """La solicitud global (agente/backend) sigue siendo visible para cualquiera."""
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-global"})

    for cliente in ("cliente-A", "cliente-B", None):
        vista = resolve_pending_hitl(store, cliente)
        assert vista is not None and vista["request_id"] == "req-global", (
            f"{cliente} no vería una aprobación sin dueño y quedaría colgada hasta el timeout"
        )


def test_la_propia_gana_sobre_la_global() -> None:
    """Con ambas presentes, el cliente atiende primero la que él lanzó."""
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-global"})
    store.set(pending_hitl_key("cliente-A"), {"request_id": "req-A"})

    vista = resolve_pending_hitl(store, "cliente-A")

    assert vista is not None and vista["request_id"] == "req-A"


# ---------------------------------------------------------------------------
# El ContextVar del dispatch
# ---------------------------------------------------------------------------


async def test_run_ritual_arma_y_desarma_el_client_id() -> None:
    """Espeja el contrato de ``_ritual_auto_approve``: armado solo durante el dispatch."""
    store = ReactiveStore()
    visto: list[str | None] = []

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            visto.append(ritual_client_id())
            return {"success": True}

    assert ritual_client_id() is None, "el ContextVar arranca sin armar"

    await run_ritual("loot", supervisor=_Supervisor(), store=store, client_id="cliente-A")

    assert visto == ["cliente-A"], "el dispatch no vio el cliente que lanzó el Ritual"
    assert ritual_client_id() is None, "el ContextVar quedó armado tras el dispatch"


async def test_un_dispatch_concurrente_no_hereda_el_cliente() -> None:
    """Otra task (Telegram/LLM) debe ver el default, no el cliente del Ritual.

    Es la misma garantía que M-9 le da al auto-approve: la copia de contexto de
    una task ajena no ve lo que armó la task del dispatch.
    """
    store = ReactiveStore()
    ajeno: list[str | None] = []
    en_vuelo = asyncio.Event()
    seguir = asyncio.Event()

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            en_vuelo.set()
            await seguir.wait()
            return {"success": True}

    async def _otra_task() -> None:
        await en_vuelo.wait()
        ajeno.append(ritual_client_id())
        seguir.set()

    await asyncio.gather(
        run_ritual("loot", supervisor=_Supervisor(), store=store, client_id="cliente-A"),
        _otra_task(),
    )

    assert ajeno == [None], "una task concurrente heredó el cliente del Ritual"


async def test_el_finally_limpia_la_clave_del_cliente() -> None:
    """Al terminar el dispatch no puede quedar un modal stale en el cliente."""
    store = ReactiveStore()

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            store.set(pending_hitl_key("cliente-A"), {"request_id": "req-A"})
            return {"success": True}

    await run_ritual("loot", supervisor=_Supervisor(), store=store, client_id="cliente-A")

    assert not store.get(pending_hitl_key("cliente-A")), "quedó un modal colgado en el cliente tras terminar el Ritual"
