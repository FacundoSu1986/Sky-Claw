"""Post-D2 (PR #493): superficie de producción del resume TexGen → DynDOLOD.

F-001: el resume durable DEBE ser alcanzable desde la superficie real (GUI
Rituales) sin que "Generar" pierda su semántica. Estos tests atraviesan el
boundary de producción — ``ritual_runner`` → ``supervisor.dispatch_tool`` con
payload real → ``GenerateLodsStrategy`` → ``DynDOLODPipelineService`` — y NO
mockean ``service.execute(run_texgen=False)`` como punto de partida.

Fase RED: ``run_ritual_resume``, ``resume_action_from_result`` y la clave de
store del resultado estructural no existen todavía — los tests que los ejercen
son ROJOS. Los verdes por construcción son los dos controles (el payload de
"Generar" intacto y el filtro del strategy), anclas de conducta preexistente.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sky_claw.app.gui.controllers import ritual_runner as rr_mod
from sky_claw.app.gui.state.reactive_store import ReactiveStore
from sky_claw.app.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy


class _FakeSupervisor:
    """Supervisor duck-typed mínimo: registra (tool_name, payload) y devuelve el
    result configurado. Es el boundary que ``run_ritual``/``run_ritual_resume``
    atraviesan tal cual en producción."""

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else {"success": True}

    async def dispatch_tool(self, tool_name: str, payload: dict) -> dict:
        self.calls.append((tool_name, dict(payload)))
        return dict(self.result)


# ── F-001: acción de resume estructural (no infiere por strings) ─────────────


def test_resume_action_es_estructural_y_no_parsea_el_message() -> None:
    """La GUI decide por ``needs_deployment`` (flag), nunca por el texto del
    mensaje. Con el flag: acción explícita con payload resume; sin él: nada,
    aunque el message mencione deployment."""
    action = rr_mod.resume_action_from_result(
        "dyndolod",
        {
            "success": False,
            "needs_deployment": True,
            "texgen_mod_path": "C:/MO2/mods/TexGen Output",
            "message": "materializá el árbol antes de reintentar",
        },
    )
    assert action is not None
    assert action["tool_key"] == "dyndolod"
    assert action["payload"] == {"run_texgen": False}
    assert action["label"], "la acción necesita un label legible"
    assert "TexGen Output" in action.get("detail", "")

    # El flag ausente no se suple con el string del message.
    assert (
        rr_mod.resume_action_from_result(
            "dyndolod",
            {"success": False, "message": "needs deployment, por favor"},
        )
        is None
    )
    # Otra tool nunca ofrece la acción, ni con el flag.
    assert rr_mod.resume_action_from_result("loot", {"success": False, "needs_deployment": True}) is None


# ── F-001: la intención resume despacha el payload real ──────────────────────


async def test_run_ritual_resume_despacha_generate_lods_con_run_texgen_false() -> None:
    """La NUEVA intención de continuación atraviesa el MISMO dispatcher real con
    ``run_texgen=False`` — el service conserva la validación durable."""
    sup = _FakeSupervisor({"success": True})
    store = ReactiveStore()

    await rr_mod.run_ritual_resume("dyndolod", supervisor=sup, store=store)

    assert sup.calls == [("generate_lods", {"run_texgen": False})]


async def test_run_ritual_generar_conserva_el_payload_vacio() -> None:
    """Control (no confundir intenciones): la acción normal "Generar" SIGUE
    despachando ``{}`` — ``run_texgen`` queda en su default ``True`` y la vía de
    regeneración/supersede no se toca."""
    sup = _FakeSupervisor({"success": True})
    store = ReactiveStore()

    await rr_mod.run_ritual("dyndolod", supervisor=sup, store=store)

    assert sup.calls == [("generate_lods", {})]


async def test_run_ritual_publica_el_resultado_estructural_para_el_panel() -> None:
    """F-001 GUI actionability: el panel consume el RESULTADO (flag + path), no
    el texto. ``run_ritual`` publica el envelope con dueño + tool_key + dict
    crudo bajo la clave estructural (R-004/R-005) y el resume lo re-publica
    igual."""
    sup = _FakeSupervisor(
        {
            "success": False,
            "needs_deployment": True,
            "texgen_mod_path": "C:/MO2/mods/TexGen Output",
            "message": "falló",
        }
    )
    store = ReactiveStore()

    await rr_mod.run_ritual("dyndolod", supervisor=sup, store=store)

    publicado = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)
    assert publicado is not None
    assert publicado.get(rr_mod.RITUAL_RESULT_TOOL_KEY) == "dyndolod", (
        "el tool_key de la corrida no viaja con el resultado"
    )
    assert publicado.get(rr_mod.RITUAL_RESULT_OWNER_TAB) is None, "sin tab_id el resultado no tiene dueño"
    resultado = publicado.get(rr_mod.RITUAL_RESULT_PAYLOAD_KEY)
    assert resultado is not None
    assert resultado.get("needs_deployment") is True
    assert resultado.get("texgen_mod_path") == "C:/MO2/mods/TexGen Output"


async def test_run_ritual_resume_usa_el_mismo_single_flight_y_feedback() -> None:
    """El resume no es un segundo camino: comparte el single-flight y el
    feedback del ritual. Con uno en vuelo, el resume también se rehúsa."""
    store = ReactiveStore()
    store.set(rr_mod.STORE_KEY_RITUAL_IN_FLIGHT, True)
    sup = _FakeSupervisor()

    await rr_mod.run_ritual_resume("dyndolod", supervisor=sup, store=store)

    assert sup.calls == []
    fb = store.get(rr_mod.STORE_KEY_RITUAL_FEEDBACK)
    assert fb is not None and fb["type"] == "warning"


# ── R-004/R-005: ownership del resultado accionable entre pestañas ────────────


def _resultado_needs_deployment() -> dict:
    return {
        "success": False,
        "needs_deployment": True,
        "texgen_mod_path": "C:/MO2/mods/TexGen Output",
        "message": "materializá el árbol antes de reintentar",
    }


def test_solo_la_pestana_duena_resuelve_la_accion_de_resume() -> None:
    """R-004: la acción destructiva de Resume no migra entre pestañas — sólo el
    ``owner_tab`` la puede resolver/consumir."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )

    assert rr_mod.resolve_ritual_resume_action(store, "tab-A") is not None
    assert rr_mod.resolve_ritual_resume_action(store, "tab-B") is None, (
        "una pestaña que NO lanzó el ritual puede consumir la acción destructiva de Resume"
    )


def test_la_accion_sin_dueno_la_resuelve_cualquier_pestana() -> None:
    """Un resultado sin pestaña lanzadora (agente/backend, o dispatch sin
    contexto) queda sin dueño y lo ve cualquiera — el mismo criterio que
    ``resolve_pending_hitl``."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id=None,
    )

    for pestana in ("tab-A", "tab-B", None):
        assert rr_mod.resolve_ritual_resume_action(store, pestana) is not None, (
            f"{pestana} no vería una acción sin dueño"
        )


def test_el_resultado_de_otro_ritual_no_produce_accion_dyndolod() -> None:
    """R-005: un ``needs_deployment`` accidental/fixture de OTRO ritual no
    produce una acción de DynDOLOD — el tool_key real de la corrida viaja con
    el resultado y decide."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="loot",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )

    assert rr_mod.resolve_ritual_resume_action(store, "tab-A") is None, (
        "un resultado de otro ritual con needs_deployment de fixture produjo una acción de DynDOLOD"
    )


def test_tras_consumir_la_accion_desaparece() -> None:
    """El consumo de la vista limpia las dos claves: la acción deja de
    resolverse para cualquiera (incluida la dueña)."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )
    assert rr_mod.resolve_ritual_resume_action(store, "tab-A") is not None

    store.set(rr_mod.STORE_KEY_RITUAL_FEEDBACK, None)
    store.set(rr_mod.STORE_KEY_RITUAL_LAST_RESULT, None)

    assert rr_mod.resolve_ritual_resume_action(store, "tab-A") is None


async def test_tras_un_success_la_accion_desaparece() -> None:
    """El resultado del nuevo run reemplaza al viejo: un success no ofrece
    acción, ni siquiera a la pestaña dueña del resultado anterior."""
    store = ReactiveStore()
    rr_mod.publicar_resultado_de_ritual(
        store,
        tool_key="dyndolod",
        resultado=_resultado_needs_deployment(),
        tab_id="tab-A",
    )
    assert rr_mod.resolve_ritual_resume_action(store, "tab-A") is not None

    await rr_mod.run_ritual("dyndolod", supervisor=_FakeSupervisor({"success": True}), store=store, tab_id="tab-A")

    assert rr_mod.resolve_ritual_resume_action(store, "tab-A") is None, (
        "un success dejó consumible la acción de Resume del resultado viejo"
    )


async def test_una_nueva_generacion_no_consume_el_resultado_viejo() -> None:
    """El arranque de un run nuevo limpia la clave antes de despachar: el
    resultado VIEJO no se consume ni se re-resuelve tras la nueva generación."""
    store = ReactiveStore()
    viejo = dict(_resultado_needs_deployment())
    rr_mod.publicar_resultado_de_ritual(store, tool_key="dyndolod", resultado=viejo, tab_id="tab-A")

    await rr_mod.run_ritual("dyndolod", supervisor=_FakeSupervisor({"success": True}), store=store, tab_id="tab-A")

    publicado = store.get(rr_mod.STORE_KEY_RITUAL_LAST_RESULT)
    assert publicado is not None
    assert publicado.get(rr_mod.RITUAL_RESULT_PAYLOAD_KEY) != viejo
    assert rr_mod.resolve_ritual_resume_action(store, "tab-A") is None


def test_la_vista_no_deriva_la_accion_con_literal_de_tool() -> None:
    """R-005/M5-8: la vista debe resolver la acción por el seam
    ``resolve_ritual_resume_action`` (que lee el tool_key del envelope), nunca
    derivarla directo con un literal ``"dyndolod"``. El ancla por AST congela
    la ausencia del call directo en el módulo de la vista."""
    import pathlib

    vista = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "app" / "gui" / "views" / "forge_dashboard.py"
    fuente = vista.read_text(encoding="utf-8")

    assert "resolve_ritual_resume_action" in fuente, "la vista no usa el seam de ownership del resume"
    assert "resume_action_from_result(" not in fuente, (
        "la vista deriva la acción con un call directo (y por lo tanto un tool_key literal): "
        "el tool_key debe salir del envelope publicado por la corrida"
    )


# ── Ancla del tramo dispatcher→strategy→service ──────────────────────────────


async def test_el_strategy_despacha_run_texgen_false_al_service() -> None:
    """El tramo final del boundary: ``GenerateLodsStrategy`` filtra el payload
    resume y llama al service con ``run_texgen=False`` — el punto donde la
    validación durable de D2 toma el control."""
    service = SimpleNamespace(execute=AsyncMock(return_value={"success": True}))
    strategy = GenerateLodsStrategy(service)  # type: ignore[arg-type]

    await strategy.execute({"run_texgen": False})

    service.execute.assert_awaited_once_with(run_texgen=False)


# ── Ancla enumerativa de superficies ─────────────────────────────────────────


def test_todo_estado_que_necesita_transicion_de_usuario_tiene_superficie() -> None:
    """Cada estado durable que exige una transición del usuario (hoy solo
    AWAITING_DEPLOYMENT → deploy + resume) debe tener una ruta de producción
    verificada que lo emite. El dict se congela por igualdad literal: un estado
    nuevo sin superficie rompe acá."""
    from sky_claw.app.db.handoffs import HandoffState

    superficies_verificadas = {
        HandoffState.AWAITING_DEPLOYMENT.value: "run_ritual_resume → generate_lods {run_texgen: false}",
    }
    assert superficies_verificadas == {
        "awaiting_deployment": "run_ritual_resume → generate_lods {run_texgen: false}",
    }
    assert callable(getattr(rr_mod, "run_ritual_resume", None)), "la ruta de producción debe existir"


@pytest.mark.asyncio
async def test_la_superficie_llm_no_registra_generate_lods_sin_guard_upstream() -> None:
    """STOP_LLM_SURFACE_SCOPE_EXPANSION: el agente LLM NO expone ``generate_lods``
    en este commit — el SOP §2 exige un guard de upstream-completion (Wrye Bash +
    Synthesis) que ``GenerateLodsStrategy`` todavía no tiene (deuda abierta).
    Este ancla congela la ausencia (por AST, sin instanciar el registry) para
    que la expansión sea una decisión explícita, no un registro accidental."""
    import ast
    import pathlib

    from sky_claw.app.agent.tools import __file__ as tools_init

    arbol = ast.parse(pathlib.Path(str(tools_init)).read_text(encoding="utf-8"))
    registradas: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Subscript) and isinstance(nodo.value, ast.Attribute) and nodo.value.attr == "_tools":
            clave = nodo.slice
            if isinstance(clave, ast.Constant) and isinstance(clave.value, str):
                registradas.add(clave.value)

    assert "generate_lods" not in registradas, (
        "generate_lods entró a la superficie LLM sin el guard de upstream-completion del SOP §2"
    )
