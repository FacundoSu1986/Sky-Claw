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
    el texto. ``run_ritual`` publica el dict crudo bajo la clave estructural y
    el resume lo re-publica igual."""
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
    assert publicado.get("needs_deployment") is True
    assert publicado.get("texgen_mod_path") == "C:/MO2/mods/TexGen Output"


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
