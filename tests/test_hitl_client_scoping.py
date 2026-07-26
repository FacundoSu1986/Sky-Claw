"""P1-7 — el gate HITL debe ser solo del cliente que lanzó el Ritual.

La aprobación de operaciones DESTRUCTIVAS se parkeaba en una única clave del
store (``pending_hitl``), y ``get_store()`` es un singleton de proceso
(``global _store``). Con dos pestañas abiertas —o un F5 sin cerrar la
anterior— el modal se renderiza y es accionable en AMBAS: cualquier sesión
podía aprobar un Ritual que no inició.

El fix espeja el patrón que este mismo módulo ya usa para el auto-approve
(``_ritual_auto_approve``, M-9): un ContextVar scoped a la task del dispatch
lleva la identidad de la pestaña que lanzó, y el bridge la estampa en el
payload; el panel solo renderiza la suya.

Dos decisiones que el diseño ingenuo erraba:

- La identidad es ``tab_id``, NO ``client.id``. Este último es un ``uuid4()``
  por instancia y cada carga de página crea un Client nuevo, así que un F5
  habría hecho desaparecer el modal y dejado la aprobación huérfana hasta su
  timeout. El ``tab_id`` lo manda el navegador en el handshake y sobrevive la
  recarga.
- El dueño va en el PAYLOAD, no en la clave. ``ReactiveStore.subscribe`` es
  estrictamente por clave exacta y la página suscribe el refresh del modal
  sobre ``STORE_KEY_PENDING_HITL``: una clave derivada por pestaña no
  dispararía ese refresh y el modal no aparecería nunca.

Alcance deliberado: una solicitud SIN pestaña lanzadora (agente LLM, Telegram,
backend) queda sin dueño y sigue visible para todas. Nadie la "lanzó" desde la
GUI, y ocultarla la dejaría colgada hasta el timeout sin que nadie pudiera
responderla.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sky_claw.app.gui.controllers.ritual_runner import (
    HITL_OWNER_TAB,
    STORE_KEY_PENDING_HITL,
    clear_answered_hitl,
    clear_owned_hitl,
    make_gui_hitl_notify,
    resolve_pending_hitl,
    ritual_tab_id,
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


def _bridge_con_store(store: ReactiveStore, tab_id_getter, delegate=None):
    """Arma el notify del GUI cableado como lo hace el bootloader."""
    respondidos: list[tuple[str, bool]] = []

    async def _respond(request_id: str, approved: bool) -> None:
        respondidos.append((request_id, approved))

    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=lambda payload: store.set(STORE_KEY_PENDING_HITL, payload),
        auto_approve_getter=lambda: False,
        tab_id_getter=tab_id_getter,
        delegate=delegate,
    )
    return notify, respondidos


# ---------------------------------------------------------------------------
# El dueño viaja en el payload
# ---------------------------------------------------------------------------


async def test_la_solicitud_queda_marcada_con_la_pestana_que_la_lanzo() -> None:
    """El caso del bug: A lanza el Ritual, B no debe poder aprobarlo."""
    store = ReactiveStore()
    notify, _ = _bridge_con_store(store, tab_id_getter=lambda: "tab-A")

    await notify(_Req())

    pendiente = store.get(STORE_KEY_PENDING_HITL)
    assert pendiente, "la pestaña que lanzó no recibió el prompt"
    assert pendiente[HITL_OWNER_TAB] == "tab-A", "la solicitud no quedó marcada con su dueño"


async def test_el_dueno_va_en_el_payload_no_en_la_clave() -> None:
    """Contrato estructural: la clave del store NO puede depender de la pestaña.

    ``ReactiveStore.subscribe`` es estrictamente por clave exacta y la página
    suscribe el refresh del modal sobre ``STORE_KEY_PENDING_HITL``. Derivar una
    clave por pestaña no dispararía ese refresh: el modal no aparecería NUNCA.
    """
    store = ReactiveStore()
    refrescos: list[int] = []
    store.subscribe(STORE_KEY_PENDING_HITL, lambda: refrescos.append(1))
    notify, _ = _bridge_con_store(store, tab_id_getter=lambda: "tab-A")

    await notify(_Req())

    assert refrescos, (
        "parkear la aprobación no despertó al subscriber de STORE_KEY_PENDING_HITL: el modal no se renderizaría nunca"
    )


@pytest.mark.parametrize("categoria", ["tool_execution", "sandbox_promotion", "download"])
async def test_todas_las_categorias_del_modal_marcan_dueno(categoria: str) -> None:
    """Las tres categorías que parkean modal deben marcarse, no solo una."""
    store = ReactiveStore()
    notify, _ = _bridge_con_store(store, tab_id_getter=lambda: "tab-A")

    await notify(_Req(category=categoria))

    pendiente = store.get(STORE_KEY_PENDING_HITL)
    assert pendiente and pendiente[HITL_OWNER_TAB] == "tab-A", f"{categoria} no marcó al dueño"


async def test_sin_pestana_lanzadora_queda_sin_dueno() -> None:
    """Una solicitud del agente/backend no tiene dueño: no se descarta ni se reruta."""
    store = ReactiveStore()
    notify, _ = _bridge_con_store(store, tab_id_getter=lambda: None)

    await notify(_Req())

    pendiente = store.get(STORE_KEY_PENDING_HITL)
    assert pendiente, "una aprobación sin pestaña lanzadora se perdió: nadie podría aprobarla"
    assert pendiente[HITL_OWNER_TAB] is None


async def test_las_categorias_ajenas_siguen_delegando() -> None:
    """Guard de no-regresión: el scoping no debe robarle categorías a Telegram."""
    store = ReactiveStore()
    delegados: list[Any] = []

    async def _delegate(req: Any) -> None:
        delegados.append(req)

    notify, _ = _bridge_con_store(store, tab_id_getter=lambda: "tab-A", delegate=_delegate)

    await notify(_Req(category="scope"))

    assert len(delegados) == 1, "una categoría ajena dejó de delegar al closure original"
    assert not store.get(STORE_KEY_PENDING_HITL)


# ---------------------------------------------------------------------------
# Qué ve cada pestaña al renderizar
# ---------------------------------------------------------------------------


def test_solo_la_pestana_duena_ve_su_aprobacion() -> None:
    """El panel de B no debe renderizar el modal de un Ritual que lanzó A."""
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"})

    assert resolve_pending_hitl(store, "tab-A") is not None
    assert resolve_pending_hitl(store, "tab-B") is None, (
        "una pestaña que NO lanzó el Ritual puede aprobar una operación destructiva"
    )


def test_la_misma_pestana_la_sigue_viendo_tras_un_f5() -> None:
    """F5 conserva el ``tab_id``, así que la aprobación sigue a la vista.

    Es lo que distingue ``tab_id`` de ``client.id``: este último cambia en cada
    carga de página y habría hecho desaparecer el modal al recargar.
    """
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"})

    # Misma pestaña, Client nuevo tras la recarga: el tab_id no cambió.
    assert resolve_pending_hitl(store, "tab-A") is not None, (
        "tras un F5 el modal desaparecería y la aprobación quedaría huérfana"
    )


def test_una_aprobacion_sin_dueno_la_ven_todas() -> None:
    """La solicitud del agente/backend sigue siendo visible para cualquiera."""
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-global", HITL_OWNER_TAB: None})

    for pestana in ("tab-A", "tab-B", None):
        assert resolve_pending_hitl(store, pestana) is not None, (
            f"{pestana} no vería una aprobación sin dueño y quedaría colgada hasta el timeout"
        )


def test_una_solicitud_legacy_sin_marca_la_ven_todas() -> None:
    """Compat: un payload sin la marca de dueño se trata como sin dueño."""
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-legacy"})

    assert resolve_pending_hitl(store, "tab-A") is not None


# ---------------------------------------------------------------------------
# El ContextVar del dispatch
# ---------------------------------------------------------------------------


async def test_run_ritual_arma_y_desarma_el_tab_id() -> None:
    """Espeja el contrato de ``_ritual_auto_approve``: armado solo durante el dispatch."""
    store = ReactiveStore()
    visto: list[str | None] = []

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            visto.append(ritual_tab_id())
            return {"success": True}

    assert ritual_tab_id() is None, "el ContextVar arranca sin armar"

    await run_ritual("loot", supervisor=_Supervisor(), store=store, tab_id="tab-A")

    assert visto == ["tab-A"], "el dispatch no vio la pestaña que lanzó el Ritual"
    assert ritual_tab_id() is None, "el ContextVar quedó armado tras el dispatch"


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
        ajeno.append(ritual_tab_id())
        seguir.set()

    await asyncio.gather(
        run_ritual("loot", supervisor=_Supervisor(), store=store, tab_id="tab-A"),
        _otra_task(),
    )

    assert ajeno == [None], "una task concurrente heredó la pestaña del Ritual"


async def test_el_finally_limpia_la_pendiente_de_este_mismo_dispatch() -> None:
    """Al terminar el dispatch no puede quedar un modal stale — si es el propio."""
    store = ReactiveStore()

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"})
            return {"success": True}

    await run_ritual("loot", supervisor=_Supervisor(), store=store, tab_id="tab-A")

    assert not store.get(STORE_KEY_PENDING_HITL), "quedó un modal colgado tras terminar el Ritual"


async def test_el_finally_no_desaloja_una_pendiente_ajena() -> None:
    """Una aprobación de OTRO origen no puede desaparecer al terminar este Ritual.

    ``STORE_KEY_RITUAL_IN_FLIGHT`` solo serializa entre Rituales/instalaciones;
    no bloquea un ``tool_execution`` concurrente disparado por el agente LLM o
    Telegram, que comparte la misma clave global del store. Si el `finally` de
    tab-A limpia a ciegas, esa otra aprobación —que nadie respondió todavía—
    desaparece de la vista sin que nadie pudiera contestarla, hasta su propio
    timeout fail-closed.
    """
    store = ReactiveStore()

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            # Simula al agente concurrente parkeando SU pendiente mientras el
            # Ritual de tab-A sigue en vuelo.
            store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-ajena", HITL_OWNER_TAB: "tab-B"})
            return {"success": True}

    await run_ritual("loot", supervisor=_Supervisor(), store=store, tab_id="tab-A")

    assert store.get(STORE_KEY_PENDING_HITL) == {"request_id": "req-ajena", HITL_OWNER_TAB: "tab-B"}, (
        "el finally de un Ritual desalojó una aprobación ajena que nadie respondió"
    )


# ---------------------------------------------------------------------------
# Hallazgos de revisión (bot de CI)
# ---------------------------------------------------------------------------


def test_la_identidad_es_la_pestana_no_la_carga_de_pagina() -> None:
    """F5 NO debe perder el modal: la identidad tiene que sobrevivir la recarga.

    ``Client.id`` es ``str(uuid.uuid4())`` por INSTANCIA, y cada recarga crea un
    Client nuevo: scopear por él hacía desaparecer el modal al apretar F5, y la
    aprobación quedaba huérfana en el backend hasta su timeout. ``tab_id`` lo
    manda el navegador en el handshake (por eso NiceGUI tiene ``old_tab_id`` +
    ``copy_tab`` para migrar el storage), así que sobrevive la recarga.
    """
    from sky_claw.app.gui.views.forge_dashboard import tab_id_de

    class _ClienteTrasF5:
        id = "client-2"  # nuevo en cada carga de página
        tab_id = "tab-1"  # estable: lo manda el navegador

    assert tab_id_de(_ClienteTrasF5()) == "tab-1", (
        "la identidad se toma de client.id: un F5 haría desaparecer el modal y "
        "dejaría la aprobación huérfana hasta el timeout"
    )


def test_sin_handshake_todavia_no_hay_pestana() -> None:
    """Antes del handshake ``tab_id`` es None; no inventar una identidad."""
    from sky_claw.app.gui.views.forge_dashboard import tab_id_de

    class _SinHandshake:
        id = "client-1"
        tab_id = None

    assert tab_id_de(_SinHandshake()) is None


def test_un_click_tardio_no_desaloja_la_solicitud_siguiente() -> None:
    """Responder algo ya reemplazado no puede borrar la solicitud vigente.

    Escenario: la pestaña muestra ``req-vieja``; antes de que el operador
    conteste, el backend parkea ``req-nueva`` (el timeout de la anterior la
    reemplazó). El click tardío sobre la vieja no debe desalojar la nueva, que
    nadie respondió: quedaría huérfana en el backend hasta expirar, y sin nada
    en pantalla.
    """
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-nueva"})

    clear_answered_hitl(store, "req-vieja")

    assert store.get(STORE_KEY_PENDING_HITL) == {"request_id": "req-nueva"}, (
        "un click tardío borró la solicitud vigente sin responderla: queda huérfana hasta el timeout"
    )


def test_responder_la_global_la_limpia() -> None:
    """El camino inverso: si lo respondido fue lo global, eso se limpia."""
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-agente"})

    clear_answered_hitl(store, "req-agente")

    assert not store.get(STORE_KEY_PENDING_HITL)


def test_el_clear_tolera_que_no_haya_nada_pendiente() -> None:
    """Sin solicitud viva (ya la limpió el ``finally`` del dispatch) no explota."""
    store = ReactiveStore()

    clear_answered_hitl(store, "req-A")

    assert store.get(STORE_KEY_PENDING_HITL) is None


def test_clear_owned_borra_solo_si_el_dueno_coincide() -> None:
    """``clear_owned_hitl`` es el guard que usa el ``finally`` de cada dispatch."""
    store = ReactiveStore()
    store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"})

    clear_owned_hitl(store, "tab-B")
    assert store.get(STORE_KEY_PENDING_HITL) == {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"}, (
        "clear_owned_hitl borró una pendiente de OTRA pestaña"
    )

    clear_owned_hitl(store, "tab-A")
    assert not store.get(STORE_KEY_PENDING_HITL), "clear_owned_hitl no borró la pendiente de su propio dueño"


async def test_el_ritual_de_instalacion_tambien_marca_su_dueno() -> None:
    """El botón "Instalar" despacha un HITL ``download`` y también debe scoparse.

    Es egress de red y por eso NUNCA se auto-aprueba: es de las aprobaciones más
    sensibles que hay. Sin armar el ContextVar en este camino, la solicitud
    quedaba sin dueño y era accionable desde cualquier pestaña — el mismo
    defecto P1-7, entrando por la puerta de instalación.
    """
    from sky_claw.app.gui.controllers.ritual_runner import run_ritual_install

    store = ReactiveStore()
    visto: list[str | None] = []

    class _Installer:
        async def ensure_loot(self, _install_dir: Any, _session: Any) -> object:
            # Acá corre el gate HITL del installer, inline en esta misma task.
            visto.append(ritual_tab_id())
            raise RuntimeError("corta acá: solo interesa el contexto")

    class _Ctx:
        tools_installer = _Installer()
        install_dir = None
        session = None

    await run_ritual_install("loot", app_context=_Ctx(), store=store, tab_id="tab-A")

    assert visto == ["tab-A"], (
        "el Ritual de instalación no armó el dueño: la aprobación de descarga queda accionable desde cualquier pestaña"
    )


async def test_el_finally_de_la_instalacion_no_desaloja_una_pendiente_ajena() -> None:
    """El mismo cuidado del ``finally`` de ``run_ritual`` aplica a la instalación.

    ``run_ritual`` y ``run_ritual_install`` comparten ``STORE_KEY_RITUAL_IN_FLIGHT``
    entre sí, pero ninguno de los dos bloquea un ``tool_execution`` concurrente
    del agente/Telegram, que parkea bajo la misma clave global.
    """
    from sky_claw.app.gui.controllers.ritual_runner import run_ritual_install

    store = ReactiveStore()

    class _Installer:
        async def ensure_loot(self, _install_dir: Any, _session: Any) -> object:
            store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-ajena", HITL_OWNER_TAB: "tab-B"})
            return {"success": True}

    class _Ctx:
        tools_installer = _Installer()
        install_dir = None
        session = None

    await run_ritual_install("loot", app_context=_Ctx(), store=store, tab_id="tab-A")

    assert store.get(STORE_KEY_PENDING_HITL) == {"request_id": "req-ajena", HITL_OWNER_TAB: "tab-B"}, (
        "el finally de la instalación desalojó una aprobación ajena que nadie respondió"
    )
