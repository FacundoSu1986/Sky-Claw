"""Una etapa 9 a la vez, para la GUI y el agente (seguimiento de #1007).

La propiedad del mecanismo: *el estado vivo de una corrida (paso, corte, progreso) solo lo escribe y lo descarta su
dueña, y hay UNA dueña a la vez para TODAS las superficies*. Antes, la GUI (single-flight del dispatcher) y el agente
(``GestorDeTrabajosDeLods._actual``) tenían cada una su control, y ninguna veía a la otra: una segunda corrida
limpiaba el paso y el corte de la primera al arrancar y otra vez al salir (review CodeRabbit y PR-Agent en #1007).

El turno lo toma el servicio —por donde pasan las dos superficies— en el límite de ``execute``. Las superficies lo
consultan antes de preguntar o lanzar (UX: no se le pide aprobar algo que se va a rechazar), pero la autoridad sigue
siendo el servicio, que también rechaza a quien llegue sin haber consultado.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools.lod_tools import GestorDeTrabajosDeLods
from sky_claw.app.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy
from sky_claw.local.tools import dyndolod_guia as guia
from sky_claw.local.tools.dyndolod_guia import CambioDeFase, FaseAsistida
from sky_claw.local.tools.dyndolod_runner import DynDOLODExecutionError
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.validators.etapas_previas_lods import EvidenciaDeEtapasPrevias

SKY_CLAW = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"


class _PreflightQueEspera:
    """La corrida dueña avanza hasta «esperando tu aprobación» y se queda ahí hasta que el test la suelte."""

    def __init__(self) -> None:
        self.servicio: DynDOLODPipelineService | None = None
        self.en_espera = asyncio.Event()
        self.soltar = asyncio.Event()
        self.corridas = 0

    async def run(self) -> MagicMock:
        assert self.servicio is not None
        self.corridas += 1
        await self.servicio._al_cambiar_de_fase(  # noqa: SLF001
            CambioDeFase(tool_name="TexGen", fase=FaseAsistida.ESPERANDO_TU_APROBACION)
        )
        self.en_espera.set()
        await self.soltar.wait()
        # Semáforo rojo: la corrida termina sin runner (el test mide el turno, no el pipeline).
        return MagicMock(blocks_mutations=True, checks=[])


_PREFLIGHTS_VIVOS: list[_PreflightQueEspera] = []


@pytest.fixture(autouse=True)
async def _soltar_toda_corrida_al_terminar():
    """Un test que falla a mitad no deja una corrida dueña esperando para siempre (colgaría la suite)."""
    yield
    for preflight in _PREFLIGHTS_VIVOS:
        preflight.soltar.set()
    _PREFLIGHTS_VIVOS.clear()
    await asyncio.sleep(0)


def _servicio_ocupable(monkeypatch: pytest.MonkeyPatch) -> tuple[DynDOLODPipelineService, _PreflightQueEspera]:
    preflight = _PreflightQueEspera()
    _PREFLIGHTS_VIVOS.append(preflight)
    servicio = DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=AsyncMock(),
        mo2_profile="Default",
        preflight=preflight,  # type: ignore[arg-type]
    )
    preflight.servicio = servicio
    monkeypatch.setattr(servicio, "verificar_checklist", AsyncMock(return_value=MagicMock(listo=True, avisos=list)))
    monkeypatch.setattr(
        servicio, "verificar_etapas_previas", MagicMock(return_value=EvidenciaDeEtapasPrevias(faltantes=()))
    )
    return servicio, preflight


# ---------------------------------------------------------------------------
# Lanzadores: cada superficie, con el mismo servicio real
# ---------------------------------------------------------------------------


async def _lanzar_desde_la_gui(servicio: DynDOLODPipelineService) -> dict[str, Any]:
    return await GenerateLodsStrategy(servicio).execute({"interaction_mode": "asistido"})


async def _lanzar_desde_el_agente(servicio: DynDOLODPipelineService) -> dict[str, Any]:
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio)
    respuesta = await gestor.iniciar(
        preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="asistido"
    )
    if gestor.actual is not None and gestor.actual.tarea is not None:
        await gestor.actual.tarea
        return gestor.actual.resultado or respuesta
    return respuesta


#: La familia de superficies que lanzan la etapa 9 (la misma que congela ``test_dyndolod_guia_superficies``).
LANZADORES = {"gui": _lanzar_desde_la_gui, "agente": _lanzar_desde_el_agente}


@pytest.mark.parametrize("segunda", sorted(LANZADORES))
@pytest.mark.parametrize("primera", sorted(LANZADORES))
async def test_con_una_etapa9_en_curso_ninguna_superficie_lanza_otra_ni_le_pisa_el_paso(
    primera: str, segunda: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    servicio, preflight = _servicio_ocupable(monkeypatch)
    dueña = asyncio.create_task(LANZADORES[primera](servicio))
    await asyncio.wait_for(preflight.en_espera.wait(), timeout=5)
    paso_de_la_dueña = servicio.fase_actual

    rechazo = await asyncio.wait_for(LANZADORES[segunda](servicio), timeout=5)

    assert rechazo["success"] is False
    assert rechazo["reason"] == guia.RAZON_ETAPA_EN_CURSO
    assert rechazo["message"], "el contrato success/message sigue: el técnico va en message"
    assert rechazo["guia"]["titulo"] == "Ya hay una etapa 9 corriendo."
    assert servicio.fase_actual is paso_de_la_dueña, "la segunda corrida pisó el paso de la dueña"
    assert preflight.corridas == 1, "la segunda corrida llegó a correr"

    preflight.soltar.set()
    await asyncio.wait_for(dueña, timeout=5)
    assert servicio.fase_actual is None, "al salir, la dueña descarta su propio paso"


async def test_el_servicio_rechaza_a_quien_llega_sin_consultar_y_no_le_pisa_el_paso_ni_el_corte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La autoridad es el servicio: una llamada directa a ``execute`` que no consultó antes también se rechaza."""
    servicio, preflight = _servicio_ocupable(monkeypatch)
    dueña = asyncio.create_task(servicio.execute(interaction_mode="asistido"))
    await asyncio.wait_for(preflight.en_espera.wait(), timeout=5)
    await servicio._al_cambiar_de_fase(  # noqa: SLF001
        CambioDeFase(tool_name="TexGen", fase=FaseAsistida.CORTADA)
    )
    paso, corte = servicio.fase_actual, servicio._corte_de_la_corrida  # noqa: SLF001

    rechazo = await asyncio.wait_for(servicio.execute(interaction_mode="desatendido"), timeout=5)

    assert rechazo["reason"] == guia.RAZON_ETAPA_EN_CURSO
    assert rechazo["interaction_mode"] == "desatendido", "el rechazo declara el modo de quien llamó"
    assert servicio.fase_actual is paso
    assert servicio._corte_de_la_corrida is corte  # noqa: SLF001
    preflight.soltar.set()
    await asyncio.wait_for(dueña, timeout=5)


async def test_el_turno_se_libera_en_toda_salida_de_la_dueña(monkeypatch: pytest.MonkeyPatch) -> None:
    servicio, preflight = _servicio_ocupable(monkeypatch)
    dueña = asyncio.create_task(servicio.execute(interaction_mode="asistido"))
    await asyncio.wait_for(preflight.en_espera.wait(), timeout=5)
    dueña.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dueña

    assert servicio.rechazo_si_hay_corrida_en_curso(interaction_mode="asistido") is None
    preflight.en_espera.clear()
    preflight.soltar.set()
    resultado = await asyncio.wait_for(servicio.execute(interaction_mode="asistido"), timeout=5)
    assert resultado.get("reason") != guia.RAZON_ETAPA_EN_CURSO, "una cancelación dejó el turno tomado"


async def test_el_preview_no_toma_el_turno_ni_lo_respeta(monkeypatch: pytest.MonkeyPatch) -> None:
    """El ``dry_run`` es de solo lectura: no ocupa la etapa y no se rechaza por una corrida en curso."""
    servicio, preflight = _servicio_ocupable(monkeypatch)
    monkeypatch.setattr(servicio, "_preview", AsyncMock(return_value={"success": True, "status": "dry_run_preview"}))
    dueña = asyncio.create_task(servicio.execute(interaction_mode="asistido"))
    await asyncio.wait_for(preflight.en_espera.wait(), timeout=5)

    preview = await servicio.execute(dry_run=True)

    assert preview["status"] == "dry_run_preview"
    preflight.soltar.set()
    await asyncio.wait_for(dueña, timeout=5)


async def test_la_gui_no_pide_aprobar_una_etapa9_mientras_otra_corre(monkeypatch: pytest.MonkeyPatch) -> None:
    servicio, preflight = _servicio_ocupable(monkeypatch)
    dueña = asyncio.create_task(servicio.execute(interaction_mode="asistido"))
    await asyncio.wait_for(preflight.en_espera.wait(), timeout=5)

    with pytest.raises(DynDOLODExecutionError, match="Ya hay una etapa 9 corriendo"):
        await GenerateLodsStrategy(servicio).prepare_for_approval({"interaction_mode": "asistido"})

    preflight.soltar.set()
    await asyncio.wait_for(dueña, timeout=5)


async def test_la_consulta_temprana_no_inventa_un_rechazo_si_el_servicio_no_la_ofrece() -> None:
    """UX, no autoridad: un servicio sin la consulta (o un doble) no frena al lanzador; ``execute`` decide."""
    assert guia.rechazo_temprano_por_corrida_en_curso(object(), interaction_mode="asistido") is None
    assert guia.rechazo_temprano_por_corrida_en_curso(MagicMock(), interaction_mode="asistido") is None
    roto = MagicMock()
    roto.rechazo_si_hay_corrida_en_curso.side_effect = RuntimeError("roto")
    assert guia.rechazo_temprano_por_corrida_en_curso(roto, interaction_mode="asistido") is None


# ---------------------------------------------------------------------------
# Anclas AST: todo lanzador consulta el turno antes de preguntar o lanzar
# ---------------------------------------------------------------------------


def _funcion(arbol: ast.AST, clase: str, metodo: str) -> ast.AST:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in nodo.body:
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)) and hijo.name == metodo:
                    return hijo
    raise AssertionError(f"no encontré {clase}.{metodo}")


def _primera_linea_de_llamada(arbol: ast.AST, nombre: str) -> int:
    lineas = [
        nodo.lineno
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and (
            (isinstance(nodo.func, ast.Name) and nodo.func.id == nombre)
            or (isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre)
        )
    ]
    assert lineas, f"no hay llamada a {nombre}"
    return min(lineas)


#: Por cada lanzador de la familia, qué tiene que venir DESPUÉS de consultar el turno.
CONSULTA_ANTES_DE = {
    ("app/orchestrator/tool_strategies/generate_lods.py", "GenerateLodsStrategy", "prepare_for_approval"): (
        "checklist_temprano",
        "verificar_backend_listo",
    ),
    ("app/orchestrator/tool_strategies/generate_lods.py", "GenerateLodsStrategy", "execute"): (
        "checklist_temprano",
        "_guard_de_etapas_previas_del_asistido",
        "etapas_previas_faltantes",
    ),
    ("app/agent/tools/lod_tools.py", "GestorDeTrabajosDeLods", "iniciar"): (
        "checklist_temprano",
        "_etapas_faltantes",
        "create_task",
    ),
}


@pytest.mark.parametrize(("ubicacion", "despues"), sorted(CONSULTA_ANTES_DE.items()))
def test_cada_lanzador_consulta_el_turno_antes_de_preguntar_o_lanzar(
    ubicacion: tuple[str, str, str], despues: tuple[str, ...]
) -> None:
    archivo, clase, metodo = ubicacion
    funcion = _funcion(ast.parse((SKY_CLAW / archivo).read_text("utf-8")), clase, metodo)
    linea = _primera_linea_de_llamada(funcion, "rechazo_temprano_por_corrida_en_curso")
    for nombre in despues:
        assert linea < _primera_linea_de_llamada(funcion, nombre), (
            f"{clase}.{metodo} consulta el turno después de {nombre}"
        )


def test_los_lanzadores_consultados_son_toda_la_familia() -> None:
    """Si la familia de lanzadores cambia, este censo también: un lanzador nuevo necesita su receta acá."""
    from tests.test_dyndolod_guia_superficies import LANZADORES_DE_LA_ETAPA9

    assert {f"sky_claw/{archivo}" for archivo, _, _ in CONSULTA_ANTES_DE} == LANZADORES_DE_LA_ETAPA9
