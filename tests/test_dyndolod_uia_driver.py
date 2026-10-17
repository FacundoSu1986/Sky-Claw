"""Driver UIA desatendido (#661 P3–P7): clasificación, pasos del helper y conductor.

Los criterios de aceptación CI de #661 están enumerados en los nombres de los
tests (``test_ci_NN_...``) para que falte uno se note leyendo el archivo. La GUI
es la simulada de ``tests/_dyndolod_gui_simulada.py``, construida con la
estructura medida en rig; el ejecutor es el EN PROCESO (el productivo por
helper tiene sus propios tests).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

import pytest

from sky_claw.local.tools.dyndolod_uia_contrato import CodigoDesatendido, MecanismoDeEscritura
from sky_claw.local.tools.dyndolod_uia_driver import (
    CapacidadDesatendidaUIA,
    ConductorDesatendido,
    ContextoDeConduccion,
    ContratoDePasoError,
    DesatendidoError,
    ElementoUI,
    EstadoDePaso,
    FaseDeObservacion,
    InstantaneaUI,
    LedgerDeAcciones,
    MotivoDePrecondicion,
    PedidoDePaso,
    ResultadoDePaso,
    TipoDePantalla,
    TipoDePaso,
    VentanaUI,
    clasificar_pantalla,
    consultar_aprobador,
    ejecutar_paso_sincrono,
    output_coincide,
    pedido_de_paso_a_json,
    pedido_de_paso_desde_json,
    resultado_de_paso_a_json,
    resultado_de_paso_desde_json,
)
from sky_claw.local.tools.dyndolod_uia_driver_ejecutor import EjecutorDePasoEnProceso
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion
from sky_claw.local.tools.dyndolod_uia_preflight import UIANoDisponibleError
from tests._dyndolod_gui_simulada import (
    CONTRATO_DYNDOLOD,
    CONTRATO_TEXGEN,
    OUTPUT_ADMINISTRADO,
    OUTPUT_RANCIO,
    GuiSimulada,
    boton,
    elemento,
    elementos_de_dialogo,
    output_edit,
    ventana_principal,
)

# =============================================================================
# Utilidades
# =============================================================================


def _inst(*ventanas: VentanaUI, pid: int = 4242, valor: str | None = None) -> InstantaneaUI:
    return InstantaneaUI(pid=pid, ventanas=ventanas, valor_output=valor)


def _pedido(gui: GuiSimulada, tipo: TipoDePaso, **campos: object) -> PedidoDePaso:
    base: dict[str, object] = {
        "tipo": tipo,
        "tool": gui.contrato.tool,
        "exe_sha256": gui.contrato.exe_sha256,
        "modo_de_juego": "SSE",
        "pid": gui.pid,
        "ejecutable": gui.exe,
        "timeout_segundos": 0.2,
        "intervalo_segundos": 0.001,
    }
    base.update(campos)
    return PedidoDePaso(**base)  # type: ignore[arg-type]


def _paso(gui: GuiSimulada, pedido: PedidoDePaso) -> ResultadoDePaso:
    return ejecutar_paso_sincrono(pedido, fabrica_backend=lambda: gui, localizador=gui, dormir=lambda _s: None)


class AprobadorFalso:
    def __init__(
        self,
        *,
        modal: ResultadoConfirmacion = ResultadoConfirmacion.APROBADA,
        al_aprobar_modal: object = None,
    ) -> None:
        self._modal = modal
        self._al_aprobar_modal = al_aprobar_modal
        self.modales: list[object] = []
        self.configuraciones: list[object] = []
        self.avisos: list[tuple[str, str]] = []

    async def aprobar_configuracion(self, solicitud: object) -> ResultadoConfirmacion:
        self.configuraciones.append(solicitud)
        return ResultadoConfirmacion.APROBADA

    async def aprobar_modal(self, solicitud: object) -> ResultadoConfirmacion:
        self.modales.append(solicitud)
        if callable(self._al_aprobar_modal):
            self._al_aprobar_modal()
        return self._modal

    async def informar(self, *, tool: str, mensaje: str) -> None:
        self.avisos.append((tool, mensaje))


def _capacidad(gui: GuiSimulada, aprobador: AprobadorFalso | None = None, **plazos: float) -> CapacidadDesatendidaUIA:
    ejecutor = EjecutorDePasoEnProceso(
        fabrica_backend=lambda: gui,
        localizador=gui,
        dormir=lambda s: time.sleep(min(s, 0.002)),
        gracia_externa_segundos=2.0,
    )
    valores: dict[str, float] = {
        "arranque_timeout_segundos": 1.0,
        "observacion_intervalo_segundos": 0.005,
        "ventana_de_generacion_segundos": 0.5,
        "generacion_intervalo_segundos": 0.005,
        "confirmacion_de_inicio_segundos": 0.3,
        "confirmacion_intervalo_segundos": 0.01,
        "cierre_timeout_segundos": 0.3,
        "aprobacion_timeout_segundos": 1.0,
        "gracia_externa_segundos": 1.0,
    }
    valores.update(plazos)
    return CapacidadDesatendidaUIA(ejecutor=ejecutor, aprobador=aprobador or AprobadorFalso(), **valores)  # type: ignore[arg-type]


async def _sin_dormir(_segundos: float) -> None:
    await asyncio.sleep(0)


def _contexto(gui: GuiSimulada, tmp_path) -> ContextoDeConduccion:
    raiz = tmp_path / "root"
    raiz.mkdir(exist_ok=True)
    gui.raiz_output = raiz
    return ContextoDeConduccion(
        contrato=gui.contrato,
        pid=gui.pid,
        ejecutable=gui.exe,
        output_esperado=OUTPUT_ADMINISTRADO,
        raiz_output=raiz,
        ruta_log=None,
    )


async def _correr(gui: GuiSimulada, tmp_path, aprobador: AprobadorFalso | None = None, **plazos: float):
    capacidad = _capacidad(gui, aprobador, **plazos)
    conductor = ConductorDesatendido(capacidad, dormir=_sin_dormir)
    ctx = _contexto(gui, tmp_path)
    ledger = LedgerDeAcciones()
    await conductor.conducir_hasta_inicio(ctx, ledger, proceso_vivo=gui.proceso_vivo)
    await conductor.esperar_fin_y_cerrar(ctx, ledger, proceso_vivo=gui.proceso_vivo)
    return ledger


# =============================================================================
# Clasificación de pantalla
# =============================================================================


def test_el_modal_medido_de_texgen_se_reconoce_aunque_comparta_exit_con_el_terminal():
    modal = CONTRATO_TEXGEN.modales_conocidos[0]
    pantalla = clasificar_pantalla(
        CONTRATO_TEXGEN,
        _inst(ventana_principal(elementos_de_dialogo(modal.firma.instruccion, sorted(modal.firma.botones), "TexGen"))),
    )
    assert pantalla.tipo is TipoDePantalla.MODAL_CONOCIDO
    assert pantalla.modal == modal
    assert "texgen_modal_ignore" in pantalla.acciones_habilitadas


def test_el_dialogo_terminal_medido_se_reconoce():
    firma = CONTRATO_DYNDOLOD.dialogo_terminal
    pantalla = clasificar_pantalla(
        CONTRATO_DYNDOLOD, _inst(ventana_principal(elementos_de_dialogo(firma.instruccion, sorted(firma.botones), "D")))
    )
    assert pantalla.tipo is TipoDePantalla.DIALOGO_TERMINAL
    assert "dyndolod_save_exit" in pantalla.acciones_habilitadas


def test_un_dialogo_sin_firma_es_desconocido_y_lleva_su_fingerprint():
    pantalla = clasificar_pantalla(
        CONTRATO_TEXGEN,
        _inst(
            ventana_principal(elementos_de_dialogo("LOD billboard(s) not found.", [("Ignore", "CommandButton_5")], "T"))
        ),
    )
    assert pantalla.tipo is TipoDePantalla.DIALOGO_DESCONOCIDO
    assert "LOD billboard(s) not found." in pantalla.describir_dialogo()


def test_un_dialogo_sin_instruccion_legible_no_coincide_con_ninguna_firma():
    modal = CONTRATO_TEXGEN.modales_conocidos[0]
    elementos = [e for e in elementos_de_dialogo(modal.firma.instruccion, sorted(modal.firma.botones), "T")]
    elementos = [e for e in elementos if e.automation_id != "MainInstruction"]
    pantalla = clasificar_pantalla(CONTRATO_TEXGEN, _inst(ventana_principal(elementos)))
    assert pantalla.tipo is TipoDePantalla.DIALOGO_DESCONOCIDO


def test_dos_dialogos_simultaneos_son_ambiguos():
    modal = CONTRATO_TEXGEN.modales_conocidos[0]
    dialogo = elementos_de_dialogo(modal.firma.instruccion, sorted(modal.firma.botones), "T")
    pantalla = clasificar_pantalla(CONTRATO_TEXGEN, _inst(ventana_principal([*dialogo, *dialogo])))
    assert pantalla.tipo is TipoDePantalla.AMBIGUA


def test_el_dialogo_tapa_al_asistente():
    # Un Begin "visible" detrás de un modal es exactamente el clic que no se
    # puede probar: el diálogo manda.
    modal = CONTRATO_TEXGEN.modales_conocidos[0]
    elementos = [
        output_edit(),
        boton("Start"),
        *elementos_de_dialogo(modal.firma.instruccion, sorted(modal.firma.botones), "T"),
    ]
    assert (
        clasificar_pantalla(CONTRATO_TEXGEN, _inst(ventana_principal(elementos))).tipo is TipoDePantalla.MODAL_CONOCIDO
    )


def test_asistente_con_start_habilitado():
    pantalla = clasificar_pantalla(CONTRATO_TEXGEN, _inst(ventana_principal([output_edit(), boton("Start")])))
    assert pantalla.tipo is TipoDePantalla.ASISTENTE
    assert pantalla.acciones_habilitadas == frozenset({"texgen_start"})


def test_asistente_con_start_deshabilitado_lo_ve_pero_no_lo_habilita():
    pantalla = clasificar_pantalla(CONTRATO_TEXGEN, _inst(ventana_principal([output_edit(), boton("Start", False)])))
    assert pantalla.acciones_visibles == frozenset({"texgen_start"})
    assert pantalla.acciones_habilitadas == frozenset()


@pytest.mark.parametrize(
    ("elementos", "esperado"),
    [
        ([output_edit(), output_edit(), boton("Start")], TipoDePantalla.AMBIGUA),
        ([output_edit(), boton("Start"), boton("Start")], TipoDePantalla.AMBIGUA),
        ([boton("Start")], TipoDePantalla.CARGANDO),
    ],
)
def test_el_mapa_de_controles_fuera_de_contrato_falla_cerrado(elementos, esperado):
    assert clasificar_pantalla(CONTRATO_TEXGEN, _inst(ventana_principal(elementos))).tipo is esperado


def test_sin_ventanas_y_con_dos_ventanas_contractuales():
    assert clasificar_pantalla(CONTRATO_TEXGEN, _inst()).tipo is TipoDePantalla.SIN_VENTANA
    v = ventana_principal([output_edit(), boton("Start")])
    assert clasificar_pantalla(CONTRATO_TEXGEN, _inst(v, v)).tipo is TipoDePantalla.AMBIGUA


def test_el_ok_de_un_dialogo_no_es_el_begin_de_dyndolod():
    # Un "OK" con clase CCPushButton dentro de un #32770 NO es el TButton OK.
    elementos = elementos_de_dialogo("Algo raro", [("OK", "CommandButton_1")], "D")
    pantalla = clasificar_pantalla(CONTRATO_DYNDOLOD, _inst(ventana_principal([output_edit(), *elementos])))
    assert pantalla.tipo is TipoDePantalla.DIALOGO_DESCONOCIDO
    assert "dyndolod_begin_ok" not in pantalla.acciones_visibles


@pytest.mark.parametrize(
    ("observado", "esperado", "coincide"),
    [
        (r"E:\SkyClawWork\DynDOLOD\TexGen\\", r"E:\SkyClawWork\DynDOLOD\TexGen", True),
        (r"e:/skyclawwork/dyndolod/texgen", r"E:\SkyClawWork\DynDOLOD\TexGen", True),
        (r"E:\Otro", r"E:\SkyClawWork\DynDOLOD\TexGen", False),
        (None, r"E:\SkyClawWork\DynDOLOD\TexGen", False),
        (r"..\relativo", r"E:\SkyClawWork\DynDOLOD\TexGen", False),
    ],
)
def test_output_coincide_usa_la_canonicalizacion_del_gate(observado, esperado, coincide):
    assert output_coincide(observado, esperado) is coincide


# =============================================================================
# Pasos del helper (ejecutar_paso_sincrono)
# =============================================================================


def test_ci_01_sha_sin_contrato_no_muta_nada():
    gui = GuiSimulada(CONTRATO_TEXGEN)
    pedido = _pedido(gui, TipoDePaso.INVOCAR, exe_sha256="0" * 64, accion_id="texgen_start")
    resultado = _paso(gui, pedido)
    assert resultado.estado is EstadoDePaso.PRECONDICION_FALLIDA
    assert resultado.motivo is MotivoDePrecondicion.CONTRATO_DESCONOCIDO
    assert gui.invocaciones == [] and gui.escrituras == []


@pytest.mark.parametrize("tipo", [TipoDePaso.INVOCAR, TipoDePaso.ESCRIBIR_OUTPUT])
def test_ci_02_pid_o_exe_que_no_coinciden_no_mutan(tipo):
    campos = {"accion_id": "texgen_start"} if tipo is TipoDePaso.INVOCAR else {"valor": OUTPUT_ADMINISTRADO}
    otra_instalacion = GuiSimulada(CONTRATO_TEXGEN, ruta_observada=r"D:\Otra\TexGenx64.exe")
    assert (
        _paso(otra_instalacion, _pedido(otra_instalacion, tipo, **campos)).estado is EstadoDePaso.IDENTIDAD_NO_COINCIDE
    )
    otro_pid = GuiSimulada(CONTRATO_TEXGEN)
    assert _paso(otro_pid, _pedido(otro_pid, tipo, pid=999, **campos)).estado is EstadoDePaso.PROCESO_AUSENTE
    for gui in (otra_instalacion, otro_pid):
        assert gui.invocaciones == [] and gui.escrituras == []


class _BackendFijo:
    """Backend con una instantánea fija y registro de primitivas."""

    def __init__(self, instantanea: InstantaneaUI, valor: str | None = OUTPUT_RANCIO) -> None:
        self._instantanea = instantanea
        self._valor = valor
        self.mutaciones: list[str] = []

    def instantanea(self, pid: int) -> InstantaneaUI:
        return self._instantanea

    def leer_output(self, elemento_ui: ElementoUI) -> str | None:
        return self._valor

    def escribir_output(self, elemento_ui: ElementoUI, mecanismo: MecanismoDeEscritura, valor: str) -> str:
        self.mutaciones.append(f"escribir:{valor}")
        return "S_OK"

    def invocar(self, elemento_ui: ElementoUI) -> str:
        self.mutaciones.append(f"invocar:{elemento_ui.nombre}")
        return "S_OK"

    def liberar(self) -> None:
        return None


@pytest.mark.parametrize(
    "elementos",
    [
        [output_edit(), output_edit(), boton("Start")],
        [output_edit(), boton("Start"), boton("Start")],
    ],
    ids=["dos_outputs", "dos_starts"],
)
@pytest.mark.parametrize("tipo", [TipoDePaso.INVOCAR, TipoDePaso.ESCRIBIR_OUTPUT])
def test_ci_03_mapa_de_controles_distinto_no_muta(elementos, tipo):
    gui = GuiSimulada(CONTRATO_TEXGEN)
    backend = _BackendFijo(_inst(ventana_principal(elementos)))
    campos = {"accion_id": "texgen_start"} if tipo is TipoDePaso.INVOCAR else {"valor": OUTPUT_ADMINISTRADO}
    resultado = ejecutar_paso_sincrono(_pedido(gui, tipo, **campos), fabrica_backend=lambda: backend, localizador=gui)
    assert resultado.estado is EstadoDePaso.PRECONDICION_FALLIDA
    assert not resultado.invocado
    assert backend.mutaciones == []


def test_ci_05_el_mecanismo_que_falla_no_se_sustituye_por_otro():
    gui = GuiSimulada(CONTRATO_TEXGEN, hresult_escritura="COMError -0x7feceaf7")
    resultado = _paso(gui, _pedido(gui, TipoDePaso.ESCRIBIR_OUTPUT, valor=OUTPUT_ADMINISTRADO))
    assert resultado.estado is EstadoDePaso.MECANISMO_FALLO
    assert resultado.invocado
    # UNA escritura y por el mecanismo del contrato: nada de ValuePattern después.
    assert gui.escrituras == [(MecanismoDeEscritura.LEGACY_IACCESSIBLE, OUTPUT_ADMINISTRADO)]


def test_ci_08_begin_deshabilitado_no_se_invoca():
    gui = GuiSimulada(CONTRATO_TEXGEN, inicio_habilitado=False)
    resultado = _paso(gui, _pedido(gui, TipoDePaso.INVOCAR, accion_id="texgen_start"))
    assert resultado.motivo is MotivoDePrecondicion.CONTROL_DESHABILITADO
    assert gui.invocaciones == []


def test_ci_09_drift_del_output_antes_del_invoke_no_invoca():
    gui = GuiSimulada(CONTRATO_TEXGEN, output_inicial=OUTPUT_RANCIO)
    resultado = _paso(
        gui,
        _pedido(gui, TipoDePaso.INVOCAR, accion_id="texgen_start", precondicion_output=OUTPUT_ADMINISTRADO),
    )
    assert resultado.motivo is MotivoDePrecondicion.OUTPUT_DIFIERE
    assert gui.invocaciones == []


def test_ci_09_drift_de_pantalla_antes_del_invoke_no_invoca():
    # Entre la observación y el invoke apareció un modal: el helper re-clasifica
    # en el MISMO paso y no toca el Begin.
    gui = GuiSimulada(CONTRATO_TEXGEN, modal_inicial=True)
    resultado = _paso(gui, _pedido(gui, TipoDePaso.INVOCAR, accion_id="texgen_start"))
    assert resultado.motivo is MotivoDePrecondicion.PANTALLA_INCORRECTA
    assert gui.invocaciones == []


def test_una_accion_fuera_del_contrato_no_se_invoca():
    gui = GuiSimulada(CONTRATO_TEXGEN)
    resultado = _paso(gui, _pedido(gui, TipoDePaso.INVOCAR, accion_id="dyndolod_begin_ok"))
    assert resultado.motivo is MotivoDePrecondicion.ACCION_FUERA_DE_CONTRATO
    assert gui.invocaciones == []


def test_el_ignore_exige_el_modal_pedido():
    gui = GuiSimulada(CONTRATO_TEXGEN, modal_inicial=True)
    resultado = _paso(gui, _pedido(gui, TipoDePaso.INVOCAR, accion_id="texgen_modal_ignore", modal_id="otro"))
    assert resultado.motivo is MotivoDePrecondicion.PANTALLA_INCORRECTA
    assert gui.invocaciones == []


def test_sin_backend_uia_el_paso_responde_no_disponible_sin_actuar():
    gui = GuiSimulada(CONTRATO_TEXGEN)

    def _fabrica():
        raise UIANoDisponibleError("Linux")

    resultado = ejecutar_paso_sincrono(
        _pedido(gui, TipoDePaso.INVOCAR, accion_id="texgen_start"), fabrica_backend=_fabrica, localizador=gui
    )
    assert resultado.estado is EstadoDePaso.UIA_NO_DISPONIBLE
    assert not resultado.invocado


def test_observar_arranque_espera_el_asistente_con_la_accion_habilitada():
    gui = GuiSimulada(CONTRATO_TEXGEN)
    resultado = _paso(
        gui,
        _pedido(gui, TipoDePaso.OBSERVAR, fase=FaseDeObservacion.ARRANQUE, accion_esperada="texgen_start"),
    )
    assert resultado.estado is EstadoDePaso.OK
    assert resultado.instantanea is not None
    assert resultado.instantanea.valor_output == OUTPUT_RANCIO
    assert gui.invocaciones == [] and gui.escrituras == []


def test_observar_arranque_agota_el_plazo_si_el_begin_no_se_habilita():
    gui = GuiSimulada(CONTRATO_TEXGEN, inicio_habilitado=False)
    resultado = _paso(
        gui,
        _pedido(
            gui,
            TipoDePaso.OBSERVAR,
            fase=FaseDeObservacion.ARRANQUE,
            accion_esperada="texgen_start",
            timeout_segundos=0.01,
        ),
    )
    assert resultado.estado is EstadoDePaso.PLAZO_AGOTADO
    assert resultado.instantanea is not None


def test_la_instantanea_que_cruza_la_frontera_no_lleva_referencias_com():
    gui = GuiSimulada(CONTRATO_TEXGEN)
    resultado = _paso(
        gui, _pedido(gui, TipoDePaso.OBSERVAR, fase=FaseDeObservacion.ARRANQUE, accion_esperada="texgen_start")
    )
    assert resultado.instantanea is not None
    assert all(e.referencia is None for v in resultado.instantanea.ventanas for e in v.elementos)


def test_readback_ilegible_tras_escribir_reporta_que_si_se_escribio():
    class _Backend(_BackendFijo):
        def leer_output(self, elemento_ui: ElementoUI) -> str | None:
            if self.mutaciones:
                from sky_claw.local.tools.dyndolod_uia_preflight import ObservacionUIAError

                raise ObservacionUIAError("stale")
            return OUTPUT_RANCIO

    gui = GuiSimulada(CONTRATO_TEXGEN)
    backend = _Backend(_inst(ventana_principal([output_edit(), boton("Start")])))
    resultado = ejecutar_paso_sincrono(
        _pedido(gui, TipoDePaso.ESCRIBIR_OUTPUT, valor=OUTPUT_ADMINISTRADO),
        fabrica_backend=lambda: backend,
        localizador=gui,
    )
    assert resultado.invocado
    assert resultado.valor_leido is None


# =============================================================================
# Protocolo serializable
# =============================================================================


def test_pedido_y_resultado_hacen_round_trip():
    gui = GuiSimulada(CONTRATO_TEXGEN)
    pedido = _pedido(gui, TipoDePaso.INVOCAR, accion_id="texgen_start", precondicion_output=OUTPUT_ADMINISTRADO)
    assert pedido_de_paso_desde_json(pedido_de_paso_a_json(pedido)) == pedido
    resultado = _paso(
        gui, _pedido(gui, TipoDePaso.OBSERVAR, fase=FaseDeObservacion.ARRANQUE, accion_esperada="texgen_start")
    )
    assert resultado_de_paso_desde_json(resultado_de_paso_a_json(resultado)) == resultado


def _resultado_crudo(**cambios: object) -> str:
    base = json.loads(resultado_de_paso_a_json(ResultadoDePaso(estado=EstadoDePaso.OK)))
    base.update(cambios)
    return json.dumps(base)


@pytest.mark.parametrize(
    "texto",
    [
        "no es json",
        "[]",
        _resultado_crudo(extra=1),
        _resultado_crudo(estado="SIN_RESPUESTA"),
        _resultado_crudo(estado="INVENTADO"),
        _resultado_crudo(estado="PRECONDICION_FALLIDA"),
        _resultado_crudo(motivo="OUTPUT_DIFIERE"),
        _resultado_crudo(estado="PRECONDICION_FALLIDA", motivo="OUTPUT_DIFIERE", invocado=True),
        _resultado_crudo(invocado="si"),
    ],
    ids=[
        "no_json",
        "lista",
        "campo_extra",
        "sin_respuesta_del_helper",
        "estado_inventado",
        "sin_motivo",
        "motivo_huerfano",
        "motivo_invocado",
        "bool_texto",
    ],
)
def test_el_resultado_fuera_de_contrato_se_rechaza(texto):
    with pytest.raises(ContratoDePasoError):
        resultado_de_paso_desde_json(texto)


@pytest.mark.parametrize(
    "cambios",
    [{"pid": True}, {"pid": 0}, {"tipo": "BORRAR"}, {"timeout_segundos": -1}, {"extra": "x"}, {"fase": None}],
    ids=["pid_bool", "pid_cero", "tipo_inventado", "timeout_negativo", "campo_extra", "observar_sin_fase"],
)
def test_el_pedido_fuera_de_contrato_se_rechaza(cambios):
    gui = GuiSimulada(CONTRATO_TEXGEN)
    base = json.loads(
        pedido_de_paso_a_json(_pedido(gui, TipoDePaso.OBSERVAR, fase=FaseDeObservacion.ARRANQUE, accion_esperada="x"))
    )
    base.update(cambios)
    with pytest.raises(ContratoDePasoError):
        pedido_de_paso_desde_json(json.dumps(base))


# =============================================================================
# Conductor end-to-end (criterios CI de #661)
# =============================================================================


async def test_ci_04_output_rancio_se_corrige_con_readback_exacto_y_la_cadena_texgen_completa(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, output_inicial=OUTPUT_RANCIO)
    ledger = await _correr(gui, tmp_path)
    assert gui.escrituras == [(MecanismoDeEscritura.LEGACY_IACCESSIBLE, OUTPUT_ADMINISTRADO)]
    assert ledger.intentadas() == ("texgen_start", "texgen_exit")
    assert not gui.vivo


async def test_output_ya_correcto_no_se_escribe(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, output_inicial=OUTPUT_ADMINISTRADO + "\\")
    await _correr(gui, tmp_path)
    assert gui.escrituras == []


async def test_ci_10_y_16_begin_y_terminal_exactamente_una_vez(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, polls_de_generacion=4)
    await _correr(gui, tmp_path)
    assert gui.invocaciones.count("Start") == 1
    assert gui.invocaciones.count("Exit TexGen") == 1
    assert "Zip and Exit" not in gui.invocaciones


async def test_dyndolod_advanced_recarga_el_preset_y_se_reconverge_antes_del_ok(tmp_path):
    gui = GuiSimulada(
        CONTRATO_DYNDOLOD,
        output_inicial=OUTPUT_ADMINISTRADO,
        output_tras_advanced=r"E:\Sky-Claw T5 Rig\PR2 Work\DynDOLOD\DynDOLOD",
    )
    ledger = await _correr(gui, tmp_path)
    # El primer Output ya coincidía; la ÚNICA escritura es la reconvergencia.
    assert gui.escrituras == [(MecanismoDeEscritura.LEGACY_IACCESSIBLE, OUTPUT_ADMINISTRADO)]
    assert gui.invocaciones == ["Advanced >>>", "OK", "Save and Exit"]
    assert ledger.intentadas() == ("dyndolod_advanced", "dyndolod_begin_ok", "dyndolod_save_exit")


async def test_modal_conocido_se_continua_solo_con_aprobacion_remota(tmp_path):
    aprobador = AprobadorFalso()
    gui = GuiSimulada(CONTRATO_TEXGEN, modal_inicial=True)
    await _correr(gui, tmp_path, aprobador)
    assert len(aprobador.modales) == 1
    assert aprobador.modales[0].modal_id == "texgen_stitched_lod_previo"
    assert gui.invocaciones[0] == "Ignore"
    assert gui.invocaciones.count("Ignore") == 1


async def test_modal_conocido_denegado_no_se_toca(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, modal_inicial=True)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path, AprobadorFalso(modal=ResultadoConfirmacion.DENEGADA))
    assert error.value.codigo is CodigoDesatendido.KNOWN_MODAL_NOT_APPROVED
    assert gui.invocaciones == []


async def test_modal_que_reaparece_tras_continuarlo_corta(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, modal_inicial=True, modal_reaparece=True)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.UNEXPECTED_MODAL
    assert gui.invocaciones == ["Ignore"]


async def test_modal_desconocido_en_el_arranque_corta_sin_tocarlo(tmp_path):
    class _Gui(GuiSimulada):
        def _ventanas(self):
            if self.estado == "asistente":
                return (
                    ventana_principal(
                        elementos_de_dialogo("Something else happened", [("Ignore", "CommandButton_5")], "T")
                    ),
                )
            return super()._ventanas()

    gui = _Gui(CONTRATO_TEXGEN)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.UNEXPECTED_MODAL
    assert "Something else happened" in error.value.detalle
    assert gui.invocaciones == [] and gui.escrituras == []


async def test_escritura_rechazada_corta_como_output_write_unsupported(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, hresult_escritura="COMError -0x7feceaf7")
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.OUTPUT_WRITE_UNSUPPORTED
    assert gui.invocaciones == []


async def test_readback_distinto_corta_antes_del_begin(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, escritura_no_persiste=True)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.OUTPUT_READBACK_MISMATCH
    assert gui.invocaciones == []


async def test_ci_08_begin_que_nunca_se_habilita_corta_sin_invocar(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, output_inicial=OUTPUT_ADMINISTRADO, inicio_habilitado=False)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path, arranque_timeout_segundos=0.1)
    assert error.value.codigo is CodigoDesatendido.BEGIN_NOT_ENABLED
    assert gui.invocaciones == []


@pytest.mark.parametrize("falla", ["hresult", "excepcion"])
async def test_ci_11_desenlace_ambiguo_del_begin_no_se_reintenta(tmp_path, falla):
    if falla == "hresult":
        gui = GuiSimulada(CONTRATO_TEXGEN, hresult_invoke={"Start": "COMError 0x80004005"})
    else:
        gui = GuiSimulada(CONTRATO_TEXGEN, excepcion_en_invoke={"Start"})
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.BEGIN_OUTCOME_UNKNOWN
    assert gui.invocaciones.count("Start") == 1


async def test_ci_12_muerte_del_proceso_antes_del_begin_corta(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, modal_inicial=True)

    def _matar():
        gui.vivo = False

    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path, AprobadorFalso(al_aprobar_modal=_matar))
    assert error.value.codigo is CodigoDesatendido.PROCESS_DIED_PRE_BEGIN
    assert gui.invocaciones == []


async def test_ci_13_cero_mutaciones_durante_la_generacion(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, polls_de_generacion=6)
    await _correr(gui, tmp_path)
    indice_begin = gui.invocaciones.index("Start")
    # Entre el Begin y el terminal no hubo NINGUNA otra primitiva: la única
    # invocación posterior es la acción terminal, y las escrituras son previas.
    assert gui.invocaciones[indice_begin + 1 :] == ["Exit TexGen"]
    assert len(gui.escrituras) == 1


async def test_ci_15_dialogo_inesperado_durante_la_generacion_corta_sin_terminal(tmp_path):
    gui = GuiSimulada(
        CONTRATO_TEXGEN,
        dialogo_en_generacion=("Error: out of memory", (("OK", "CommandButton_1"),)),
    )
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.UNEXPECTED_DIALOG_DURING_GENERATION
    assert "Exit TexGen" not in gui.invocaciones
    assert "OK" not in gui.invocaciones


async def test_dialogo_terminal_ambiguo_corta_sin_terminal(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, terminal_ambiguo=True)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.UIA_CONTROL_MAP_MISMATCH
    assert "Exit TexGen" not in gui.invocaciones


@pytest.mark.parametrize("falla", ["hresult", "no_sale"])
async def test_ci_17_desenlace_ambiguo_del_terminal_no_se_reintenta(tmp_path, falla):
    if falla == "hresult":
        gui = GuiSimulada(CONTRATO_DYNDOLOD, hresult_invoke={"Save and Exit": "COMError 0x80004005"})
    else:
        gui = GuiSimulada(CONTRATO_DYNDOLOD, no_sale_tras_terminal=True)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path)
    assert error.value.codigo is CodigoDesatendido.TERMINAL_OUTCOME_UNKNOWN
    assert gui.invocaciones.count("Save and Exit") == 1


async def test_proceso_que_sale_solo_durante_la_generacion_deja_decidir_al_post_check(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, sale_sin_dialogo=True)
    ledger = await _correr(gui, tmp_path)
    assert ledger.intentadas() == ("texgen_start",)


async def test_begin_sin_progreso_observable_corta(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, inicio_sin_progreso=True)
    with pytest.raises(DesatendidoError) as error:
        await _correr(gui, tmp_path, confirmacion_de_inicio_segundos=0.05)
    assert error.value.codigo is CodigoDesatendido.BEGIN_NOT_CONFIRMED


async def test_sin_respuesta_del_ejecutor_en_el_begin_es_desenlace_desconocido(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, output_inicial=OUTPUT_ADMINISTRADO)
    capacidad = _capacidad(gui)
    real = capacidad.ejecutor

    class _EjecutorQueSeCuelgaEnElBegin:
        async def ejecutar(self, pedido: PedidoDePaso) -> ResultadoDePaso:
            if pedido.tipo is TipoDePaso.INVOCAR:
                return ResultadoDePaso(estado=EstadoDePaso.SIN_RESPUESTA, detalle="helper colgado")
            return await real.ejecutar(pedido)

    conductor = ConductorDesatendido(
        dataclasses.replace(capacidad, ejecutor=_EjecutorQueSeCuelgaEnElBegin()), dormir=_sin_dormir
    )
    with pytest.raises(DesatendidoError) as error:
        await conductor.conducir_hasta_inicio(
            _contexto(gui, tmp_path), LedgerDeAcciones(), proceso_vivo=gui.proceso_vivo
        )
    assert error.value.codigo is CodigoDesatendido.BEGIN_OUTCOME_UNKNOWN


async def test_observaciones_fallidas_durante_la_generacion_se_toleran_hasta_la_cota(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN, output_inicial=OUTPUT_ADMINISTRADO)
    capacidad = _capacidad(gui)
    real = capacidad.ejecutor
    conteo = {"generacion": 0}

    class _EjecutorConObservacionCaida:
        async def ejecutar(self, pedido: PedidoDePaso) -> ResultadoDePaso:
            if pedido.fase is FaseDeObservacion.GENERACION:
                conteo["generacion"] += 1
                return ResultadoDePaso(estado=EstadoDePaso.SIN_RESPUESTA, detalle="COM colgado")
            return await real.ejecutar(pedido)

    conductor = ConductorDesatendido(
        dataclasses.replace(capacidad, ejecutor=_EjecutorConObservacionCaida(), fallos_de_observacion_tolerados=3),
        dormir=_sin_dormir,
    )
    ctx = _contexto(gui, tmp_path)
    ledger = LedgerDeAcciones()
    await conductor.conducir_hasta_inicio(ctx, ledger, proceso_vivo=gui.proceso_vivo)
    with pytest.raises(DesatendidoError) as error:
        await conductor.esperar_fin_y_cerrar(ctx, ledger, proceso_vivo=gui.proceso_vivo)
    assert error.value.codigo is CodigoDesatendido.UIA_UNAVAILABLE
    assert conteo["generacion"] == 3
    assert "Exit TexGen" not in gui.invocaciones


def test_el_ledger_no_reserva_dos_veces_la_misma_accion():
    ledger = LedgerDeAcciones()
    ledger.reservar("texgen_start", tool="TexGen")
    with pytest.raises(DesatendidoError) as error:
        ledger.reservar("texgen_start", tool="TexGen")
    assert error.value.codigo is CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION


async def test_una_instantanea_de_otro_pid_es_violacion_de_contrato(tmp_path):
    gui = GuiSimulada(CONTRATO_TEXGEN)

    class _EjecutorMentiroso:
        async def ejecutar(self, pedido: PedidoDePaso) -> ResultadoDePaso:
            return ResultadoDePaso(
                estado=EstadoDePaso.OK,
                instantanea=InstantaneaUI(
                    pid=1, ventanas=(ventana_principal([output_edit(), boton("Start")]),), valor_output="x"
                ),
            )

    conductor = ConductorDesatendido(
        dataclasses.replace(_capacidad(gui), ejecutor=_EjecutorMentiroso()), dormir=_sin_dormir
    )
    with pytest.raises(DesatendidoError) as error:
        await conductor.conducir_hasta_inicio(
            _contexto(gui, tmp_path), LedgerDeAcciones(), proceso_vivo=gui.proceso_vivo
        )
    assert error.value.codigo is CodigoDesatendido.UNATTENDED_CONTRACT_VIOLATION


@pytest.mark.parametrize(
    ("comportamiento", "esperado"),
    [
        ("aprobada", ResultadoConfirmacion.APROBADA),
        ("excepcion", ResultadoConfirmacion.CANAL_NO_DISPONIBLE),
        ("fuera_de_contrato", ResultadoConfirmacion.CANAL_NO_DISPONIBLE),
        ("colgado", ResultadoConfirmacion.TIMEOUT),
    ],
)
async def test_consultar_aprobador_es_fail_closed(comportamiento, esperado):
    async def _consulta():
        if comportamiento == "excepcion":
            raise RuntimeError("Telegram caído")
        if comportamiento == "fuera_de_contrato":
            return True
        if comportamiento == "colgado":
            await asyncio.sleep(10)
        return ResultadoConfirmacion.APROBADA

    assert await consultar_aprobador(_consulta, timeout_segundos=0.01, gracia_segundos=0.01) is esperado


def test_la_capacidad_rechaza_plazos_invalidos():
    gui = GuiSimulada(CONTRATO_TEXGEN)
    with pytest.raises(ValueError):
        _capacidad(gui, arranque_timeout_segundos=0.0)
    with pytest.raises(ValueError):
        _capacidad(gui, cierre_timeout_segundos=float("nan"))
    with pytest.raises(ValueError):
        dataclasses.replace(_capacidad(gui), fallos_de_observacion_tolerados=0)


def test_elemento_ui_compara_sin_referencia():
    assert elemento("Button", "TButton", "Start") == dataclasses.replace(
        elemento("Button", "TButton", "Start"), referencia=object()
    )
