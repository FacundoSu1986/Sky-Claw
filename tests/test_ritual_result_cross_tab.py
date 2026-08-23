"""PR #493 PATCH 0010 — CROSS_TAB_RITUAL_RESULT_EVICTION.

El resultado accionable (Resume post-DynDOLOD) vivía en UNA clave global del
store: ``run_ritual`` la limpiaba al iniciar y ``publicar_resultado_de_ritual``
la reemplazaba al terminar. Con dos pestañas, el arranque/éxito/error de la
pestaña B desalojaba la acción pendiente de la pestaña A.

Principio bajo prueba: un resultado accionable pertenece a SU ``owner_tab``.
El single-flight serializa la EJECUCIÓN (clave global, intacta); el resultado
POST-run es multi-owner. Dos pestañas pueden tener resultados pendientes
simultáneamente en el MISMO ``ReactiveStore``.

Los tests usan dos ``tab_id`` reales y lógicos (``tab-A``/``tab-B``) contra un
único store — un store por pestaña ocultaría el bug. La secuencia completa del
finding es el acceptance test al final del archivo.

Fase RED: contra b0408d7e estos tests fallan (el clear global del inicio y el
replace global del final eviccionan a la pestaña ajena).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Any

from sky_claw.app.gui.controllers import ritual_runner as rr_mod
from sky_claw.app.gui.state.reactive_store import ReactiveStore


def _resultado_needs_deployment(texgen_mod_path: str = "C:/MO2/mods/TexGen Output") -> dict[str, Any]:
    return {
        "success": False,
        "needs_deployment": True,
        "texgen_mod_path": texgen_mod_path,
        "message": "materializá el árbol antes de reintentar",
    }


class _SupervisorInstantaneo:
    """Devuelve el result configurado sin demora (run completo)."""

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else {"success": True}

    async def dispatch_tool(self, tool_name: str, payload: dict) -> dict:
        self.calls.append((tool_name, dict(payload)))
        return dict(self.result)


class _SupervisorBloqueado:
    """``dispatch_tool`` queda esperando hasta que el test lo libere: permite
    inspeccionar el store DESPUÉS del inicio del ritual y ANTES de que termine
    (la ventana donde el clear global de arranque eviccionaba a la otra tab)."""

    def __init__(self, result: dict | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.result = result if result is not None else {"success": True}
        self.calls: list[tuple[str, dict]] = []

    async def dispatch_tool(self, tool_name: str, payload: dict) -> dict:
        self.calls.append((tool_name, dict(payload)))
        self.entered.set()
        await self.release.wait()
        return dict(self.result)


class _SupervisorQueExplota:
    """Lanza en ``dispatch_tool``: el camino de excepción de ``run_ritual``."""

    async def dispatch_tool(self, tool_name: str, payload: dict) -> dict:
        raise RuntimeError("boom de fixture")


def _accion(store: ReactiveStore, tab_id: str | None) -> dict[str, Any] | None:
    """Alias corto del seam de resolución para mantener los asserts legibles."""
    return rr_mod.resolve_ritual_resume_action(store, tab_id)


# ── RED 10-1: el clear al INICIAR B no evicciona la acción de A ────────────────


async def test_red_10_1_clear_al_iniciar_no_desaloja_la_accion_de_otra_tab() -> None:
    """Mientras B está corriendo (post-inicio, pre-fin), la acción de A sigue
    resoluble. Contra b0408d7e el ``store.set(KEY, None)`` del arranque de B la
    desaloja — RED."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )

    sup_b = _SupervisorBloqueado({"success": True})
    tarea_b = asyncio.create_task(rr_mod.run_ritual("loot", supervisor=sup_b, store=store, tab_id="tab-B"))
    await asyncio.wait_for(sup_b.entered.wait(), timeout=5)

    assert _accion(store, "tab-A") is not None, "el inicio del ritual de B desalojó la acción de Resume pendiente de A"

    sup_b.release.set()
    await asyncio.wait_for(tarea_b, timeout=5)
    assert _accion(store, "tab-A") is not None, "al terminar B, la acción de A ya no se resuelve"


# ── RED 10-2: el replace al FINALIZAR B no pisa el envelope de A ───────────────


async def test_red_10_2_reemplazo_al_finalizar_no_pisa_el_envelope_de_a() -> None:
    """B completa un ritual exitoso; el resultado accionable de A sigue presente
    y resoluble. Contra b0408d7e la publicación final de B reemplaza el envelope
    global de A — RED."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )

    await rr_mod.run_ritual("loot", supervisor=_SupervisorInstantaneo({"success": True}), store=store, tab_id="tab-B")

    accion_a = _accion(store, "tab-A")
    assert accion_a is not None, "el éxito de B reemplazó el envelope accionable de A"
    assert accion_a["payload"] == {"run_texgen": False}
    assert _accion(store, "tab-B") is None, "loot success no produce acción de Resume"


# ── RED 10-3: A y B necesitan Resume simultáneamente ───────────────────────────


async def test_red_10_3_dos_tabs_con_resume_pendiente_simultaneo() -> None:
    """El caso estructural: ambas pestañas terminan un DynDOLOD con
    ``needs_deployment=True`` y CADA UNA conserva su acción con owner, tool_key
    y payload correctos. Obliga a multi-owner, no a «no sobrescribir si ya hay
    algo» (que le robaría a B su propio resultado)."""
    store = ReactiveStore()
    await rr_mod.run_ritual(
        "dyndolod",
        supervisor=_SupervisorInstantaneo(_resultado_needs_deployment("C:/mods/TexGen A")),
        store=store,
        tab_id="tab-A",
    )
    await rr_mod.run_ritual(
        "dyndolod",
        supervisor=_SupervisorInstantaneo(_resultado_needs_deployment("C:/mods/TexGen B")),
        store=store,
        tab_id="tab-B",
    )

    accion_a = _accion(store, "tab-A")
    accion_b = _accion(store, "tab-B")
    assert accion_a is not None and "TexGen A" in str(accion_a.get("detail", ""))
    assert accion_b is not None and "TexGen B" in str(accion_b.get("detail", ""))
    assert accion_a["tool_key"] == "dyndolod"
    assert accion_b["tool_key"] == "dyndolod"
    assert accion_a["payload"] == {"run_texgen": False}
    assert accion_b["payload"] == {"run_texgen": False}

    # Envelope-level: cada slot conserva owner_tab, tool_key y payload propios.
    publicado = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)
    assert isinstance(publicado, dict)
    resultados = publicado.get(rr_mod.RITUAL_RESULTS_BY_OWNER)
    assert isinstance(resultados, dict)
    env_a = resultados.get("tab-A")
    env_b = resultados.get("tab-B")
    assert isinstance(env_a, dict) and isinstance(env_b, dict)
    assert env_a.get(rr_mod.RITUAL_RESULT_OWNER_TAB) == "tab-A"
    assert env_b.get(rr_mod.RITUAL_RESULT_OWNER_TAB) == "tab-B"
    assert env_a.get(rr_mod.RITUAL_RESULT_TOOL_KEY) == "dyndolod"
    assert env_b.get(rr_mod.RITUAL_RESULT_TOOL_KEY) == "dyndolod"
    assert env_a[rr_mod.RITUAL_RESULT_PAYLOAD_KEY]["texgen_mod_path"] == "C:/mods/TexGen A"
    assert env_b[rr_mod.RITUAL_RESULT_PAYLOAD_KEY]["texgen_mod_path"] == "C:/mods/TexGen B"


# ── RED 10-4: la excepción de B no limpia el resultado de A ────────────────────


async def test_red_10_4_excepcion_en_b_no_limpia_el_resultado_de_a() -> None:
    """B inicia y ``dispatch_tool`` lanza: el error de B sólo publica su feedback
    y NO desaloja la acción pendiente de A. Contra b0408d7e el clear global del
    except la borra — RED."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )

    await rr_mod.run_ritual("loot", supervisor=_SupervisorQueExplota(), store=store, tab_id="tab-B")

    assert _accion(store, "tab-A") is not None, "la excepción de B desalojó la acción de Resume de A"
    fb = store.get(rr_mod.STORE_KEY_RITUAL_FEEDBACK)
    assert isinstance(fb, dict) and fb.get("type") == "negative", "B debe reportar su error por feedback"


# ── CONTROL 10-5: la MISMA tab sigue sustituyendo/limpiando lo suyo ────────────


async def test_control_10_5_misma_tab_sustituye_su_propio_resultado() -> None:
    """Una regeneración exitosa de A elimina la acción stale de A: el success
    nuevo reemplaza el slot propio y ya no ofrece Resume."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )
    assert _accion(store, "tab-A") is not None

    await rr_mod.run_ritual(
        "dyndolod", supervisor=_SupervisorInstantaneo({"success": True}), store=store, tab_id="tab-A"
    )

    assert _accion(store, "tab-A") is None, (
        "un success dejó consumible la acción stale del resultado viejo de la MISMA pestaña"
    )


def test_control_10_5c_publicacion_misma_tab_reemplaza_su_slot() -> None:
    """A nivel del helper de publicación: una pestaña PUEDE sustituir SU
    resultado anterior (ancla del mutante M10-4). Un publish que «no sobrescriba
    si ya hay algo» conservaría la acción stale de la MISMA pestaña tras una
    corrida nueva."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )
    assert _accion(store, "tab-A") is not None

    # Nueva corrida de A sin needs_deployment (success): el slot propio se
    # reemplaza y la acción vieja deja de resolverse.
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado={"success": True},
        tab_id="tab-A",
    )

    assert _accion(store, "tab-A") is None, (
        "la publicación nueva de A no reemplazó el resultado viejo de la MISMA pestaña"
    )


async def test_control_10_5b_misma_tab_limpia_su_propio_resultado_al_iniciar() -> None:
    """Durante la nueva generación de A, su acción VIEJA ya está desalojada (el
    clear de arranque es per-owner) y el resultado de B sigue intacto."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment("C:/mods/TexGen A"),
        tab_id="tab-A",
    )
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment("C:/mods/TexGen B"),
        tab_id="tab-B",
    )

    sup_a = _SupervisorBloqueado({"success": True})
    tarea_a = asyncio.create_task(rr_mod.run_ritual("dyndolod", supervisor=sup_a, store=store, tab_id="tab-A"))
    await asyncio.wait_for(sup_a.entered.wait(), timeout=5)

    assert _accion(store, "tab-A") is None, "durante su nueva generación, la acción vieja de A debe estar desalojada"
    assert _accion(store, "tab-B") is not None, "la regeneración de A no debe tocar el resultado de B"

    sup_a.release.set()
    await asyncio.wait_for(tarea_a, timeout=5)


# ── CONTROL 10-6: el dismiss aislado por dueño ─────────────────────────────────


def test_control_10_6_clear_owned_aisla_por_dueno() -> None:
    """``clear_ritual_result_owned(store, "tab-B")`` NO borra A; el clear de la
    dueña sí. Con dos resultados pendientes simultáneos."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store, tool_key="dyndolod", resultado=_resultado_needs_deployment(), tab_id="tab-A"
    )
    rr_mod.publicar_resultado_de_ritual(
        store, tool_key="dyndolod", resultado=_resultado_needs_deployment(), tab_id="tab-B"
    )

    rr_mod.clear_ritual_result_owned(store, "tab-B")

    assert _accion(store, "tab-A") is not None, "el dismiss de B desalojó el resultado de A"
    assert _accion(store, "tab-B") is None

    rr_mod.clear_ritual_result_owned(store, "tab-A")

    assert _accion(store, "tab-A") is None


# ── CONTROL 10-7: semántica ownerless preservada + coexistencia ────────────────


def test_control_10_7_ownerless_coexiste_con_resultados_con_dueno() -> None:
    """La política vigente se preserva EXPLÍCITAMENTE: un resultado sin dueño
    sigue siendo visible/limpiable por cualquier pestaña, y ahora puede coexistir
    con resultados con dueño. Precedencia: la pestaña ve SU resultado; sin
    resultado propio, ve el ownerless."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment("C:/mods/TexGen Ownerless"),
        tab_id=None,
    )
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment("C:/mods/TexGen A"),
        tab_id="tab-A",
    )

    accion_a = _accion(store, "tab-A")
    assert accion_a is not None and "TexGen A" in str(accion_a.get("detail", "")), (
        "la pestaña dueña ve SU resultado, no el ownerless"
    )
    accion_b = _accion(store, "tab-B")
    assert accion_b is not None and "Ownerless" in str(accion_b.get("detail", "")), (
        "una pestaña sin resultado propio ve el ownerless (política vigente)"
    )
    assert _accion(store, None) is not None, "sin contexto de pestaña se ve el ownerless"

    rr_mod.clear_ritual_result_owned(store, "tab-A")
    accion_a2 = _accion(store, "tab-A")
    assert accion_a2 is not None and "Ownerless" in str(accion_a2.get("detail", "")), (
        "sin resultado propio, A cae al slot ownerless"
    )

    rr_mod.clear_ritual_result_owned(store, "tab-B")
    assert _accion(store, "tab-B") is None, "cualquier pestaña limpia el ownerless (política vigente)"


# ── Migración: el reader adopta el envelope legacy en transición ───────────────


def test_reader_adopta_el_envelope_legacy_en_transicion() -> None:
    """Si durante hot reload/estado existente aparece el envelope simple
    (``{owner_tab, tool_key, result}``), el reader lo adopta en vez de
    descartarlo — y un write nuevo conserva el resultado heredado."""
    store = ReactiveStore()
    store.set(
        rr_mod.STORE_KEY_RITUAL_LAST_RESULT,
        {
            rr_mod.RITUAL_RESULT_OWNER_TAB: "tab-A",
            rr_mod.RITUAL_RESULT_TOOL_KEY: "dyndolod",
            rr_mod.RITUAL_RESULT_PAYLOAD_KEY: _resultado_needs_deployment(),
        },
    )

    assert _accion(store, "tab-A") is not None
    assert _accion(store, "tab-B") is None

    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment("C:/mods/TexGen B"),
        tab_id="tab-B",
    )

    assert _accion(store, "tab-A") is not None, "el write nuevo descartó el resultado heredado"
    assert _accion(store, "tab-B") is not None


# ── ACCEPTANCE: secuencia completa del finding, un store, dos tabs ─────────────


async def test_acceptance_dos_tabs_mismo_store() -> None:
    """El acceptance principal: A generate → needs_deployment → acción de A
    visible; B generate → DURANTE B la acción de A sigue visible → B termina →
    sigue visible; B needs_deployment → acciones de A y B visibles cada una en
    su pestaña; A consume/clear → A desaparece y B permanece."""
    store = ReactiveStore()

    # A generate → needs_deployment → acción de A visible.
    await rr_mod.run_ritual(
        "dyndolod",
        supervisor=_SupervisorInstantaneo(_resultado_needs_deployment("C:/mods/TexGen A")),
        store=store,
        tab_id="tab-A",
    )
    assert _accion(store, "tab-A") is not None
    assert _accion(store, "tab-B") is None

    # B generate → durante B, la acción de A es visible.
    sup_b = _SupervisorBloqueado({"success": True})
    tarea_b = asyncio.create_task(rr_mod.run_ritual("loot", supervisor=sup_b, store=store, tab_id="tab-B"))
    await asyncio.wait_for(sup_b.entered.wait(), timeout=5)
    assert _accion(store, "tab-A") is not None, "durante el run de B, la acción de A desapareció"

    # B termina → la acción de A sigue visible.
    sup_b.release.set()
    await asyncio.wait_for(tarea_b, timeout=5)
    assert _accion(store, "tab-A") is not None, "al terminar B, la acción de A desapareció"

    # B needs_deployment → acciones de A y de B visibles, cada una en su pestaña.
    await rr_mod.run_ritual(
        "dyndolod",
        supervisor=_SupervisorInstantaneo(_resultado_needs_deployment("C:/mods/TexGen B")),
        store=store,
        tab_id="tab-B",
    )
    assert _accion(store, "tab-A") is not None
    assert _accion(store, "tab-B") is not None

    # A consume/clear → A desaparece, B permanece.
    rr_mod.clear_ritual_result_owned(store, "tab-A")
    assert _accion(store, "tab-A") is None
    assert _accion(store, "tab-B") is not None, "el consumo de A desalojó la acción pendiente de B"


# ── HARDENING follow-up #493: el reader valida el dueño del slot ───────────────


def _publicar_contenedor_crudo(
    store: ReactiveStore,
    resultados: dict[str | None, dict[str, Any]],
) -> None:
    """Escribe el contenedor multi-owner SIN pasar por el writer productivo.

    Sólo para tests: simula estados internamente inconsistentes que ningún
    writer productivo produce hoy (por eso el finding es hardening defensivo
    y no un bug con productor demostrado). El ancla AST del writer queda
    intacta: este helper vive en tests, no en ``ritual_runner``.
    """
    store.set(rr_mod.STORE_KEY_RITUAL_LAST_RESULT, {rr_mod.RITUAL_RESULTS_BY_OWNER: dict(resultados)})


def _envelope_dyndolod(
    owner: str | None,
    texgen_mod_path: str = "C:/MO2/mods/TexGen Output",
) -> dict[str, Any]:
    """Envelope bien formado de ``dyndolod`` con ``needs_deployment``.

    ``texgen_mod_path`` distinguible permite afirmar QUÉ envelope se resolvió
    cuando hay varios slots pendientes (precedencia own vs ownerless)."""
    return {
        rr_mod.RITUAL_RESULT_OWNER_TAB: owner,
        rr_mod.RITUAL_RESULT_TOOL_KEY: "dyndolod",
        rr_mod.RITUAL_RESULT_PAYLOAD_KEY: _resultado_needs_deployment(texgen_mod_path),
    }


def test_la_accion_de_resume_rechaza_owner_distinto_del_slot() -> None:
    """Follow-up de PR #493 (RITUAL_RESULT_OWNER_KEY_MISMATCH): el envelope
    seleccionado por el slot ``tab-B`` declara ``owner_tab="tab-A"``. Ningún
    writer productivo produce este estado hoy, pero si aparece, la acción de
    Resume NO puede derivarse desde la pestaña equivocada: fail-closed →
    ``None``, sin reparar el store ni consumir nada ajeno."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {"tab-B": _envelope_dyndolod("tab-A")})
    antes = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)

    accion = _accion(store, "tab-B")

    assert accion is None, "un envelope con owner_tab ajeno produjo una acción de Resume"
    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, (
        "el rechazo mutó el store: el reader es defensivo y no repara nada"
    )


# ── Matriz de regresión del hardening (casos A–J del follow-up #493) ───────────


def test_hardening_a_slot_propio_consistente_resuelve_su_accion() -> None:
    """Caso A: slot ``tab-A`` con envelope dueño ``tab-A`` sigue resolviendo la
    acción propia — el hardening no castiga el estado bien formado."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {"tab-A": _envelope_dyndolod("tab-A")})

    accion = _accion(store, "tab-A")

    assert accion is not None
    assert accion["tool_key"] == "dyndolod"


def test_hardening_b_aislamiento_cross_tab_preservado() -> None:
    """Caso B: con SÓLO el resultado con dueño de A presente, B no resuelve
    nada — y el hardening tampoco convierte ese rechazo en consumo ajeno."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {"tab-A": _envelope_dyndolod("tab-A")})
    antes = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)

    assert _accion(store, "tab-B") is None
    assert _accion(store, None) is None
    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, "la lectura mutó el store"


def test_hardening_d_ownerless_valido_sigue_resolviendo() -> None:
    """Caso D: slot ``None`` con envelope sin dueño — cualquier pestaña lo ve
    por fallback (política vigente), porque dueño declarado == slot."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {None: _envelope_dyndolod(None)})

    assert _accion(store, "tab-A") is not None
    assert _accion(store, None) is not None


def test_hardening_e_slot_none_con_envelope_ajeno_rechaza() -> None:
    """Caso E: el slot ``None`` contiene un envelope que declara dueño
    ``tab-A``. NO es un resultado sin dueño: ninguna pestaña puede derivar la
    acción de otro desde el fallback — fail-closed."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {None: _envelope_dyndolod("tab-A")})

    assert _accion(store, "tab-B") is None
    assert _accion(store, "tab-A") is None, (
        "el envelope está en el slot equivocado: ni siquiera su dueño declarado"
        " lo consume desde acá (el writer productivo nunca publica así)"
    )


def test_hardening_f_precedencia_del_resultado_propio_intacta() -> None:
    """Caso F: con resultado propio Y ownerless pendientes, la pestaña sigue
    viendo EL SUYO (precedencia fijada por PATCH 0010)."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(
        store,
        {
            "tab-A": _envelope_dyndolod("tab-A"),
            None: _envelope_dyndolod(None, "C:/mods/TexGen Ownerless"),
        },
    )

    accion = _accion(store, "tab-A")

    assert accion is not None
    assert "TexGen Output" in str(accion.get("detail", "")), "A dejó de ver su resultado propio y cayó al ownerless"
    assert _accion(store, "tab-B") is not None, "sin resultado propio, B sigue viendo el ownerless"


def test_hardening_g_slot_propio_corrupto_no_cae_al_ownerless() -> None:
    """Caso G — decisión de contrato: «slot ausente» y «slot presente con valor
    inválido» son cosas distintas. Si el slot explícito de A existe pero su
    envelope es inconsistente (dueño ajeno, o basura no-dict), el resolver
    rechaza y NO salta silenciosamente al ownerless: saltar escondería la
    corrupción detrás de una acción que nadie publicó para esta pestaña."""
    # Envelope con dueño ajeno en el slot propio + ownerless válido disponible.
    store = ReactiveStore()
    _publicar_contenedor_crudo(
        store,
        {
            "tab-A": _envelope_dyndolod("tab-Otra"),
            None: _envelope_dyndolod(None),
        },
    )
    assert _accion(store, "tab-A") is None, "el slot corrupto de A delegó en el ownerless"

    # Valor no-dict en el slot propio + ownerless válido disponible.
    store2 = ReactiveStore()
    _publicar_contenedor_crudo(
        store2,
        {
            "tab-A": {"tool_key": "dyndolod"},  # sin result: malformado
            None: _envelope_dyndolod(None),
        },
    )
    assert _accion(store2, "tab-A") is None

    # Slot presente con valor no-dict crudo + ownerless válido disponible.
    store3 = ReactiveStore()
    _publicar_contenedor_crudo(store3, {"tab-A": "basura", None: _envelope_dyndolod(None)})
    assert _accion(store3, "tab-A") is None

    # El contraste: slot AUSENTE sí cae al ownerless (comportamiento vigente).
    store4 = ReactiveStore()
    _publicar_contenedor_crudo(store4, {None: _envelope_dyndolod(None)})
    assert _accion(store4, "tab-A") is not None


def test_hardening_h_envelope_legacy_sigue_adoptandose() -> None:
    """Caso H: el envelope simple pre-0010 se re-indexa por SU ``owner_tab`` y
    tras el hardening conserva exactamente el mismo scoping: la dueña lo
    resuelve, otra pestaña no."""
    store = ReactiveStore()
    store.set(
        rr_mod.STORE_KEY_RITUAL_LAST_RESULT,
        {
            rr_mod.RITUAL_RESULT_OWNER_TAB: "tab-A",
            rr_mod.RITUAL_RESULT_TOOL_KEY: "dyndolod",
            rr_mod.RITUAL_RESULT_PAYLOAD_KEY: _resultado_needs_deployment(),
        },
    )

    assert _accion(store, "tab-A") is not None, "el hardening rompió la adopción del envelope legacy"
    assert _accion(store, "tab-B") is None


def test_hardening_i_tipos_malformados_de_owner_fallan_cerrados() -> None:
    """Caso I: ``owner_tab`` con tipos que ningún writer produce (lista, dict,
    int) no debe derivar acción NI lanzar excepción — la comparación de
    desigualdad falla cerrada sola."""
    for owner_malformado in ([], {}, 123):
        store = ReactiveStore()
        _publicar_contenedor_crudo(store, {"tab-B": _envelope_dyndolod(owner_malformado)})  # type: ignore[arg-type]
        assert _accion(store, "tab-B") is None, f"owner malformado {owner_malformado!r} derivó acción"

        store2 = ReactiveStore()
        _publicar_contenedor_crudo(store2, {None: _envelope_dyndolod(owner_malformado)})  # type: ignore[arg-type]
        assert _accion(store2, "tab-B") is None, f"owner malformado {owner_malformado!r} pasó como ownerless"


def test_hardening_j_owner_ausente_envelope_explicito_rechaza() -> None:
    """Caso J: envelope SIN clave ``owner_tab`` en un slot EXPLÍCITO es
    malformado → ``None``. No se acepta silenciosamente como resultado de esa
    pestaña: la ausencia no prueba ownership.

    Contraste documentado (decisión de contrato): bajo el slot ``None``, un
    envelope sin ``owner_tab`` SÍ se trata como ownerless — es exactamente la
    semántica que ``_resultados_por_owner`` ya aplica al re-indexar envelopes
    legacy sin dueño (``raw.get(RITUAL_RESULT_OWNER_TAB)`` → clave ``None``).
    Una sola regla uniforme: falta de dueño ≡ dueño ``None``."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(
        store,
        {
            "tab-B": {
                rr_mod.RITUAL_RESULT_TOOL_KEY: "dyndolod",
                rr_mod.RITUAL_RESULT_PAYLOAD_KEY: _resultado_needs_deployment(),
            },
        },
    )
    assert _accion(store, "tab-B") is None, "un envelope sin dueño fue aceptado como resultado de tab-B"

    store2 = ReactiveStore()
    _publicar_contenedor_crudo(
        store2,
        {
            None: {
                rr_mod.RITUAL_RESULT_TOOL_KEY: "dyndolod",
                rr_mod.RITUAL_RESULT_PAYLOAD_KEY: _resultado_needs_deployment(),
            },
        },
    )
    assert _accion(store2, "tab-B") is not None, (
        "bajo el slot None, sin owner_tab declarado, la semántica legacy lo trata como ownerless"
    )


def test_slot_propio_presente_con_none_no_cae_al_ownerless() -> None:
    """Review PR #501 (CR-1): «slot ausente» y «slot presente con valor
    inválido» son cosas distintas TAMBIÉN cuando el valor es ``None``.
    ``resultados.get(tab_id)`` confunde ambos y habilitaba el fallback
    ownerless para un slot que EXISTE. La selección es por pertenencia — la
    misma semántica que ``clear_ritual_result_owned`` ya usa para borrar."""
    # Slot presente con valor None + ownerless válido disponible.
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {"tab-A": None, None: _envelope_dyndolod(None)})
    antes = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)

    assert _accion(store, "tab-A") is None, "el slot propio existe (con valor None): no puede caer al ownerless"
    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, "el rechazo mutó el store"

    # Slot presente con valor no-dict ([]), mismo contrato.
    store2 = ReactiveStore()
    _publicar_contenedor_crudo(store2, {"tab-A": [], None: _envelope_dyndolod(None)})
    assert _accion(store2, "tab-A") is None

    # El contraste sigue vigente: slot AUSENTE sí cae al ownerless.
    store3 = ReactiveStore()
    _publicar_contenedor_crudo(store3, {None: _envelope_dyndolod(None)})
    assert _accion(store3, "tab-A") is not None


def test_legacy_con_owner_malformado_falla_cerrado_sin_excepcion() -> None:
    """Review PR #501 (CR-2): el envelope legacy se re-indexa usando su
    ``owner_tab`` como clave del dict de slots; con un valor unhashable
    ([], {}) ``_resultados_por_owner`` lanzaba TypeError ANTES de llegar a
    cualquier chequeo — todo el panel dejaba de refrescar en vez de fallar
    cerrado. Dueño válido: ``str`` o ``None``; lo demás rechaza sin excepción."""
    for owner_malformado in ([], {}, 123):
        store = ReactiveStore()
        store.set(
            rr_mod.STORE_KEY_RITUAL_LAST_RESULT,
            {
                rr_mod.RITUAL_RESULT_OWNER_TAB: owner_malformado,
                rr_mod.RITUAL_RESULT_TOOL_KEY: "dyndolod",
                rr_mod.RITUAL_RESULT_PAYLOAD_KEY: _resultado_needs_deployment(),
            },
        )
        antes = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)

        assert _accion(store, "tab-B") is None, f"legacy con owner {owner_malformado!r} derivó acción"
        assert _accion(store, "tab-A") is None, f"legacy con owner {owner_malformado!r} derivó acción"
        assert _accion(store, None) is None, f"legacy con owner {owner_malformado!r} se resolvió como sin dueño"
        assert rr_mod.publicar_resultado_de_ritual is not None  # el módulo quedó importable/operativo
        assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, "el rechazo mutó el store"


class _StoreEspia(ReactiveStore):
    """ReactiveStore que registra cada ``set`` para probar que un rechazo de
    ownership NO escribe nada (ni reescrituras idénticas del contenedor)."""

    def __init__(self) -> None:
        super().__init__()
        self.sets: list[tuple[str, Any]] = []

    def set(self, key: str, value: Any) -> None:
        self.sets.append((key, value))
        super().set(key, value)


def _store_espia_con_notificaciones() -> tuple[_StoreEspia, list[int]]:
    """Store espía + contador de notifications sobre la clave del resultado."""
    store = _StoreEspia()
    notificaciones: list[int] = []
    store.subscribe(rr_mod.STORE_KEY_RITUAL_LAST_RESULT, lambda: notificaciones.append(1))
    return store, notificaciones


# ── Matriz del clear (review PR #501: el hermano de resolve) ───────────────────


def test_clear_rechaza_envelope_cuyo_owner_no_coincide_con_el_slot() -> None:
    """Review PR #501 (Codex P2): B no puede CONSUMIR la acción de A y tampoco
    puede BORRARLA. El ``×`` del panel de feedback llama al clear SIN importar
    si hay acción de Resume (el botón vive fuera del guard de la acción), así
    que el reachability GUI es real: con el envelope de A alojado en el slot de
    B, el clear debe rechazar SIN escribir nada — ni borrado, ni reescritura
    idéntica del contenedor, ni notification."""
    store, notificaciones = _store_espia_con_notificaciones()
    _publicar_contenedor_crudo(store, {"tab-B": _envelope_dyndolod("tab-A")})
    antes = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)
    store.sets.clear()
    notificaciones.clear()

    rr_mod.clear_ritual_result_owned(store, "tab-B")

    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, (
        "el clear eliminó un envelope cuyo dueño declarado no coincide con el slot"
    )
    assert store.sets == [], "el rechazo escribió en el store"
    assert notificaciones == [], "el rechazo disparó notifications del store"


def test_clear_con_slot_propio_presente_invalido_no_mutar() -> None:
    """C3: slot propio PRESENTE con valor inválido (None) + ownerless válido.
    El clear no puede caer al ownerless ni reescribir el contenedor para
    recortar la basura propia: presencia ≠ ausencia, misma regla del resolver."""
    store, notificaciones = _store_espia_con_notificaciones()
    _publicar_contenedor_crudo(store, {"tab-A": None, None: _envelope_dyndolod(None)})
    antes = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)
    store.sets.clear()
    notificaciones.clear()

    rr_mod.clear_ritual_result_owned(store, "tab-A")

    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, (
        "el clear recortó/mutó ante slot propio presente-inválido"
    )
    assert store.sets == []
    assert notificaciones == []


def test_clear_sin_slot_propio_sigue_limpiando_el_ownerless_valido() -> None:
    """C4 (+C7 válido): sin slot propio, la política vigente se conserva —
    cualquier pestaña limpia el resultado sin dueño bien formado."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {None: _envelope_dyndolod(None)})

    rr_mod.clear_ritual_result_owned(store, "tab-A")

    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) is None, "el ownerless válido dejó de ser limpiable"


def test_clear_de_slot_none_con_dueno_ajeno_no_muta() -> None:
    """C5: el slot ``None`` contiene un envelope que declara dueño ``tab-A``;
    B no puede desalojarlo vía fallback — el owner declarado no coincide con
    el dueño del slot seleccionado."""
    store, notificaciones = _store_espia_con_notificaciones()
    _publicar_contenedor_crudo(store, {None: _envelope_dyndolod("tab-A")})
    antes = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)
    store.sets.clear()
    notificaciones.clear()

    rr_mod.clear_ritual_result_owned(store, "tab-B")

    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, (
        "B desalojó por fallback un envelope con dueño declarado ajeno"
    )
    assert store.sets == []
    assert notificaciones == []


def test_clear_tab_id_none_respeta_el_dueno_declarado() -> None:
    """C7: ``tab_id=None`` selecciona DIRECTAMENTE el slot sin dueño (no hay
    fallback posible). Ownerless bien formado → se limpia; el slot ``None``
    con dueño declarado ajeno → rechazo sin mutación."""
    store = ReactiveStore()
    _publicar_contenedor_crudo(store, {None: _envelope_dyndolod(None)})
    rr_mod.clear_ritual_result_owned(store, None)
    assert store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) is None

    store2, notificaciones = _store_espia_con_notificaciones()
    _publicar_contenedor_crudo(store2, {None: _envelope_dyndolod("tab-A")})
    antes = store2.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)
    store2.sets.clear()
    notificaciones.clear()

    rr_mod.clear_ritual_result_owned(store2, None)

    assert store2.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT) == antes, (
        "tab_id=None desalojó un envelope cuyo dueño declarado es una pestaña"
    )
    assert store2.sets == []
    assert notificaciones == []


# ── Ancla enumerativa: ningún write directo fuera del writer del contenedor ────


def _funciones_que_escriben_la_clave(ruta: pathlib.Path) -> set[str]:
    """Funciones (o ``"<modulo>"``) que llaman ``store.set(...)`` con
    ``STORE_KEY_RITUAL_LAST_RESULT`` como primera clave, por AST."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    escritores: set[str] = set()

    def _es_write_del_resultado(nodo: ast.AST) -> bool:
        if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
            return False
        if nodo.func.attr != "set" or not nodo.args:
            return False
        clave = nodo.args[0]
        if isinstance(clave, ast.Name):
            return clave.id == "STORE_KEY_RITUAL_LAST_RESULT"
        return isinstance(clave, ast.Attribute) and clave.attr == "STORE_KEY_RITUAL_LAST_RESULT"

    for nodo in ast.walk(arbol):
        if not _es_write_del_resultado(nodo):
            continue
        dueña: str | None = None
        for fn in arbol.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(hijo is nodo for hijo in ast.walk(fn)):
                dueña = fn.name
                break
        escritores.add(dueña or "<modulo>")
    return escritores


def test_ningun_camino_productivo_escribe_la_clave_fuera_del_writer() -> None:
    """Ancla por AST: TODOS los writes directos a ``STORE_KEY_RITUAL_LAST_RESULT``
    viven en el writer del contenedor. Un ``store.set(KEY, None)`` global suelto
    (el bug cross-tab) rompe acá — mismo patrón de ancla enumerativa del repo."""
    ruta = pathlib.Path(rr_mod.__file__).resolve()
    escritores = _funciones_que_escriben_la_clave(ruta)
    assert escritores == {"_escribir_resultados_de_ritual"}, (
        f"writes directos a STORE_KEY_RITUAL_LAST_RESULT fuera del writer: {sorted(escritores)}"
    )


def _llamadores_directos(nombre_funcion: str, ruta: pathlib.Path) -> set[str]:
    """Funciones top-level de ``ritual_runner`` que llaman ``nombre_funcion(...)``, por AST.

    Igualdad exacta, no muestreo: el conjunto completo de callers es la
    superficie hermana que AGENTS.md exige enumerar."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    llamadores: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == nombre_funcion:
            dueña = next(
                (
                    fn.name
                    for fn in arbol.body
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(hijo is nodo for hijo in ast.walk(fn))
                ),
                "<modulo>",
            )
            llamadores.add(dueña)
    return llamadores


def test_ancla_llamadores_de_resultados_por_owner() -> None:
    """Ancla por AST, igualdad EXACTA: TODAS las funciones productivas que leen
    el contenedor multi-owner. Si aparece un nuevo caller de
    ``_resultados_por_owner``, este test rompe hasta que ese sibling sea
    clasificado y reciba cobertura de ownership propia — leer el contenedor sin
    la receta de validación es exactamente como nacen los caminos gemelos sin
    cablear (la clase de defecto dominante del repo)."""
    ruta = pathlib.Path(rr_mod.__file__).resolve()
    llamadores = _llamadores_directos("_resultados_por_owner", ruta)
    assert llamadores == {
        "publicar_resultado_de_ritual",
        "resolve_ritual_resume_action",
        "clear_ritual_result_owned",
    }, f"caller nuevo (o ausente) de _resultados_por_owner sin clasificar: {sorted(llamadores)}"


def test_ancla_llamadores_del_selector_compartido() -> None:
    """Ancla por AST, igualdad EXACTA: resolver Y limpiar comparten la MISMA
    política vía ``_seleccionar_resultado_owned``. Un sibling que vuelva a
    seleccionar por su cuenta (o que deje de usar el selector) diverge de su
    gemelo y rompe acá antes de poder hacerlo en producción."""
    ruta = pathlib.Path(rr_mod.__file__).resolve()
    llamadores = _llamadores_directos("_seleccionar_resultado_owned", ruta)
    assert llamadores == {
        "resolve_ritual_resume_action",
        "clear_ritual_result_owned",
    }, f"hermano desconectado del selector compartido de ownership: {sorted(llamadores)}"
