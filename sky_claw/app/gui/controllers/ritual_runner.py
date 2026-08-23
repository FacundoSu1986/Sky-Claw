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

import contextvars
import logging
import os
import pathlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sky_claw.local.tools.tool_result import normalize_tool_result
from sky_claw.local.tools_installer import InstallVerification

if TYPE_CHECKING:
    from sky_claw.app.gui.state.reactive_store import ReactiveStore

logger = logging.getLogger(__name__)

#: M-9: auto-approve armado SÓLO para el árbol de tasks del dispatch en curso.
#: Un ContextVar (no un flag global del store) garantiza que la aprobación
#: automática quede scoped a exactamente la task de ``run_ritual`` que lo armó:
#: ``HITLGuard.request_approval`` invoca ``notify_fn`` inline dentro de esa misma
#: task, mientras que un ``tool_execution`` concurrente de Telegram/LLM/API corre
#: en OTRA task cuya copia de contexto tiene el default ``False`` → nunca se
#: auto-aprueba. Antes esto era un bool global del store, armado por toda la
#: duración del dispatch del GUI, que auto-aprobaba cualquier tool_execution de
#: cualquier source concurrente (bypass del gate HITL).
_ritual_auto_approve: contextvars.ContextVar[bool] = contextvars.ContextVar("ritual_auto_approve", default=False)


def ritual_auto_approve_armed() -> bool:
    """True si el dispatch de la task actual armó auto-approve (Modo local).

    Getter que el bootloader pasa a :func:`make_gui_hitl_notify`; lee el
    ContextVar scoped a la task, no un flag global.
    """
    return _ritual_auto_approve.get()


#: P1-7: identidad del cliente que lanzó el Ritual en curso. Mismo mecanismo y
#: mismo motivo que ``_ritual_auto_approve``: la aprobación de una operación
#: DESTRUCTIVA se parkeaba en una única clave del store, y ``get_store()`` es un
#: singleton de proceso — con dos pestañas (o un F5 sin cerrar la anterior) el
#: modal se renderizaba y era accionable en AMBAS, así que cualquier sesión podía
#: aprobar un Ritual que no inició. El ContextVar lleva el dueño hasta el bridge
#: HITL, que corre inline dentro de la misma task del dispatch.
_ritual_tab_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("ritual_tab_id", default=None)


def ritual_tab_id() -> str | None:
    """Id del cliente que lanzó el dispatch de la task actual, o ``None``.

    ``None`` significa que la solicitud no vino de un Ritual del GUI (agente
    LLM, Telegram, backend): no tiene dueño al que scopearla.
    """
    return _ritual_tab_id.get()


# Store key the bridge parks a pending tool_execution approval under, and the key
# the run flow publishes its result feedback under (both consumed by refreshable
# panels in forge_dashboard so the chat input is never reset).
STORE_KEY_PENDING_HITL = "pending_hitl"
STORE_KEY_RITUAL_FEEDBACK = "ritual_feedback"
# F-001 (post-D2): el RESULTADO estructural del último dispatch de Ritual
# (dict crudo con ``needs_deployment``/``texgen_mod_path``) para que el panel
# ofrezca la acción de Resume sin parsear el texto del feedback. R-004/R-005:
# viaja en ENVELOPE — dueño (``owner_tab``) + ``tool_key`` real de la corrida +
# dict — para que una acción destructiva no migre entre pestañas ni se derive
# con un literal de tool desde la vista.
#
# PATCH 0010 (CROSS_TAB_RITUAL_RESULT_EVICTION): la clave sigue siendo UNA —
# los subscribers de ``ReactiveStore`` son estrictamente por clave y la página
# refresca el panel sobre cambios del valor — pero el valor ahora es un
# CONTENEDOR multi-owner: un envelope POR ``owner_tab``. Cada pestaña sustituye
# o limpia SÓLO su slot; dos pestañas pueden conservar resultados pendientes
# simultáneamente. El single-flight global (``STORE_KEY_RITUAL_IN_FLIGHT``)
# serializa la EJECUCIÓN y no se toca: el estado POST-run es el multi-owner.
STORE_KEY_RITUAL_LAST_RESULT = "ritual_last_result"

#: Claves del envelope publicado bajo ``STORE_KEY_RITUAL_LAST_RESULT``.
RITUAL_RESULT_OWNER_TAB = "owner_tab"
RITUAL_RESULT_TOOL_KEY = "tool_key"
RITUAL_RESULT_PAYLOAD_KEY = "result"
#: Clave INTERNA del contenedor multi-owner (PATCH 0010): mapea ``owner_tab``
#: (o ``None`` para el resultado sin dueño) → envelope. Nunca se manipula desde
#: la vista; todo read/write/delete pasa por los helpers de este módulo.
RITUAL_RESULTS_BY_OWNER = "results_by_owner"


#: Dueño de una aprobación pendiente: el ``tab_id`` de la pestaña que lanzó el
#: Ritual, o ``None`` si no la originó una pestaña (agente LLM, Telegram,
#: backend). Viaja DENTRO del payload y no como parte de la clave: los
#: subscribers de ``ReactiveStore`` son estrictamente por clave, así que una
#: clave derivada por pestaña no dispararía el refresh que la página registra
#: sobre ``STORE_KEY_PENDING_HITL`` — el modal no aparecería nunca.
HITL_OWNER_TAB = "owner_tab"


def stamp_hitl_owner(payload: dict[str, Any], tab_id: str | None) -> dict[str, Any]:
    """Marca la solicitud con la pestaña dueña, sin mutar el payload original."""
    return {**payload, HITL_OWNER_TAB: tab_id}


def clear_answered_hitl(store: ReactiveStore, request_id: str) -> None:
    """Limpia la solicitud pendiente SOLO si es la que se acaba de responder.

    Un clear incondicional desalojaba una solicitud que nadie contestó: entre
    que se parkea una y el operador responde otra, borrar a ciegas la dejaba
    huérfana en el backend hasta su timeout fail-closed, y sin nada en pantalla.
    """
    pending = store.get(STORE_KEY_PENDING_HITL)
    if isinstance(pending, dict) and pending.get("request_id") == request_id:
        store.set(STORE_KEY_PENDING_HITL, None)


def clear_owned_hitl(store: ReactiveStore, tab_id: str | None) -> None:
    """Limpia la pendiente SOLO si su ``owner_tab`` coincide con ``tab_id``.

    El ``finally`` de ``run_ritual``/``run_ritual_install`` necesita descartar
    SU PROPIA aprobación si quedó sin responder (denegada/timeout), para no
    dejar un modal stale — pero un clear incondicional podía desalojar una
    pendiente AJENA: ``STORE_KEY_RITUAL_IN_FLIGHT`` solo serializa entre
    Rituales/instalaciones entre sí, no bloquea un ``tool_execution``
    concurrente del agente LLM o Telegram, que parkea bajo la misma clave
    global (review de PR #373).
    """
    pending = store.get(STORE_KEY_PENDING_HITL)
    if isinstance(pending, dict) and pending.get(HITL_OWNER_TAB) == tab_id:
        store.set(STORE_KEY_PENDING_HITL, None)


def resolve_pending_hitl(store: ReactiveStore, tab_id: str | None) -> dict[str, Any] | None:
    """La aprobación que ESTA pestaña debe renderizar, o ``None``.

    Solo la que ella lanzó. Una solicitud sin dueño (agente/backend) la ve
    cualquiera: nadie la "lanzó" desde la GUI, y ocultarla la dejaría colgada
    hasta el timeout sin que nadie pudiera responderla.

    Seam puro, para que el panel refrescable sea testeable sin NiceGUI.
    """
    pending = store.get(STORE_KEY_PENDING_HITL)
    if not isinstance(pending, dict):
        return None
    owner = pending.get(HITL_OWNER_TAB)
    if owner is None or owner == tab_id:
        return pending
    return None


def _es_envelope_legacy(raw: Any) -> bool:
    """True si ``raw`` es el envelope simple pre-multi-owner (PATCH 0010).

    El store es efímero en proceso, pero un hot reload o una corrida larga
    puede dejar publicado el shape anterior ``{owner_tab, tool_key, result}``.
    El reader lo adopta en transición en vez de descartar el resultado
    accionable que ya tenía un dueño legítimo.
    """
    return isinstance(raw, dict) and RITUAL_RESULT_TOOL_KEY in raw and RITUAL_RESULT_PAYLOAD_KEY in raw


def _resultados_por_owner(store: ReactiveStore) -> dict[str | None, dict[str, Any]]:
    """Resultados vigentes por ``owner_tab`` (``None`` = sin dueño).

    Normaliza el contenedor multi-owner y adopta el envelope legacy si aparece
    (transición sin migración durable). Devuelve el dict interno SÓLO para
    lectura: quien muta hace una copia antes (``dict(...)``) y escribe por
    :func:`_escribir_resultados_de_ritual`.
    """
    raw = store.get(STORE_KEY_RITUAL_LAST_RESULT)
    if _es_envelope_legacy(raw):
        owner = raw.get(RITUAL_RESULT_OWNER_TAB)
        return {owner: raw}
    if isinstance(raw, dict):
        resultados = raw.get(RITUAL_RESULTS_BY_OWNER)
        if isinstance(resultados, dict):
            return resultados
    return {}


def _escribir_resultados_de_ritual(
    store: ReactiveStore,
    resultados: dict[str | None, dict[str, Any]],
) -> None:
    """ÚNICO punto de producción que escribe ``STORE_KEY_RITUAL_LAST_RESULT``.

    Escribe el contenedor completo (o ``None`` si quedó vacío) como UNA
    mutación de la clave, para conservar el mecanismo de subscriptions del
    ``ReactiveStore``. Ancla por AST en ``test_ritual_result_cross_tab``: un
    ``store.set`` directo a esta clave fuera de acá es un clear/replace global
    accidental (el bug cross-tab) y rompe el test.
    """
    if resultados:
        store.set(STORE_KEY_RITUAL_LAST_RESULT, {RITUAL_RESULTS_BY_OWNER: dict(resultados)})
    else:
        store.set(STORE_KEY_RITUAL_LAST_RESULT, None)


def publicar_resultado_de_ritual(
    store: ReactiveStore,
    *,
    tool_key: str,
    resultado: dict[str, Any],
    tab_id: str | None,
) -> None:
    """Publica el resultado estructural en el slot del dueño (R-004/R-005).

    El envelope persiste juntos el ``owner_tab``, el ``tool_key`` REAL de la
    corrida y el dict estructurado — no sólo el result. Con eso, la acción de
    Resume sólo puede resolverse/consumirse desde la pestaña dueña, y la vista
    nunca hardcodea "dyndolod": el tool sale del dato, no de un literal.

    PATCH 0010: la publicación es PER-OWNER — cada pestaña sustituye SÓLO su
    resultado anterior. Un ritual de B ya no puede reemplazar el envelope
    accionable de A (CROSS_TAB_RITUAL_RESULT_EVICTION); dos pestañas pueden
    tener resultados pendientes simultáneamente. Un dispatch sin pestaña
    lanzadora (agente LLM, Telegram, backend) ocupa el slot sin dueño
    (``None``), visible para cualquiera como siempre.
    """
    resultados = dict(_resultados_por_owner(store))
    resultados[tab_id] = {
        RITUAL_RESULT_OWNER_TAB: tab_id,
        RITUAL_RESULT_TOOL_KEY: tool_key,
        RITUAL_RESULT_PAYLOAD_KEY: resultado,
    }
    _escribir_resultados_de_ritual(store, resultados)


def resolve_ritual_resume_action(store: ReactiveStore, tab_id: str | None) -> dict[str, Any] | None:
    """La acción de Resume que ESTA pestaña puede resolver/consumir, o ``None``.

    R-004: espeja :func:`resolve_pending_hitl` — una acción DESTRUCTIVA no
    migra entre pestañas. Sólo el ``owner_tab`` del envelope la ve; un
    resultado sin dueño (agente/backend, o dispatch sin contexto de pestaña)
    lo ve cualquiera, con el mismo criterio del modal HITL.

    R-005: el ``tool_key`` sale del envelope publicado por la corrida — un
    resultado de otro ritual con ``needs_deployment`` de fixture NO produce una
    acción de DynDOLOD. Seam puro, testeable sin NiceGUI.

    PATCH 0010 (precedencia multi-owner): la pestaña ve SU último resultado; si
    no tiene ninguno propio, ve el resultado sin dueño (política vigente:
    ownerless es visible para cualquiera). Nunca el de OTRA pestaña — con dos
    resultados pendientes, cada tab resuelve exactamente el suyo.
    """
    resultados = _resultados_por_owner(store)
    envelope = resultados.get(tab_id)
    if envelope is None:
        envelope = resultados.get(None)
    if not isinstance(envelope, dict):
        return None
    tool_key = envelope.get(RITUAL_RESULT_TOOL_KEY)
    resultado = envelope.get(RITUAL_RESULT_PAYLOAD_KEY)
    if not isinstance(tool_key, str) or not isinstance(resultado, dict):
        return None
    return resume_action_from_result(tool_key, resultado)


def clear_ritual_result_owned(store: ReactiveStore, tab_id: str | None) -> None:
    """Limpia el resultado accionable que ESTA pestaña puede desalojar (F-004).

    Tab B no puede CONSUMIR la acción de A, y tampoco borrarla con el ``×``:
    el clear/dismiss respeta el mismo ``owner_tab`` del envelope. Un resultado
    sin dueño conserva la política ya aceptada — lo limpia cualquiera. El
    ``tab_id=None`` (sin contexto de pestaña) sólo puede limpiar resultados sin
    dueño, igual que en :func:`resolve_pending_hitl`.

    PATCH 0010 (multi-owner): borra el slot PROPIO de ``tab_id``; si la pestaña
    no tiene slot propio, cae al slot sin dueño (política vigente). NUNCA borra
    el resultado de OTRA pestaña, ni siquiera un ``tab_id=None`` de contexto.
    """
    resultados = dict(_resultados_por_owner(store))
    if tab_id in resultados:
        del resultados[tab_id]
    elif None in resultados:
        del resultados[None]
    _escribir_resultados_de_ritual(store, resultados)


#: Per-client "Modo local" toggle, stored in ``app.storage.client`` (server-side,
#: one entry per browser connection, auto-cleared on disconnect) — NOT the global
#: store. So one window's choice never enables auto-approval for another client
#: (Codex review on #211).
CLIENT_KEY_AUTO_APPROVE = "modo_local"
#: Single-flight guard: a ritual is dispatching or awaiting approval right now.
STORE_KEY_RITUAL_IN_FLIGHT = "ritual_in_flight"
#: Último reporte de preflight (``PreflightReport.to_dict()``) que un dispatch
#: adjuntó — hoy solo el sort de LOOT lo produce. El panel refrescable
#: ``_ritual_preflight_panel`` lo renderiza con ``create_preflight_panel`` (T-16b).
STORE_KEY_RITUAL_PREFLIGHT = "ritual_preflight"

# Los 5 Rituales del Panel, cada uno con su estrategia HITL-gated en el dispatcher.
RITUAL_TOOL_MAP: dict[str, str] = {
    "loot": "execute_loot_sorting",
    "wrye_bash": "generate_bashed_patch",
    "dyndolod": "generate_lods",
    "pandora": "generate_animations",
    "xedit": "quick_auto_clean",
}


def ritual_tool_name(tool_key: str) -> str | None:
    """Map a Ritual's scanner tool key to its dispatcher tool name, or ``None``."""
    return RITUAL_TOOL_MAP.get(tool_key)


# Follow-up C: Ritual tool key → ``ToolsInstaller`` method for the "Instalar" button.
# Only the GitHub-release-backed tools have an auto-installer; Wrye Bash and DynDOLOD
# are not on GitHub releases, so they stay out of the map (the card keeps its interim
# "manual install" notice).
RITUAL_INSTALLER_MAP: dict[str, str] = {
    "loot": "ensure_loot",
    "xedit": "ensure_xedit",
    "pandora": "ensure_pandora",
    # SKSE vive acá y NO en la superficie del agente LLM (`setup_tools`): escribe
    # ejecutables en el directorio del juego, así que la aprobación tiene que ser la
    # del operador frente a la GUI.
    # `test_skse_es_gui_only_y_no_lo_alcanza_el_agente_llm` (tests/test_ritual_install.py)
    # congela el recorte por igualdad literal.
    "skse": "ensure_skse",
    # Community Shaders vive en AMBAS superficies (agente LLM y GUI, clase NGIO):
    # la aprobación de descarga corre por el mismo modal HITL category="download".
    # `test_ritual_installer_map_congela_las_tools_autoinstalables` congela la
    # presencia por igualdad literal.
    "community_shaders": "ensure_community_shaders",
}

#: Ritual tool key → the resolver env var seeded with the freshly installed exe path,
#: so a just-installed tool can run without waiting for the next environment scan.
#: Mirrors the var names in :class:`PathResolutionService` / ``_SNAPSHOT_TOOL_ENV``.
#: ``skse`` está deliberadamente ausente y por eso este mapa tiene una clave menos que
#: ``RITUAL_INSTALLER_MAP``: SKSE no es una tool que Sky-Claw ejecute (es un runtime que
#: carga el juego), no hay exe que resolver ni var en ``_SNAPSHOT_TOOL_ENV``.
RITUAL_INSTALL_ENV: dict[str, str] = {
    "loot": "LOOT_EXE",
    "xedit": "XEDIT_PATH",
    "pandora": "PANDORA_EXE",
}


def ritual_installer_name(tool_key: str) -> str | None:
    """Map a Ritual's scanner tool key to its ``ToolsInstaller`` method, or ``None``."""
    return RITUAL_INSTALLER_MAP.get(tool_key)


def summarize_ritual_result(tool_key: str, result: dict[str, Any]) -> tuple[str, str]:
    """Build a (message, kind) pair from a dispatcher result dict.

    ``kind`` is one of NiceGUI's notify types ("positive"/"negative"/"warning").
    Denied/timed-out HITL approvals get a friendly Spanish hint instead of the
    raw reason code.

    Contrato compartido (deuda #5 cerrada): los servicios emiten ``success`` +
    ``message``; :func:`normalize_tool_result` absorbe las shapes legacy
    (``logs``/``errors``/``error``/``stderr``/``details``), así que acá ya no se
    adivina inspeccionando claves.
    """
    normalized = normalize_tool_result(result)
    if normalized["success"]:
        return (f"Ritual «{tool_key}» completado.", "positive")

    reason = str(result.get("reason", "") or "")
    if reason in {"HITLApprovalDenied", "HITLGateUnavailable"}:
        return (
            "Ejecución no aprobada. Activá «Modo local» o aprobá la acción para continuar.",
            "negative",
        )
    return (f"El ritual «{tool_key}» falló: {normalized['message']}", "negative")


def preflight_from_result(result: Any) -> dict[str, Any] | None:
    """Extrae el reporte de preflight de un result de dispatch, o ``None``.

    Hoy solo el sort de LOOT corre preflight y adjunta
    ``result["preflight"] = PreflightReport.to_dict()`` cuando el semáforo no está
    verde o bloquea la mutación (``loot_service``). Defensivo: solo devuelve un
    dict; cualquier otra shape (sin la clave, o un valor no-dict) → ``None``.
    """
    if isinstance(result, dict):
        preflight = result.get("preflight")
        if isinstance(preflight, dict):
            return preflight
    return None


def make_gui_hitl_notify(
    *,
    respond: Callable[[str, bool], Awaitable[None]],
    set_pending: Callable[[dict[str, Any]], None],
    auto_approve_getter: Callable[[], bool],
    tab_id_getter: Callable[[], str | None] = ritual_tab_id,
    delegate: Callable[[Any], Awaitable[None]] | None,
) -> Callable[[Any], Awaitable[None]]:
    """Build the GUI's HITL ``notify_fn`` (composes over the original closure).

    For ``category == "tool_execution"`` the GUI owns the decision:
    auto-approve when the toggle is on, otherwise park the request via
    ``set_pending`` so the page renders an Aprobar/Denegar modal.

    For ``category == "download"`` (the "Instalar" button — Follow-up C) the GUI
    also parks an Aprobar/Denegar modal, but it is **never** auto-approved by
    "Modo local": a network download is egress and is always confirmed by hand.
    The parked entry carries the asset ``url`` so the modal can show its origin.

    For ``category == "sandbox_promotion"`` (T-27b·2, ADR 0005) the modal is
    also parked and **never** auto-approved: es la decisión post-run de
    promover el diff de un sandbox al perfil real — revisar ese diff es el
    propósito del sandbox, así que «Modo local» no la salta.

    Every other category falls through to ``delegate`` (the original Telegram
    closure), so scope approvals keep their existing behaviour.

    P1-7: las tres categorías que parkean modal se scopean al cliente que lanzó
    el Ritual (``tab_id_getter``). Sin él, la aprobación de una operación
    destructiva era accionable desde CUALQUIER sesión abierta. Una solicitud sin
    cliente lanzador (agente LLM, Telegram, backend) conserva la clave global:
    no tiene dueño al que scoparla, y descartarla la dejaría colgada hasta su
    timeout fail-closed.
    """

    async def _notify(req: Any) -> None:
        category = getattr(req, "category", "")
        tab_id = tab_id_getter()
        if category == "tool_execution":
            if auto_approve_getter():
                logger.info("HITL(GUI): auto-approving %s (Modo local ON)", req.request_id)
                await respond(req.request_id, True)
            else:
                set_pending(
                    stamp_hitl_owner(
                        {
                            "request_id": req.request_id,
                            "reason": getattr(req, "reason", ""),
                            "detail": getattr(req, "detail", ""),
                        },
                        tab_id,
                    )
                )
            return
        if category == "sandbox_promotion":
            # Promoción post-run del sandbox (T-27b·2): siempre modal, nunca
            # auto-aprobada — el operador decide sobre el diff real.
            set_pending(
                stamp_hitl_owner(
                    {
                        "request_id": req.request_id,
                        "reason": getattr(req, "reason", ""),
                        "detail": getattr(req, "detail", ""),
                    },
                    tab_id,
                )
            )
            return
        if category == "download":
            # Egress: never auto-approved — always parks a manual Aprobar/Denegar.
            set_pending(
                stamp_hitl_owner(
                    {
                        "request_id": req.request_id,
                        "reason": getattr(req, "reason", ""),
                        "detail": getattr(req, "detail", ""),
                        "url": getattr(req, "url", "") or "",
                    },
                    tab_id,
                )
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
    tab_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Dispatch a Ritual's tool and publish a feedback message to the store.

    ``auto_approve`` is the launching client's "Modo local" preference, read in
    the click handler (the only place with client context). It is armed in the
    store for this single dispatch so the HITL bridge can auto-grant *this*
    request — and disarmed afterwards — instead of consulting a process-global
    flag that would also affect other clients/agent calls.

    ``tab_id`` viaja por el mismo camino y por el mismo motivo (P1-7): la
    aprobación de esta operación destructiva se parkea bajo ESE cliente, para
    que no sea accionable desde otra pestaña que no lanzó el Ritual.

    ``payload`` (F-001, post-D2): el dict que viaja al dispatcher. ``None``
    conserva el contrato histórico —payload vacío, defaults del service—, que
    es la intención "Generar/regenerar" (``run_texgen`` default ``True``). La
    intención "Continuar" pasa por :func:`run_ritual_resume` con el MISMO
    camino y un payload explícito ``{"run_texgen": False}``: nunca se
    re-significa el botón normal en auto-resume.

    Never raises: dispatch failures and a missing supervisor are converted into
    a ``ritual_feedback`` entry so the click handler (a fire-and-forget task)
    cannot crash the loop. The HITL gate inside ``dispatch_tool`` is what asks
    for approval — see :func:`make_gui_hitl_notify`.
    """
    # Limpiar el semáforo de preflight de cualquier run anterior: cada invocación
    # arranca sin panel stale (T-16b). Se re-puebla al final si el dispatch adjunta
    # un reporte (hoy solo el sort de LOOT lo hace).
    store.set(STORE_KEY_RITUAL_PREFLIGHT, None)
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
    # F-001/PATCH 0010: una generación nueva sustituye el resultado accionable
    # ANTERIOR de la MISMA pestaña — y sólo el propio. El clear global que vivía
    # acá desalojaba la acción de Resume pendiente de OTRA pestaña al arrancar
    # este run (CROSS_TAB_RITUAL_RESULT_EVICTION). Va DESPUÉS de los guards: un
    # click rechazado (single-flight / sin supervisor / ritual no cableado) no
    # consume la acción propia, porque la corrida ni siquiera empieza.
    clear_ritual_result_owned(store, tab_id)
    store.set(STORE_KEY_RITUAL_IN_FLIGHT, True)
    # F1a: un click del operador ES intervención humana — rearmar el
    # cortacircuitos cognitivo del dispatcher antes de despachar, para que
    # repetir el mismo ritual a mano no se confunda con un bucle del agente
    # (contrato reset() del AgenticLoopGuardrail). Duck-typed: supervisores
    # fake sin el método simplemente no rearman.
    reset_guardrail = getattr(supervisor, "reset_loop_guardrail", None)
    if callable(reset_guardrail):
        reset_guardrail()
    # M-9: armar auto-approve SÓLO para el árbol de tasks de ESTE dispatch, vía
    # ContextVar. No es un flag global: un tool_execution concurrente (Telegram/
    # LLM/API) corre en otra task cuya copia de contexto tiene el default False,
    # así que NUNCA se auto-aprueba por el Modo local de este ritual.
    cv_token = _ritual_auto_approve.set(bool(auto_approve))
    # P1-7: mismo scoping por task para el dueño de la aprobación.
    cid_token = _ritual_tab_id.set(tab_id)
    try:
        result = await supervisor.dispatch_tool(tool_name, dict(payload) if payload else {})
    except Exception as exc:  # noqa: BLE001 — fire-and-forget task must not crash the loop
        logger.exception("Ritual %s (%s) dispatch failed", tool_key, tool_name)
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": f"El ritual «{tool_key}» falló: {type(exc).__name__}", "type": "negative"},
        )
        # PATCH 0010: el clear de arranque ya desalojó el resultado PROPIO antes
        # de despachar; el fallo de ESTE run sólo publica su feedback y NUNCA
        # limpia el resultado accionable de otra pestaña — el ``store.set(KEY,
        # None)`` global que vivía acá era la segunda pata del
        # CROSS_TAB_RITUAL_RESULT_EVICTION.
        return
    finally:
        _ritual_auto_approve.reset(cv_token)  # disarm (scoped a esta task)
        _ritual_tab_id.reset(cid_token)
        store.set(STORE_KEY_RITUAL_IN_FLIGHT, False)
        # Drop the approval prompt tied to THIS run so no stale modal lingers on
        # the timeout/denied path where the operator never clicked (Codex #211)
        # — pero solo si es la propia: nunca una ajena que nadie respondió
        # todavía (review de PR #373).
        clear_owned_hitl(store, tab_id)
    resultado = result if isinstance(result, dict) else {}
    # F-001: el panel consume el dict ESTRUCTURAL — needs_deployment + path —
    # para ofrecer la acción de Resume. R-004/R-005: se publica en ENVELOPE
    # (dueño + tool_key real + dict), ANTES del texto del toast.
    publicar_resultado_de_ritual(store, tool_key=tool_key, resultado=resultado, tab_id=tab_id)
    text, kind = summarize_ritual_result(tool_key, resultado)
    store.set(STORE_KEY_RITUAL_FEEDBACK, {"text": text, "type": kind})
    # Surface del reporte de preflight que el dispatch adjuntó (hoy solo LOOT): el
    # panel refrescable lo renderiza con create_preflight_panel (T-16b). Rojo = el
    # gate de loot_service ya frenó el sort; el panel lo hace visible al operador.
    store.set(STORE_KEY_RITUAL_PREFLIGHT, preflight_from_result(resultado))


async def run_ritual_resume(
    tool_key: str,
    *,
    supervisor: Any,
    store: ReactiveStore,
    auto_approve: bool = False,
    tab_id: str | None = None,
) -> None:
    """Intención EXPLÍCITA de continuación post-deployment (F-001, post-D2).

    Es la MISMA implementación que :func:`run_ritual` —mismo single-flight,
    mismo HITL gate del dispatcher, mismo feedback— con UN payload explícito:
    ``{"run_texgen": False}``. La GUI expresa la intención "Continuar DynDOLOD
    después de materializar el artifact"; la validación durable del handoff
    sigue siendo propiedad exclusiva de ``DynDOLODPipelineService.execute``
    (acá no se consulta ni se duplica estado durable).

    El botón normal "Generar" NO pasa por acá: ``run_texgen=True`` sigue siendo
    la vía de regeneración/supersede y conserva su payload vacío histórico.
    """
    await run_ritual(
        tool_key,
        supervisor=supervisor,
        store=store,
        auto_approve=auto_approve,
        tab_id=tab_id,
        payload={"run_texgen": False},
    )


def resume_action_from_result(tool_key: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """Acción de Resume derivada ESTRUCTURALMENTE del resultado (F-001).

    Sólo ``dyndolod`` + ``needs_deployment is True`` (el flag, nunca el texto
    del ``message``) produce la acción. El dict devuelto es lo único que el
    panel necesita para renderizar el botón; el payload es lo único que
    :func:`run_ritual_resume` necesita para despachar.
    """
    if tool_key != "dyndolod" or result.get("needs_deployment") is not True:
        return None
    detail = result.get("texgen_mod_path")
    return {
        "tool_key": "dyndolod",
        "label": "Continuar DynDOLOD",
        "detail": str(detail) if detail else "",
        "payload": {"run_texgen": False},
    }


async def run_ritual_install(
    tool_key: str,
    *,
    app_context: Any,
    store: ReactiveStore,
    tab_id: str | None = None,
) -> None:
    """Install a Ritual's tool via ``ToolsInstaller`` and publish feedback to the store.

    Wired to the "Instalar" button of a Ritual card in the ``missing`` state. Reuses
    the same single-flight guard as :func:`run_ritual` so an install and a run can
    never overlap (both serialize on ``STORE_KEY_RITUAL_IN_FLIGHT`` and share the one
    ``pending_hitl`` modal slot). The download's HITL approval is requested by the
    installer with ``category="download"`` and routed to the GUI modal by
    :func:`make_gui_hitl_notify` — it is never auto-approved by "Modo local".

    P1-7: ``tab_id`` arma el mismo ContextVar que :func:`run_ritual`, así que la
    aprobación de descarga queda marcada con la pestaña que apretó "Instalar".
    Sin esto quedaba sin dueño y era accionable desde cualquier pestaña — y
    justamente es egress de red, de las aprobaciones más sensibles que hay.

    Never raises: a missing installer and any download/extraction failure are turned
    into a ``ritual_feedback`` entry so the click handler (a fire-and-forget task)
    cannot crash the loop.
    """
    # Espejo del clear de ``run_ritual``: el install NO produce reporte de
    # preflight (solo el sort de LOOT lo hace), así que un panel rojo remanente
    # de un ritual previo no puede quedar asociado a esta tarjeta de instalación
    # (defecto #1 del repo: dos superficies hermanas, un reset que solo hacía una).
    store.set(STORE_KEY_RITUAL_PREFLIGHT, None)
    method_name = ritual_installer_name(tool_key)
    if method_name is None:
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": f"«{tool_key}» no tiene instalación automática; instalalo manualmente.", "type": "info"},
        )
        return
    installer = getattr(app_context, "tools_installer", None) if app_context is not None else None
    if installer is None:
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": "El daemon todavía no está listo. Probá de nuevo en un momento.", "type": "negative"},
        )
        return
    # Single-flight: an install or a ritual run is already dispatching/awaiting approval.
    if store.get(STORE_KEY_RITUAL_IN_FLIGHT):
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": "Ya hay un ritual o instalación en curso. Esperá a que termine.", "type": "warning"},
        )
        return

    cs_kwargs: dict[str, object] | None = None
    if tool_key == "skse":
        # SKSE se instala DENTRO del directorio del juego, no en el directorio
        # genérico de tools (`app_context.install_dir`, donde van LOOT/xEdit/
        # Pandora — MO2 y los tools viven aparte de Skyrim). Se lee del último
        # snapshot del scanner: la misma fuente que decidió mostrar la tarjeta
        # de "SKSE faltante" en primer lugar.
        from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV

        snapshot = store.get(STORE_KEY_ENV)
        skyrim = getattr(snapshot, "skyrim", None)
        install_dir = getattr(skyrim, "path", None)
        if install_dir is None:
            store.set(
                STORE_KEY_RITUAL_FEEDBACK,
                {
                    "text": "No se detectó la carpeta de Skyrim: corré el escaneo de entorno primero.",
                    "type": "negative",
                },
            )
            return
    elif tool_key == "community_shaders":
        # Community Shaders se instala como mods de MO2 (clase NGIO, no SKSE):
        # requiere mo2_root + ruta/edición/versión del juego del snapshot y el
        # NexusDownloader de app_context (Address Library y Engine Fixes viven
        # en Nexus). La firma no es ensure(install_dir, session): se despacha
        # con kwargs y se maneja list[ModInstallResult].
        from sky_claw.app.gui.views.forge_dashboard import STORE_KEY_ENV

        snapshot = store.get(STORE_KEY_ENV)
        mo2 = getattr(snapshot, "mo2", None)
        mo2_root = getattr(mo2, "path", None)
        skyrim = getattr(snapshot, "skyrim", None)
        game_dir = getattr(skyrim, "path", None)
        if mo2_root is None:
            store.set(
                STORE_KEY_RITUAL_FEEDBACK,
                {
                    "text": "No se detectó Mod Organizer 2: corré el escaneo de entorno primero.",
                    "type": "negative",
                },
            )
            return
        if game_dir is None:
            store.set(
                STORE_KEY_RITUAL_FEEDBACK,
                {
                    "text": "No se detectó la carpeta de Skyrim: corré el escaneo de entorno primero.",
                    "type": "negative",
                },
            )
            return
        network = getattr(app_context, "network", None)
        downloader = getattr(network, "downloader", None) if network is not None else None
        install_dir = pathlib.Path(mo2_root) / "mods"
        cs_kwargs = {
            "edition": getattr(skyrim, "edition", None),
            "game_version": getattr(skyrim, "version", "") or "",
            "game_dir": game_dir,
        }
    else:
        install_dir = getattr(app_context, "install_dir", None)
        cs_kwargs = None
    session = getattr(app_context, "session", None)
    ensure = getattr(installer, method_name)

    store.set(STORE_KEY_RITUAL_IN_FLIGHT, True)
    # P1-7: mismo scoping por task que run_ritual. El gate HITL del installer
    # corre inline en esta misma task, así que ve el ContextVar y marca la
    # aprobación de descarga con la pestaña que apretó "Instalar".
    tab_token = _ritual_tab_id.set(tab_id)
    try:
        if cs_kwargs is not None:
            result = await ensure(install_dir, session, downloader, **cs_kwargs)
        else:
            result = await ensure(install_dir, session)
    except Exception as exc:  # noqa: BLE001 — fire-and-forget task must not crash the loop
        logger.exception("Ritual install %s (%s) failed", tool_key, method_name)
        store.set(
            STORE_KEY_RITUAL_FEEDBACK,
            {"text": f"No se pudo instalar «{tool_key}»: {exc}", "type": "negative"},
        )
        return
    finally:
        _ritual_tab_id.reset(tab_token)
        store.set(STORE_KEY_RITUAL_IN_FLIGHT, False)
        # Drop the approval prompt tied to this install so no stale modal lingers
        # on the denied/timed-out path — pero solo la propia (mismo cuidado que
        # run_ritual, review de PR #373).
        clear_owned_hitl(store, tab_id)

    # Seed the resolver env var so the just-installed tool can run immediately,
    # without waiting for the next environment scan to refresh the snapshot.
    env_name = RITUAL_INSTALL_ENV.get(tool_key)
    exe_path = getattr(result, "exe_path", None)
    if env_name and exe_path:
        os.environ[env_name] = str(exe_path)

    if tool_key == "community_shaders":
        # Contrato NGIO: el orquestador activa en modlist.txt con estos nombres;
        # se persisten igual que en la rama del agente (`community_shaders_mods`).
        mods = list(result) if result is not None else []
        nombres = [m.mod_name for m in mods]
        config_path = getattr(app_context, "config_path", None)
        # Sin config_path (boot incompleto) la persistencia NO corre: la
        # operación también es parcial y se degrada igual que en el camino que
        # lanza — cubrir solo la excepción dejaba vivo el éxito visual que este
        # bloque vino a cerrar (review adversarial del PR #445).
        persistencia_ok = config_path is not None
        if config_path is not None:
            try:
                # `persistir_campo_bloqueante` despacha por el formato REAL del
                # archivo: `AppContext.config_path` es el TOML canónico
                # (`Config.DEFAULT_CONFIG_FILE`), no el JSON legacy. Leerlo con
                # el parser equivocado devolvía defaults y el guardado
                # posterior reescribía el archivo entero — la config del
                # usuario se perdía sin un solo error visible. La variante
                # `_bloqueante` además serializa con `setup_tools` del agente
                # (mismo config.toml, mismo proceso): sin eso, un ritual de GUI
                # y un `setup_tools` concurrentes podían pisarse el campo que
                # cada uno acababa de escribir (review PR #444).
                from sky_claw.local.local_config import persistir_campo_bloqueante

                await persistir_campo_bloqueante(config_path, "community_shaders_mods", nombres)
            except Exception:  # noqa: BLE001 — la instalación ya tuvo éxito; no romper el feedback
                logger.exception("No se pudo persistir community_shaders_mods en %s", config_path)
                persistencia_ok = False
        if persistencia_ok:
            store.set(
                STORE_KEY_RITUAL_FEEDBACK,
                {
                    "text": f"«{tool_key}» instalado correctamente: {', '.join(nombres) or 'sin componentes'}.",
                    "type": "positive",
                },
            )
        else:
            # El install en disco SÍ ocurrió, pero sin el campo persistido el
            # orquestador no puede activar los mods en modlist.txt. Reportarlo
            # como éxito total convierte una operación parcial en éxito visual
            # (invariante GUI); el operador necesita saber que debe reintentar.
            store.set(
                STORE_KEY_RITUAL_FEEDBACK,
                {
                    "text": (
                        f"«{tool_key}» instalado en disco ({', '.join(nombres) or 'sin componentes'}), "
                        "pero no se pudo persistir la configuración: el orquestador no podrá "
                        "activar los mods hasta que se reintente la instalación."
                    ),
                    "type": "warning",
                },
            )
        return

    # Presencia != compatibilidad probada. `ensure_skse` puede devolver una
    # instalación que EXISTE en disco pero cuya compatibilidad no se pudo verificar
    # porque el ejecutable de Skyrim no expone su versión exacta. Colapsar ese estado
    # en el cartel verde le afirma al operador algo que nadie probó — el mismo falso
    # positivo que el installer rechazó, una capa más arriba. Se reporta con el
    # `warning` que la rama de Community Shaders ya usa para una operación que ocurrió
    # pero no quedó garantizada. `getattr` porque el campo es opcional en
    # `InstallResult` y los demás `ensure_*` no lo llenan.
    if getattr(result, "verification", None) is InstallVerification.PRESENT_BUT_UNVERIFIED:
        # Dos situaciones distintas comparten el estado, y el cartel tiene que
        # distinguirlas: "no pude probar nada" NO implica "no toqué nada". La
        # idempotente encontró algo y no escribió; la fresca sin ejecutable descargó y
        # copió, pero no había runtime contra el cual probar el build. Un texto único
        # que afirme "no se modificó nada" le miente al operador en la segunda, y esa
        # mentira lo empuja a reintentar una instalación que ya ocurrió.
        # `already_existed` es exactamente la dimensión que las separa — por eso vive
        # aparte de `verification` y acá se leen las dos.
        if getattr(result, "already_existed", False):
            aviso = (
                f"«{tool_key}» detectado en el directorio del juego, pero no se pudo "
                "verificar su compatibilidad: no se pudo leer la versión exacta de "
                "Skyrim. No se descargó ni se modificó nada. Comprobá a mano que el "
                "build corresponda (https://skse.silverlock.org/)."
            )
        else:
            aviso = (
                f"«{tool_key}» quedó instalado, pero su compatibilidad no se pudo "
                "verificar: no hay un ejecutable de Skyrim en esa carpeta contra el "
                "cual comprobar el build. Verificá a mano que corresponda "
                "(https://skse.silverlock.org/)."
            )
        store.set(STORE_KEY_RITUAL_FEEDBACK, {"text": aviso, "type": "warning"})
        return

    store.set(
        STORE_KEY_RITUAL_FEEDBACK,
        {"text": f"«{tool_key}» instalado correctamente.", "type": "positive"},
    )
