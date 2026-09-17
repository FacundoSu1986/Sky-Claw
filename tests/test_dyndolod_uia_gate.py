"""Gate UIA puro de Output — contrato U1–U14 (T5-v2, Fase 2).

Cubre :mod:`sky_claw.local.tools.dyndolod_uia_gate` SIN cableado productivo:
el caller (Fase 3) construye la solicitud con el expected derivado del layout
(``str(layout.raiz_de(herramienta))``); acá el expected entra como string ya
calculado y el gate no importa layout, runner ni service.

**Mapa tipado exigido por el encargo (§9) sobre el modelo existente.** No se
inventa un enum nuevo: la semántica del donor es más rica y se preserva —

* ``MATCH`` ≡ ``EstadoPreflight.MATCH``;
* ``MISMATCH`` ≡ ``EstadoPreflight.MISMATCH`` (``OUTPUT_DIFIERE``);
* ``UNAVAILABLE`` ≡ ``UNKNOWN`` + ``UIA_NO_DISPONIBLE`` (sensor ausente, U8);
* ``INDETERMINATE`` ≡ ``UNKNOWN`` + razón estructural (U11, U12);
* ``TIMEOUT`` ≡ ``UNKNOWN`` + razón transitoria + prueba de que el gate
  durmió el presupuesto COMPLETO (U9, U14a: el reloj falso lo demuestra);
* ``CANCELLED`` ≡ la excepción de aborto viaja SIN convertir (U10): el gate
  síncrono no recibe ``CancelledError`` directo; el boundary async vive en el
  caller (``to_thread`` + ``wait_for`` exterior, Fase 3) y acá se prueba que
  nada en el loop lo traga ni lo convierte en veredicto.

**Stale con dos caras (U13/U14).** Un preset rancio se puede mostrar de dos
formas y el contrato las distingue: ilegible (``TEdit`` vacío o sin patrón,
medido en el rig) es TRANSITORIO y se espera hasta MATCH (U13) o hasta el
deadline (U14a, contrato TIMEOUT); DIVERGENTE (muestra otra ruta) es
CONCLUYENTE y corta en ``MISMATCH`` sin esperar (U14b). Esperar ante un valor
divergente sólo dilataría el rojo.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib

import pytest

from sky_claw.local.tools.dyndolod_uia_gate import (
    DEFAULT_READINESS_TIMEOUT_SEGUNDOS,
    GATE_UIA_INTERVALO_SEGUNDOS,
    GATE_UIA_TIMEOUT_FINAL_SEGUNDOS,
    GATE_UIA_TIMEOUT_SEGUNDOS,
    RAZONES_TRANSITORIAS_DE_INICIO,
    RAZONES_TRANSITORIAS_FINAL,
    CapacidadDeReadinessUIA,
    ConfirmadorNoDisponible,
    ContratoDeHelperError,
    OperatorConfigurationReadyRequest,
    PedidoDeGate,
    PoliticaDeReintento,
    ResolucionProtocoloReadiness,
    ResultadoConfirmacion,
    VeredictoInitial,
    clasificar_initial,
    ejecutar_gate_sincrono,
    evidencia_de_resultado_coherente,
    pedido_desde_json,
    resultado_a_json,
    resultado_desde_json,
    resultado_proceso_muerto,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    RAZONES_DE_UNKNOWN,
    RAZONES_VALIDAS_POR_ESTADO,
    ControlObservado,
    EstadoPreflight,
    EvidenciaControlObservado,
    ProcesoObservado,
    RazonPreflight,
    ResultadoPreflightUIA,
    SolicitudPreflightUIA,
    UIANoDisponibleError,
    VentanaObservada,
    canonicalizar_ruta_windows,
    selector_de_output,
)

#: Forma del layout productivo (``derivar_layout_de_dyndolod``): en Fase 2 el
#: expected entra como string ya calculado —``str(layout.raiz_de(h))``— así que
#: los tests lo escriben literal con esa FORMA, sin importar el layout.
RAIZ_EXTERNA = "E:/Modding/ExternalWork"
FAMILY_ROOT = f"{RAIZ_EXTERNA}/DynDOLOD"
TEXGEN_ROOT = f"{RAIZ_EXTERNA}/DynDOLOD/TexGen"
DYNDOLOD_ROOT = f"{RAIZ_EXTERNA}/DynDOLOD/DynDOLOD"

BINARIOS = {"TexGen": "TexGenx64.exe", "DynDOLOD": "DynDOLODx64.exe"}


# ---------------------------------------------------------------------------
# Dobles de prueba
# ---------------------------------------------------------------------------


class LocalizadorFalso:
    """Procesos fijos + registro de si el gate siquiera preguntó."""

    def __init__(self, procesos):
        self._procesos = tuple(procesos)
        self.consultas = 0

    def procesos(self):
        self.consultas += 1
        return self._procesos


class ObservadorGuionado:
    """Un control Output con valores guionados por ronda + ``liberar`` real.

    Agotado el guion, repite el último valor: así se modela "stale permanente"
    sin listas infinitas. Implementa ``liberar`` (capacidad
    ``ObservadorLiberable`` por duck-typing estructural) y lo cuenta, para que
    U10 pueda probar cleanup en el camino del aborto.
    """

    def __init__(self, ventana, control, valores):
        self._ventana = ventana
        self._control = control
        self._valores = list(valores)
        self.rondas_de_lectura = 0
        self.liberaciones = 0

    def ventanas_de_proceso(self, pid):
        if self._ventana is None:
            return ()
        return (self._ventana,)

    def controles_de_ventana(self, ventana):
        return (self._control,)

    def leer_valor(self, control):
        self.rondas_de_lectura += 1
        return self._valores[min(self.rondas_de_lectura - 1, len(self._valores) - 1)]

    def liberar(self):
        self.liberaciones += 1


class ObservadorSinLiberar:
    """Doble mínimo SIN capacidad liberable: el ``finally`` no debe exigirla."""

    def __init__(self, ventana, control, valor):
        self._ventana = ventana
        self._control = control
        self._valor = valor

    def ventanas_de_proceso(self, pid):
        return (self._ventana,)

    def controles_de_ventana(self, ventana):
        return (self._control,)

    def leer_valor(self, control):
        return self._valor


class RelojFalso:
    """Reloj monotónico manual: demuestra el deadline sin esperar de verdad."""

    def __init__(self):
        self.ahora = 1000.0
        self.suenos: list[float] = []

    def reloj(self):
        return self.ahora

    def dormir(self, segundos):
        self.suenos.append(segundos)
        self.ahora += segundos


def _proceso(tool, pid=4242):
    return ProcesoObservado(pid=pid, nombre_ejecutable=BINARIOS[tool], ruta_ejecutable=None)


def _ventana(pid=4242, titulo="TexGen 3.00"):
    return VentanaObservada(pid=pid, titulo=titulo, class_name="TMainForm", handle="w1")


def _control(pid=4242):
    return ControlObservado(pid=pid, automation_id="", nombre="", tipo_de_control="Edit", class_name="TEdit")


def _solicitud(tool, esperado, pid=4242):
    return SolicitudPreflightUIA(
        tool=tool,
        ejecutable_esperado=BINARIOS[tool],
        salida_administrada_esperada=esperado,
        criterios_del_control=selector_de_output(tool),
        pid=pid,
    )


def _ejecutar(solicitud, procesos, observador, timeout=30.0, intervalo=5.0, **kwargs):
    reloj = RelojFalso()
    localizador = LocalizadorFalso(procesos)
    resultado = ejecutar_gate_sincrono(
        solicitud,
        fabrica_observador=lambda: observador,
        localizador=localizador,
        timeout_segundos=timeout,
        intervalo_segundos=intervalo,
        reloj=reloj.reloj,
        dormir=reloj.dormir,
        **kwargs,
    )
    return resultado, reloj, localizador


def _caso_directo(tool, esperado, observado):
    """Una ronda: proceso, ventana y control sanos; el valor decide."""
    return _ejecutar(
        _solicitud(tool, esperado),
        [_proceso(tool)],
        ObservadorGuionado(_ventana(), _control(), [observado]),
    )[0]


# ---------------------------------------------------------------------------
# U1–U5 — identidad del expected por herramienta
# ---------------------------------------------------------------------------


def test_u1_texgen_observado_igual_a_texgen_root_da_match():
    resultado = _caso_directo("TexGen", TEXGEN_ROOT, TEXGEN_ROOT)
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE


def test_u2_dyndolod_observado_igual_a_dyndolod_root_da_match():
    resultado = _caso_directo("DynDOLOD", DYNDOLOD_ROOT, DYNDOLOD_ROOT)
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE


def test_u3_texgen_que_muestra_family_root_da_mismatch():
    """El family_root NO es output: verlo en el campo es divergencia."""
    resultado = _caso_directo("TexGen", TEXGEN_ROOT, FAMILY_ROOT)
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE


def test_u4_dyndolod_que_muestra_texgen_root_da_mismatch():
    """Los tool roots no se comparten: el de TexGen en DynDOLOD es rojo."""
    resultado = _caso_directo("DynDOLOD", DYNDOLOD_ROOT, TEXGEN_ROOT)
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE


def test_u5_texgen_que_muestra_dyndolod_root_da_mismatch():
    resultado = _caso_directo("TexGen", TEXGEN_ROOT, DYNDOLOD_ROOT)
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE


# ---------------------------------------------------------------------------
# U6–U7 — canonicalización lógica (sin autoridad física)
# ---------------------------------------------------------------------------


def test_u6_ruta_con_espacios_da_match():
    esperado = f"{RAIZ_EXTERNA} con espacios/DynDOLOD/TexGen"
    resultado = _caso_directo("TexGen", esperado, esperado)
    assert resultado.estado is EstadoPreflight.MATCH


@pytest.mark.parametrize(
    "observado",
    [
        "e:/modding/externalwork/dyndolod/texgen",
        "E:\\Modding\\ExternalWork\\DynDOLOD\\TexGen",
        "E:/Modding/ExternalWork/DynDOLOD/TexGen/",
        "E:\\Modding/ExternalWork\\DynDOLOD/TexGen",
    ],
)
def test_u7_diferencias_inocuas_de_windows_dan_match(observado):
    resultado = _caso_directo("TexGen", TEXGEN_ROOT, observado)
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE


# ---------------------------------------------------------------------------
# U8 — unavailable fail-closed
# ---------------------------------------------------------------------------


def test_u8_observador_no_disponible_no_es_match():
    localizador = LocalizadorFalso([_proceso("TexGen")])

    def _fabrica_rota():
        raise UIANoDisponibleError("UI Automation es Windows-only")

    resultado = ejecutar_gate_sincrono(
        _solicitud("TexGen", TEXGEN_ROOT),
        fabrica_observador=_fabrica_rota,
        localizador=localizador,
        timeout_segundos=30.0,
        intervalo_segundos=5.0,
        reloj=RelojFalso().reloj,
        dormir=lambda s: None,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert resultado.valor_esperado == TEXGEN_ROOT
    assert localizador.consultas == 0, "sin backend no hay observación que intentar"


# ---------------------------------------------------------------------------
# U9 — deadline agotado (contrato TIMEOUT)
# ---------------------------------------------------------------------------


def test_u9_sin_proceso_hasta_el_deadline_da_unknown_tras_dormir_el_presupuesto():
    """TIMEOUT tipado sin inventar un estado: UNKNOWN + razón transitoria +
    prueba de presupuesto consumido. Un corte estructural o por muerte duerme
    ~0 (U12); el deadline duerme el presupuesto entero."""
    resultado, reloj, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT, pid=None),
        [],
        ObservadorGuionado(_ventana(), _control(), [None]),
        timeout=30.0,
        intervalo=5.0,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PROCESO_NO_ENCONTRADO
    assert resultado.razon in RAZONES_TRANSITORIAS_DE_INICIO
    assert sum(reloj.suenos) == pytest.approx(30.0)
    assert all(s <= 5.0 for s in reloj.suenos), "ningún sueño puede pasarse del intervalo"


def test_u9b_timeout_invalido_es_bug_del_caller_no_unknown():
    with pytest.raises(ValueError):
        _ejecutar(_solicitud("TexGen", TEXGEN_ROOT), [], ObservadorGuionado(_ventana(), _control(), [None]), timeout=0)
    with pytest.raises(ValueError):
        _ejecutar(
            _solicitud("TexGen", TEXGEN_ROOT), [], ObservadorGuionado(_ventana(), _control(), [None]), intervalo=-1
        )


@pytest.mark.parametrize("valor", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_u9c_plazos_no_finitos_son_bug_del_caller(valor):
    observador = ObservadorGuionado(_ventana(), _control(), [None])
    with pytest.raises(ValueError):
        _ejecutar(_solicitud("TexGen", TEXGEN_ROOT), [], observador, timeout=valor)
    with pytest.raises(ValueError):
        _ejecutar(_solicitud("TexGen", TEXGEN_ROOT), [], observador, intervalo=valor)


@pytest.mark.parametrize("valor", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_u9d_el_pedido_ipc_rechaza_plazos_no_finitos(valor):
    with pytest.raises(ValueError):
        PedidoDeGate(
            solicitud=_solicitud("TexGen", TEXGEN_ROOT),
            politica=PoliticaDeReintento.INICIO,
            timeout_segundos=valor,
            intervalo_segundos=0.25,
        )
    with pytest.raises(ValueError):
        PedidoDeGate(
            solicitud=_solicitud("TexGen", TEXGEN_ROOT),
            politica=PoliticaDeReintento.INICIO,
            timeout_segundos=1.0,
            intervalo_segundos=valor,
        )


def test_u9e_el_pedido_ipc_acepta_un_plazo_finito_positivo():
    pedido = PedidoDeGate(
        solicitud=_solicitud("TexGen", TEXGEN_ROOT),
        politica=PoliticaDeReintento.INICIO,
        timeout_segundos=12.5,
        intervalo_segundos=0.25,
    )
    assert pedido.timeout_segundos == 12.5
    assert pedido.intervalo_segundos == 0.25


def test_u9f_el_deserializador_del_pedido_rechaza_nan_textual_via_decimal():
    """El JSON estándar no trae NaN; un 0/negativo en el cable también es contrato."""
    with pytest.raises(ContratoDeHelperError):
        pedido_desde_json(
            '{"solicitud": {"tool": "TexGen", "ejecutable_esperado": "C:\\\\x.exe",'
            ' "salida_administrada_esperada": "E:\\\\out", "pid": 1,'
            ' "criterios_del_control": {"tipo_de_control": "Edit", "class_name": "TEdit"}},'
            ' "politica": "inicio", "timeout_segundos": 0, "intervalo_segundos": 0.1}'
        )


# ---------------------------------------------------------------------------
# U10 — el aborto nunca se convierte en veredicto
# ---------------------------------------------------------------------------


def test_u10_cancelled_durante_la_espera_propaga_y_libera_sin_fabricar_match():
    """Boundary async documentado: el gate síncrono no recibe CancelledError
    directo; el caller lo manda a ``to_thread`` y cancela el await exterior.
    Lo que el gate SÍ garantiza: una excepción del ``dormir`` (el punto donde
    el thread espera) viaja intacta, el ``finally`` libera igual, y jamás sale
    un MATCH/MISMATCH/UNKNOWN fabricado."""
    observador = ObservadorGuionado(_ventana(), _control(), [None])
    reloj = RelojFalso()

    def _dormir_que_aborta(segundos):
        raise asyncio.CancelledError("cancelación del caller")

    with pytest.raises(asyncio.CancelledError):
        ejecutar_gate_sincrono(
            _solicitud("TexGen", TEXGEN_ROOT),
            fabrica_observador=lambda: observador,
            localizador=LocalizadorFalso([]),
            timeout_segundos=30.0,
            intervalo_segundos=5.0,
            reloj=reloj.reloj,
            dormir=_dormir_que_aborta,
        )
    assert observador.liberaciones == 1, "el aborto no puede saltear el cleanup del apartamento"


def test_u10b_observador_sin_capacidad_liberable_no_rompe_el_finally():
    resultado, _, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT),
        [_proceso("TexGen")],
        ObservadorSinLiberar(_ventana(), _control(), TEXGEN_ROOT),
    )
    assert resultado.estado is EstadoPreflight.MATCH


# ---------------------------------------------------------------------------
# U11–U12 — ventana equivocada / control ausente (INDETERMINATE)
# ---------------------------------------------------------------------------


def test_u11_ventana_de_otro_proceso_no_produce_match():
    """El adaptador devuelve SÓLO una ventana ajena: el gate la rechaza y,
    con el proceso vivo sin ventana propia, agota el deadline en UNKNOWN."""
    ajena = VentanaObservada(pid=9999, titulo="TexGen 3.00", class_name="TMainForm", handle="wX")
    resultado, reloj, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT),
        [_proceso("TexGen")],
        ObservadorGuionado(ajena, _control(pid=9999), [TEXGEN_ROOT]),
        timeout=3.0,
        intervalo=1.0,
        proceso_vivo=lambda: True,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VENTANA_NO_ENCONTRADA
    assert sum(reloj.suenos) == pytest.approx(3.0)


def test_u12_control_output_ausente_con_politica_final_da_unknown_inmediato():
    """Con política sin transitorias (final gate), lo ausente corta ya."""
    sin_control = ObservadorGuionado(_ventana(), _control(), [TEXGEN_ROOT])

    def _sin_controles(ventana):
        return ()

    sin_control.controles_de_ventana = _sin_controles
    resultado, reloj, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT),
        [_proceso("TexGen")],
        sin_control,
        razones_transitorias=frozenset(),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.CONTROL_NO_ENCONTRADO
    assert reloj.suenos == []


def test_u12b_proceso_muerto_corta_la_espera_transitoria():
    """Otra ronda de UIA no resucita al binario: el corte es inmediato."""
    resultado, reloj, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT),
        [_proceso("TexGen")],
        ObservadorGuionado(_ventana(), _control(), [None]),
        proceso_vivo=lambda: False,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert reloj.suenos == [], "esperar al deadline con el proceso muerto sólo dilata el rojo"


# ---------------------------------------------------------------------------
# U13–U14 — stale: ilegible se espera, divergente corta
# ---------------------------------------------------------------------------


def test_u13_valor_inicialmente_ilegible_luego_esperado_termina_match():
    """El TEdit recién creado puede no exponer texto (medido): el gate espera
    el readiness y cierra en MATCH cuando aparece el expected."""
    observador = ObservadorGuionado(_ventana(), _control(), [None, None, TEXGEN_ROOT])
    resultado, reloj, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT),
        [_proceso("TexGen")],
        observador,
        timeout=30.0,
        intervalo=5.0,
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE
    assert observador.rondas_de_lectura == 3
    assert sum(reloj.suenos) == pytest.approx(10.0)


def test_u14a_ilegible_permanente_agota_el_deadline_en_unknown():
    resultado, reloj, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT),
        [_proceso("TexGen")],
        ObservadorGuionado(_ventana(), _control(), [None]),
        timeout=30.0,
        intervalo=5.0,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VALOR_NO_LEIBLE
    assert sum(reloj.suenos) == pytest.approx(30.0)


def test_u14b_divergente_permanente_da_mismatch_sin_esperar():
    """Un stale que MUESTRA otra ruta no es espera: es veredicto concluyente."""
    observador = ObservadorGuionado(_ventana(), _control(), [FAMILY_ROOT])
    resultado, reloj, _ = _ejecutar(
        _solicitud("TexGen", TEXGEN_ROOT),
        [_proceso("TexGen")],
        observador,
        timeout=30.0,
        intervalo=5.0,
    )
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE
    assert observador.rondas_de_lectura == 1
    assert reloj.suenos == []


# ---------------------------------------------------------------------------
# Política congelada + progreso + tipos del puerto de readiness
# ---------------------------------------------------------------------------


def test_las_razones_transitorias_de_inicio_estan_congeladas():
    assert set(RAZONES_TRANSITORIAS_DE_INICIO) == {
        RazonPreflight.PROCESO_NO_ENCONTRADO,
        RazonPreflight.PID_NO_COINCIDE,
        RazonPreflight.VENTANA_NO_ENCONTRADA,
        RazonPreflight.CONTROL_NO_ENCONTRADO,
        RazonPreflight.VALOR_NO_LEIBLE,
    }


def test_las_razones_transitorias_del_final_son_solo_valor_no_leible():
    """Tras la confirmación humana el wizard ya existe: sin ventana/control no
    hay nada que "se resuelva solo". Sólo un Edit recién redibujado puede no
    exponer texto un latido."""
    assert set(RAZONES_TRANSITORIAS_FINAL) == {RazonPreflight.VALOR_NO_LEIBLE}


def test_las_cotas_del_gate_estan_congeladas():
    assert GATE_UIA_TIMEOUT_SEGUNDOS == 300.0
    assert GATE_UIA_TIMEOUT_FINAL_SEGUNDOS == 30.0
    assert GATE_UIA_INTERVALO_SEGUNDOS == 1.0
    assert DEFAULT_READINESS_TIMEOUT_SEGUNDOS == 600.0
    # Propiedad de la política, no sólo el número: tras la confirmación humana
    # el deadline es CORTO (la GUI ya está idle y la única razón transitoria es
    # un redraw); regalarle el presupuesto del inicial dilataría el fail-closed.
    assert GATE_UIA_TIMEOUT_FINAL_SEGUNDOS < GATE_UIA_TIMEOUT_SEGUNDOS


def test_la_capacidad_usa_el_deadline_final_corto_por_defecto():
    capacidad = CapacidadDeReadinessUIA(ejecutor=object(), confirmador=object())  # type: ignore[arg-type]
    assert capacidad.gate_final_timeout_segundos == GATE_UIA_TIMEOUT_FINAL_SEGUNDOS
    assert capacidad.gate_timeout_segundos == GATE_UIA_TIMEOUT_SEGUNDOS


def test_al_progreso_solo_se_invoca_cuando_cambia_la_razon():
    vistos: list[RazonPreflight] = []

    class _LocalizadorGuionado:
        def __init__(self):
            self.llamadas = 0

        def procesos(self):
            self.llamadas += 1
            if self.llamadas <= 2:
                return ()
            return [_proceso("TexGen")]

    reloj = RelojFalso()
    resultado = ejecutar_gate_sincrono(
        _solicitud("TexGen", TEXGEN_ROOT, pid=None),
        fabrica_observador=lambda: ObservadorGuionado(None, _control(), [TEXGEN_ROOT]),
        localizador=_LocalizadorGuionado(),
        timeout_segundos=60.0,
        intervalo_segundos=5.0,
        reloj=reloj.reloj,
        dormir=reloj.dormir,
        al_progreso=lambda r: vistos.append(r.razon),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VENTANA_NO_ENCONTRADA
    assert vistos == [RazonPreflight.PROCESO_NO_ENCONTRADO, RazonPreflight.VENTANA_NO_ENCONTRADA]


def test_los_desenlaces_de_confirmacion_estan_congelados():
    assert [e.value for e in ResultadoConfirmacion] == ["aprobada", "denegada", "timeout", "canal_no_disponible"]
    assert [e.value for e in ResolucionProtocoloReadiness] == [
        "aprobada",
        "denegada",
        "timeout",
        "canal_no_disponible",
        "proceso_termino",
    ]


async def test_el_confirmador_no_disponible_falla_cerrado():
    confirmador = ConfirmadorNoDisponible()
    solicitud = OperatorConfigurationReadyRequest(
        tool_name="TexGen",
        pid=4242,
        executable=pathlib.Path("C:/Modding/DynDOLOD/TexGenx64.exe"),
        expected_output=pathlib.Path(TEXGEN_ROOT),
        timeout_seconds=600.0,
    )
    assert await confirmador.confirmar(solicitud) is ResultadoConfirmacion.CANAL_NO_DISPONIBLE
    await confirmador.informar(tool="TexGen", mensaje="hola")  # best-effort: no lanza


def test_la_solicitud_de_readiness_es_inmutable():
    solicitud = OperatorConfigurationReadyRequest(
        tool_name="TexGen",
        pid=4242,
        executable=pathlib.Path("C:/Modding/DynDOLOD/TexGenx64.exe"),
        expected_output=pathlib.Path(TEXGEN_ROOT),
        timeout_seconds=600.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        solicitud.pid = 9999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T5-v2.1 — la policy del INITIAL, enumerada sobre TODAS las razones
# ---------------------------------------------------------------------------


def _resultado_con(estado: EstadoPreflight, razon: RazonPreflight) -> ResultadoPreflightUIA:
    return ResultadoPreflightUIA(
        estado=estado,
        razon=razon,
        tool="TexGen",
        detalle="prueba de policy",
        valor_esperado=TEXGEN_ROOT,
    )


#: La clasificación entera, por igualdad literal: la ÚNICA excepción configurable
#: es el par concluyente del pipeline (MISMATCH/OUTPUT_DIFIERE). Todo lo demás es
#: BLOCKED, incluidas todas las razones de UNKNOWN y cualquier combinación que
#: ningún camino del pipeline emite. Si una razón nueva cae en la caja verde sin
#: pasar por acá, este ancla se rompe a propósito.
CLASIFICACION_INITIAL_CONGELADA: dict[tuple[EstadoPreflight, RazonPreflight], VeredictoInitial] = {
    (EstadoPreflight.MATCH, RazonPreflight.OUTPUT_COINCIDE): VeredictoInitial.MATCH,
    (EstadoPreflight.MISMATCH, RazonPreflight.OUTPUT_DIFIERE): VeredictoInitial.CONFIGURABLE_MISMATCH,
}


def test_la_clasificacion_initial_enumera_todas_las_razones():
    """Enumeración, no muestreo: cada razón cae en la caja declarada o en BLOCKED.

    El eje que este test cubre —y que un caso escrito a mano no cubriría— es que
    una razón NUEVA no puede quedar verde por olvido: la caja segura es el
    default, y sólo los dos pares concluyentes tienen una entrada explícita.
    """
    for estado in EstadoPreflight:
        for razon in RazonPreflight:
            esperado = CLASIFICACION_INITIAL_CONGELADA.get((estado, razon), VeredictoInitial.BLOCKED)
            obtenido = clasificar_initial(_resultado_con(estado, razon))
            assert obtenido is esperado, f"({estado.value}, {razon.value}) clasificó {obtenido} y no {esperado}"


def test_el_mismatch_inicial_solo_es_configurable_con_su_razon_concluyente():
    """No hay atajos: el estado solo no habilita la corrección humana."""
    assert (
        clasificar_initial(_resultado_con(EstadoPreflight.MISMATCH, RazonPreflight.OUTPUT_COINCIDE))
        is VeredictoInitial.BLOCKED
    )
    assert (
        clasificar_initial(_resultado_con(EstadoPreflight.MATCH, RazonPreflight.OUTPUT_DIFIERE))
        is VeredictoInitial.BLOCKED
    )


def test_el_resultado_de_proceso_muerto_es_unknown_y_bloquea():
    """La muerte detectada por el padre es evidencia honesta, no un veredicto mudo."""
    solicitud = _solicitud("TexGen", TEXGEN_ROOT, pid=4242)
    resultado = resultado_proceso_muerto(solicitud)
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PROCESO_NO_ENCONTRADO
    assert resultado.pid == 4242
    assert clasificar_initial(resultado) is VeredictoInitial.BLOCKED


# ---------------------------------------------------------------------------
# T5-v2.1 — contrato serializable del helper: el par (estado, razón) se valida
# ---------------------------------------------------------------------------
#
# El FINAL gate autoriza por ``resultado_final.estado is MATCH``. Si el canal
# IPC aceptara un par semánticamente corrupto —``{"estado": "MATCH", "razon":
# "OUTPUT_DIFIERE"}``— ese par se convertiría en autorización sin pasar por
# ``clasificar_initial``. La frontera helper → padre es fail-closed: el par
# imposible se rechaza AL DESERIALIZAR (``ContratoDeHelperError`` → el padre lo
# traduce a ``UNKNOWN``/``UIA_NO_DISPONIBLE``), y la única autoridad de qué
# pares son posibles es ``RAZONES_VALIDAS_POR_ESTADO``.


def _valores_de_evidencia_coherente(estado: EstadoPreflight) -> dict[str, object]:
    """La evidencia que el veredicto de ``estado`` exige para ser concluyente.

    ``MATCH`` recibe la ruta esperada; ``MISMATCH``, una divergente; ``UNKNOWN``
    no exige evidencia y no recibe ninguna — su contrato es no concluir. Ver la
    sección P2 (#590) al final.
    """
    if estado is EstadoPreflight.UNKNOWN:
        return {}
    observado = (
        "E:/Modding/ExternalWork/DynDOLOD/TexGen (rancio)" if estado is EstadoPreflight.MISMATCH else TEXGEN_ROOT
    )
    return {
        "valor_observado": observado,
        "valor_observado_canonico": canonicalizar_ruta_windows(observado),
        "valor_esperado_canonico": canonicalizar_ruta_windows(TEXGEN_ROOT),
    }


def _control_observado_de_contrato() -> EvidenciaControlObservado:
    """Descriptor del control que un veredicto concluyente debe transportar."""
    return EvidenciaControlObservado(
        pid=4242,
        automation_id="",
        nombre="",
        tipo_de_control="Edit",
        class_name="TEdit",
    )


def _control_observado_json() -> dict[str, object]:
    """El mismo descriptor, tal como viaja serializado por el canal."""
    return {
        "pid": 4242,
        "automation_id": "",
        "nombre": "",
        "tipo_de_control": "Edit",
        "class_name": "TEdit",
    }


def _json_de_resultado(estado: EstadoPreflight, razon: RazonPreflight) -> str:
    import json  # noqa: PLC0415

    # Un veredicto concluyente sin descriptor de control es contrato roto desde
    # el selector binding (#590): el payload de prueba lo incluye.
    control = {} if estado is EstadoPreflight.UNKNOWN else {"control_observado": _control_observado_json()}
    return json.dumps(
        {
            "estado": estado.value,
            "razon": razon.value,
            "tool": "TexGen",
            "detalle": "prueba de contrato",
            "valor_esperado": TEXGEN_ROOT,
            "evidencia": ["línea de evidencia"],
            **control,
            **_valores_de_evidencia_coherente(estado),
        }
    )


#: Los pares IMPOSIBLES, enumerados y no muestreados: el producto entero de
#: estados × razones menos los válidos. Incluye explícitamente los casos del
#: finding F3 (#590) —``MATCH``+``OUTPUT_DIFIERE``, ``MISMATCH``+``OUTPUT_COINCIDE``,
#: ``UNKNOWN``+razón concluyente, y cada estado concluyente con cada razón de
#: ``UNKNOWN``— sin que el test dependa de cuántas razones existan hoy.
PARES_IMPOSIBLES: list[tuple[EstadoPreflight, RazonPreflight]] = (
    [
        (EstadoPreflight.MATCH, RazonPreflight.OUTPUT_DIFIERE),
        (EstadoPreflight.MISMATCH, RazonPreflight.OUTPUT_COINCIDE),
        (EstadoPreflight.UNKNOWN, RazonPreflight.OUTPUT_COINCIDE),
        (EstadoPreflight.UNKNOWN, RazonPreflight.OUTPUT_DIFIERE),
    ]
    + [(EstadoPreflight.MATCH, razon) for razon in RAZONES_DE_UNKNOWN]
    + [(EstadoPreflight.MISMATCH, razon) for razon in RAZONES_DE_UNKNOWN]
)


@pytest.mark.parametrize(("estado", "razon"), PARES_IMPOSIBLES, ids=lambda valor: valor.value)
def test_el_deserializador_rechaza_pares_estado_razon_imposibles(estado, razon):
    """Un par que ningún camino del pipeline emite no es un veredicto: LANZA.

    Aceptarlo reabriría el camino medido en #590: ``MATCH`` + ``OUTPUT_DIFIERE``
    deserializa como un ``MATCH`` que el FINAL gate autoriza. El rechazo ocurre
    acá, en la frontera IPC, y el padre lo traduce a ``UIA_NO_DISPONIBLE`` por
    el camino fail-closed que ya existe — no se inventa una vía de éxito.
    """
    with pytest.raises(ContratoDeHelperError, match="inconsistente"):
        resultado_desde_json(_json_de_resultado(estado, razon))


#: Los pares VÁLIDOS, derivados del mismo contrato: ``MATCH`` sólo con su razón
#: concluyente, ``MISMATCH`` con la suya, y ``UNKNOWN`` con cada razón legítima
#: de ``UNKNOWN``.
PARES_VALIDOS: list[tuple[EstadoPreflight, RazonPreflight]] = [
    (EstadoPreflight.MATCH, RazonPreflight.OUTPUT_COINCIDE),
    (EstadoPreflight.MISMATCH, RazonPreflight.OUTPUT_DIFIERE),
] + [(EstadoPreflight.UNKNOWN, razon) for razon in RAZONES_DE_UNKNOWN]


@pytest.mark.parametrize(("estado", "razon"), PARES_VALIDOS, ids=lambda valor: valor.value)
def test_el_deserializador_conserva_los_pares_validos_del_contrato(estado, razon):
    resultado = resultado_desde_json(_json_de_resultado(estado, razon))
    assert resultado.estado is estado
    assert resultado.razon is razon


def test_el_serializador_round_trip_preserva_todo_par_valido():
    """``resultado_a_json`` → ``resultado_desde_json``: mismo veredicto, sin pérdida."""
    for estado, razon in PARES_VALIDOS:
        control = None if estado is EstadoPreflight.UNKNOWN else _control_observado_de_contrato()
        original = ResultadoPreflightUIA(
            estado=estado,
            razon=razon,
            tool="TexGen",
            detalle="d",
            valor_esperado=TEXGEN_ROOT,
            control_observado=control,
            evidencia=("e1", "e2"),
            **_valores_de_evidencia_coherente(estado),
        )
        reconstruido = resultado_desde_json(resultado_a_json(original))
        assert reconstruido.estado is estado
        assert reconstruido.razon is razon
        assert reconstruido.evidencia == ("e1", "e2")
        assert reconstruido.control_observado == control


def test_la_autoridad_del_par_estado_razon_esta_congelada():
    """Igualdad literal: una sola tabla, y la emisión y el rechazo la comparten.

    Si alguien agrega una razón concluyente nueva o un estado nuevo sin declarar
    su caja, este ancla se rompe a propósito en vez de dejar que serializer,
    deserializer, policy y tests mantengan cuatro listas divergentes.
    """
    assert {
        EstadoPreflight.MATCH: frozenset({RazonPreflight.OUTPUT_COINCIDE}),
        EstadoPreflight.MISMATCH: frozenset({RazonPreflight.OUTPUT_DIFIERE}),
        EstadoPreflight.UNKNOWN: RAZONES_DE_UNKNOWN,
    } == RAZONES_VALIDAS_POR_ESTADO


# ---------------------------------------------------------------------------
# P2 (#590) — un veredicto concluyente sólo sobrevive si su evidencia lo sostiene
# ---------------------------------------------------------------------------
#
# La frontera IPC validaba el par (estado, razón), el tool, el pid y el
# ``valor_esperado``, pero no que la evidencia OBSERVADA del helper sostuviera
# el veredicto. Como el FINAL gate autoriza por ``estado is EstadoPreflight.MATCH``,
# un helper corrupto o regresionado podía devolver un ``MATCH`` internamente
# "consistente" cuyo ``valor_observado`` nombraba OTRA salida —o sin evidencia
# observada— y el Start quedaba autorizado.
#
# Política, por estado, porque el contrato de cada caja es distinto:
#
# * ``MATCH`` / ``OUTPUT_COINCIDE`` — exige ``valor_observado`` y
#   ``valor_esperado`` canonicalizables que coincidan entre sí.
# * ``MISMATCH`` / ``OUTPUT_DIFIERE`` — la misma exigencia, con las dos rutas
#   divergentes.
# * ``UNKNOWN`` — no exige evidencia concluyente: su contrato es no concluir, y
#   sus cortes tempranos no llegan a observar valor.
#
# La coherencia se recomputa desde los valores CRUDOS con
# ``canonicalizar_ruta_windows``: los canónicos declarados por el helper tienen
# que ser exactamente los recomputados, porque ``valor_esperado_canonico``
# viene del helper y no es autoridad por sí solo. El incumplimiento es
# ``ContratoDeHelperError`` y el padre lo degrada a ``UNKNOWN`` /
# ``UIA_NO_DISPONIBLE`` por el camino fail-closed que ya existe — no se agrega
# una vía nueva de éxito.


def _json_de_evidencia(
    estado: EstadoPreflight,
    razon: RazonPreflight,
    *,
    observado: str | None,
    observado_canonico: str | None,
    esperado_canonico: str | None,
    esperado: str = TEXGEN_ROOT,
) -> str:
    """Payload del helper con la evidencia EXACTA que pide cada caso."""
    import json  # noqa: PLC0415

    return json.dumps(
        {
            "estado": estado.value,
            "razon": razon.value,
            "tool": "TexGen",
            "detalle": "prueba de evidencia",
            "valor_esperado": esperado,
            "pid": 4242,
            "ventana": "TexGen 3.00",
            "control_observado": _control_observado_json(),
            "valor_observado": observado,
            "valor_observado_canonico": observado_canonico,
            "valor_esperado_canonico": esperado_canonico,
            "evidencia": ["línea de evidencia"],
        }
    )


def test_un_match_con_observed_stale_no_sobrevive_la_frontera():
    """Caso A: la evidencia observada nombra OTRA salida que la esperada."""
    texto = _json_de_evidencia(
        EstadoPreflight.MATCH,
        RazonPreflight.OUTPUT_COINCIDE,
        observado=r"E:\Old",
        observado_canonico=r"e:\old",
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    with pytest.raises(ContratoDeHelperError, match="evidencia"):
        resultado_desde_json(texto)


def test_un_match_sin_evidencia_observada_no_sobrevive_la_frontera():
    """Caso B: sin valor observado no hay MATCH que autorice."""
    texto = _json_de_evidencia(
        EstadoPreflight.MATCH,
        RazonPreflight.OUTPUT_COINCIDE,
        observado=None,
        observado_canonico=None,
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    with pytest.raises(ContratoDeHelperError, match="evidencia"):
        resultado_desde_json(texto)


def test_un_match_legitimo_sigue_siendo_match():
    """Caso C: evidencia observada coherente con la salida esperada."""
    texto = _json_de_evidencia(
        EstadoPreflight.MATCH,
        RazonPreflight.OUTPUT_COINCIDE,
        observado=TEXGEN_ROOT,
        observado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    resultado = resultado_desde_json(texto)
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE


def test_un_match_con_otra_escritura_de_la_misma_ruta_win32_sigue_siendo_match():
    """Caso D: ``e:/.../TexGen/`` y ``E:\\...\\TexGen`` son la misma ruta."""
    observado = "e:/Modding/ExternalWork/DynDOLOD/TexGen/"
    assert canonicalizar_ruta_windows(observado) == canonicalizar_ruta_windows(TEXGEN_ROOT)
    texto = _json_de_evidencia(
        EstadoPreflight.MATCH,
        RazonPreflight.OUTPUT_COINCIDE,
        observado=observado,
        observado_canonico=canonicalizar_ruta_windows(observado),
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    resultado = resultado_desde_json(texto)
    assert resultado.estado is EstadoPreflight.MATCH


def test_un_mismatch_honesto_sigue_siendo_mismatch():
    """Caso E: la evidencia divergente sostiene el MISMATCH concluyente."""
    observado = r"E:\Old"
    texto = _json_de_evidencia(
        EstadoPreflight.MISMATCH,
        RazonPreflight.OUTPUT_DIFIERE,
        observado=observado,
        observado_canonico=canonicalizar_ruta_windows(observado),
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    resultado = resultado_desde_json(texto)
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE


def test_un_mismatch_sin_evidencia_observada_no_sobrevive_la_frontera():
    """El hermano del caso B: tampoco un MISMATCH concluye sin evidencia."""
    texto = _json_de_evidencia(
        EstadoPreflight.MISMATCH,
        RazonPreflight.OUTPUT_DIFIERE,
        observado=None,
        observado_canonico=None,
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    with pytest.raises(ContratoDeHelperError, match="evidencia"):
        resultado_desde_json(texto)


def test_un_mismatch_con_evidencia_de_coincidencia_no_sobrevive_la_frontera():
    """El espejo del caso A: un MISMATCH con rutas que coinciden es incoherente."""
    texto = _json_de_evidencia(
        EstadoPreflight.MISMATCH,
        RazonPreflight.OUTPUT_DIFIERE,
        observado=TEXGEN_ROOT,
        observado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    with pytest.raises(ContratoDeHelperError, match="evidencia"):
        resultado_desde_json(texto)


def test_un_match_con_canonico_observado_declarado_no_recomputado_no_sobrevive():
    """El canónico declarado que contradice al valor crudo es contrato roto."""
    texto = _json_de_evidencia(
        EstadoPreflight.MATCH,
        RazonPreflight.OUTPUT_COINCIDE,
        observado=TEXGEN_ROOT,
        observado_canonico=r"e:\otra",
        esperado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
    )
    with pytest.raises(ContratoDeHelperError, match="evidencia"):
        resultado_desde_json(texto)


def test_un_match_con_canonico_esperado_declarado_no_recomputado_no_sobrevive():
    """Hermano del anterior, del lado del esperado: no confiar en el canónico ajeno."""
    texto = _json_de_evidencia(
        EstadoPreflight.MATCH,
        RazonPreflight.OUTPUT_COINCIDE,
        observado=TEXGEN_ROOT,
        observado_canonico=canonicalizar_ruta_windows(TEXGEN_ROOT),
        esperado_canonico=r"e:\otra",
    )
    with pytest.raises(ContratoDeHelperError, match="evidencia"):
        resultado_desde_json(texto)


def test_la_exencion_de_evidencia_es_solo_de_unknown():
    """Enumeración: la exención es de ``UNKNOWN``, no de un caso suelto.

    El eje que un caso escrito a mano no cubre: todo estado concluyente —
    presente o FUTURO, porque la función es fail-closed ante un estado sin regla
    declarada— exige evidencia, y ``UNKNOWN`` es la única caja eximida.
    """
    for estado in EstadoPreflight:
        resultado = ResultadoPreflightUIA(
            estado=estado,
            razon=RazonPreflight.OUTPUT_COINCIDE,
            tool="TexGen",
            detalle="prueba de evidencia",
            valor_esperado=TEXGEN_ROOT,
        )
        if estado is EstadoPreflight.UNKNOWN:
            assert evidencia_de_resultado_coherente(resultado)
        else:
            assert not evidencia_de_resultado_coherente(resultado), (
                f"{estado.value} sobreviviría sin evidencia observada"
            )
