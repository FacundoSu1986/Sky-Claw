"""#997 — el indicador de paso en vivo del modo asistido: qué está esperando Sky-Claw en cada momento.

Antes, entre «se abrió la ventana» y «Output verificado» no había ninguna señal: el operador apretó Start antes de
tiempo (#6) o se quedó mirando un cartel de stitched que tapaba la ventana (#8). La propiedad:

- el runner emite UNA fase por paso del protocolo de readiness, y un corte con su código TIPADO antes de cada
  ``raise`` del protocolo (enumerado por AST: un ``raise`` nuevo sin su aviso rompe);
- el aviso es best-effort: un observador roto nunca cambia el resultado de la corrida;
- el servicio guarda la fase con el MISMO ciclo de vida que el progreso (se descarta en TODA salida) y con ella le
  arma al resultado su ``guia`` en criollo;
- los que leen ``fase_actual`` están congelados (la GUI y ``lods_status``).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.tools import dyndolod_runner as modulo_runner
from sky_claw.local.tools import dyndolod_service as modulo_servicio
from sky_claw.local.tools.dyndolod_guia import CambioDeFase, FaseAsistida
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODPreflightUIAError,
    DynDOLODReadinessProtocolError,
    DynDOLODRunner,
    ReadinessMode,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.dyndolod_uia_gate import ResolucionProtocoloReadiness, ResultadoConfirmacion
from sky_claw.local.tools.dyndolod_uia_preflight import RazonPreflight
from sky_claw.local.tools.output_targets import HerramientaDynDOLOD
from tests.test_dyndolod_t5v21_readiness import (
    ConfirmadorFalso,
    FabricaQueFalla,
    GuionUIA,
    ProcesoFalso,
    _capacidad,
    _correr,
    _layout_windows,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]


class _Registro:
    def __init__(self, *, explota: bool = False) -> None:
        self.cambios: list[CambioDeFase] = []
        self._explota = explota

    async def __call__(self, cambio: CambioDeFase) -> None:
        self.cambios.append(cambio)
        if self._explota:
            raise RuntimeError("observador roto")


def _runner_con_fases(capacidad, registro: _Registro) -> DynDOLODRunner:
    config = MagicMock()
    config.timeout_seconds = 3600
    config.heartbeat_interval = 60
    config.fence_ownership = None
    config.output_layout = _layout_windows()
    config.external_work_root = None
    config.texgen_exe = pathlib.Path("TexGenx64.exe")
    config.dyndolod_exe = pathlib.Path("DynDOLODx64.exe")
    return DynDOLODRunner(config, readiness=capacidad, on_fase=registro)


# ---------------------------------------------------------------------------
# El runner emite las fases del asistido
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "herramienta"), [("TexGen", HerramientaDynDOLOD.TEXGEN), ("DynDOLOD", HerramientaDynDOLOD.DYNDOLOD)]
)
async def test_el_camino_feliz_recorre_las_fases_en_orden(tool: str, herramienta: HerramientaDynDOLOD) -> None:
    layout = _layout_windows()
    registro = _Registro()
    runner = _runner_con_fases(_capacidad(GuionUIA([str(layout.raiz_de(herramienta))]), ConfirmadorFalso()), registro)

    await _correr(runner, ProcesoFalso(tool=tool), tool=tool)

    assert [c.fase for c in registro.cambios] == [
        FaseAsistida.ABRIENDO_VENTANA,
        FaseAsistida.ESPERANDO_TU_APROBACION,
        FaseAsistida.VERIFICANDO_OUTPUT,
        FaseAsistida.PODES_APRETAR_START,
        FaseAsistida.HERRAMIENTA_TERMINO,
    ]
    assert {c.tool_name for c in registro.cambios} == {tool}
    pedido = registro.cambios[1]
    assert pedido.esperado == str(layout.raiz_de(herramienta)), "el operador lee la ruta exacta del -o:"


async def test_un_rechazo_del_operador_emite_el_corte_con_su_desenlace() -> None:
    layout = _layout_windows()
    registro = _Registro()
    runner = _runner_con_fases(
        _capacidad(GuionUIA([str(layout.texgen_root)]), ConfirmadorFalso(ResultadoConfirmacion.DENEGADA)), registro
    )

    with pytest.raises(DynDOLODReadinessProtocolError):
        await _correr(runner, ProcesoFalso())

    assert [c.fase for c in registro.cambios][-1] is FaseAsistida.CORTADA
    assert registro.cambios[-1].corte is ResolucionProtocoloReadiness.DENEGADA


async def test_un_gate_que_no_puede_observar_emite_el_corte_con_su_razon() -> None:
    from sky_claw.local.tools.dyndolod_uia_preflight import UIANoDisponibleError

    registro = _Registro()
    capacidad = _capacidad(None, ConfirmadorFalso(), fabrica_observador=FabricaQueFalla(UIANoDisponibleError("x")))
    runner = _runner_con_fases(capacidad, registro)

    with pytest.raises(DynDOLODPreflightUIAError):
        await _correr(runner, ProcesoFalso())

    assert [c.fase for c in registro.cambios] == [FaseAsistida.ABRIENDO_VENTANA, FaseAsistida.CORTADA]
    assert registro.cambios[-1].corte is RazonPreflight.UIA_NO_DISPONIBLE


async def test_un_observador_de_fases_roto_no_cambia_la_corrida() -> None:
    layout = _layout_windows()
    registro = _Registro(explota=True)
    runner = _runner_con_fases(_capacidad(GuionUIA([str(layout.texgen_root)]), ConfirmadorFalso()), registro)

    _stdout, _stderr, returncode, _dur = await _correr(runner, ProcesoFalso())

    assert returncode == 0
    assert len(registro.cambios) == 5


async def test_el_opt_out_de_readiness_no_emite_fases() -> None:
    registro = _Registro()
    runner = _runner_con_fases(ReadinessMode.DISABLED_FOR_TEST, registro)
    await _correr(runner, ProcesoFalso())
    assert registro.cambios == []


def _metodo_del_runner(nombre: str) -> ast.AST:
    arbol = ast.parse(pathlib.Path(modulo_runner.__file__).read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "DynDOLODRunner":
            for hijo in nodo.body:
                if isinstance(hijo, ast.AsyncFunctionDef) and hijo.name == nombre:
                    return hijo
    raise AssertionError(nombre)


def test_todo_raise_del_protocolo_asistido_avisa_el_corte_justo_antes() -> None:
    """Enumerado: un ``raise`` nuevo en el protocolo sin su ``_reportar_corte`` deja a la guía sin el motivo."""
    protocolo = _metodo_del_runner("_protocolo_de_readiness")
    raises: list[tuple[ast.Raise, ast.stmt | None]] = []
    for nodo in ast.walk(protocolo):
        for campo in ("body", "orelse", "finalbody"):
            bloque = getattr(nodo, campo, None)
            if not isinstance(bloque, list):
                continue
            for indice, sentencia in enumerate(bloque):
                if isinstance(sentencia, ast.Raise):
                    raises.append((sentencia, bloque[indice - 1] if indice else None))
    assert len(raises) == 4, "cambió la familia de cortes del protocolo asistido: decidí su aviso"
    for sentencia, anterior in raises:
        assert anterior is not None and "self._reportar_corte(" in ast.unparse(anterior), (
            f"el raise de la línea {sentencia.lineno} no avisa el corte"
        )


# ---------------------------------------------------------------------------
# El servicio guarda la fase y arma la guía con ella
# ---------------------------------------------------------------------------


def _servicio(preflight: object | None = None) -> DynDOLODPipelineService:
    return DynDOLODPipelineService(
        lock_manager=AsyncMock(),
        snapshot_manager=AsyncMock(),
        journal=AsyncMock(),
        path_resolver=MagicMock(),
        event_bus=AsyncMock(),
        mo2_profile="Default",
        preflight=preflight,  # type: ignore[arg-type]
    )


async def test_el_servicio_expone_la_ultima_fase_con_su_texto() -> None:
    servicio = _servicio()
    await servicio._al_cambiar_de_fase(  # noqa: SLF001
        CambioDeFase(tool_name="TexGen", fase=FaseAsistida.VERIFICANDO_OUTPUT)
    )
    hechos = servicio.fase_actual.hechos(ahora=servicio.fase_actual.desde + 7)  # type: ignore[union-attr]
    assert hechos["herramienta"] == "TexGen"
    assert hechos["fase"] == "verificando_output"
    assert "NO toques «Start»" in hechos["texto"]
    assert hechos["hace_segundos"] == 7


class _PreflightQueAvanzaYCorta:
    """Durante la corrida el runner avisa fases y un corte; acá lo hace el preflight, y después sale."""

    def __init__(self, ref: list[DynDOLODPipelineService], desenlace: str) -> None:
        self._ref = ref
        self._desenlace = desenlace

    async def run(self) -> MagicMock:
        servicio = self._ref[0]
        await servicio._al_cambiar_de_fase(  # noqa: SLF001
            CambioDeFase(tool_name="DynDOLOD", fase=FaseAsistida.VERIFICANDO_OUTPUT)
        )
        await servicio._al_cambiar_de_fase(  # noqa: SLF001
            CambioDeFase(
                tool_name="DynDOLOD", fase=FaseAsistida.CORTADA, corte=ResolucionProtocoloReadiness.PROCESO_TERMINO
            )
        )
        if self._desenlace == "excepcion":
            raise RuntimeError("falla inesperada")
        if self._desenlace == "cancelacion":
            raise asyncio.CancelledError
        return MagicMock(blocks_mutations=True, checks=[])


def _servicio_que_corta(desenlace: str) -> DynDOLODPipelineService:
    ref: list[DynDOLODPipelineService] = []
    servicio = _servicio(_PreflightQueAvanzaYCorta(ref, desenlace))
    ref.append(servicio)
    return servicio


@pytest.mark.parametrize(
    ("desenlace", "esperado"),
    [("retorno_temprano", None), ("excepcion", RuntimeError), ("cancelacion", asyncio.CancelledError)],
)
async def test_toda_salida_descarta_la_fase(desenlace: str, esperado: object, monkeypatch: pytest.MonkeyPatch) -> None:
    servicio = _servicio_que_corta(desenlace)
    monkeypatch.setattr(servicio, "verificar_checklist", AsyncMock(return_value=MagicMock(listo=True)))
    if esperado is None:
        await servicio.execute()
    else:
        with pytest.raises(esperado):  # type: ignore[call-overload]
            await servicio.execute()
    assert servicio.fase_actual is None


async def test_un_corte_de_la_corrida_llega_con_su_guia_en_criollo(monkeypatch: pytest.MonkeyPatch) -> None:
    servicio = _servicio_que_corta("retorno_temprano")
    monkeypatch.setattr(servicio, "verificar_checklist", AsyncMock(return_value=MagicMock(listo=True)))

    resultado = await servicio.execute()

    assert resultado["success"] is False
    assert "antes de tiempo" in resultado["guia"]["titulo"], "se cortó mientras Sky-Claw verificaba"
    assert "«OK»" in resultado["guia"]["arreglo"], "nombra el botón de DynDOLOD"
    assert resultado["guia"]["detalle_tecnico"] == resultado["message"]


async def test_un_fallo_sin_corte_tambien_trae_guia(monkeypatch: pytest.MonkeyPatch) -> None:
    servicio = _servicio()
    monkeypatch.setattr(servicio, "verificar_checklist", AsyncMock(return_value=MagicMock(listo=True)))
    monkeypatch.setattr(servicio, "_ensure_preflight", MagicMock(return_value=None))
    monkeypatch.setattr(
        servicio, "_ensure_runner", MagicMock(side_effect=modulo_runner.DynDOLODExecutionError("sin runner"))
    )
    resultado = await servicio.execute()
    assert resultado["success"] is False
    assert resultado["guia"]["titulo"]
    assert "sin runner" in resultado["guia"]["detalle_tecnico"]


# ---------------------------------------------------------------------------
# Cableado y consumidores
# ---------------------------------------------------------------------------


def test_el_unico_sitio_que_construye_el_runner_le_pasa_el_aviso_de_fases() -> None:
    arbol = ast.parse(pathlib.Path(modulo_servicio.__file__).read_text(encoding="utf-8"))
    construcciones = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "DynDOLODRunner"
    ]
    assert len(construcciones) == 1
    cableado = {k.arg: ast.unparse(k.value) for k in construcciones[0].keywords if k.arg == "on_fase"}
    assert cableado == {"on_fase": "self._al_cambiar_de_fase"}


CONSUMIDORES_DE_FASE_ACTUAL = {
    "sky_claw/app/agent/tools/lod_tools.py",
    "sky_claw/app/gui/controllers/etapa9_estado.py",
}


def _toca(arbol: ast.AST, nombre: str) -> bool:
    return any(
        (isinstance(n, ast.Attribute) and n.attr == nombre) or (isinstance(n, ast.Constant) and n.value == nombre)
        for n in ast.walk(arbol)
    )


def test_los_consumidores_de_la_fase_estan_congelados() -> None:
    encontrados = set()
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        relativa = ruta.relative_to(RAIZ).as_posix()
        if relativa == "sky_claw/local/tools/dyndolod_service.py":
            continue
        if _toca(ast.parse(ruta.read_text(encoding="utf-8", errors="replace")), "fase_actual"):
            encontrados.add(relativa)
    assert encontrados == CONSUMIDORES_DE_FASE_ACTUAL


async def test_lods_status_cuenta_la_fase_del_trabajo_en_curso() -> None:
    from sky_claw.app.agent.tools.lod_tools import GestorDeTrabajosDeLods, TrabajoDeLods

    servicio = _servicio()
    await servicio._al_cambiar_de_fase(  # noqa: SLF001
        CambioDeFase(tool_name="TexGen", fase=FaseAsistida.PODES_APRETAR_START)
    )
    gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: servicio)
    gestor._actual = TrabajoDeLods(id="lods-1", parametros={}, iniciado_en=0.0, estado="en_curso")  # noqa: SLF001

    estado = gestor.estado()

    assert estado["fase"]["fase"] == "podes_apretar_start"
    assert "«Start»" in estado["fase"]["texto"]
