"""Pruebas AAA del contrato de errores del dispatcher para generate_lods (#592 F6).

Verifica que el dispatcher envuelva excepciones y valide que el resultado sea dict:
1. Excepción de preflight.
2. Excepción de configuración.
3. Excepción temprana de servicio.
4. Respuesta exitosa válida.
5. Resultado inválido (no-dict).
6. Cancelación propagada correctamente (CancelledError no es envuelto).
7. Ningún falso éxito ante error.
8. Compatibilidad con consumidores existentes (normalize_tool_result y summarize_ritual_result).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.gui.controllers.ritual_runner import summarize_ritual_result
from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware
from sky_claw.local.tools.tool_result import normalize_tool_result
from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher


def _crear_dispatcher_con_servicio(dyndolod_service: Any) -> Any:
    """Helper para instanciar el dispatcher con el doble de dyndolod_service inyectado."""
    deps = crear_dependencias_dispatcher(dyndolod_service=dyndolod_service)
    gate = HitlGateMiddleware(allow_unattended=True)
    return build_orchestration_dispatcher(deps, hitl_gate=gate)


@pytest.mark.asyncio
async def test_excepcion_de_preflight_es_envuelta_en_error_dict() -> None:
    # Arrange: simular una falla de I/O o permisos inesperada durante el preflight
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(side_effect=PermissionError("Acceso denegado en directorio de staging"))
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act
    resultado = await dispatcher.dispatch("generate_lods", {})

    # Assert
    assert isinstance(resultado, dict)
    assert resultado["status"] == "error"
    assert resultado["reason"] == "DynDOLODPipelineExecutionFailed"
    assert "Acceso denegado en directorio de staging" in resultado["details"]


@pytest.mark.asyncio
async def test_excepcion_de_configuracion_es_envuelta_en_error_dict() -> None:
    # Arrange: simular un error de configuración al inicializar parámetros o runner
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(side_effect=ValueError("Ruta de salida configurada es inválida"))
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act
    resultado = await dispatcher.dispatch("generate_lods", {})

    # Assert
    assert isinstance(resultado, dict)
    assert resultado["status"] == "error"
    assert resultado["reason"] == "DynDOLODPipelineExecutionFailed"
    assert "Ruta de salida configurada es inválida" in resultado["details"]


@pytest.mark.asyncio
async def test_excepcion_temprana_de_servicio_es_envuelta_en_error_dict() -> None:
    # Arrange: simular un error temprano en la consulta de resume o base de datos
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(side_effect=RuntimeError("Fallo crítico en verificación de estado de resume"))
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act
    resultado = await dispatcher.dispatch("generate_lods", {})

    # Assert
    assert isinstance(resultado, dict)
    assert resultado["status"] == "error"
    assert resultado["reason"] == "DynDOLODPipelineExecutionFailed"
    assert "Fallo crítico en verificación de estado de resume" in resultado["details"]


@pytest.mark.asyncio
async def test_respuesta_exitosa_valida_pasa_intacta() -> None:
    # Arrange: el servicio completa con éxito devolviendo un resultado estructurado
    datos_exitosos = {
        "status": "success",
        "success": True,
        "message": "LODs generados exitosamente",
        "lods_count": 4200,
    }
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(return_value=datos_exitosos)
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act
    resultado = await dispatcher.dispatch("generate_lods", {"preset": "High"})

    # Assert
    assert resultado == datos_exitosos
    dyndolod_service.execute.assert_awaited_once_with(preset="High")


@pytest.mark.asyncio
async def test_resultado_invalido_no_dict_es_capturado_por_guard() -> None:
    # Arrange: el servicio devuelve un tipo inválido (p. ej. None o lista)
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(return_value=None)
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act
    resultado = await dispatcher.dispatch("generate_lods", {})

    # Assert
    assert resultado == {"status": "error", "reason": "InvalidDynDOLODResult"}


@pytest.mark.asyncio
async def test_cancelacion_es_propagada_sin_envolver() -> None:
    # Arrange: el servicio es cancelado vía asyncio.CancelledError
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(side_effect=asyncio.CancelledError())
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act & Assert: CancelledError no debe ser capturado ni transformado a dict
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.dispatch("generate_lods", {})


@pytest.mark.asyncio
async def test_ningun_falso_exito_ante_error() -> None:
    # Arrange: excepción de servicio
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(side_effect=RuntimeError("Fallo no recuperable"))
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act
    resultado = await dispatcher.dispatch("generate_lods", {})

    # Assert: verificar que el resultado sea explícitamente de error y no falsamente exitoso
    assert resultado.get("success") is not True
    assert resultado["status"] == "error"
    vista_normalizada = normalize_tool_result(resultado)
    assert vista_normalizada["success"] is False
    assert "Fallo no recuperable" in vista_normalizada["message"]


@pytest.mark.asyncio
async def test_compatibilidad_con_consumidores_existentes() -> None:
    # Arrange: excepción inesperada capturada por el dispatcher
    dyndolod_service = MagicMock()
    dyndolod_service.execute = AsyncMock(side_effect=RuntimeError("Subproceso DynDOLOD abortó con código 1"))
    dispatcher = _crear_dispatcher_con_servicio(dyndolod_service)

    # Act: despachar y pasar el resultado por los normalizadores de GUI y herramientas
    resultado = await dispatcher.dispatch("generate_lods", {})
    vista_normalizada = normalize_tool_result(resultado)
    mensaje_feedback, tipo_feedback = summarize_ritual_result("dyndolod", resultado)

    # Assert: los consumidores operan sin lanzar excepciones y con diagnóstico legible
    assert vista_normalizada["success"] is False
    assert "Subproceso DynDOLOD abortó con código 1" in vista_normalizada["message"]
    assert tipo_feedback == "negative"
    assert "Subproceso DynDOLOD abortó con código 1" in mensaje_feedback
