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
from collections.abc import Callable
from typing import Any

import pytest

from sky_claw.app.gui.controllers.ritual_runner import (
    HITL_OWNER_TAB,
    STORE_KEY_PENDING_HITL,
    STORE_KEY_RITUAL_IN_FLIGHT,
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


# ---------------------------------------------------------------------------
# Ancla de familia: TODO lanzador que parkea una aprobación
# ---------------------------------------------------------------------------
#
# P1-7 reincidió porque el fix original cubrió run_ritual y dejó intacto a su
# hermano run_ritual_install — 90 líneas más abajo en el MISMO archivo, con la
# misma estructura (single-flight, try/except/finally, misma clave del store).
# Lo atajó un bot revisor, no la suite. Y la primera reparación fue otro caso
# escrito a mano, que no habría atajado a un tercer lanzador.
#
# Estos tests convierten «acordate de mirar al hermano» en un fallo de CI:
# la familia se detecta por introspección y se congela contra una lista
# literal, y cada miembro necesita su receta de invocación. Un lanzador nuevo
# rompe el ancla hasta que se lo agrega — y agregarlo lo mete automáticamente
# en los tests de comportamiento de abajo, que es lo que realmente obliga.
#
# Mismo instrumento que ya usa tests/test_ritual_dispatch.py con
# `assert RITUAL_TOOL_MAP == {...}`: enumeración literal, no muestra a mano.

#: Recibir ``tab_id`` ES la firma de un lanzador: significa que su dispatch
#: puede terminar parkeando una aprobación con dueño en el store compartido.
#: ``run_ritual_resume`` (post-D2, F-001) es el tercer miembro: atraviesa el
#: MISMO dispatcher HITL-gated de generate_lods con un payload resume, así que
#: comparte exactamente la misma obligación de scoping que sus hermanos.
LANZADORES_QUE_PARKEAN_APROBACION = frozenset(
    {"run_ritual", "run_ritual_install", "run_ritual_resume"}
)


def _lanzadores_detectados() -> frozenset[str]:
    """Corrutinas públicas DEFINIDAS en ``ritual_runner`` que reciben un ``store``.

    El filtro por ``__module__`` no es cosmético: ``getmembers`` también devuelve
    lo que el módulo *importa*, así que una corrutina ajena con un parámetro
    ``store`` rompería el ancla sin que exista un lanzador nuevo. Un ancla que
    grita en falso termina desactivada, y ahí deja de proteger.
    """
    import inspect

    from sky_claw.app.gui.controllers import ritual_runner

    return frozenset(
        nombre
        for nombre, fn in inspect.getmembers(ritual_runner, inspect.iscoroutinefunction)
        if not nombre.startswith("_")
        and fn.__module__ == ritual_runner.__name__
        and "store" in inspect.signature(fn).parameters
    )


async def _invocar_run_ritual(store: ReactiveStore, tab_id: str | None, espia: Callable[[], None]) -> None:
    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            espia()  # acá corre el gate HITL, inline en esta misma task
            return {"success": True}

    await run_ritual("loot", supervisor=_Supervisor(), store=store, tab_id=tab_id)


async def _invocar_run_ritual_resume(store: ReactiveStore, tab_id: str | None, espia: Callable[[], None]) -> None:
    """Misma receta que ``run_ritual``: el resume despacha por el MISMO
    dispatcher HITL-gated, así que el espía debe correr en la task del
    lanzador y la aprobación debe quedar scoped al mismo ``tab_id``."""
    from sky_claw.app.gui.controllers.ritual_runner import run_ritual_resume

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            espia()  # acá corre el gate HITL, inline en esta misma task
            return {"success": True}

    await run_ritual_resume("dyndolod", supervisor=_Supervisor(), store=store, tab_id=tab_id)


async def _invocar_run_ritual_install(store: ReactiveStore, tab_id: str | None, espia: Callable[[], None]) -> None:
    from sky_claw.app.gui.controllers.ritual_runner import run_ritual_install

    class _Installer:
        async def ensure_loot(self, _install_dir: Any, _session: Any) -> dict:
            espia()
            return {"success": True}

    class _Ctx:
        tools_installer = _Installer()
        install_dir = None
        session = None

    await run_ritual_install("loot", app_context=_Ctx(), store=store, tab_id=tab_id)


#: Cómo llevar a cada lanzador hasta su punto de aprobación HITL.
RECETAS_DE_INVOCACION = {
    "run_ritual": _invocar_run_ritual,
    "run_ritual_install": _invocar_run_ritual_install,
    "run_ritual_resume": _invocar_run_ritual_resume,
}


def test_la_familia_de_lanzadores_esta_congelada() -> None:
    """Un lanzador nuevo no puede entrar sin pasar por estos tests.

    Si este test rompe, agregaste (o renombraste) una corrutina que recibe
    ``store``. No lo "arregles" tocando la constante y listo: agregale su
    receta en ``RECETAS_DE_INVOCACION``, y los tests parametrizados de abajo
    van a exigirle el mismo trato que a sus hermanos.
    """
    import inspect

    from sky_claw.app.gui.controllers import ritual_runner

    detectados = _lanzadores_detectados()
    assert detectados == LANZADORES_QUE_PARKEAN_APROBACION, (
        "cambió la familia de lanzadores que parkean aprobaciones HITL: "
        "cada miembro necesita marcar su dueño y no desalojar aprobaciones ajenas"
    )
    for nombre in detectados:
        fn = getattr(ritual_runner, nombre)
        assert "tab_id" in inspect.signature(fn).parameters, f"{nombre} no acepta tab_id"


def test_cada_lanzador_tiene_receta_de_invocacion() -> None:
    """Sin receta, un lanzador quedaría fuera de los tests parametrizados."""
    assert set(RECETAS_DE_INVOCACION) == set(LANZADORES_QUE_PARKEAN_APROBACION)


@pytest.mark.parametrize("nombre", sorted(LANZADORES_QUE_PARKEAN_APROBACION))
async def test_todo_lanzador_marca_su_dueno(nombre: str) -> None:
    """El defecto original: la aprobación quedaba sin dueño y la accionaba cualquiera."""
    store = ReactiveStore()
    visto: list[str | None] = []

    await RECETAS_DE_INVOCACION[nombre](store, "tab-A", lambda: visto.append(ritual_tab_id()))

    assert visto == ["tab-A"], f"{nombre} no armó el dueño: su aprobación queda accionable desde cualquier pestaña"


@pytest.mark.parametrize("nombre", sorted(LANZADORES_QUE_PARKEAN_APROBACION))
async def test_ningun_lanzador_desaloja_una_aprobacion_ajena(nombre: str) -> None:
    """El `finally` no puede borrar lo que no es suyo.

    ``STORE_KEY_RITUAL_IN_FLIGHT`` solo serializa lanzadores entre sí; no bloquea
    un ``tool_execution`` concurrente del agente LLM o Telegram, que parkea bajo
    la misma clave global. Limpiar a ciegas dejaba esa aprobación —que nadie
    respondió— invisible hasta su propio timeout fail-closed.
    """
    store = ReactiveStore()
    ajena = {"request_id": "req-ajena", HITL_OWNER_TAB: "tab-B"}

    await RECETAS_DE_INVOCACION[nombre](store, "tab-A", lambda: store.set(STORE_KEY_PENDING_HITL, dict(ajena)))

    assert store.get(STORE_KEY_PENDING_HITL) == ajena, (
        f"el finally de {nombre} desalojó una aprobación ajena que nadie respondió"
    )


@pytest.mark.parametrize("nombre", sorted(LANZADORES_QUE_PARKEAN_APROBACION))
async def test_todo_lanzador_limpia_su_propia_aprobacion(nombre: str) -> None:
    """El camino inverso: la propia sí se limpia, para no dejar un modal stale.

    A diferencia de sus dos hermanos, este test afirma una AUSENCIA, así que
    pasaría en verde si la receta nunca llegara al punto de aprobación (un
    early-return por ``STORE_KEY_RITUAL_IN_FLIGHT``, un installer ausente): no
    se parkea nada y "está vacío" se cumple sin haber probado nada. El flag
    corta ese falso verde.
    """
    store = ReactiveStore()
    propia = {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"}
    parkeada = False

    def _parkear() -> None:
        nonlocal parkeada
        parkeada = True
        store.set(STORE_KEY_PENDING_HITL, dict(propia))

    await RECETAS_DE_INVOCACION[nombre](store, "tab-A", _parkear)

    assert parkeada, f"la receta de {nombre} nunca llegó al punto de aprobación: el test no probó nada"
    assert not store.get(STORE_KEY_PENDING_HITL), f"{nombre} dejó su propio modal colgado al terminar"


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


async def test_el_ritual_falla_sin_perder_la_limpieza() -> None:
    """El hermano ``run_ritual`` tambien libera el estado ante una excepcion."""
    store = ReactiveStore()

    class _Supervisor:
        async def dispatch_tool(self, _name: str, _args: dict) -> dict:
            store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"})
            raise RuntimeError("dispatch failed")

    await run_ritual("loot", supervisor=_Supervisor(), store=store, tab_id="tab-A")

    assert not store.get(STORE_KEY_PENDING_HITL), "un fallo de Ritual dejo el modal colgado"
    assert ritual_tab_id() is None, "el ContextVar quedo armado tras un fallo de Ritual"
    assert not store.get(STORE_KEY_RITUAL_IN_FLIGHT), "un fallo de Ritual dejo el single-flight trabado"


async def test_el_ritual_de_instalacion_falla_sin_perder_la_limpieza() -> None:
    """Camino de excepción del installer: el `finally` corre igual.

    Complementa a los parametrizados de la familia, que solo ejercitan el camino
    feliz. Acá el installer explota y la aprobación propia tiene que limpiarse
    igual, o queda un modal colgado tras un fallo de descarga.
    """
    from sky_claw.app.gui.controllers.ritual_runner import run_ritual_install

    store = ReactiveStore()

    class _Installer:
        async def ensure_loot(self, _install_dir: Any, _session: Any) -> object:
            store.set(STORE_KEY_PENDING_HITL, {"request_id": "req-A", HITL_OWNER_TAB: "tab-A"})
            raise RuntimeError("se cayó la descarga")

    class _Ctx:
        tools_installer = _Installer()
        install_dir = None
        session = None

    await run_ritual_install("loot", app_context=_Ctx(), store=store, tab_id="tab-A")

    assert not store.get(STORE_KEY_PENDING_HITL), "un fallo de instalación dejó el modal colgado"
    assert ritual_tab_id() is None, "el ContextVar quedó armado tras un fallo de instalación"
    assert not store.get(STORE_KEY_RITUAL_IN_FLIGHT), "un fallo de instalación dejó el single-flight trabado"
