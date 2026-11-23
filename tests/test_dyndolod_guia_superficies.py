"""#997 — ancla (b): el checklist de la etapa 9 solo protege si TODAS las superficies lo consultan.

La propiedad del mecanismo: *el checklist vive en el único camino que llega al runner*
(``DynDOLODPipelineService.execute``), así que toda superficie que lance la etapa 9 participa por construcción; y
cada lanzador lo consulta ANTES de preguntarle algo al operador o de lanzar un trabajo, para no pedir una
aprobación que el servicio va a refutar.

Las superficies se ENUMERAN por AST (no se escriben a mano): la familia de lanzadores se reconoce por la huella de
los guards hermanos que ya existen (``etapas_previas_faltantes`` / ``verificar_backend_listo``). Un lanzador nuevo
que copie esos guards sin el checklist rompe acá.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools.lod_tools import GestorDeTrabajosDeLods, TrabajoDeLods
from sky_claw.app.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy
from sky_claw.local.tools import dyndolod_guia as guia
from sky_claw.local.tools.dyndolod_guia import ChecklistDeEtapa9, Estado, IdPrecondicion
from sky_claw.local.tools.dyndolod_runner import DynDOLODExecutionError, PreflightDeBackend
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion
from sky_claw.local.validators.etapas_previas_lods import EvidenciaDeEtapasPrevias

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SKY_CLAW = RAIZ / "sky_claw"


def _bloqueado() -> ChecklistDeEtapa9:
    return ChecklistDeEtapa9(
        resultados=(
            guia.resultado(IdPrecondicion.HERRAMIENTAS_FUERA_DEL_JUEGO, Estado.FALLA, detalle=r"C:\Skyrim\DynDOLOD"),
            guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.OK),
        )
    )


def _con_aviso() -> ChecklistDeEtapa9:
    return ChecklistDeEtapa9(
        resultados=(guia.resultado(IdPrecondicion.DLL_NG_Y_MASTERS, Estado.FALLA, detalle="DynDOLOD.dll en Data"),)
    )


def _servicio_falso(checklist: ChecklistDeEtapa9, *, faltantes: tuple[str, ...] = ()) -> MagicMock:
    servicio = MagicMock()
    servicio.execute = AsyncMock(return_value={"success": True, "message": ""})
    servicio.verificar_backend_listo = AsyncMock(return_value=PreflightDeBackend.ok())
    servicio.verificar_etapas_previas = MagicMock(return_value=EvidenciaDeEtapasPrevias(faltantes=faltantes))
    servicio.verificar_checklist = AsyncMock(return_value=checklist)
    servicio.progreso_actual = None
    servicio.fase_actual = None
    return servicio


class _Aprobador:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, ...]] = []

    async def __call__(self, faltantes: tuple[str, ...]) -> ResultadoConfirmacion:
        self.llamadas.append(faltantes)
        return ResultadoConfirmacion.APROBADA


# ---------------------------------------------------------------------------
# Paridad dinámica: con un ❌, ninguna superficie lanza ni pregunta, y las dos devuelven el MISMO checklist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"interaction_mode": "asistido"}, {"run_texgen": False}])
async def test_la_gui_no_pide_aprobar_una_corrida_que_el_checklist_va_a_cortar(payload: dict) -> None:
    servicio = _servicio_falso(_bloqueado())

    with pytest.raises(DynDOLODExecutionError) as excinfo:
        await GenerateLodsStrategy(servicio).prepare_for_approval(payload)

    assert "carpeta del juego" in str(excinfo.value), "el corte temprano llega en criollo al cartel"
    servicio.execute.assert_not_awaited()
    servicio.verificar_checklist.assert_awaited_once_with(run_texgen=payload.get("run_texgen", True))


async def test_la_gui_no_lanza_ni_pregunta_etapas_previas_con_un_bloqueante() -> None:
    servicio = _servicio_falso(_bloqueado(), faltantes=("etapa 6: falta",))
    aprobador = _Aprobador()

    resultado = await GenerateLodsStrategy(servicio, aprobar_sin_etapas_previas=aprobador).execute({})

    servicio.execute.assert_not_awaited()
    assert aprobador.llamadas == [], "no se pregunta por las etapas previas si la corrida igual va a cortar"
    assert resultado["success"] is False
    assert resultado["checklist"] == _bloqueado().to_dict()
    assert resultado["guia"]["titulo"]
    assert resultado["interaction_mode"] == "asistido"


async def test_el_agente_no_lanza_ni_pregunta_con_un_bloqueante() -> None:
    servicio = _servicio_falso(_bloqueado(), faltantes=("etapa 6: falta",))
    aprobador = _Aprobador()
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio, aprobar_sin_etapas_previas=aprobador)

    respuesta = await gestor.iniciar(
        preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="asistido"
    )
    await asyncio.sleep(0)

    assert respuesta["success"] is False
    assert respuesta["checklist"] == _bloqueado().to_dict(), "la GUI y el agente leen el MISMO checklist"
    assert respuesta["guia"]["titulo"]
    assert gestor.actual is None, "no se ocupó el lugar del trabajo"
    servicio.execute.assert_not_awaited()
    assert aprobador.llamadas == []


async def test_las_dos_superficies_devuelven_el_mismo_checklist_ante_el_mismo_bloqueante() -> None:
    servicio_gui = _servicio_falso(_bloqueado())
    servicio_agente = _servicio_falso(_bloqueado())
    de_la_gui = await GenerateLodsStrategy(servicio_gui).execute({})
    del_agente = await GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio_agente).iniciar(
        preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="asistido"
    )
    assert de_la_gui["checklist"] == del_agente["checklist"]
    assert de_la_gui["guia"] == del_agente["guia"]


async def test_el_agente_lanza_con_avisos_y_se_los_cuenta_en_criollo() -> None:
    servicio = _servicio_falso(_con_aviso())
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio)

    respuesta = await gestor.iniciar(
        preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="asistido"
    )
    assert gestor.actual is not None
    await gestor.actual.tarea  # type: ignore[misc]

    assert respuesta["success"] is True
    assert respuesta["avisos"] and "DLL NG" in respuesta["avisos"][0]
    servicio.execute.assert_awaited_once()


def test_el_resumen_del_trabajo_conserva_la_guia_el_checklist_y_el_preflight() -> None:
    """Hoy el agente tiraba ``preflight``: el mismo agujero por el que se perdería la guía."""
    trabajo = TrabajoDeLods(id="lods-1", parametros={}, iniciado_en=0.0, estado="fallido")
    trabajo.resultado = {
        "success": False,
        "message": "técnico",
        "guia": {"titulo": "t", "arreglo": "a", "detalle_tecnico": "técnico"},
        "checklist": {"listo": False, "items": []},
        "preflight": {"status": "red"},
    }
    resumen = trabajo.resumen()["resultado"]
    assert resumen["guia"]["titulo"] == "t"
    assert resumen["checklist"] == {"listo": False, "items": []}
    assert resumen["preflight"] == {"status": "red"}


def test_el_aviso_de_fin_por_telegram_pone_el_criollo_primero() -> None:
    from sky_claw.app.agent.tools.lod_tools import resumen_para_el_operador

    trabajo = TrabajoDeLods(id="lods-1", parametros={"interaction_mode": "asistido"}, iniciado_en=0.0, estado="fallido")
    trabajo.terminado_en = 60.0
    trabajo.resultado = {
        "success": False,
        "message": "protocolo de readiness de TexGen cortó la corrida: denegada",
        "guia": {"titulo": "Dijiste que no.", "arreglo": "Volvé a correr.", "detalle_tecnico": "técnico"},
    }
    texto = resumen_para_el_operador(trabajo)
    assert texto.index("Dijiste que no.") < texto.index("protocolo de readiness")


# ---------------------------------------------------------------------------
# La autoridad: el servicio corta ANTES de tocar nada
# ---------------------------------------------------------------------------


def _servicio_real() -> DynDOLODPipelineService:
    return DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=AsyncMock(),
        mo2_profile="Default",
    )


async def test_el_servicio_corta_con_un_bloqueante_sin_preflight_ni_eventos_ni_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servicio = _servicio_real()
    monkeypatch.setattr(servicio, "verificar_checklist", AsyncMock(return_value=_bloqueado()))
    preflight = MagicMock(side_effect=AssertionError("el preflight no corre con un bloqueante"))
    monkeypatch.setattr(servicio, "_ensure_preflight", preflight)
    monkeypatch.setattr(servicio, "_ensure_runner", MagicMock(side_effect=AssertionError("ni el runner")))

    resultado = await servicio.execute(interaction_mode="asistido")

    assert resultado["success"] is False
    assert resultado["reason"] == "ChecklistBloqueado"
    assert resultado["message"], "el contrato success/message sigue: el técnico va en message"
    assert resultado["checklist"] == _bloqueado().to_dict()
    assert "carpeta del juego" in resultado["guia"]["titulo"]
    assert resultado["interaction_mode"] == "asistido"
    assert resultado["assisted"] is True
    servicio._event_bus.publish.assert_not_awaited()  # noqa: SLF001


async def test_el_servicio_sigue_al_preflight_si_solo_hay_avisos(monkeypatch: pytest.MonkeyPatch) -> None:
    servicio = _servicio_real()
    monkeypatch.setattr(servicio, "verificar_checklist", AsyncMock(return_value=_con_aviso()))
    llegó = MagicMock(return_value=None)
    monkeypatch.setattr(servicio, "_ensure_preflight", llegó)
    monkeypatch.setattr(servicio, "_ensure_runner", MagicMock(side_effect=DynDOLODExecutionError("corte")))

    await servicio.execute(interaction_mode="asistido")

    llegó.assert_called_once()


@pytest.mark.parametrize("run_texgen", [True, False])
async def test_el_servicio_consulta_el_checklist_con_el_run_texgen_de_la_corrida(
    run_texgen: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    servicio = _servicio_real()
    checklist = AsyncMock(return_value=_bloqueado())
    monkeypatch.setattr(servicio, "verificar_checklist", checklist)
    await servicio.execute(run_texgen=run_texgen)
    checklist.assert_awaited_once_with(run_texgen=run_texgen)


async def test_el_preview_no_corre_el_checklist(monkeypatch: pytest.MonkeyPatch) -> None:
    servicio = _servicio_real()
    checklist = AsyncMock(return_value=_bloqueado())
    monkeypatch.setattr(servicio, "verificar_checklist", checklist)
    monkeypatch.setattr(servicio, "_preview", AsyncMock(return_value={"success": True, "message": ""}))
    await servicio.execute(dry_run=True)
    checklist.assert_not_awaited()


# ---------------------------------------------------------------------------
# Anclas AST
# ---------------------------------------------------------------------------


def _modulos_de_produccion() -> list[tuple[str, ast.Module]]:
    return [
        (ruta.relative_to(RAIZ).as_posix(), ast.parse(ruta.read_text(encoding="utf-8", errors="replace")))
        for ruta in sorted(SKY_CLAW.rglob("*.py"))
    ]


def _llama(arbol: ast.AST, nombre: str) -> bool:
    return any(
        isinstance(nodo, ast.Call)
        and (
            (isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre)
            or (isinstance(nodo.func, ast.Name) and nodo.func.id == nombre)
        )
        for nodo in ast.walk(arbol)
    )


def _primera_linea_de_llamada(arbol: ast.AST, nombre: str) -> int:
    lineas = [
        nodo.lineno
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and (
            (isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre)
            or (isinstance(nodo.func, ast.Name) and nodo.func.id == nombre)
        )
    ]
    assert lineas, f"no hay llamada a {nombre}"
    return min(lineas)


def _funcion(arbol: ast.AST, clase: str, metodo: str) -> ast.AST:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in nodo.body:
                if isinstance(hijo, ast.FunctionDef | ast.AsyncFunctionDef) and hijo.name == metodo:
                    return hijo
    raise AssertionError(f"no está {clase}.{metodo}")


#: Los lanzadores de la etapa 9: los módulos que ya consultan los guards previos al lanzamiento. Detectados por AST.
LANZADORES_DE_LA_ETAPA9 = {
    "sky_claw/app/agent/tools/lod_tools.py",
    "sky_claw/app/orchestrator/tool_strategies/generate_lods.py",
}

#: Aguas abajo de la compuerta: el servicio (que ES la compuerta) y el runner (que tiene su propio
#: ``verificar_backend_listo`` para el desatendido). No es un recorte por prosa: que al runner solo se llega por
#: ``execute`` lo verifica ``test_al_runner_solo_se_llega_por_el_servicio``.
AGUAS_ABAJO_DE_LA_COMPUERTA = {
    "sky_claw/local/tools/dyndolod_service.py",
    "sky_claw/local/tools/dyndolod_runner.py",
}


def test_la_familia_de_lanzadores_esta_congelada_y_todos_consultan_el_checklist() -> None:
    lanzadores = {
        relativa
        for relativa, arbol in _modulos_de_produccion()
        if relativa not in AGUAS_ABAJO_DE_LA_COMPUERTA
        and (_llama(arbol, "etapas_previas_faltantes") or _llama(arbol, "verificar_backend_listo"))
    }
    assert lanzadores == LANZADORES_DE_LA_ETAPA9, "cambió la familia de lanzadores: decidí su checklist"
    sin_checklist = {
        relativa
        for relativa, arbol in _modulos_de_produccion()
        if relativa in lanzadores and not _llama(arbol, "checklist_temprano")
    }
    assert sin_checklist == set()


def test_cada_lanzador_consulta_el_checklist_antes_de_preguntar_o_lanzar() -> None:
    estrategia = ast.parse((SKY_CLAW / "app/orchestrator/tool_strategies/generate_lods.py").read_text("utf-8"))
    prepare = _funcion(estrategia, "GenerateLodsStrategy", "prepare_for_approval")
    assert _primera_linea_de_llamada(prepare, "checklist_temprano") < _primera_linea_de_llamada(
        prepare, "verificar_backend_listo"
    ), "la GUI preguntaría tool_execution antes del checklist"
    execute = _funcion(estrategia, "GenerateLodsStrategy", "execute")
    assert _primera_linea_de_llamada(execute, "checklist_temprano") < _primera_linea_de_llamada(
        execute, "_guard_de_etapas_previas_del_asistido"
    )
    assert _primera_linea_de_llamada(execute, "checklist_temprano") < _primera_linea_de_llamada(
        execute, "etapas_previas_faltantes"
    )

    agente = ast.parse((SKY_CLAW / "app/agent/tools/lod_tools.py").read_text("utf-8"))
    iniciar = _funcion(agente, "GestorDeTrabajosDeLods", "iniciar")
    linea = _primera_linea_de_llamada(iniciar, "checklist_temprano")
    assert linea < _primera_linea_de_llamada(iniciar, "_etapas_faltantes")
    assert linea < _primera_linea_de_llamada(iniciar, "create_task")


async def test_la_consulta_temprana_no_inventa_un_bloqueo_si_el_servicio_no_la_ofrece() -> None:
    """UX, no autoridad: sin checklist del servicio, el lanzador sigue y ``execute`` decide."""
    assert await guia.checklist_temprano(object(), run_texgen=True) is None
    roto = MagicMock()
    roto.verificar_checklist = AsyncMock(side_effect=RuntimeError("disco caído"))
    assert await guia.checklist_temprano(roto, run_texgen=True) is None
    cancelado = MagicMock()
    cancelado.verificar_checklist = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await guia.checklist_temprano(cancelado, run_texgen=True)


def test_el_servicio_consulta_el_checklist_antes_de_cualquier_efecto() -> None:
    servicio = ast.parse((SKY_CLAW / "local/tools/dyndolod_service.py").read_text("utf-8"))
    execute = _funcion(servicio, "DynDOLODPipelineService", "execute")
    linea = _primera_linea_de_llamada(execute, "verificar_checklist")
    for efecto in ("_ensure_preflight", "_publish_started", "_ensure_runner", "run_full_pipeline"):
        assert linea < _primera_linea_de_llamada(execute, efecto), f"el checklist corre después de {efecto}"


def test_al_runner_solo_se_llega_por_el_servicio() -> None:
    """Si alguien llamara al runner por fuera de ``execute``, ese camino no tendría checklist."""
    llamadores: dict[str, set[str]] = {}
    for relativa, arbol in _modulos_de_produccion():
        for nombre in ("run_full_pipeline", "run_texgen", "run_dyndolod"):
            if _llama(arbol, nombre):
                llamadores.setdefault(nombre, set()).add(relativa)
    assert llamadores == {
        "run_full_pipeline": {"sky_claw/local/tools/dyndolod_service.py"},
        "run_texgen": {"sky_claw/local/tools/dyndolod_runner.py"},
        "run_dyndolod": {"sky_claw/local/tools/dyndolod_runner.py"},
    }
    servicio = ast.parse((SKY_CLAW / "local/tools/dyndolod_service.py").read_text("utf-8"))
    metodos_que_llaman = {
        nodo.name
        for nodo in ast.walk(servicio)
        if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef) and _llama(nodo, "run_full_pipeline")
    }
    assert metodos_que_llaman == {"execute"}
