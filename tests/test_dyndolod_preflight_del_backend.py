"""B-3 (H-6): al operador no se le pregunta por una corrida que el backend no puede servir.

Antes, la etapa 9 desatendida le pedía al operador remoto que aprobara la configuración y recién en el ``spawn``
descubría que el bridge de MO2 no estaba conectado (o que el bundle instalado era viejo, #934): el operador
aprobaba una corrida condenada. Con el backend brokered y la barrera de contención puesta (``WIRING_SECURITY_HOLD``)
pasaba SIEMPRE.

La propiedad del mecanismo: **ninguna pregunta al operador antes del ``spawn`` se hace sin haber probado, justo antes,
que el backend puede servir la corrida.** Hay tres sitios que preguntan antes del ``spawn``, en dos superficies:

* el runner (``preparar_interaccion_desatendida``): la aprobación remota de la configuración;
* el guard de etapas previas del modo ASISTIDO de la GUI (``GenerateLodsStrategy``);
* el mismo guard del agente remoto (``GestorDeTrabajosDeLods``).

El cuarto sitio —``aprobar_modal`` del driver UIA— corre con el proceso YA vivo (su ``pid``): el ``spawn`` ya ocurrió,
así que el backend estuvo listo por construcción. Las anclas de abajo lo verifican por estructura, no por prosa.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import types
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

import sky_claw.local.tools.dyndolod_runner as ddl
from sky_claw.app.agent.tools.lod_tools import GestorDeTrabajosDeLods
from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from sky_claw.app.orchestrator.tool_strategies.base import ApprovalPreparer
from sky_claw.app.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy
from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware
from sky_claw.app.security.hitl import Decision
from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODSpawnStrategy, VfsRequiredDynDOLODSpawnStrategy
from sky_claw.local.mo2.vfs_broker import VfsBrokerError, VfsExecutionBroker
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODPipelineResult,
    DynDOLODRunner,
    InteraccionDeCorrida,
    PreflightDeBackend,
    ReadinessMode,
    StandaloneDynDOLODSpawnStrategy,
    preflight_del_backend,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.dyndolod_uia_contrato import CodigoDesatendido, ModoDeInteraccion
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion
from sky_claw.local.tools.output_targets import derivar_layout_de_dyndolod
from sky_claw.local.validators.etapas_previas_lods import EvidenciaDeEtapasPrevias
from tests._dyndolod_gui_simulada import CONTRATO_DYNDOLOD, CONTRATO_TEXGEN
from tests._orchestration_dispatcher_dependencies import crear_dependencias_dispatcher
from tests.test_dyndolod_desatendido_integracion import AprobadorFalso, _capacidad, _instalacion

RAIZ = pathlib.Path(__file__).resolve().parents[1]
RAIZ_WINDOWS = pathlib.Path(r"E:\Sky-Claw T5 Rig")
MOTIVO_BRIDGE = "MO2 bridge no se conectó al broker: reinstalalo con `--mode install-vfs-bridge` y reabrí MO2"


# =============================================================================
# Dobles
# =============================================================================


class _EstrategiaFalsa:
    """Una strategy mínima del Protocol: lo único que varía es el veredicto del preflight."""

    def __init__(self, veredicto: PreflightDeBackend) -> None:
        self._veredicto = veredicto
        self.preflights = 0

    @property
    def data_visibility_domain(self) -> str:
        return "physical"

    async def spawn(self, **_kwargs: Any) -> Any:
        raise AssertionError("el preflight no puede spawnear nada")

    async def verify_texgen_handoff(self, _request: Any) -> Any:
        raise AssertionError("el preflight no verifica el handoff")

    async def preflight_backend(self) -> PreflightDeBackend:
        self.preflights += 1
        return self._veredicto


class _BrokerFalso:
    """Lo único que el preflight puede pedirle al broker es esperar al bridge: nunca abrir una sesión."""

    def __init__(self, error: BaseException | None = None, *, quarantine_reason: str | None = None) -> None:
        self._error = error
        self._quarantine_reason = quarantine_reason
        self.esperas = 0

    @property
    def quarantine_reason(self) -> str | None:
        return self._quarantine_reason

    async def wait_until_ready(self, **_kwargs: Any) -> None:
        self.esperas += 1
        if self._error is not None:
            raise self._error

    async def open_session(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("el preflight no abre sesiones")

    async def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("el preflight no envía jobs")


def _brokered(tmp_path: pathlib.Path, broker: _BrokerFalso) -> BrokeredDynDOLODSpawnStrategy:
    return BrokeredDynDOLODSpawnStrategy(
        broker=broker,
        instance_id="instancia",
        profile="Default",
        data_root=tmp_path,
        mods_dir=tmp_path,
        install_root=tmp_path,
        physical_data_dir=tmp_path,
        virtual_data_dir=tmp_path,
        output_roots={},
    )


@pytest.fixture
def sha_de_alpha209(monkeypatch):
    """El binario de test no es Alpha-209: el hash se fija por nombre (el contrato sí es el real)."""
    por_nombre = {"TexGenx64.exe": CONTRATO_TEXGEN.exe_sha256, "DynDOLODx64.exe": CONTRATO_DYNDOLOD.exe_sha256}

    def _sha(ruta: pathlib.Path) -> str:
        for nombre, sha in por_nombre.items():
            if str(ruta).endswith(nombre):
                return sha
        return "0" * 64

    monkeypatch.setattr(ddl, "_sha256_de_archivo", _sha)
    return por_nombre


def _runner_con(instalacion, capacidad, estrategia) -> DynDOLODRunner:
    config = MagicMock()
    config.timeout_seconds = 60
    config.heartbeat_interval = 60
    config.fence_ownership = None
    config.output_layout = derivar_layout_de_dyndolod(external_work_root=RAIZ_WINDOWS)
    config.external_work_root = None
    config.game_mode = "sse"
    config.texgen_exe = instalacion["texgen"]
    config.dyndolod_exe = instalacion["dyndolod"]
    return DynDOLODRunner(
        config, readiness=ReadinessMode.DISABLED_FOR_TEST, desatendido=capacidad, spawn_strategy=estrategia
    )


# =============================================================================
# El contrato: un código propio y un veredicto que siempre trae el motivo
# =============================================================================


def test_el_corte_por_backend_no_listo_tiene_su_propio_codigo_machine_readable():
    assert CodigoDesatendido.BACKEND_NOT_READY.value == "BACKEND_NOT_READY"
    assert CodigoDesatendido.BACKEND_NOT_READY.name == CodigoDesatendido.BACKEND_NOT_READY.value


def test_un_veredicto_listo_no_lleva_motivo_y_uno_bloqueado_siempre_lo_lleva():
    assert PreflightDeBackend.ok().listo is True
    bloqueado = PreflightDeBackend.bloqueado(MOTIVO_BRIDGE)
    assert bloqueado.listo is False and bloqueado.motivo == MOTIVO_BRIDGE
    with pytest.raises(ValueError):
        PreflightDeBackend.bloqueado("")  # el operador tiene que poder leer POR QUÉ no hay backend


# =============================================================================
# preflight_del_backend: el ÚNICO fail-closed de la capability
# =============================================================================


async def test_una_strategy_sin_la_capability_no_es_un_backend_listo():
    veredicto = await preflight_del_backend(types.SimpleNamespace())
    assert veredicto.listo is False
    assert "preflight_backend" in veredicto.motivo


@pytest.mark.parametrize("devuelto", [True, None, "listo", {"listo": True}])
async def test_un_veredicto_que_no_es_un_preflight_de_backend_no_pasa(devuelto):
    estrategia = types.SimpleNamespace(preflight_backend=AsyncMock(return_value=devuelto))
    veredicto = await preflight_del_backend(estrategia)
    assert veredicto.listo is False
    assert "PreflightDeBackend" in veredicto.motivo


async def test_una_excepcion_del_preflight_es_un_backend_no_listo_y_no_se_propaga():
    estrategia = types.SimpleNamespace(preflight_backend=AsyncMock(side_effect=RuntimeError("el broker explotó")))
    veredicto = await preflight_del_backend(estrategia)
    assert veredicto.listo is False
    assert "RuntimeError" in veredicto.motivo
    # el motivo viaja al operador: la excepción inesperada (rutas, detalles internos) NO va en él; vive en el log
    assert "el broker explotó" not in veredicto.motivo


async def test_la_causa_completa_de_la_excepcion_queda_en_el_log_local(caplog):
    """Lo que se le oculta al operador no se pierde: el log local conserva la causa y la traza."""
    estrategia = types.SimpleNamespace(preflight_backend=AsyncMock(side_effect=RuntimeError("detalle interno 42")))
    with caplog.at_level(logging.WARNING):
        await preflight_del_backend(estrategia)
    assert "detalle interno 42" in caplog.text
    assert any(r.exc_info for r in caplog.records)


async def test_la_cancelacion_del_preflight_se_propaga_y_no_se_traga():
    estrategia = types.SimpleNamespace(preflight_backend=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await preflight_del_backend(estrategia)


async def test_un_veredicto_listo_pasa_tal_cual():
    estrategia = _EstrategiaFalsa(PreflightDeBackend.ok())
    veredicto = await preflight_del_backend(estrategia)
    assert veredicto.listo is True and estrategia.preflights == 1


# =============================================================================
# Las tres strategies
# =============================================================================


async def test_standalone_esta_listo_sin_esperar_a_nadie():
    veredicto = await StandaloneDynDOLODSpawnStrategy().preflight_backend()
    assert veredicto == PreflightDeBackend.ok()


async def test_brokered_listo_si_el_bridge_esta_conectado(tmp_path):
    broker = _BrokerFalso()
    veredicto = await _brokered(tmp_path, broker).preflight_backend()
    assert veredicto == PreflightDeBackend.ok()
    assert broker.esperas == 1


async def test_brokered_no_listo_lleva_el_motivo_del_broker_tal_cual(tmp_path):
    """El motivo trae la instrucción de reinstalar del rechazo por bundle (#934): no se pierde por el camino."""
    broker = _BrokerFalso(VfsBrokerError(MOTIVO_BRIDGE))
    veredicto = await _brokered(tmp_path, broker).preflight_backend()
    assert veredicto.listo is False
    assert veredicto.motivo == MOTIVO_BRIDGE


async def test_brokered_con_un_error_inesperado_del_broker_tampoco_esta_listo(tmp_path):
    """Lo que no es un ``VfsBrokerError`` lo resuelve el fail-closed único, no esta strategy."""
    estrategia = _brokered(tmp_path, _BrokerFalso(OSError("socket cerrado")))
    veredicto = await preflight_del_backend(estrategia)
    assert veredicto.listo is False
    assert "OSError" in veredicto.motivo
    assert "socket cerrado" not in veredicto.motivo


async def test_brokered_en_cuarentena_no_esta_listo_aunque_el_bridge_este_conectado(tmp_path):
    """El broker rechaza ``open_session``/``submit`` en cuarentena (``_precondiciones``) aunque el bridge esté
    conectado: el preflight no puede responder «listo» mirando solo la conexión (Codex P2 en #950)."""
    broker = _BrokerFalso(quarantine_reason="terminalidad desconocida demostrada")
    veredicto = await _brokered(tmp_path, broker).preflight_backend()
    assert veredicto.listo is False
    assert "terminalidad desconocida demostrada" in veredicto.motivo
    assert "--mode vfs-quarantine" in veredicto.motivo, "el operador tiene que poder leer cómo liberarla"
    assert broker.esperas == 0, "la cuarentena es estado local: no se gasta el plazo del bridge para enterarse"


async def test_brokered_con_el_broker_real_en_cuarentena_no_esta_listo(tmp_path):
    """Con el broker REAL (no un doble): lo que ``quarantine_reason`` dice es lo que ``_precondiciones`` rechaza."""
    broker = VfsExecutionBroker(instance_id="inst-b3", state_dir=tmp_path / "estado")
    await broker.start()
    try:
        broker._quarantine_instance("job-1", "terminalidad desconocida demostrada")  # noqa: SLF001
        veredicto = await _brokered(tmp_path, broker).preflight_backend()  # type: ignore[arg-type]
    finally:
        await broker.close()
    assert veredicto.listo is False
    assert "terminalidad desconocida demostrada" in veredicto.motivo


async def test_el_guard_de_la_barrera_no_esta_listo_y_dice_por_que():
    razon = "la barrera de contención del worker no está levantada"
    veredicto = await VfsRequiredDynDOLODSpawnStrategy(razon).preflight_backend()
    assert veredicto.listo is False
    assert razon in veredicto.motivo


async def test_el_servicio_y_el_runner_responden_con_el_preflight_de_su_strategy():
    estrategia = _EstrategiaFalsa(PreflightDeBackend.bloqueado("MO2 cerrado"))
    huesped = types.SimpleNamespace(_spawn_strategy=estrategia)

    del_servicio = await DynDOLODPipelineService.verificar_backend_listo(huesped)  # type: ignore[arg-type]
    del_runner = await DynDOLODRunner.verificar_backend_listo(huesped)  # type: ignore[arg-type]

    assert del_servicio == del_runner == PreflightDeBackend.bloqueado("MO2 cerrado")
    assert estrategia.preflights == 2


# =============================================================================
# Sitio 1: el runner, antes de la aprobación remota de la configuración
# =============================================================================


async def test_con_el_backend_no_listo_el_operador_no_recibe_la_pregunta(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    aprobador = AprobadorFalso()
    estrategia = _EstrategiaFalsa(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE))
    runner = _runner_con(instalacion, _capacidad(None, tmp_path / "estado", aprobador), estrategia)

    resultado = await runner.preparar_interaccion_desatendida(run_texgen=True, preset="Medium")

    assert isinstance(resultado, DynDOLODPipelineResult)
    assert resultado.success is False
    assert resultado.codigos_desatendido == ("BACKEND_NOT_READY",)
    assert MOTIVO_BRIDGE in resultado.errors[0]
    assert aprobador.configuraciones == [], "no se le pregunta al operador por una corrida que no puede arrancar"
    assert estrategia.preflights == 1


async def test_un_preflight_que_lanza_deja_un_solo_registro_de_etapa_y_conserva_la_traza(
    tmp_path, sha_de_alpha209, caplog
):
    """SOP §5 regla 5 («log once»): el corte es el único registro de la etapa; el diagnóstico de la excepción del
    preflight lleva su propio ``operation_type`` y la traza, pero NO ``pipeline_stage`` (un conteo
    ``WHERE pipeline_stage=9`` no cuenta dos veces el mismo intento)."""
    estrategia = types.SimpleNamespace(
        data_visibility_domain="physical",
        spawn=AsyncMock(),
        verify_texgen_handoff=AsyncMock(),
        preflight_backend=AsyncMock(side_effect=RuntimeError("detalle interno 7")),
    )
    instalacion = _instalacion(tmp_path)
    runner = _runner_con(instalacion, _capacidad(None, tmp_path / "estado", AprobadorFalso()), estrategia)

    with caplog.at_level(logging.WARNING):
        resultado = await runner.preparar_interaccion_desatendida(run_texgen=True, preset="Medium")

    assert resultado.codigos_desatendido == ("BACKEND_NOT_READY",)
    de_etapa = [r for r in caplog.records if getattr(r, "pipeline_stage", None) == 9]
    assert len(de_etapa) == 1, [r.getMessage() for r in de_etapa]
    diagnostico = [
        r for r in caplog.records if getattr(r, "operation_type", None) == "dyndolod_preflight_backend_fallo"
    ]
    assert len(diagnostico) == 1 and diagnostico[0].exc_info, "la traza local no se pierde"
    assert not hasattr(diagnostico[0], "pipeline_stage")


async def test_con_el_backend_listo_el_operador_si_recibe_la_pregunta(tmp_path, sha_de_alpha209):
    """Control positivo: el hook no saltea la aprobación cuando el backend está bien."""
    instalacion = _instalacion(tmp_path)
    aprobador = AprobadorFalso()
    estrategia = _EstrategiaFalsa(PreflightDeBackend.ok())
    runner = _runner_con(instalacion, _capacidad(None, tmp_path / "estado", aprobador), estrategia)

    resultado = await runner.preparar_interaccion_desatendida(run_texgen=True, preset="Medium")

    assert isinstance(resultado, InteraccionDeCorrida)
    assert len(aprobador.configuraciones) == 1
    assert estrategia.preflights == 1


async def test_el_preflight_va_despues_de_los_chequeos_locales(tmp_path, sha_de_alpha209):
    """Un preset ausente corta por su propia causa y NO hace esperar al bridge: lo barato y determinista va primero."""
    instalacion = _instalacion(tmp_path)
    instalacion["preset_dyndolod"].unlink()
    estrategia = _EstrategiaFalsa(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE))
    runner = _runner_con(instalacion, _capacidad(None, tmp_path / "estado", AprobadorFalso()), estrategia)

    resultado = await runner.preparar_interaccion_desatendida(run_texgen=True, preset="Medium")

    assert resultado.codigos_desatendido == ("PRESET_ABSENT",)
    assert estrategia.preflights == 0


async def test_la_corrida_completa_no_spawnea_con_el_backend_no_listo(tmp_path, sha_de_alpha209):
    instalacion = _instalacion(tmp_path)
    aprobador = AprobadorFalso()
    estrategia = _EstrategiaFalsa(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE))
    runner = _runner_con(instalacion, _capacidad(None, tmp_path / "estado", aprobador), estrategia)

    with mock.patch.object(ddl.asyncio, "create_subprocess_exec", mock.AsyncMock()) as lanzar:
        resultado = await runner.run_full_pipeline(
            run_texgen=True, preset="Medium", modo_de_interaccion=ModoDeInteraccion.DESATENDIDO
        )

    assert resultado.codigos_desatendido == ("BACKEND_NOT_READY",)
    assert aprobador.configuraciones == []
    lanzar.assert_not_awaited()


async def test_el_modo_asistido_del_runner_no_consulta_el_preflight(tmp_path, sha_de_alpha209):
    """El runner no le pregunta nada al operador en ASISTIDO: ese guard es del sitio 2, no de acá."""
    estrategia = _EstrategiaFalsa(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE))
    runner = _runner_con(_instalacion(tmp_path), None, estrategia)

    interaccion = await runner._preparar_interaccion(  # noqa: SLF001 -- el seam bajo prueba ES privado
        ModoDeInteraccion.ASISTIDO, run_texgen=True, preset="Medium"
    )

    assert isinstance(interaccion, InteraccionDeCorrida)
    assert estrategia.preflights == 0


# =============================================================================
# Sitio 2: el guard de etapas previas del modo ASISTIDO de la GUI
# =============================================================================

_FALTA = EvidenciaDeEtapasPrevias(faltantes=("etapa 6: falta",))


class _AprobadorDeEtapas:
    """El canal humano al que se le pide seguir SIN las etapas previas (GUI o Telegram)."""

    def __init__(self, decision: ResultadoConfirmacion = ResultadoConfirmacion.APROBADA) -> None:
        self.decision = decision
        self.llamadas: list[tuple[str, ...]] = []

    async def __call__(self, faltantes: tuple[str, ...]) -> ResultadoConfirmacion:
        self.llamadas.append(faltantes)
        return self.decision


def _servicio_de_la_strategy(veredicto: PreflightDeBackend) -> MagicMock:
    servicio = MagicMock()
    servicio.execute = AsyncMock(return_value={"success": True, "message": ""})
    servicio.verificar_etapas_previas = MagicMock(return_value=_FALTA)
    servicio.verificar_backend_listo = AsyncMock(return_value=veredicto)
    return servicio


@pytest.mark.parametrize("payload", [{"preset": "Medium"}, {"preset": "Medium", "interaction_mode": "asistido"}])
async def test_el_guard_del_asistido_no_pregunta_si_el_backend_no_esta_listo(payload):
    servicio = _servicio_de_la_strategy(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE))
    aprobador = _AprobadorDeEtapas()

    resultado = await GenerateLodsStrategy(servicio, aprobar_sin_etapas_previas=aprobador).execute(payload)

    assert aprobador.llamadas == [], "no se le pregunta al operador de la GUI por una corrida que no puede arrancar"
    servicio.execute.assert_not_awaited()
    assert resultado["success"] is False
    assert MOTIVO_BRIDGE in resultado["message"]
    assert resultado["backend_no_listo"] == MOTIVO_BRIDGE
    assert resultado["etapas_faltantes"] == ["etapa 6: falta"]
    assert resultado["assisted"] is True and resultado["interaction_mode"] == "asistido"


async def test_el_guard_del_asistido_pregunta_si_el_backend_esta_listo():
    servicio = _servicio_de_la_strategy(PreflightDeBackend.ok())
    aprobador = _AprobadorDeEtapas()

    await GenerateLodsStrategy(servicio, aprobar_sin_etapas_previas=aprobador).execute({"preset": "Medium"})

    assert aprobador.llamadas == [("etapa 6: falta",)]
    servicio.execute.assert_awaited_once()


async def test_con_las_etapas_completas_el_asistido_no_consulta_el_preflight():
    """Sin pregunta no hay nada que proteger: el spawn mismo ya refuta un backend caído."""
    servicio = _servicio_de_la_strategy(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE))
    servicio.verificar_etapas_previas = MagicMock(return_value=EvidenciaDeEtapasPrevias(faltantes=()))

    await GenerateLodsStrategy(servicio).execute({"preset": "Medium"})

    servicio.verificar_backend_listo.assert_not_awaited()
    servicio.execute.assert_awaited_once()


# =============================================================================
# Sitio 3: el mismo guard, por el agente remoto (Telegram)
# =============================================================================


class _ServicioRemotoFalso:
    def __init__(self, veredicto: PreflightDeBackend) -> None:
        self._veredicto = veredicto
        self.preflights = 0
        self.llamadas: list[dict[str, Any]] = []

    def verificar_etapas_previas(self) -> EvidenciaDeEtapasPrevias:
        return _FALTA

    async def verificar_backend_listo(self) -> PreflightDeBackend:
        self.preflights += 1
        return self._veredicto

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.llamadas.append(kwargs)
        return {"success": True, "message": ""}


async def _lanzar_remoto(servicio: _ServicioRemotoFalso, aprobador: _AprobadorDeEtapas) -> GestorDeTrabajosDeLods:
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio, aprobar_sin_etapas_previas=aprobador)
    await gestor.iniciar(preset="Medium", run_texgen=True, create_snapshot=True, interaction_mode="desatendido")
    assert gestor.actual is not None and gestor.actual.tarea is not None
    await asyncio.wait_for(gestor.actual.tarea, timeout=5)
    return gestor


async def test_el_agente_remoto_no_pregunta_por_las_etapas_si_el_backend_no_esta_listo():
    servicio = _ServicioRemotoFalso(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE))
    aprobador = _AprobadorDeEtapas()

    gestor = await _lanzar_remoto(servicio, aprobador)

    assert aprobador.llamadas == [], "no se le pregunta al operador remoto por una corrida que no puede arrancar"
    assert servicio.llamadas == []
    assert gestor.actual is not None and gestor.actual.resultado is not None
    assert gestor.actual.resultado["success"] is False
    assert MOTIVO_BRIDGE in gestor.actual.resultado["message"]
    assert gestor.actual.resultado["backend_no_listo"] == MOTIVO_BRIDGE


async def test_el_agente_remoto_pregunta_y_lanza_si_el_backend_esta_listo():
    servicio = _ServicioRemotoFalso(PreflightDeBackend.ok())
    aprobador = _AprobadorDeEtapas()

    await _lanzar_remoto(servicio, aprobador)

    assert aprobador.llamadas == [("etapa 6: falta",)]
    assert len(servicio.llamadas) == 1


# =============================================================================
# Sitio 4: la compuerta HITL del dispatcher (``tool_execution``), antes de ejecutar la strategy
# =============================================================================
#
# Es el sitio que el censo de arriba no veía (Codex P1 en #950): ``generate_lods`` está registrado detrás de
# ``HitlGateMiddleware``, que le pide al operador aprobar ``tool_execution`` ANTES de llamar a ``execute``. Con la
# barrera de contención puesta, el operador aprobaba igual una corrida que el backend iba a refutar.


class _GuardHITLFalso:
    """Lo único que la compuerta le pide al ``HITLGuard``: una decisión por ``request_approval``."""

    def __init__(self, decision: Decision = Decision.APPROVED) -> None:
        self._decision = decision
        self.pedidos: list[dict[str, Any]] = []

    async def request_approval(self, **kwargs: Any) -> Decision:
        self.pedidos.append(kwargs)
        return self._decision


def _dispatcher_de_produccion(veredicto: PreflightDeBackend, guard: _GuardHITLFalso) -> tuple[Any, MagicMock]:
    """El dispatcher REAL de producción (mismo ``build_orchestration_dispatcher`` y la misma cadena de middleware)."""
    servicio = _servicio_de_la_strategy(veredicto)
    servicio.verificar_etapas_previas = MagicMock(return_value=EvidenciaDeEtapasPrevias(faltantes=()))
    dispatcher = build_orchestration_dispatcher(
        crear_dependencias_dispatcher(dyndolod_service=servicio), hitl_gate=HitlGateMiddleware(hitl_guard=guard)
    )
    return dispatcher, servicio


async def test_la_compuerta_del_dispatcher_no_le_pregunta_al_operador_si_el_backend_no_esta_listo():
    guard = _GuardHITLFalso()
    dispatcher, servicio = _dispatcher_de_produccion(PreflightDeBackend.bloqueado(MOTIVO_BRIDGE), guard)

    resultado = await dispatcher.dispatch("generate_lods", {"preset": "Medium"})

    assert guard.pedidos == [], "no se le pide al operador aprobar tool_execution por una corrida que no puede arrancar"
    servicio.execute.assert_not_awaited()
    assert resultado["status"] == "error"
    assert resultado["reason"] == "DynDOLODPipelineExecutionFailed"
    assert "BACKEND_NOT_READY" in resultado["details"] and MOTIVO_BRIDGE in resultado["details"]


async def test_la_compuerta_del_dispatcher_pregunta_y_ejecuta_si_el_backend_esta_listo():
    """Control positivo: el preflight no saltea la aprobación cuando el backend está bien."""
    guard = _GuardHITLFalso()
    dispatcher, servicio = _dispatcher_de_produccion(PreflightDeBackend.ok(), guard)

    resultado = await dispatcher.dispatch("generate_lods", {"preset": "Medium"})

    assert [p["category"] for p in guard.pedidos] == ["tool_execution"]
    servicio.execute.assert_awaited_once()
    assert resultado == {"success": True, "message": ""}


def test_generate_lods_es_un_preparador_de_aprobacion_de_la_compuerta():
    """Sin la capability, ``HitlGateMiddleware`` no tiene dónde probar el backend antes de preguntar."""
    assert isinstance(GenerateLodsStrategy(MagicMock()), ApprovalPreparer)


# =============================================================================
# Anclas que ENUMERAN: un sitio o una strategy nueva rompe esto hasta que se decida
# =============================================================================


def _arbol(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def _funciones(arbol: ast.AST, prefijo: str = ""):
    """(nombre calificado, nodo) de cada función, con el nombre de su clase."""
    for nodo in ast.iter_child_nodes(arbol):
        if isinstance(nodo, ast.ClassDef):
            yield from _funciones(nodo, f"{prefijo}{nodo.name}.")
        elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefijo}{nodo.name}", nodo
            yield from _funciones(nodo, f"{prefijo}{nodo.name}.")


def _funcion(ruta: str, nombre: str) -> ast.AST:
    (nodo,) = [n for q, n in _funciones(_arbol(RAIZ / ruta)) if q == nombre]
    return nodo


def _nombre_de_llamada(nodo: ast.Call) -> str | None:
    if isinstance(nodo.func, ast.Attribute):
        return nodo.func.attr
    if isinstance(nodo.func, ast.Name):
        return nodo.func.id
    return None


def _es_pregunta_al_operador(nodo: ast.AST) -> bool:
    """Llama a ``consultar_aprobador`` o lee el canal ``self._aprobar_sin_etapas_previas`` (no su asignación)."""
    if isinstance(nodo, ast.Call):
        return _nombre_de_llamada(nodo) == "consultar_aprobador"
    return (
        isinstance(nodo, ast.Attribute)
        and isinstance(nodo.ctx, ast.Load)
        and nodo.attr == "_aprobar_sin_etapas_previas"
    )


def _sitios_que_le_preguntan_al_operador() -> set[tuple[str, str]]:
    sitios: set[tuple[str, str]] = set()
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = _arbol(ruta)
        for calificado, funcion in _funciones(arbol):
            if any(_es_pregunta_al_operador(n) for n in ast.walk(funcion)):
                sitios.add((ruta.relative_to(RAIZ).as_posix(), calificado))
    return sitios


_PREGUNTAS_ANTES_DEL_SPAWN = {
    ("sky_claw/local/tools/dyndolod_runner.py", "DynDOLODRunner.preparar_interaccion_desatendida"),
    (
        "sky_claw/app/orchestrator/tool_strategies/generate_lods.py",
        "GenerateLodsStrategy._pedir_confirmacion_sin_etapas_previas",
    ),
    ("sky_claw/app/agent/tools/lod_tools.py", "GestorDeTrabajosDeLods._operador_aprueba_seguir"),
}
_PREGUNTAS_CON_EL_PROCESO_VIVO = {
    ("sky_claw/local/tools/dyndolod_uia_driver.py", "ConductorDesatendido._atender_modal"),
}
#: Lee el canal del operador SOLO para saber si existe (``is None``): nunca lo invoca, así que no pregunta nada.
_LECTURAS_DEL_CANAL_SIN_PREGUNTAR = {
    ("sky_claw/app/agent/tools/lod_tools.py", "GestorDeTrabajosDeLods.iniciar"),
}


def test_los_sitios_que_le_preguntan_al_operador_estan_enumerados():
    """Un sitio nuevo que toque el canal del operador rompe esto hasta decidir su clase: antes del spawn (y entonces
    necesita el preflight), con el proceso vivo, o una mera lectura de presencia (las dos últimas con su prueba)."""
    assert _sitios_que_le_preguntan_al_operador() == (
        _PREGUNTAS_ANTES_DEL_SPAWN | _PREGUNTAS_CON_EL_PROCESO_VIVO | _LECTURAS_DEL_CANAL_SIN_PREGUNTAR
    )


def test_la_lectura_de_presencia_del_canal_nunca_lo_invoca():
    """Estructura, no prosa: toda lectura del canal en ``iniciar`` es el operando de un ``is None`` / ``is not None``."""
    ((ruta, calificado),) = _LECTURAS_DEL_CANAL_SIN_PREGUNTAR
    funcion = _funcion(ruta, calificado)
    lecturas = [n for n in ast.walk(funcion) if _es_pregunta_al_operador(n) and isinstance(n, ast.Attribute)]
    assert lecturas, "ya no lee el canal: sacala de esta lista"
    comparados = {
        id(c.left)
        for c in ast.walk(funcion)
        if isinstance(c, ast.Compare)
        and len(c.ops) == 1
        and isinstance(c.ops[0], (ast.Is, ast.IsNot))
        and isinstance(c.comparators[0], ast.Constant)
        and c.comparators[0].value is None
    }
    assert all(id(lectura) in comparados for lectura in lecturas)


def test_la_pregunta_del_driver_uia_corre_con_el_proceso_ya_vivo():
    """Estructura, no prosa: el modal se atiende sobre ``ctx.pid`` (un proceso que ya existe), así que el ``spawn``
    —y con él el backend— ya ocurrió."""
    ((ruta, calificado),) = _PREGUNTAS_CON_EL_PROCESO_VIVO
    funcion = _funcion(ruta, calificado)
    usa_el_pid = any(
        isinstance(n, ast.Attribute) and n.attr == "pid" and isinstance(n.value, ast.Name) and n.value.id == "ctx"
        for n in ast.walk(funcion)
    )
    assert usa_el_pid


_ORDEN_DEL_PREFLIGHT = [
    (
        "sky_claw/local/tools/dyndolod_runner.py",
        "DynDOLODRunner.preparar_interaccion_desatendida",
        "consultar_aprobador",
    ),
    (
        "sky_claw/app/orchestrator/tool_strategies/generate_lods.py",
        "GenerateLodsStrategy._guard_de_etapas_previas_del_asistido",
        "_pedir_confirmacion_sin_etapas_previas",
    ),
    ("sky_claw/app/agent/tools/lod_tools.py", "GestorDeTrabajosDeLods._correr", "_operador_aprueba_seguir"),
]


@pytest.mark.parametrize(
    ("ruta", "calificado", "pregunta"), _ORDEN_DEL_PREFLIGHT, ids=[c for _, c, _ in _ORDEN_DEL_PREFLIGHT]
)
def test_cada_sitio_prueba_el_backend_antes_de_preguntar(ruta, calificado, pregunta):
    funcion = _funcion(ruta, calificado)
    llamadas = [n for n in ast.walk(funcion) if isinstance(n, ast.Call)]
    preflight = [n.lineno for n in llamadas if _nombre_de_llamada(n) == "verificar_backend_listo"]
    preguntas = [n.lineno for n in llamadas if _nombre_de_llamada(n) == pregunta]
    assert preflight, f"{calificado} no prueba el backend"
    assert preguntas, f"{calificado} ya no llama a {pregunta}: actualizá esta tabla"
    assert min(preflight) < min(preguntas), f"{calificado}: el preflight tiene que ir ANTES de la pregunta"


def test_todo_sitio_que_pregunta_antes_del_spawn_tiene_su_orden_anclado():
    anclados = {(ruta, calificado) for ruta, calificado, _ in _ORDEN_DEL_PREFLIGHT}
    # el guard de la GUI y el del agente preguntan por una función auxiliar: el orden se ancla en quien la llama.
    auxiliares = {
        "GenerateLodsStrategy._pedir_confirmacion_sin_etapas_previas": "GenerateLodsStrategy._guard_de_etapas_previas_del_asistido",
        "GestorDeTrabajosDeLods._operador_aprueba_seguir": "GestorDeTrabajosDeLods._correr",
    }
    for ruta, calificado in _PREGUNTAS_ANTES_DEL_SPAWN:
        responsable = auxiliares.get(calificado, calificado)
        assert (ruta, responsable) in anclados, f"{calificado} pregunta antes del spawn y no tiene orden anclado"


_STRATEGIES_DE_SPAWN = {
    ("sky_claw/local/tools/dyndolod_runner.py", "DynDOLODSpawnStrategy"),
    ("sky_claw/local/tools/dyndolod_runner.py", "StandaloneDynDOLODSpawnStrategy"),
    ("sky_claw/local/mo2/brokered_dyndolod.py", "BrokeredDynDOLODSpawnStrategy"),
    ("sky_claw/local/mo2/brokered_dyndolod.py", "VfsRequiredDynDOLODSpawnStrategy"),
}


def test_toda_strategy_de_spawn_declara_la_capability_del_preflight():
    """Detecta por forma (``spawn`` + ``verify_texgen_handoff``), no por lista: una strategy nueva sin preflight rompe."""
    encontradas: dict[tuple[str, str], set[str]] = {}
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        for nodo in ast.walk(_arbol(ruta)):
            if isinstance(nodo, ast.ClassDef):
                metodos = {m.name for m in nodo.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
                if {"spawn", "verify_texgen_handoff"} <= metodos:
                    encontradas[(ruta.relative_to(RAIZ).as_posix(), nodo.name)] = metodos
    assert set(encontradas) == _STRATEGIES_DE_SPAWN
    sin_preflight = sorted(n for (_, n), metodos in encontradas.items() if "preflight_backend" not in metodos)
    assert not sin_preflight, f"strategies sin preflight_backend: {sin_preflight}"


def test_solo_preflight_del_backend_toca_la_capability_de_la_strategy():
    """El fail-closed (capability ausente, veredicto ajeno, excepción) vive en UNA pieza: nadie más llama a
    ``preflight_backend`` ni lo lee con ``getattr``."""
    lectores: set[tuple[str, str]] = set()
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        for calificado, funcion in _funciones(_arbol(ruta)):
            if calificado.endswith(".preflight_backend") or calificado == "preflight_backend":
                continue  # la definición de la capability misma
            for nodo in ast.walk(funcion):
                toca = (isinstance(nodo, ast.Attribute) and nodo.attr == "preflight_backend") or (
                    isinstance(nodo, ast.Constant) and nodo.value == "preflight_backend"
                )
                if toca:
                    lectores.add((ruta.relative_to(RAIZ).as_posix(), calificado))
    assert lectores == {("sky_claw/local/tools/dyndolod_runner.py", "preflight_del_backend")}


def test_el_runner_y_el_servicio_delegan_en_la_misma_pieza():
    for ruta, calificado in [
        ("sky_claw/local/tools/dyndolod_runner.py", "DynDOLODRunner.verificar_backend_listo"),
        ("sky_claw/local/tools/dyndolod_service.py", "DynDOLODPipelineService.verificar_backend_listo"),
    ]:
        llamadas = [n for n in ast.walk(_funcion(ruta, calificado)) if isinstance(n, ast.Call)]
        assert [_nombre_de_llamada(n) for n in llamadas] == ["preflight_del_backend"], calificado


# -----------------------------------------------------------------------------
# El censo CERRADO: toda pregunta humana pasa por ``HITLGuard.request_approval``
# -----------------------------------------------------------------------------
#
# El censo de arriba busca por NOMBRE (``consultar_aprobador``, el canal de etapas previas) y por eso no vio la
# compuerta del dispatcher: otro nombre, mismo recurso. ``request_approval`` es la única primitiva por la que un
# humano recibe una pregunta, así que enumerar SUS sitios cierra la clase. (``tests/test_hitl.py`` congela los mismos
# sitios con otro fin —la identidad del ``request_id``—; este los clasifica por lo que importa acá: ¿antes del spawn?)

_ASK_ANTES_DEL_SPAWN_CON_PREFLIGHT = {
    # sitio de la pregunta -> (archivo, función) que prueba el backend antes de que ese sitio se alcance
    ("sky_claw/app/orchestrator/dyndolod_desatendido_hitl.py", "AprobadorDesatendidoHITL.aprobar_configuracion"): (
        "sky_claw/local/tools/dyndolod_runner.py",
        "DynDOLODRunner.preparar_interaccion_desatendida",
    ),
    (
        "sky_claw/app/orchestrator/dyndolod_desatendido_hitl.py",
        "AprobadorDesatendidoHITL.aprobar_sin_etapas_previas",
    ): ("sky_claw/app/agent/tools/lod_tools.py", "GestorDeTrabajosDeLods._correr"),
    (
        "sky_claw/app/orchestrator/dyndolod_desatendido_hitl.py",
        "AprobadorDesatendidoHITL.aprobar_sin_etapas_previas_asistido",
    ): (
        "sky_claw/app/orchestrator/tool_strategies/generate_lods.py",
        "GenerateLodsStrategy._guard_de_etapas_previas_del_asistido",
    ),
    ("sky_claw/app/orchestrator/tool_strategies/middleware.py", "HitlGateMiddleware.__call__"): (
        "sky_claw/app/orchestrator/tool_strategies/generate_lods.py",
        "GenerateLodsStrategy.prepare_for_approval",
    ),
}
#: Con el proceso ya vivo, o después de la corrida: el ``spawn`` ya ocurrió, así que el backend estuvo listo.
_ASK_DESPUES_DEL_SPAWN = {
    ("sky_claw/app/orchestrator/dyndolod_desatendido_hitl.py", "AprobadorDesatendidoHITL.aprobar_modal"),
    ("sky_claw/app/orchestrator/dyndolod_readiness_hitl.py", "ConfirmadorHITL.confirmar"),
    ("sky_claw/app/orchestrator/sandbox_promotion.py", "SandboxPromotionFlow._request_decision"),
}
#: Preguntas que no están en el camino de ``generate_lods`` (otras tools, instaladores, sincronización de mods).
_ASK_FUERA_DE_LA_ETAPA_9 = {
    ("sky_claw/app/agent/tools/nexus_tools.py", "download_mod"),
    ("sky_claw/app/agent/tools/system_tools.py", "install_mod_from_archive"),
    ("sky_claw/app/agent/tools/xedit_bridge_tools.py", "_pedir_aprobacion_de_admision"),
    ("sky_claw/app/orchestrator/sync_engine.py", "SyncEngine._check_and_update_mod"),
    ("sky_claw/local/tools_installer.py", "ToolsInstaller._ensure_github_mod"),
    ("sky_claw/local/tools_installer.py", "ToolsInstaller._ensure_nexus_mod"),
    ("sky_claw/local/tools_installer.py", "ToolsInstaller.ensure_bodyslide"),
    ("sky_claw/local/tools_installer.py", "ToolsInstaller.ensure_loot"),
    ("sky_claw/local/tools_installer.py", "ToolsInstaller.ensure_pandora"),
    ("sky_claw/local/tools_installer.py", "ToolsInstaller.ensure_skse"),
    ("sky_claw/local/tools_installer.py", "ToolsInstaller.ensure_xedit"),
    # PR-3d (ADR 0019): egress del build de patchers de Synthesis (Fase 1), fuera del camino de LODs.
    ("sky_claw/local/tools/synthesis_compilacion.py", "compilar_patcher_local"),
    # PR-3e (ADR 0019): egress de las fuentes Git de un patcher de Synthesis (Fase 1).
    ("sky_claw/local/tools/synthesis_fuentes_git.py", "obtener_fuentes_git"),
    # PR-3k (ADR 0019): egress de solo lectura (git ls-remote) para fijar el SynthesisPreset.
    ("sky_claw/local/tools/synthesis_fuentes_git.py", "resolver_refs_remotos"),
    # Gate de la cadena LOOT→xEdit→DynDOLOD con preview: hoy NO tiene call site de producción (lo prueba
    # ``test_el_gate_de_la_cadena_con_preview_no_tiene_call_site_de_produccion``); si se cablea, esto rompe.
    ("sky_claw/app/orchestrator/preview/approval_gate.py", "ChainPreviewApprovalGate.preview_then_execute"),
}


def _sitios_de_request_approval() -> set[tuple[str, str]]:
    sitios: set[tuple[str, str]] = set()
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        for calificado, funcion in _funciones(_arbol(ruta)):
            if any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "request_approval"
                for n in ast.walk(funcion)
            ):
                sitios.add((ruta.relative_to(RAIZ).as_posix(), calificado))
    return sitios


def test_toda_pregunta_humana_esta_clasificada_por_su_relacion_con_el_spawn():
    """Un sitio nuevo de ``request_approval`` rompe esto hasta decidir su clase: si pregunta ANTES del spawn de la
    etapa 9, necesita el preflight (y su entrada en la primera tabla, con la función que lo prueba)."""
    clasificados = set(_ASK_ANTES_DEL_SPAWN_CON_PREFLIGHT) | _ASK_DESPUES_DEL_SPAWN | _ASK_FUERA_DE_LA_ETAPA_9
    assert _sitios_de_request_approval() == clasificados


def test_cada_pregunta_previa_al_spawn_tiene_un_responsable_que_prueba_el_backend():
    for sitio, (ruta, calificado) in _ASK_ANTES_DEL_SPAWN_CON_PREFLIGHT.items():
        llamadas = [n for n in ast.walk(_funcion(ruta, calificado)) if isinstance(n, ast.Call)]
        assert "verificar_backend_listo" in {_nombre_de_llamada(n) for n in llamadas}, (
            f"{calificado} es quien protege la pregunta de {sitio} y no prueba el backend"
        )


def test_la_compuerta_prueba_el_backend_antes_de_abrir_la_espera_humana():
    """Orden dentro de ``HitlGateMiddleware.__call__``: ``_prepare_for_approval`` (donde el preflight corre) va ANTES
    de ``request_approval``."""
    funcion = _funcion("sky_claw/app/orchestrator/tool_strategies/middleware.py", "HitlGateMiddleware.__call__")
    llamadas = [n for n in ast.walk(funcion) if isinstance(n, ast.Call)]
    preparar = [n.lineno for n in llamadas if _nombre_de_llamada(n) == "_prepare_for_approval"]
    preguntar = [n.lineno for n in llamadas if _nombre_de_llamada(n) == "request_approval"]
    assert preparar and preguntar and min(preparar) < min(preguntar)


def test_el_gate_de_la_cadena_con_preview_no_tiene_call_site_de_produccion():
    """Fundamento de su exclusión, verificado y no escrito: nadie en ``sky_claw/`` lo instancia fuera de su módulo."""
    propio = "sky_claw/app/orchestrator/preview/approval_gate.py"
    usuarios = sorted(
        ruta.relative_to(RAIZ).as_posix()
        for ruta in (RAIZ / "sky_claw").rglob("*.py")
        if ruta.relative_to(RAIZ).as_posix() != propio
        and "ChainPreviewApprovalGate" in ruta.read_text(encoding="utf-8")
    )
    assert usuarios == [], f"ChainPreviewApprovalGate ya tiene call sites de producción: {usuarios}; falta su preflight"
