"""Contrato de errores del dispatcher para las estrategias destructivas restantes.

Origen: issue #726 (auditoría del orquestador), separado del PR #722 —que cerró
este mismo defecto SOLO para ``generate_lods`` (Finding 6 de #592)— para no
ampliar su blast radius.

``generate_animations`` (Pandora), ``quick_auto_clean`` (xEdit: reescribe los
DLC oficiales en disco) y ``generate_bashed_patch`` (Wrye Bash) viven en
``DESTRUCTIVE_TOOL_PATTERNS`` y se registran con el gate HITL, pero SIN
``ErrorWrappingMiddleware`` ni ``DictResultGuardMiddleware``. Dos huecos, ambos
en el camino real de ejecución de la GUI y del agente:

1. **Demostrado (rojo→verde):** el ``ValueError`` de ``validate_for_approval``
   —el rechazo de claves que la estrategia no honra, Codex #213— escapa del
   dispatcher hacia ``SupervisorAgent.dispatch_tool`` (que no envuelve nada) en
   vez de degradar al contrato legacy ``{"status": "error", ...}``. Afecta a
   ``generate_animations`` y ``quick_auto_clean``.
2. **Por tipo de resultado:** un resultado no-dict llega a
   ``normalize_tool_result`` / ``summarize_ritual_result``, que asumen ``dict``
   (``raw.get``), y revienta el resumen del ritual.

Alcance honesto por herramienta (releído contra el servicio, no asumido):

* ``quick_auto_clean`` es el hueco **medido**: ``XEditPipelineService`` sólo
  captura ``_ActionManifestError``, ``LockAcquisitionError`` y
  ``(PatchingError, XEditError)``. A diferencia de sus hermanos, NO tiene el
  ``except Exception → dict`` de la Regla T11, así que un ``OSError`` al
  resolver ``Data`` escapaba del dispatcher.
* ``generate_animations`` y ``generate_bashed_patch`` **sí** cierran con ese
  ``except Exception → dict`` (T11), y Wrye Bash convierte incluso
  ``WryeBashHeadlessUnsupportedError`` en ``success: False``. Para esas dos el
  wrapping es **defensa en profundidad y uniformidad del contrato del
  dispatcher**, no la corrección de un escape observado. Se aplica igual porque
  la garantía es de la FAMILIA: un contrato que depende de que cada servicio
  recuerde su propio ``except`` no es un contrato.

Este módulo congela el contrato DESPUÉS del fix. Por cada herramienta:

1. excepción del servicio → error dict con su propio ``reason``;
2. resultado no-dict → error dict del ``DictResultGuardMiddleware``;
3. payload inválido rechazado por ``validate_for_approval`` → error dict, no
   excepción (el operador ve el rechazo, no un crash);
4. cancelación → ``CancelledError`` se propaga SIN envolver (no hereda de
   ``Exception``; el wrapping no la toca) y el lock se libera;
5. estado terminal ``FAILED`` en ``ToolStateMachine`` —nunca ``COMPLETED`` ante
   un error— con la clave de idempotencia liberada;
6. resultado exitoso pasa intacto (red de regresión).

Y dos invariantes de seguridad que el fix NO puede romper:

7. el wrapping no tapa la denegación HITL: sin aprobación no se ejecuta nada y
   el caller recibe ``HITLApprovalDenied``;
8. el ancla de familia (por introspección, no por muestreo): TODA herramienta
   de ``DESTRUCTIVE_TOOL_PATTERNS`` registrada lleva wrapping + guard + gate.
   Registrar una estrategia destructiva nueva sin la tripleta rompe este test.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.gui.controllers.ritual_runner import summarize_ritual_result
from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from sky_claw.app.orchestrator.tool_state_machine import ToolStateMachine
from sky_claw.app.orchestrator.tool_strategies.middleware import (
    DESTRUCTIVE_TOOL_PATTERNS,
    DictResultGuardMiddleware,
    ErrorWrappingMiddleware,
    HitlGateMiddleware,
    IdempotencyMiddleware,
)
from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools.tool_result import normalize_tool_result
from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher

#: ``reason`` de error y del guard por herramienta (contrato consumido por la GUI).
_RAZON_ERROR: dict[str, str] = {
    "generate_animations": "PandoraAnimationsExecutionFailed",
    "quick_auto_clean": "XEditQuickAutoCleanFailed",
    "generate_bashed_patch": "WryeBashPipelineExecutionFailed",
}
_RAZON_RESULTADO_INVALIDO: dict[str, str] = {
    "generate_animations": "InvalidPandoraAnimationsResult",
    "quick_auto_clean": "InvalidXEditQuickAutoCleanResult",
    "generate_bashed_patch": "InvalidWryeBashPipelineResult",
}

#: Las tres herramientas destructivas que #722 dejó fuera de su fix.
HERRAMIENTAS = list(_RAZON_ERROR)


class _GuardQueNiega:
    """Doble de ``HITLGuard`` que resuelve siempre con DENIED."""

    def __init__(self) -> None:
        self.contador = 0

    async def request_approval(self, **kwargs: Any) -> Decision:
        self.contador += 1
        return Decision.DENIED


class _GuardQueAprueba:
    """Doble de ``HITLGuard`` que resuelve siempre con APPROVED.

    Con ``allow_unattended=True`` el gate devuelve ``next_call()`` ANTES de
    llegar a ``_validate_for_approval``, así que ese atajo no ejercita la ruta
    de producción (``orchestration_composition`` inyecta siempre un guard).
    """

    def __init__(self) -> None:
        self.contador = 0

    async def request_approval(self, **kwargs: Any) -> Decision:
        self.contador += 1
        return Decision.APPROVED


def _doble_para(
    tool: str,
    *,
    retorno: Any = None,
    excepcion: BaseException | None = None,
) -> tuple[dict[str, Any], AsyncMock]:
    """Devuelve (kwargs de dependencias, doble invocado) para la herramienta."""
    doble = AsyncMock(return_value=retorno, side_effect=excepcion)
    if tool == "generate_animations":
        servicio = MagicMock()
        servicio.generate_animations = doble
        return {"pandora_service": servicio}, doble
    if tool == "quick_auto_clean":
        servicio = MagicMock()
        servicio.quick_auto_clean = doble
        return {"xedit_service": servicio}, doble
    if tool == "generate_bashed_patch":
        return {"wrye_bash_pipeline": doble}, doble
    raise AssertionError(f"herramienta no cubierta por este contrato: {tool}")


def _construir_dispatcher(
    *,
    idempotency: IdempotencyMiddleware | None = None,
    gate: HitlGateMiddleware | None = None,
    **dependencias: Any,
) -> Any:
    """Dispatcher real con las dependencias de producción salvo los dobles inyectados."""
    return build_orchestration_dispatcher(
        crear_dependencias_dispatcher(**dependencias),
        hitl_gate=gate if gate is not None else HitlGateMiddleware(allow_unattended=True),
        idempotency=idempotency,
    )


# ---------------------------------------------------------------------------
# 1. Excepción del servicio → error dict (hoy escapa y rompe el contrato)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", HERRAMIENTAS)
async def test_excepcion_del_servicio_se_convierte_en_error_dict(tool: str) -> None:
    # Arrange: el servicio de la herramienta falla con una excepción inesperada.
    dependencias, _ = _doble_para(tool, excepcion=RuntimeError(f"{tool} explotó"))
    dispatcher = _construir_dispatcher(**dependencias)

    # Act
    resultado = await dispatcher.dispatch(tool, {})

    # Assert: contrato legacy, nunca una excepción que cruce el dispatcher.
    assert isinstance(resultado, dict)
    assert resultado["status"] == "error"
    assert resultado["reason"] == _RAZON_ERROR[tool]
    assert f"{tool} explotó" in resultado["details"]


# ---------------------------------------------------------------------------
# 2. Resultado no-dict → error dict (hoy llega crudo a los normalizadores)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", HERRAMIENTAS)
async def test_resultado_no_dict_se_convierte_en_error_dict(tool: str) -> None:
    # Arrange: el servicio devuelve un tipo que ningún consumidor sabe leer.
    dependencias, _ = _doble_para(tool, retorno=["no", "es", "un", "dict"])
    dispatcher = _construir_dispatcher(**dependencias)

    # Act
    resultado = await dispatcher.dispatch(tool, {})

    # Assert
    assert resultado == {"status": "error", "reason": _RAZON_RESULTADO_INVALIDO[tool]}


@pytest.mark.parametrize("tool", HERRAMIENTAS)
async def test_error_dict_es_consumible_por_gui_y_normalizador(tool: str) -> None:
    # Arrange: fallo realista de la herramienta (binario ausente/cancelado).
    dependencias, _ = _doble_para(tool, excepcion=RuntimeError("subproceso abortó con código 1"))
    dispatcher = _construir_dispatcher(**dependencias)

    # Act: el resultado cruza los DOS consumidores productivos del contrato.
    resultado = await dispatcher.dispatch(tool, {})
    vista = normalize_tool_result(resultado)
    mensaje, tipo = summarize_ritual_result(tool, resultado)

    # Assert: ninguno de los dos revienta y ambos nombran la causa.
    assert vista["success"] is False
    assert "subproceso abortó con código 1" in vista["message"]
    assert tipo == "negative"
    assert "subproceso abortó con código 1" in mensaje


# ---------------------------------------------------------------------------
# 3. Payload inválido rechazado por validate_for_approval → error dict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["generate_animations", "quick_auto_clean"])
async def test_payload_invalido_se_convierte_en_error_dict(tool: str) -> None:
    # Arrange: las dos estrategias rechazan claves que no honran (Codex #213) para
    # que un `dry_run=True` no produzca una aprobación engañosa. Gate con guard que
    # APRUEBA: es la única forma de llegar a `validate_for_approval` (con
    # allow_unattended el gate corta antes y no la invoca).
    dependencias, doble = _doble_para(tool, retorno={"status": "success", "success": True})
    dispatcher = _construir_dispatcher(gate=HitlGateMiddleware(hitl_guard=_GuardQueAprueba()), **dependencias)

    # Act
    resultado = await dispatcher.dispatch(tool, {"dry_run": True})

    # Assert: el rechazo se reporta, y la mutación NO corre.
    assert resultado["status"] == "error"
    assert resultado["reason"] == _RAZON_ERROR[tool]
    assert "dry_run" in resultado["details"]
    doble.assert_not_awaited()


async def test_generate_bashed_patch_filtra_claves_y_conserva_el_contrato() -> None:
    # Arrange: generate_bashed_patch NO tiene validate_for_approval — filtra y
    # advierte (semántica deliberada, distinta de las otras dos). El wrapping no
    # puede cambiar ese filtrado.
    dependencias, doble = _doble_para(
        "generate_bashed_patch",
        retorno={"status": "success", "success": True},
    )
    dispatcher = _construir_dispatcher(**dependencias)

    # Act
    resultado = await dispatcher.dispatch(
        "generate_bashed_patch",
        {"profile": "P", "validate_limit": True, "tool_name": "generate_bashed_patch", "extra": 42},
    )

    # Assert
    assert resultado["status"] == "success"
    doble.assert_awaited_once_with(profile="P", validate_limit=True)


# ---------------------------------------------------------------------------
# 4. Cancelación: se propaga sin envolver y libera el lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", HERRAMIENTAS)
async def test_cancelacion_se_propaga_sin_envolver(tool: str) -> None:
    # Arrange
    dependencias, _ = _doble_para(tool, excepcion=asyncio.CancelledError())
    dispatcher = _construir_dispatcher(**dependencias)

    # Act & Assert: CancelledError no hereda de Exception; el wrapping no lo toca.
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.dispatch(tool, {})


# ---------------------------------------------------------------------------
# 5. Estado terminal FAILED (nunca COMPLETED) con el lock liberado
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", HERRAMIENTAS)
async def test_fallo_registra_estado_failed_y_libera_el_lock(tool: str) -> None:
    # Arrange: state machine e idempotencia reales, como en la composición.
    sm = ToolStateMachine()
    idempotency = IdempotencyMiddleware(state_machine=sm)
    dependencias, _ = _doble_para(tool, excepcion=RuntimeError(f"{tool} falló"))
    dispatcher = _construir_dispatcher(idempotency=idempotency, **dependencias)

    # Act
    resultado = await dispatcher.dispatch(tool, {})

    # Assert: el resultado es el dict de error y la tarea NO queda COMPLETED.
    assert resultado["status"] == "error"
    tareas = list(sm._tasks.values())
    assert len(tareas) == 1
    tarea = tareas[0]
    assert tarea.state == "FAILED"
    assert f"{tool} falló" in str(tarea.error_message)
    assert sm.guard.is_active(tarea.idempotency_key) is False


@pytest.mark.parametrize("tool", HERRAMIENTAS)
async def test_exito_registra_estado_completed(tool: str) -> None:
    # Arrange
    sm = ToolStateMachine()
    idempotency = IdempotencyMiddleware(state_machine=sm)
    exitoso = {"status": "success", "success": True, "message": "OK"}
    dependencias, _ = _doble_para(tool, retorno=exitoso)
    dispatcher = _construir_dispatcher(idempotency=idempotency, **dependencias)

    # Act
    resultado = await dispatcher.dispatch(tool, {})

    # Assert: la red de regresión — el wrapping no convierte éxito en error.
    assert resultado == exitoso
    tareas = list(sm._tasks.values())
    assert len(tareas) == 1
    assert tareas[0].state == "COMPLETED"


# ---------------------------------------------------------------------------
# 7. El wrapping NO tapa la denegación HITL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", HERRAMIENTAS)
async def test_denegacion_hitl_no_queda_tapada_por_el_wrapping(tool: str) -> None:
    # Arrange: gate real con un guard que niega.
    guard = _GuardQueNiega()
    dependencias, doble = _doble_para(tool, retorno={"status": "success", "success": True})
    dispatcher = _construir_dispatcher(gate=HitlGateMiddleware(hitl_guard=guard), **dependencias)

    # Act
    resultado = await dispatcher.dispatch(tool, {})

    # Assert: el caller ve la denegación y la mutación NO corre.
    assert resultado["status"] == "error"
    assert resultado["reason"] == "HITLApprovalDenied"
    assert guard.contador == 1
    doble.assert_not_awaited()


# ---------------------------------------------------------------------------
# 8. Ancla de familia: TODA herramienta destructiva lleva la tripleta
# ---------------------------------------------------------------------------


def test_toda_herramienta_destructiva_lleva_wrapping_guard_y_gate() -> None:
    """Ancla por introspección (no muestrea): la propiedad, no el recordatorio.

    Un wrapping que cubra solo a `generate_lods` no protege nada: la garantía
    es de la FAMILIA. Registrar una estrategia destructiva sin la tripleta
    rompe este test hasta que se le escriba su receta.
    """
    dispatcher = _construir_dispatcher()

    cadenas = dispatcher._middleware
    for herramienta in sorted(DESTRUCTIVE_TOOL_PATTERNS):
        cadena = cadenas.get(herramienta)
        assert cadena is not None, f"la herramienta destructiva '{herramienta}' no está registrada"
        tipos = {type(mw) for mw in cadena}
        assert ErrorWrappingMiddleware in tipos, f"'{herramienta}' no envuelve excepciones"
        assert DictResultGuardMiddleware in tipos, f"'{herramienta}' no valida el tipo de resultado"
        assert HitlGateMiddleware in tipos, f"'{herramienta}' no pasa por el gate HITL"


def test_toda_herramienta_destructiva_esta_registrada() -> None:
    """El alias viejo `resolve_conflict_patch` dejó a xEdit sin gate: que no se repita."""
    dispatcher = _construir_dispatcher()

    registradas = set(dispatcher.registered_tools())
    assert set(DESTRUCTIVE_TOOL_PATTERNS) <= registradas
