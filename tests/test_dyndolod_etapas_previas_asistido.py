"""D-3: el modo ASISTIDO de la GUI pide confirmación al operador si faltan etapas previas (SOP §5.2).

Complementa ``tests/test_etapas_previas_lods.py`` (la strategy verifica y corta). Acá se ancla el canal humano:

- una categoría HITL PROPIA. La GUI solo estaciona como modal sus categorías conocidas; cualquier otra cae al
  canal REMOTO del guard (Telegram). Reutilizar la categoría del override remoto (``dyndolod_desatendido``) habría
  mandado la pregunta a Telegram aunque el operador esté frente a la GUI, y sin Telegram esperaría hasta el plazo;
- esa categoría NUNCA se auto-aprueba en «Modo local»: una confirmación que se contesta sola es de adorno;
- el aprobador es fail-closed (sin guard, o con un guard que falla, no hay «sí»);
- el cableado: la composición lo inyecta y el dispatcher lo pasa a la strategy (si no, todo lo anterior queda
  muerto y los demás tests siguen verdes).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from sky_claw.app.gui.controllers.ritual_runner import make_gui_hitl_notify
from sky_claw.app.orchestrator.dyndolod_desatendido_hitl import (
    AprobadorDesatendidoHITL,
    texto_de_etapas_previas_del_asistido,
)
from sky_claw.app.security.hitl import (
    CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
    CATEGORIA_DYNDOLOD_DESATENDIDO,
    CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO,
    Decision,
    HITLGuard,
    HITLRequest,
)
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion

RAIZ = pathlib.Path(__file__).resolve().parents[1]
FALTANTES = ("etapa 6 (Wrye Bash): falta el Bashed Patch", "etapa 7 (Synthesis): falta Synthesis.esp")


def _arbol(relativa: str) -> ast.Module:
    return ast.parse((RAIZ / relativa).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# La categoría
# ---------------------------------------------------------------------------


def test_la_categoria_del_override_asistido_existe_y_es_distinta_de_las_demas() -> None:
    categorias = [
        CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO,
        CATEGORIA_DYNDOLOD_DESATENDIDO,
        CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
        "tool_execution",
        "download",
        "sandbox_promotion",
    ]

    assert CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO == "dyndolod_etapas_previas_asistido"
    assert len(set(categorias)) == len(categorias), "cada categoría HITL tiene su propio tratamiento"


# ---------------------------------------------------------------------------
# El puente de la GUI: modal, con dueño, y NUNCA auto-aprobada
# ---------------------------------------------------------------------------


async def test_el_modo_local_no_auto_aprueba_la_confirmacion_de_etapas_previas() -> None:
    aprobaciones: list[tuple[str, bool]] = []
    parkeados: list[dict] = []

    async def _respond(request_id: str, approved: bool) -> None:
        aprobaciones.append((request_id, approved))

    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=parkeados.append,
        auto_approve_getter=lambda: True,  # «Modo local» ENCENDIDO
        tab_id_getter=lambda: "tab-1",
        delegate=None,
    )

    await notify(
        HITLRequest(
            request_id="dyndolod-etapas-asistido-abc",
            reason="faltan etapas previas",
            detail="- etapa 6",
            category=CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO,
        )
    )

    assert aprobaciones == [], "«Modo local» contestó solo una confirmación que es del operador"
    assert len(parkeados) == 1, "la GUI la estaciona como modal en vez de mandarla a otro canal"
    assert parkeados[0]["category"] == CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO
    assert parkeados[0]["detail"] == "- etapa 6"


async def test_el_puente_scopea_la_confirmacion_a_la_pestaña_que_lanzo_la_corrida() -> None:
    """P1-7: sin dueño, la confirmación sería accionable desde CUALQUIER sesión abierta."""
    parkeados: list[dict] = []

    async def _respond(request_id: str, approved: bool) -> None:  # pragma: no cover - no debe llamarse
        raise AssertionError("no se contesta sola")

    notify = make_gui_hitl_notify(
        respond=_respond,
        set_pending=parkeados.append,
        auto_approve_getter=lambda: False,
        tab_id_getter=lambda: "tab-7",
        delegate=None,
    )
    await notify(HITLRequest(request_id="x", reason="r", category=CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO))

    assert len(parkeados) == 1
    assert "tab-7" in repr(parkeados[0]), "el pedido viaja con el dueño (la pestaña que lanzó la etapa 9)"


# ---------------------------------------------------------------------------
# El aprobador
# ---------------------------------------------------------------------------


def _guard_que_contesta(decision: Decision, vistos: list[HITLRequest]) -> HITLGuard:
    guard = HITLGuard(timeout=5)

    async def _notificar(req: HITLRequest) -> None:
        vistos.append(req)
        if decision is not Decision.TIMEOUT:
            await guard.respond(req.request_id, decision is Decision.APPROVED)

    guard._notify = _notificar  # noqa: SLF001 -- el seam de notificación del guard
    return guard


@pytest.mark.parametrize(
    ("decision", "esperado"),
    [(Decision.APPROVED, ResultadoConfirmacion.APROBADA), (Decision.DENIED, ResultadoConfirmacion.DENEGADA)],
)
async def test_el_aprobador_asistido_traduce_la_decision_del_operador(decision, esperado) -> None:
    vistos: list[HITLRequest] = []
    aprobador = AprobadorDesatendidoHITL(hitl_guard=_guard_que_contesta(decision, vistos))

    resultado = await aprobador.aprobar_sin_etapas_previas_asistido(FALTANTES)

    assert resultado is esperado
    assert len(vistos) == 1


async def test_el_aprobador_asistido_pregunta_con_su_categoria_su_prefijo_y_cada_etapa_nombrada() -> None:
    vistos: list[HITLRequest] = []
    aprobador = AprobadorDesatendidoHITL(hitl_guard=_guard_que_contesta(Decision.APPROVED, vistos))

    await aprobador.aprobar_sin_etapas_previas_asistido(FALTANTES)

    pedido = vistos[0]
    assert pedido.category == CATEGORIA_DYNDOLOD_ETAPAS_PREVIAS_ASISTIDO
    assert pedido.request_id.startswith("dyndolod-etapas-asistido-")
    assert all(etapa in pedido.detail for etapa in FALTANTES)


async def test_el_vencimiento_del_plazo_no_es_un_si() -> None:
    aprobador = AprobadorDesatendidoHITL(hitl_guard=_guard_que_contesta(Decision.TIMEOUT, []))

    resultado = await aprobador.aprobar_sin_etapas_previas_asistido(FALTANTES, timeout_seconds=0.05)

    assert resultado is ResultadoConfirmacion.TIMEOUT


async def test_sin_guard_no_hay_confirmacion_para_saltear_etapas_en_asistido() -> None:
    aprobador = AprobadorDesatendidoHITL(hitl_guard=None)

    assert await aprobador.aprobar_sin_etapas_previas_asistido(FALTANTES) is ResultadoConfirmacion.CANAL_NO_DISPONIBLE


async def test_un_guard_que_falla_es_canal_no_disponible_y_nunca_un_si() -> None:
    class GuardRoto:
        async def request_approval(self, **_kwargs: object) -> Decision:
            raise RuntimeError("el canal humano explotó")

    aprobador = AprobadorDesatendidoHITL(hitl_guard=GuardRoto())  # type: ignore[arg-type]

    assert await aprobador.aprobar_sin_etapas_previas_asistido(FALTANTES) is ResultadoConfirmacion.CANAL_NO_DISPONIBLE


async def test_la_cancelacion_se_propaga_desde_el_aprobador_asistido() -> None:
    class GuardCancelado:
        async def request_approval(self, **_kwargs: object) -> Decision:
            raise asyncio.CancelledError

    aprobador = AprobadorDesatendidoHITL(hitl_guard=GuardCancelado())  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        await aprobador.aprobar_sin_etapas_previas_asistido(FALTANTES)


def test_el_texto_dice_que_es_la_gui_asistida_y_no_el_canal_remoto() -> None:
    reason, detail = texto_de_etapas_previas_del_asistido(FALTANTES)

    assert "GUI" in reason and "ASISTIDO" in reason
    assert "canal remoto" not in reason
    assert all(etapa in detail for etapa in FALTANTES)


# ---------------------------------------------------------------------------
# El cableado: sin esto nada de lo anterior llega a producción
# ---------------------------------------------------------------------------


def _llamadas(arbol: ast.Module, nombre: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == nombre)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre)
        )
    ]


def test_la_composicion_inyecta_el_aprobador_asistido_en_las_dependencias_del_dispatcher() -> None:
    construcciones = _llamadas(
        _arbol("sky_claw/app/orchestrator/orchestration_composition.py"), "OrchestrationDispatcherDependencies"
    )

    assert len(construcciones) == 1, "cambió la familia de sitios que arman las dependencias del dispatcher"
    cableado = {
        k.arg: ast.unparse(k.value) for k in construcciones[0].keywords if k.arg == "aprobar_sin_etapas_previas"
    }
    assert cableado, "las dependencias del dispatcher no reciben el aprobador de etapas previas"
    assert cableado["aprobar_sin_etapas_previas"].endswith(".aprobar_sin_etapas_previas_asistido")


def test_el_dispatcher_pasa_el_aprobador_a_la_unica_strategy_de_produccion() -> None:
    construcciones = _llamadas(_arbol("sky_claw/app/orchestrator/tool_dispatcher.py"), "GenerateLodsStrategy")

    assert len(construcciones) == 1, "cambió la familia de sitios que construyen GenerateLodsStrategy en producción"
    cableado = {
        k.arg: ast.unparse(k.value) for k in construcciones[0].keywords if k.arg == "aprobar_sin_etapas_previas"
    }
    assert cableado == {"aprobar_sin_etapas_previas": "dependencies.aprobar_sin_etapas_previas"}
