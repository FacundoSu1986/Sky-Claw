"""H-11: el servicio convierte el latido del runner en un evento y en un snapshot que lee ``lods_status``.

Complementa ``tests/test_dyndolod_progreso.py`` (el runner mide y entrega). Acá se ancla la mitad del servicio:

- el snapshot se guarda ANTES de publicar: si el bus está caído, ``lods_status`` igual ve el progreso;
- el snapshot es de UNA corrida: se limpia al iniciar la siguiente y al terminar, para no mostrar el progreso de
  la corrida anterior como si fuera de la actual;
- el único sitio de producción que construye el runner lo cablea (``on_progress``); si no, el progreso moriría
  en silencio y los demás tests seguirían verdes.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

import sky_claw.local.tools.dyndolod_service as modulo_servicio
from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.core.event_payloads import DynDOLODPipelineProgressPayload
from sky_claw.local.tools.dyndolod_runner import ProgresoDeHerramienta
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.dyndolod_uia_contrato import ModoDeInteraccion

TOPIC_DE_PROGRESO = "pipeline.dyndolod.progress"


def _servicio(bus: AsyncMock) -> DynDOLODPipelineService:
    return DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=bus,
        mo2_profile="Default",
    )


def _bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


def _muestra(**cambios: object) -> ProgresoDeHerramienta:
    base: dict[str, object] = {
        "tool_name": "DynDOLOD",
        "elapsed_seconds": 125.5,
        "log_path": "C:/DynDOLOD/Logs/DynDOLOD_SSE_log.txt",
        "log_bytes": 4096,
        "log_idle_seconds": 7.25,
    }
    base.update(cambios)
    return ProgresoDeHerramienta(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# El payload
# ---------------------------------------------------------------------------


def test_el_payload_de_progreso_es_inmutable_y_estricto() -> None:
    payload = DynDOLODPipelineProgressPayload(tool_name="TexGen", elapsed_seconds=1.0)

    assert payload.log_bytes is None and payload.log_idle_seconds is None and payload.log_path is None
    assert payload.sampled_at > 0
    with pytest.raises(ValidationError):
        payload.tool_name = "otro"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DynDOLODPipelineProgressPayload(tool_name="TexGen", elapsed_seconds="mucho")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Guardar y publicar
# ---------------------------------------------------------------------------


async def test_sin_corrida_no_hay_progreso() -> None:
    assert _servicio(_bus()).progreso_actual is None


async def test_el_latido_guarda_el_snapshot_y_publica_el_evento() -> None:
    bus = _bus()
    servicio = _servicio(bus)

    await servicio._al_progresar(_muestra())  # noqa: SLF001 -- es el callback que recibe el runner

    actual = servicio.progreso_actual
    assert actual is not None
    assert (actual.tool_name, actual.elapsed_seconds, actual.log_bytes, actual.log_idle_seconds) == (
        "DynDOLOD",
        125.5,
        4096,
        7.25,
    )
    bus.publish.assert_awaited_once()
    evento = bus.publish.await_args.args[0]
    assert evento.topic == TOPIC_DE_PROGRESO
    assert evento.payload["tool_name"] == "DynDOLOD"
    assert evento.payload["log_bytes"] == 4096
    assert evento.payload["sampled_at"] == actual.sampled_at


async def test_si_el_bus_falla_el_snapshot_ya_quedo_guardado() -> None:
    """``lods_status`` no depende de que el bus esté vivo: el snapshot va primero."""
    bus = _bus()
    bus.publish.side_effect = RuntimeError("dispatcher caído")
    servicio = _servicio(bus)

    with pytest.raises(RuntimeError):  # el guard best-effort vive en el runner; el servicio no lo duplica
        await servicio._al_progresar(_muestra())  # noqa: SLF001

    assert servicio.progreso_actual is not None
    assert servicio.progreso_actual.elapsed_seconds == 125.5


# ---------------------------------------------------------------------------
# El snapshot es de UNA corrida
# ---------------------------------------------------------------------------


async def test_iniciar_una_corrida_descarta_el_progreso_de_la_anterior() -> None:
    servicio = _servicio(_bus())
    await servicio._al_progresar(_muestra())  # noqa: SLF001
    assert servicio.progreso_actual is not None

    await servicio._publish_started(preset="Medium", run_texgen=True, modo=ModoDeInteraccion.ASISTIDO)  # noqa: SLF001

    assert servicio.progreso_actual is None


async def test_terminar_la_corrida_descarta_su_progreso() -> None:
    servicio = _servicio(_bus())
    await servicio._al_progresar(_muestra())  # noqa: SLF001

    await servicio._publish_completed(  # noqa: SLF001
        preset="Medium",
        run_texgen=True,
        success=True,
        texgen_success=True,
        dyndolod_success=True,
        errors=(),
        duration_seconds=10.0,
        rolled_back=False,
    )

    assert servicio.progreso_actual is None


# ---------------------------------------------------------------------------
# El cableado: sin esto nada de lo anterior llega a producción
# ---------------------------------------------------------------------------


def test_el_unico_sitio_que_construye_el_runner_lo_cablea_con_el_callback_del_servicio() -> None:
    arbol = ast.parse(pathlib.Path(modulo_servicio.__file__).read_text(encoding="utf-8"))
    construcciones = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "DynDOLODRunner"
    ]

    assert len(construcciones) == 1, "cambió la familia de sitios que construyen el runner: decidir su cableado"
    cableado = {
        k.arg: ast.unparse(k.value) for k in construcciones[0].keywords if k.arg is not None and k.arg == "on_progress"
    }
    assert cableado == {"on_progress": "self._al_progresar"}
