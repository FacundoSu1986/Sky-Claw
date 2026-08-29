"""T5A — preflight READ-ONLY de UI Automation sobre TexGen/DynDOLOD.

**Qué prueba este archivo y qué NO.** Prueba la máquina de decisión: dado lo que
un observador de UI Automation *dice* haber visto, ¿el preflight responde
``MATCH`` / ``MISMATCH`` / ``UNKNOWN`` con el fail-closed correcto? No prueba —ni
puede— que UIA exponga realmente el campo *Output* de TexGen/DynDOLOD: eso exige
un rig Windows con los binarios instalados y es la evidencia que
``local_scripts/probe_dyndolod_uia_readonly.py`` existe para conseguir.

**El límite epistémico está codificado, no sólo escrito.** ``MATCH`` significa
exactamente *"el valor que la GUI muestra hoy canonicaliza igual que la salida
administrada"*. No significa que una corrida futura vaya a escribir ahí: la
autoridad final entre el preset y ``-o:`` es T5-v2. Por eso el modelo de
resultado no tiene ``success: bool`` (ver el docstring del módulo) y por eso
``test_el_resultado_no_expone_un_success_booleano`` lo congela.

**Se enumera, no se muestrea** (``AGENTS.md``, "La regla que más se viola"). Tres
anclas estructurales por AST sostienen el invariante read-only, que es la
propiedad que separa T5A de la automatización de GUI:

1. ``test_la_superficie_no_nombra_primitivas_mutantes``: ningún identificador,
   atributo ni literal de los archivos de T5A puede ser una primitiva mutante de
   UIA/Win32 — y ``getattr``/``setattr``/``eval``/``exec`` también están
   prohibidos, porque son el hueco por el que una llamada mutante entraría sin
   que su nombre aparezca en el árbol.
2. ``test_el_protocolo_del_observador_esta_congelado``: los métodos declarados
   por ``ObservadorUIA`` se congelan por igualdad literal. Agregar ``click()`` al
   protocolo rompe el test aunque nadie lo llame todavía.
3. ``test_las_razones_estan_congeladas``: la familia de razones se congela por
   igualdad literal, para que una rama nueva del pipeline tenga que declarar su
   razón en vez de reciclar una existente.

Y una cuarta ancla en tiempo de ejecución: el observador espía de
``ObservadorEspia`` lleva métodos con nombre mutante que revientan si alguien los
llama, así que el guard no depende sólo de leer el AST.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import pathlib
import re
import subprocess
import sys
import tomllib
from unittest import mock

import pytest

from sky_claw.local.tools.dyndolod_uia_preflight import (
    _CARACTERES_RESERVADOS_WIN32,
    RAZONES_DE_UNKNOWN,
    TOOLS_OBSERVABLES,
    TOPE_DE_ELEMENTOS_UIA,
    ControlObservado,
    CriteriosDeControl,
    EnumeracionIncompletaError,
    EstadoPreflight,
    LocalizadorPsutil,
    ObservacionUIAError,
    ObservadorNoDisponible,
    ObservadorUIA,
    ProcesoObservado,
    RazonPreflight,
    SolicitudPreflightUIA,
    UIANoDisponibleError,
    VentanaObservada,
    canonicalizar_ruta_windows,
    exigir_enumeracion_completa,
    observador_por_defecto,
    observar_output,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
MODULO_T5A = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_preflight.py"
PROBE_T5A = RAIZ / "local_scripts" / "scripts" / "probe_dyndolod_uia_readonly.py"

#: Salida administrada que ``output_targets.dyndolod_output_target`` produce en
#: un rig Windows. Se escribe literal (y no se importa) porque el preflight
#: recibe la ruta esperada como INPUT: acoplarlo al resolver acá escondería que
#: T5A no depende de ninguna config global (§6 del encargo).
SALIDA_ADMINISTRADA = r"C:\Games\Skyrim Special Edition\Sky-Claw\DynDOLOD"


# ---------------------------------------------------------------------------
# Dobles de prueba
# ---------------------------------------------------------------------------


class LocalizadorFalso:
    """Localizador de procesos con una lista fija. Puede simular fallo del sensor."""

    def __init__(self, procesos, error=None):
        self._procesos = tuple(procesos)
        self._error = error

    def procesos(self):
        if self._error is not None:
            raise self._error
        return self._procesos


class ObservadorFalso:
    """Observador UIA con un árbol fijo por ventana.

    Cada método puede configurarse para lanzar, que es como se simulan los
    fallos COM / *stale element* del rig real sin tener rig.
    """

    def __init__(self, ventanas=(), controles=None, valores=None, error=None, error_en=None, ignorar_pid=False):
        self._ventanas = tuple(ventanas)
        # `ignorar_pid=True` modela un adaptador que devuelve de más. Sin él, el
        # doble filtraba por pid y el guard defensivo del pipeline nunca se
        # ejercía: el test pasaba por el filtro del DOBLE, no por el del código.
        self._ignorar_pid = ignorar_pid
        self._controles = controles or {}
        self._valores = valores or {}
        self._error = error
        self._error_en = error_en or set()

    def _quizas_fallar(self, metodo):
        if self._error is not None and metodo in self._error_en:
            raise self._error

    def ventanas_de_proceso(self, pid):
        self._quizas_fallar("ventanas_de_proceso")
        if self._ignorar_pid:
            return self._ventanas
        return tuple(v for v in self._ventanas if v.pid == pid)

    def controles_de_ventana(self, ventana):
        self._quizas_fallar("controles_de_ventana")
        return tuple(self._controles.get(ventana.handle, ()))

    def leer_valor(self, control):
        self._quizas_fallar("leer_valor")
        # La clave puede ser el `AutomationId` (el caso común, un control por
        # id) o la tupla COMPLETA que identifica al control. La tupla existe
        # para los tests donde dos controles comparten `AutomationId`: con la
        # clave simple el doble devolvía el MISMO valor para cualquiera de los
        # candidatos, así que el test no podía notar que el pipeline había
        # elegido el equivocado. Hallazgo de review (Qodo).
        compuesta = (control.automation_id, control.nombre, control.tipo_de_control, control.pid)
        if compuesta in self._valores:
            return self._valores[compuesta]
        return self._valores.get(control.automation_id)


class ObservadorEspia:
    """Envuelve un observador y registra QUÉ se le pidió.

    Los tres métodos mutantes de abajo no existen en el protocolo: están acá
    justamente para que, si alguna vez el pipeline intentara usarlos por
    duck-typing, el test reviente en vez de pasar en verde (T15).
    """

    def __init__(self, interno):
        self._interno = interno
        self.llamadas: list[str] = []

    def ventanas_de_proceso(self, pid):
        self.llamadas.append("ventanas_de_proceso")
        return self._interno.ventanas_de_proceso(pid)

    def controles_de_ventana(self, ventana):
        self.llamadas.append("controles_de_ventana")
        return self._interno.controles_de_ventana(ventana)

    def leer_valor(self, control):
        self.llamadas.append("leer_valor")
        return self._interno.leer_valor(control)

    def _prohibido(self, *_args, **_kwargs):
        raise AssertionError("T5A ejecutó una primitiva mutante sobre la GUI")

    set_value = _prohibido
    invoke = _prohibido
    send_keys = _prohibido


def _proceso(pid=4242, nombre="TexGenx64.exe", ruta=r"C:\Modding\DynDOLOD\TexGenx64.exe"):
    return ProcesoObservado(pid=pid, nombre_ejecutable=nombre, ruta_ejecutable=ruta)


def _ventana(pid=4242, handle="w1", titulo="TexGen 3.00", clase="TfrmMain"):
    return VentanaObservada(pid=pid, titulo=titulo, class_name=clase, handle=handle)


def _control(automation_id="edOutput", nombre="Output", tipo="Edit", pid=4242, clase="TEdit"):
    return ControlObservado(
        pid=pid,
        automation_id=automation_id,
        nombre=nombre,
        tipo_de_control=tipo,
        class_name=clase,
    )


CRITERIOS_OUTPUT = CriteriosDeControl(automation_id="edOutput", tipo_de_control="Edit")


def _solicitud(
    tool="TexGen",
    ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe",
    esperado=SALIDA_ADMINISTRADA,
    criterios=CRITERIOS_OUTPUT,
    pid=None,
):
    return SolicitudPreflightUIA(
        tool=tool,
        ejecutable_esperado=ejecutable,
        salida_administrada_esperada=esperado,
        criterios_del_control=criterios,
        pid=pid,
    )


def _observar(solicitud, procesos, ventanas=(), controles=None, valores=None, **kwargs):
    return observar_output(
        solicitud,
        localizador=LocalizadorFalso(procesos),
        observador=ObservadorFalso(ventanas=ventanas, controles=controles, valores=valores, **kwargs),
    )


# ---------------------------------------------------------------------------
# Matriz fail-closed T1..T15
# ---------------------------------------------------------------------------


def test_t1_texgen_una_ventana_output_inequivoco_y_ruta_coincidente_da_match():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE
    assert resultado.pid == 4242
    assert resultado.valor_observado == SALIDA_ADMINISTRADA


def test_t2_dyndolod_una_ventana_output_inequivoco_y_ruta_coincidente_da_match():
    resultado = _observar(
        _solicitud(tool="DynDOLOD", ejecutable=r"C:\Modding\DynDOLOD\DynDOLODx64.exe"),
        procesos=[_proceso(nombre="DynDOLODx64.exe", ruta=r"C:\Modding\DynDOLOD\DynDOLODx64.exe")],
        ventanas=[_ventana(titulo="DynDOLOD 3.00")],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.tool == "DynDOLOD"


def test_t3_output_observado_distinto_del_administrado_da_mismatch():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": r"D:\Modding\DynDOLOD\Output"},
    )
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE
    assert resultado.valor_observado_canonico == r"d:\modding\dyndolod\output"
    assert resultado.valor_esperado_canonico == SALIDA_ADMINISTRADA.lower()


def test_t4_control_de_output_ausente_da_unknown():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(automation_id="edInput", nombre="Data")]},
        valores={},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.CONTROL_NO_ENCONTRADO


def test_t5_dos_controles_plausibles_da_unknown_y_no_elige_el_primero():
    gemelos = [_control(), _control(nombre="Output folder")]
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": gemelos},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.CONTROL_AMBIGUO
    # El valor coincidía: si el pipeline hubiera desempatado por "el primero",
    # esto sería un MATCH falso. Es la mutación M3/M6 del encargo.
    assert resultado.valor_observado is None


def test_t6_dos_procesos_plausibles_da_unknown():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso(pid=1), _proceso(pid=2)],
        ventanas=[_ventana(pid=1)],
        controles={"w1": [_control(pid=1)]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PROCESO_AMBIGUO
    assert resultado.pid is None


def test_t6b_cero_procesos_candidatos_da_unknown():
    resultado = _observar(_solicitud(), procesos=[])
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PROCESO_NO_ENCONTRADO


def test_t7_pid_solicitado_que_no_corresponde_da_unknown():
    resultado = _observar(
        _solicitud(pid=9999),
        procesos=[_proceso(pid=4242)],
        ventanas=[_ventana(pid=4242)],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PID_NO_COINCIDE


def test_t7b_pid_solicitado_desempata_entre_dos_procesos_del_mismo_binario():
    resultado = _observar(
        _solicitud(pid=2),
        procesos=[_proceso(pid=1), _proceso(pid=2)],
        ventanas=[_ventana(pid=2)],
        controles={"w1": [_control(pid=2)]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.pid == 2


def test_t8_sin_patron_de_lectura_disponible_da_unknown():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={},  # leer_valor devuelve None: no hay ValuePattern/TextPattern
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VALOR_NO_LEIBLE


@pytest.mark.parametrize(
    "metodo",
    ["ventanas_de_proceso", "controles_de_ventana", "leer_valor"],
)
def test_t9_error_com_o_elemento_stale_da_unknown(metodo):
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
        error=ObservacionUIAError("stale element"),
        error_en={metodo},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ERROR_UIA


def test_t9b_uia_no_disponible_se_distingue_de_un_error_cualquiera():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        error=UIANoDisponibleError("sin backend"),
        error_en={"ventanas_de_proceso"},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE


def test_t9c_fallo_del_localizador_de_procesos_tambien_da_unknown():
    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([], error=ObservacionUIAError("psutil roto")),
        observador=ObservadorFalso(),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ERROR_UIA


@pytest.mark.parametrize(
    "observado",
    [
        "",
        "   ",
        r"..\DynDOLOD_Output",
        r"C:\Games\..\Games\Sky-Claw",
        r"Sky-Claw\DynDOLOD",
        "C:Sky-Claw",
        r"\Sky-Claw\DynDOLOD",
        "C:\\Sky-Claw\\Dyn\tDOLOD",
        r"\\?\C:\Games\Skyrim Special Edition\Sky-Claw\DynDOLOD",
    ],
)
def test_t10_ruta_observada_no_canonicalizable_da_unknown(observado):
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": observado},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.OBSERVADO_NO_CANONICALIZABLE


def test_t10b_ruta_esperada_no_canonicalizable_da_unknown_sin_tocar_uia():
    espia = ObservadorEspia(ObservadorFalso())
    resultado = observar_output(
        _solicitud(esperado=r"..\salida"),
        localizador=LocalizadorFalso([_proceso()]),
        observador=espia,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ESPERADO_NO_CANONICALIZABLE
    # La validación de la solicitud precede a toda observación: una solicitud
    # mal formada no debe generar tráfico UIA sobre la GUI del operador.
    assert espia.llamadas == []


@pytest.mark.parametrize(
    "observado",
    [
        SALIDA_ADMINISTRADA,
        SALIDA_ADMINISTRADA + "\\",
        SALIDA_ADMINISTRADA + "   ",
        SALIDA_ADMINISTRADA.replace("\\", "/"),
        SALIDA_ADMINISTRADA.replace("\\", "\\\\"),
        SALIDA_ADMINISTRADA.upper(),
        SALIDA_ADMINISTRADA.lower(),
        r"C:\Games\Skyrim Special Edition\.\Sky-Claw\DynDOLOD",
    ],
)
def test_t11_diferencias_legitimamente_normalizables_dan_match(observado):
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": observado},
    )
    assert resultado.estado is EstadoPreflight.MATCH


def test_t12_un_selector_que_apunta_a_otro_control_no_produce_match_falso():
    # El selector quedó rancio y ahora matchea el campo de entrada. El valor de
    # ESE control no es la salida administrada: el veredicto no puede ser MATCH.
    resultado = _observar(
        _solicitud(criterios=CriteriosDeControl(automation_id="edInput")),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(), _control(automation_id="edInput", nombre="Data")]},
        valores={"edOutput": SALIDA_ADMINISTRADA, "edInput": r"C:\Games\Skyrim Special Edition\Data"},
    )
    assert resultado.estado is not EstadoPreflight.MATCH
    assert resultado.estado is EstadoPreflight.MISMATCH


def test_t13_automation_id_vacio_se_resuelve_con_evidencia_alternativa_inequivoca():
    resultado = _observar(
        _solicitud(criterios=CriteriosDeControl(nombre="Output", tipo_de_control="Edit")),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={
            "w1": [
                _control(automation_id="", nombre="Output", tipo="Edit"),
                _control(automation_id="", nombre="Data", tipo="Edit"),
            ]
        },
        # Valores DISTINTOS por control: si el pipeline eligiera `Data`, leería
        # otra ruta y esto sería MISMATCH. Con un único valor para los dos, el
        # test daba MATCH eligiera el que eligiera y no probaba la selección.
        valores={
            ("", "Output", "Edit", 4242): SALIDA_ADMINISTRADA,
            ("", "Data", "Edit", 4242): r"C:\Games\Skyrim Special Edition\Sky-Claw\Otro",
        },
    )
    assert resultado.estado is EstadoPreflight.MATCH


def test_t13b_automation_id_vacio_sin_evidencia_alternativa_da_unknown():
    resultado = _observar(
        _solicitud(criterios=CriteriosDeControl(tipo_de_control="Edit")),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={
            "w1": [
                _control(automation_id="", nombre="Output", tipo="Edit"),
                _control(automation_id="", nombre="Data", tipo="Edit"),
            ]
        },
        valores={
            ("", "Output", "Edit", 4242): SALIDA_ADMINISTRADA,
            ("", "Data", "Edit", 4242): r"C:\Games\Skyrim Special Edition\Sky-Claw\Otro",
        },
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.CONTROL_AMBIGUO


def test_t14_automation_id_duplicado_fuera_del_proceso_no_afecta():
    # Mismo AutomationId en un control de OTRO proceso que el adapter devolvió
    # por error: la búsqueda es process-scoped, así que no crea ambigüedad.
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(), _control(pid=777)]},
        # El homónimo ajeno lee OTRA ruta: si el scoping por pid fallara y se
        # eligiera el del pid 777, el veredicto sería MISMATCH en vez de MATCH.
        # Con un solo valor para ambos, el test pasaba eligiera al que eligiera.
        valores={
            ("edOutput", "Output", "Edit", 4242): SALIDA_ADMINISTRADA,
            ("edOutput", "Output", "Edit", 777): r"C:\Games\Skyrim Special Edition\Sky-Claw\Ajeno",
        },
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.pid == 4242


def test_t14b_si_todos_los_candidatos_son_de_otro_proceso_da_unknown():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(pid=777)]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.CONTROL_FUERA_DEL_PROCESO


def test_t15_ninguna_operacion_mutante_se_ejecuta_en_el_camino_feliz():
    espia = ObservadorEspia(
        ObservadorFalso(
            ventanas=[_ventana()],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        )
    )
    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        observador=espia,
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert set(espia.llamadas) <= {"ventanas_de_proceso", "controles_de_ventana", "leer_valor"}


# ---------------------------------------------------------------------------
# Identidad de proceso y ventana (§9)
# ---------------------------------------------------------------------------


def test_dos_ventanas_top_level_del_mismo_proceso_dan_unknown():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana(handle="w1"), _ventana(handle="w2", titulo="TexGen — About")],
        controles={"w1": [_control()], "w2": []},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VENTANA_AMBIGUA


def test_proceso_sin_ventanas_da_unknown():
    resultado = _observar(_solicitud(), procesos=[_proceso()], ventanas=[])
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VENTANA_NO_ENCONTRADA


def test_el_ejecutable_esperado_se_compara_por_nombre_sin_importar_mayusculas():
    resultado = _observar(
        _solicitud(ejecutable="texgenx64.EXE"),
        procesos=[_proceso(nombre="TexGenx64.exe", ruta=None)],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.MATCH


def test_un_binario_homonimo_de_otra_instalacion_no_es_candidato():
    # Mismo nombre de exe, instalación distinta: si el llamador pidió una ruta
    # completa, la identidad se prueba contra la ruta completa.
    resultado = _observar(
        _solicitud(ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        procesos=[_proceso(ruta=r"D:\Otro\DynDOLOD\TexGenx64.exe")],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PROCESO_NO_ENCONTRADO


def test_una_ventana_de_otro_proceso_devuelta_por_el_adapter_se_descarta():
    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        # `ignorar_pid=True`: el adaptador devuelve la ventana de OTRO proceso,
        # así que la re-validación del pipeline es lo único que la puede frenar.
        observador=ObservadorFalso(
            ventanas=[_ventana(pid=777)],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
            ignorar_pid=True,
        ),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VENTANA_NO_ENCONTRADA


# ---------------------------------------------------------------------------
# Identidad del ejecutable: la ruta completa NO puede degradarse a basename
# ---------------------------------------------------------------------------
#
# Hallazgo de review (ronda 1) sobre el módulo productivo: cuando el llamador
# pedía una instalación concreta y el sensor no podía probar la identidad, el
# pipeline caía a comparar sólo el NOMBRE del binario y seguía adelante. La
# incertidumbre se convertía en una identidad más débil sin decirlo, y con eso
# un `MATCH` podía salir de un proceso que nadie demostró que fuera el pedido.
# El contrato dice lo contrario: identidad no demostrable → UNKNOWN.


def test_ruta_completa_pedida_y_exe_no_observable_da_unknown():
    # psutil no siempre puede leer `exe` (AccessDenied). Que no se pueda probar
    # la identidad no la convierte en probada por el basename.
    resultado = _observar(
        _solicitud(ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        procesos=[_proceso(ruta=None)],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.IDENTIDAD_NO_DEMOSTRABLE


def test_ruta_completa_pedida_no_canonicalizable_da_unknown_sin_tocar_uia():
    # Si la ruta que pide el llamador no se puede canonicalizar, la comparación
    # de identidad es imposible: es un defecto de la SOLICITUD y se corta antes
    # de generar tráfico UIA, igual que el resto de la validación de entrada.
    espia = ObservadorEspia(ObservadorFalso())
    resultado = observar_output(
        _solicitud(ejecutable=r"C:\Modding\..\DynDOLOD\TexGenx64.exe"),
        localizador=LocalizadorFalso([_proceso()]),
        observador=espia,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.EJECUTABLE_NO_CANONICALIZABLE
    assert espia.llamadas == []


def test_ruta_completa_pedida_y_exe_observado_no_canonicalizable_da_unknown():
    resultado = _observar(
        _solicitud(ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        procesos=[_proceso(ruta=r"..\TexGenx64.exe")],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.IDENTIDAD_NO_DEMOSTRABLE


def test_un_binario_homonimo_de_otra_instalacion_nunca_produce_match():
    # El caso más duro: se PUEDE probar que el proceso es de otra instalación.
    # Antes del fix esto daba MATCH.
    resultado = _observar(
        _solicitud(ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        procesos=[_proceso(ruta=r"D:\OtraInstalacion\TexGenx64.exe")],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is not EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.PROCESO_NO_ENCONTRADO


def test_un_basename_pedido_explicitamente_sigue_siendo_identidad_valida():
    # La identidad por nombre NO se prohíbe: se prohíbe llegar a ella por
    # degradación. Si el llamador pidió sólo el binario, es lo que pidió.
    resultado = _observar(
        _solicitud(ejecutable="TexGenx64.exe"),
        procesos=[_proceso(ruta=None)],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert any("sólo por nombre" in linea for linea in resultado.evidencia)


def test_un_pid_fijado_acota_la_prueba_de_identidad_a_ese_proceso():
    # Un tercero homónimo con `exe` ilegible no puede invalidar una observación
    # que el llamador ya ató a un pid concreto: la prueba se aplica DESPUÉS de
    # filtrar por pid, no antes.
    resultado = _observar(
        _solicitud(ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe", pid=4242),
        procesos=[_proceso(pid=4242), _proceso(pid=99, ruta=None)],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.pid == 4242


def test_un_homonimo_con_exe_ilegible_hace_ambigua_la_identidad_sin_pid():
    # Sin pid, ese mismo tercero SÍ importa: no se puede afirmar que hay
    # exactamente una instancia de la instalación pedida.
    resultado = _observar(
        _solicitud(ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        procesos=[_proceso(pid=4242), _proceso(pid=99, ruta=None)],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.IDENTIDAD_NO_DEMOSTRABLE


@pytest.mark.parametrize("ejecutable", ["", "   "])
def test_un_ejecutable_esperado_vacio_da_unknown(ejecutable):
    resultado = _observar(_solicitud(ejecutable=ejecutable), procesos=[_proceso()])
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.EJECUTABLE_NO_CANONICALIZABLE


# ---------------------------------------------------------------------------
# Enumeración incompleta: un recorte silencioso puede fabricar unicidad
# ---------------------------------------------------------------------------
#
# Segundo hallazgo de review: el adaptador cortaba la colección de UIA a un tope
# y devolvía el recorte como si fuera el árbol entero. Con dos controles que
# satisfacen el selector, uno dentro del tope y otro fuera, el decisor ve UN
# candidato, lo declara inequívoco y puede emitir un `MATCH` que el árbol real
# no sostiene. Evidencia parcial tratada como evidencia completa.
#
# El fix es que la incompletitud sea imposible de confundir con un resultado:
# `exigir_enumeracion_completa` LANZA en vez de recortar, así que ningún
# adaptador puede alimentar al decisor con una secuencia parcial por descuido.


def test_exigir_enumeracion_completa_acepta_justo_el_tope():
    assert exigir_enumeracion_completa(TOPE_DE_ELEMENTOS_UIA, contexto="controles") == TOPE_DE_ELEMENTOS_UIA


def test_exigir_enumeracion_completa_rechaza_uno_mas_que_el_tope():
    with pytest.raises(EnumeracionIncompletaError) as excinfo:
        exigir_enumeracion_completa(TOPE_DE_ELEMENTOS_UIA + 1, contexto="controles")
    # El diagnóstico tiene que decir cuánto se vio y cuánto había.
    assert str(TOPE_DE_ELEMENTOS_UIA) in str(excinfo.value)
    assert str(TOPE_DE_ELEMENTOS_UIA + 1) in str(excinfo.value)


def test_enumeracion_incompleta_es_un_error_uia():
    # Subclase, por el mismo motivo que UIANoDisponibleError: la red general del
    # pipeline la atrapa aunque nadie escriba un `except` específico.
    assert issubclass(EnumeracionIncompletaError, ObservacionUIAError)


@pytest.mark.parametrize("metodo", ["ventanas_de_proceso", "controles_de_ventana"])
def test_una_enumeracion_truncada_da_unknown_y_no_match(metodo):
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
        error=EnumeracionIncompletaError("713 elementos, tope 400"),
        error_en={metodo},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ENUMERACION_INCOMPLETA


def test_un_segundo_candidato_pasado_el_tope_no_puede_producir_match():
    """El caso adversarial exacto: 401 controles, matches en 0 y en 400.

    Se modela con un observador que enumera como debe hacerlo un adaptador
    correcto —consultando el total ANTES de recortar—, así que el escenario se
    reproduce sin necesitar Windows. Si el adaptador truncara en silencio, el
    decisor vería un único candidato y esto sería MATCH.
    """
    controles = [_control(automation_id="edOutput")]
    controles += [_control(automation_id=f"otro{i}") for i in range(TOPE_DE_ELEMENTOS_UIA - 1)]
    controles += [_control(automation_id="edOutput", nombre="Output (2)")]
    assert len(controles) == TOPE_DE_ELEMENTOS_UIA + 1

    class ObservadorQueEnumeraBien:
        def ventanas_de_proceso(self, pid):
            exigir_enumeracion_completa(1, contexto="ventanas")
            return (_ventana(),)

        def controles_de_ventana(self, ventana):
            exigir_enumeracion_completa(len(controles), contexto="controles")
            return tuple(controles)

        def leer_valor(self, control):
            return SALIDA_ADMINISTRADA

    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        observador=ObservadorQueEnumeraBien(),
    )
    assert resultado.estado is not EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.ENUMERACION_INCOMPLETA


def test_el_adaptador_del_probe_exige_enumeracion_completa():
    """Ancla estructural: el backend Windows no puede devolver un recorte.

    No se puede ejecutar COM acá, así que se verifica por AST que el método que
    materializa una colección de UIA llama a `exigir_enumeracion_completa`. Sin
    esto, el invariante viviría sólo en la revisión humana del adaptador, que es
    exactamente lo que dejó pasar el defecto la primera vez.
    """
    arbol = ast.parse(PROBE_T5A.read_text(encoding="utf-8"))
    materializadores = [
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "_elementos"
    ]
    assert materializadores, "el probe ya no tiene `_elementos`: revisá este ancla"
    for funcion in materializadores:
        llamadas = {
            hijo.func.id for hijo in ast.walk(funcion) if isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Name)
        }
        assert "exigir_enumeracion_completa" in llamadas
        # Validar el total y DESPUÉS materializar con el recorte de diagnóstico
        # pasaba las dos anclas mientras un veredicto salía de evidencia
        # parcial. Verificado: la suite quedaba entera en verde con esa
        # mutación. Hallazgo de review (Qodo).
        usadas = {hijo.attr for hijo in ast.walk(funcion) if isinstance(hijo, ast.Attribute)}
        assert "_elementos_truncados" not in usadas, (
            "`_elementos` materializa con el recorte de diagnóstico: validar el total no alcanza "
            "si después se devuelve una colección recortada"
        )


def test_los_metodos_del_protocolo_no_usan_la_enumeracion_truncada():
    """El ancla anterior ataba el HELPER, no el call site — y eso no alcanza.

    Hallazgo de review (Qodo): verificar que `_elementos` valida la completitud
    deja intacto el hueco de cambiar el cuerpo de `ventanas_de_proceso` o
    `controles_de_ventana` para que llamen a `_elementos_truncados`. El helper
    seguiría siendo correcto y el ancla seguiría verde mientras un recorte
    alimenta el veredicto. Es la misma forma de defecto que este PR viene
    corrigiendo: probar la pieza en vez del camino.

    `controles_para_volcado` es la ÚNICA que puede truncar: es diagnóstico, lo
    anuncia con `TRUNCATED:` y no alimenta ninguna decisión.
    """
    arbol = ast.parse(PROBE_T5A.read_text(encoding="utf-8"))
    metodos = {nodo.name: nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef)}
    for nombre in METODOS_DEL_OBSERVADOR:
        assert nombre in metodos, f"el probe ya no implementa {nombre}: revisá este ancla"
        usadas = {hijo.attr for hijo in ast.walk(metodos[nombre]) if isinstance(hijo, ast.Attribute)}
        assert "_elementos_truncados" not in usadas, (
            f"{nombre} materializa con el recorte de diagnóstico: un veredicto no puede salir de evidencia parcial"
        )

    # Y el diagnóstico sigue siendo el único que puede truncar.
    volcado = metodos.get("controles_para_volcado")
    assert volcado is not None
    assert "_elementos_truncados" in {h.attr for h in ast.walk(volcado) if isinstance(h, ast.Attribute)}


def test_el_mapa_de_control_types_esta_congelado_contra_los_ids_reales():
    """Igualdad literal contra los ids verificados en los headers de UIA.

    Hallazgo de review (Qodo): la primera versión cruzaba `50007`/`50008` y no
    tenía `Document`. Verificado contra `uiautomation` 2.0.29 (`class
    ControlType`): ListItem=50007, List=50008, Document=50030. Se congela porque
    un id mal traducido induce un selector equivocado, y el volcado existe
    justamente para que el selector NO se escriba a ojo.
    """
    assert _cargar_la_sonda().NOMBRES_DE_CONTROL_TYPE == {
        50000: "Button",
        50003: "ComboBox",
        50004: "Edit",
        50005: "Hyperlink",
        50007: "ListItem",
        50008: "List",
        50020: "Text",
        50026: "Group",
        50030: "Document",
        50032: "Window",
        50033: "Pane",
    }


# ---------------------------------------------------------------------------
# Validación de la solicitud (fail-closed antes de tocar la GUI)
# ---------------------------------------------------------------------------


def test_una_tool_fuera_de_texgen_dyndolod_da_unknown():
    resultado = _observar(_solicitud(tool="LOOT"), procesos=[_proceso()])
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.TOOL_DESCONOCIDA


def test_un_selector_sin_ningun_criterio_da_unknown():
    # Un selector vacío matchea TODOS los controles: con una sola caja de texto
    # en la ventana daría un MATCH que no prueba nada. Fail-closed antes de mirar.
    resultado = _observar(
        _solicitud(criterios=CriteriosDeControl()),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.SELECTOR_SIN_CRITERIOS


def test_las_tools_observables_estan_congeladas():
    assert set(TOOLS_OBSERVABLES) == {"TexGen", "DynDOLOD"}


# ---------------------------------------------------------------------------
# Canonicalización de rutas Windows (§13)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        (r"C:\Sky-Claw", r"c:\sky-claw"),
        (r"C:\Sky-Claw\\", r"c:\sky-claw"),
        ("C:/Sky-Claw/DynDOLOD", r"c:\sky-claw\dyndolod"),
        (r"C:\\Sky-Claw\\\DynDOLOD", r"c:\sky-claw\dyndolod"),
        ("C:\\Sky-Claw  ", r"c:\sky-claw"),  # sólo el final: el líder ahora rechaza
        (r"c:\SKY-CLAW", r"c:\sky-claw"),
        ("C:\\", "c:\\"),
        ("C:/", "c:\\"),
        (r"C:\Sky-Claw\.\DynDOLOD", r"c:\sky-claw\dyndolod"),
        (r"\\servidor\recurso\Sky-Claw", r"\\servidor\recurso\sky-claw"),
        ("//servidor/recurso/Sky-Claw", r"\\servidor\recurso\sky-claw"),
    ],
)
def test_canonicalizacion_de_rutas_validas(crudo, esperado):
    assert canonicalizar_ruta_windows(crudo) == esperado


@pytest.mark.parametrize(
    "crudo",
    [
        None,
        "",
        "    ",
        "Sky-Claw",  # relativa
        "C:Sky-Claw",  # relativa a la unidad
        r"\Sky-Claw",  # enraizada sin unidad
        r"C:\Sky-Claw\..\Otro",  # `..` no se resuelve sin tocar el filesystem
        r"..\Sky-Claw",
        r"\\servidor",  # UNC incompleta: falta el recurso
        "\\\\",
        r"\\?\C:\Sky-Claw",  # ruta extendida: equivale a `C:\…` pero no se afirma
        "\\\\.\\PhysicalDrive0",
        "1:\\Sky-Claw",  # unidad inválida
        "C:\\Sky\x00Claw",
        "C:\\Sky\nClaw",
        # Caracteres que Win32 prohíbe en un componente de ruta. Una ruta que el
        # sistema no puede interpretar no tiene forma canónica: el veredicto
        # honesto es UNKNOWN, no una comparación de cadenas en minúsculas.
        r"C:\Sky-Claw?",
        r"C:\Sky*Claw",
        r"C:\Sky|Claw",
        r"C:\Sky<Claw",
        r"C:\Sky>Claw",
        'C:\\Sky"Claw',
        # `:` sólo es legal en la unidad. Un componente interior con `:` es un
        # Alternate Data Stream o basura, no un directorio.
        r"C:\x\D:",
        r"C:\x\a:b",
        # Un espacio LÍDER no es formato neutro: rompe que la ruta sea
        # absoluta a la unidad, así que Win32 la resuelve como relativa o la
        # rechaza. Es un destino distinto, no el mismo escrito de otra forma.
        "  C:\\Sky-Claw",
        "\tC:\\Sky-Claw",
    ],
)
def test_rutas_que_no_se_pueden_canonicalizar(crudo):
    assert canonicalizar_ruta_windows(crudo) is None


def test_el_espacio_final_si_es_neutro_pero_el_lider_no():
    """La asimetría es de Win32, no una preferencia: los espacios FINALES los
    recorta la normalización de rutas del sistema, los LÍDERES no.

    Hallazgo de review (Codex, P2). Antes, `strip()` borraba los dos y
    `" C:\\Games\\Output"` daba MATCH contra `"C:\\Games\\Output"` — un MATCH
    falso, porque con el espacio adelante la ruta ni siquiera es absoluta a la
    unidad y el destino real sería otro.
    """
    assert canonicalizar_ruta_windows(r"C:\Sky-Claw   ") == r"c:\sky-claw"
    assert canonicalizar_ruta_windows(r"   C:\Sky-Claw") is None


def test_un_output_con_espacio_lider_no_produce_match():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": " " + SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.OBSERVADO_NO_CANONICALIZABLE


# ---------------------------------------------------------------------------
# Criterios en blanco: "puse un criterio" vs. "puse evidencia"
# ---------------------------------------------------------------------------
#
# Hallazgo de review (Codex, P2). `--automation-id ""` desde la CLI construía un
# selector que `esta_vacio()` daba por válido, aunque un AutomationId vacío no
# identifica nada. Si la ventana tenía exactamente un control con AutomationId
# vacío, quedaba elegido sobre evidencia nula y podía salir MATCH.


@pytest.mark.parametrize("blanco", ["", "   ", "\t"])
def test_un_criterio_en_blanco_no_cuenta_como_criterio(blanco):
    assert CriteriosDeControl(automation_id=blanco).esta_vacio()
    assert CriteriosDeControl(nombre=blanco).esta_vacio()
    assert CriteriosDeControl(tipo_de_control=blanco).esta_vacio()


def test_un_selector_solo_con_blancos_da_unknown_y_no_elige_nada():
    resultado = _observar(
        _solicitud(criterios=CriteriosDeControl(automation_id="")),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(automation_id="", nombre="Output")]},
        valores={"": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.SELECTOR_SIN_CRITERIOS


def test_un_blanco_junto_a_un_criterio_real_no_invalida_al_real():
    # El blanco se ignora; el criterio con evidencia sigue mandando.
    resultado = _observar(
        _solicitud(criterios=CriteriosDeControl(automation_id="  ", nombre="Output")),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(), _control(automation_id="edInput", nombre="Data")]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.MATCH


def test_la_canonicalizacion_no_toca_el_filesystem(tmp_path, monkeypatch):
    r"""Se INTERCEPTA el acceso a disco; no se infiere de que el resultado sea igual.

    Hallazgo de review (Qodo). La versión anterior comparaba
    `canonicalizar_ruta_windows(r"Z:\no\existe")` contra la cadena esperada y
    después miraba que no se hubiera CREADO nada. Las dos aserciones pasaban
    igual con una implementación que usara `Path.resolve(strict=False)` o
    `normpath` —para ese input devuelven exactamente lo mismo— así que el test
    no podía fallar por el defecto que decía cubrir: una comparación que
    dependiera del estado del disco del rig.

    Ahora se registran las primitivas que usaría una implementación no textual.
    Si la canonicalización toca alguna, el test lo dice con nombre y propio.
    """
    monkeypatch.chdir(tmp_path)

    tocadas: list[str] = []

    def _espiar(nombre, original):
        def _envuelto(*args, **kwargs):
            tocadas.append(nombre)
            return original(*args, **kwargs)

        return _envuelto

    for modulo, atributos in (
        (os.path, ("realpath", "exists", "abspath", "isdir", "isfile", "islink", "normpath")),
        (os, ("stat", "lstat", "listdir", "getcwd")),
        (pathlib.Path, ("resolve", "exists", "is_dir", "is_file", "stat", "absolute")),
    ):
        for atributo in atributos:
            original = getattr(modulo, atributo, None)
            if original is not None:
                monkeypatch.setattr(modulo, atributo, _espiar(f"{modulo}.{atributo}", original))

    assert canonicalizar_ruta_windows(r"Z:\no\existe") == r"z:\no\existe"
    assert not tocadas, f"la canonicalización tocó el filesystem: {sorted(set(tocadas))}"


# ---------------------------------------------------------------------------
# Contrato del resultado (§14)
# ---------------------------------------------------------------------------


def test_el_resultado_no_expone_un_success_booleano():
    # `AGENTS.md` exige `success`/`message` a TODO TOOL nuevo. T5A no es un tool:
    # no está cableado en ningún dispatcher (§21 del encargo) y `success=True`
    # sobre un preflight de GUI se leería como "DynDOLOD escribirá bien", que es
    # exactamente lo que T5A NO puede afirmar. El estado va en un enum explícito.
    resultado = _observar(_solicitud(), procesos=[])
    campos = set(vars(resultado))
    assert "success" not in campos
    # `vars()` NO ve una `@property`: con sólo esa comprobación, exponer
    # `success` como property dejaba el ancla en verde y el contrato roto.
    # Hallazgo de review (Qodo). `hasattr` cubre las dos formas.
    assert not hasattr(resultado, "success")
    assert isinstance(resultado.estado, EstadoPreflight)


def test_el_resultado_conserva_evidencia_para_diagnostico():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": r"D:\otro"},
    )
    assert resultado.tool == "TexGen"
    assert resultado.pid == 4242
    assert resultado.ventana is not None
    assert resultado.valor_observado == r"D:\otro"
    assert resultado.valor_esperado == SALIDA_ADMINISTRADA
    assert resultado.evidencia  # descriptores de los candidatos considerados
    assert resultado.detalle


def test_el_resultado_es_inmutable():
    resultado = _observar(_solicitud(), procesos=[])
    with pytest.raises((AttributeError, TypeError)):
        resultado.estado = EstadoPreflight.MATCH  # type: ignore[misc]


def test_todo_unknown_declara_una_razon_de_la_familia_de_unknown():
    resultado = _observar(_solicitud(), procesos=[])
    assert resultado.razon in RAZONES_DE_UNKNOWN


def test_las_razones_estan_congeladas():
    # Igualdad literal: una rama nueva del pipeline tiene que declarar su razón
    # acá, no reciclar una existente que diga otra cosa en el diagnóstico.
    assert {r.value for r in RazonPreflight} == {
        "OUTPUT_COINCIDE",
        "OUTPUT_DIFIERE",
        "TOOL_DESCONOCIDA",
        "SELECTOR_SIN_CRITERIOS",
        "ESPERADO_NO_CANONICALIZABLE",
        "EJECUTABLE_NO_CANONICALIZABLE",
        "UIA_UNAVAILABLE",
        "PROCESO_NO_ENCONTRADO",
        "PROCESO_AMBIGUO",
        "PID_NO_COINCIDE",
        "IDENTIDAD_NO_DEMOSTRABLE",
        # La identidad se probó bien y DESPUÉS dejó de valer: el pid se recicló
        # mientras se observaba. Es una razón propia y no un caso de
        # IDENTIDAD_NO_DEMOSTRABLE porque el diagnóstico es distinto — ahí nunca
        # se pudo probar, acá se probó y venció.
        "IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION",
        "PID_NO_OBSERVABLE",
        "VENTANA_NO_ENCONTRADA",
        "VENTANA_AMBIGUA",
        "CONTROL_NO_ENCONTRADO",
        "CONTROL_AMBIGUO",
        "CONTROL_FUERA_DEL_PROCESO",
        "VALOR_NO_LEIBLE",
        "ENUMERACION_INCOMPLETA",
        "ERROR_UIA",
        "OBSERVADO_NO_CANONICALIZABLE",
    }
    assert set(RAZONES_DE_UNKNOWN) == set(RazonPreflight) - {
        RazonPreflight.OUTPUT_COINCIDE,
        RazonPreflight.OUTPUT_DIFIERE,
    }


# ---------------------------------------------------------------------------
# Backend por defecto y seguridad de import en Linux (§19)
# ---------------------------------------------------------------------------


def test_el_observador_por_defecto_falla_cerrado_en_vez_de_adivinar():
    observador = observador_por_defecto()
    assert isinstance(observador, ObservadorNoDisponible)
    with pytest.raises(UIANoDisponibleError):
        observador.ventanas_de_proceso(1)
    with pytest.raises(UIANoDisponibleError):
        observador.controles_de_ventana(_ventana())
    with pytest.raises(UIANoDisponibleError):
        observador.leer_valor(_control())


def test_el_pipeline_con_el_observador_por_defecto_da_unknown_no_error():
    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        observador=observador_por_defecto(),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE


def test_uia_no_disponible_es_un_error_uia():
    # El pipeline atrapa `ObservacionUIAError` como red general; si `UIANoDisponibleError` no
    # fuera subclase, una plataforma sin backend escaparía del fail-closed.
    assert issubclass(UIANoDisponibleError, ObservacionUIAError)


def test_el_modulo_no_importa_nada_de_windows():
    # El import del paquete tiene que ser seguro en el CI de Ubuntu: sin COM,
    # sin UIAutomationCore, sin escritorio interactivo.
    arbol = ast.parse(MODULO_T5A.read_text(encoding="utf-8"))
    importados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            importados.add(nodo.module.split(".")[0])
    prohibidos = {"comtypes", "ctypes", "win32api", "win32gui", "win32process", "pywinauto", "uiautomation", "winreg"}
    assert not (importados & prohibidos)


def test_el_localizador_psutil_enumera_procesos_sin_mutarlos():
    # Sensor real (psutil ya es dependencia del repo) y multiplataforma: la
    # identidad de proceso de §9 no necesita nada Windows-only.
    procesos = LocalizadorPsutil().procesos()
    assert any(p.pid == __import__("os").getpid() for p in procesos)
    assert all(isinstance(p, ProcesoObservado) for p in procesos)


# ---------------------------------------------------------------------------
# Anclas estructurales del invariante READ-ONLY (§8)
# ---------------------------------------------------------------------------

#: Primitivas que MUTAN la GUI o inyectan input. Ninguna puede aparecer como
#: identificador, atributo o literal de cadena en los archivos de T5A.
#:
#: `getattr`/`setattr`/`eval`/`exec` están en la lista aunque no muten nada por sí
#: solos: son el despacho dinámico que dejaría llamar a cualquiera de las de
#: arriba sin que su nombre aparezca en el árbol sintáctico. Sin ellos, la
#: enumeración es completa; con ellos, es una muestra.
PRIMITIVAS_MUTANTES: frozenset[str] = frozenset(
    {
        # Patrones de control de UIA (nombres COM).
        "SetValue",
        "Invoke",
        "Toggle",
        "Select",
        "AddToSelection",
        "RemoveFromSelection",
        "Expand",
        "Collapse",
        "Scroll",
        "ScrollIntoView",
        "SetFocus",
        "Move",
        "Resize",
        "Rotate",
        "Close",
        "SetWindowVisualState",
        "RealizeVirtualizedItem",
        "SetDockPosition",
        "SetRangeValue",
        # Envoltorios de Python (pywinauto / uiautomation).
        "click",
        "Click",
        "click_input",
        "double_click",
        "DoubleClick",
        "right_click",
        "RightClick",
        "middle_click",
        "MiddleClick",
        "invoke",
        "toggle",
        "select",
        "expand",
        "collapse",
        "set_focus",
        "set_text",
        "set_edit_text",
        "set_value",
        "SetText",
        "SetEditText",
        "send_keys",
        "SendKeys",
        "type_keys",
        "TypeKeys",
        "press",
        "press_keys",
        # Inyección de input y manipulación de ventanas por Win32.
        "keybd_event",
        "mouse_event",
        "SendInput",
        "SetWindowText",
        "SetWindowTextW",
        "PostMessage",
        "SendMessage",
        "SetForegroundWindow",
        "ShowWindow",
        # Despacho dinámico que evadiría la enumeración de arriba.
        "getattr",
        "setattr",
        "delattr",
        "eval",
        "exec",
    }
)

#: Los tres únicos métodos que ``ObservadorUIA`` puede declarar. Congelado por
#: igualdad literal: agregar uno mutante rompe el ancla aunque nadie lo llame.
METODOS_DEL_OBSERVADOR: tuple[str, ...] = (
    "ventanas_de_proceso",
    "controles_de_ventana",
    "leer_valor",
)

ARCHIVOS_DE_T5A = (MODULO_T5A, PROBE_T5A)


def _docstrings(arbol: ast.AST) -> set[int]:
    """Ids de los nodos `Constant` que son docstring (prosa, no código)."""
    ids = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            cuerpo = getattr(nodo, "body", [])  # noqa: B009 -- ast.AST no tipa `body`
            if cuerpo and isinstance(cuerpo[0], ast.Expr) and isinstance(cuerpo[0].value, ast.Constant):
                ids.add(id(cuerpo[0].value))
    return ids


@pytest.mark.parametrize("archivo", ARCHIVOS_DE_T5A, ids=lambda p: p.name)
def test_la_superficie_no_nombra_primitivas_mutantes(archivo):
    assert archivo.exists(), f"falta {archivo}"
    fuente = archivo.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    docs = _docstrings(arbol)
    encontrados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            encontrados.add(nodo.attr)
        elif isinstance(nodo, ast.Name):
            encontrados.add(nodo.id)
        elif isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            encontrados.add(nodo.name)
        elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and id(nodo) not in docs:
            encontrados.add(nodo.value)
    assert not (encontrados & PRIMITIVAS_MUTANTES), (
        f"{archivo.name} nombra primitivas mutantes: {sorted(encontrados & PRIMITIVAS_MUTANTES)}"
    )


def test_el_protocolo_del_observador_esta_congelado():
    arbol = ast.parse(MODULO_T5A.read_text(encoding="utf-8"))
    declarados = None
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == "ObservadorUIA":
            declarados = tuple(
                hijo.name
                for hijo in nodo.body
                if isinstance(hijo, ast.FunctionDef | ast.AsyncFunctionDef) and not hijo.name.startswith("_")
            )
    assert declarados == METODOS_DEL_OBSERVADOR


def test_el_pipeline_solo_llama_a_los_metodos_del_protocolo():
    # Complemento en runtime del ancla por AST: el espía registra qué se pidió.
    espia = ObservadorEspia(
        ObservadorFalso(
            ventanas=[_ventana()],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        )
    )
    observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        observador=espia,
    )
    assert set(espia.llamadas) <= set(METODOS_DEL_OBSERVADOR)


def test_el_backend_windows_vive_fuera_del_paquete():
    """El probe puede importar `sky_claw`; `sky_claw` NO puede importar al probe.

    Esa es la propiedad que mantiene el paquete importable en el CI de Ubuntu sin
    COM y que deja la decisión de dependencia UIA para cuando haya evidencia. Se
    verifica por enumeración de TODO el paquete, no con un caso: un import nuevo
    desde cualquier módulo rompe el ancla.
    """
    assert PROBE_T5A.exists()
    assert not PROBE_T5A.is_relative_to(RAIZ / "sky_claw")

    modulo_del_probe = PROBE_T5A.stem
    culpables = []
    for archivo in (RAIZ / "sky_claw").rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            nombres = []
            if isinstance(nodo, ast.Import):
                nombres = [alias.name for alias in nodo.names]
            elif isinstance(nodo, ast.ImportFrom):
                nombres = [nodo.module or ""]
            if any(modulo_del_probe in nombre for nombre in nombres):
                culpables.append(str(archivo.relative_to(RAIZ)))
    assert not culpables, f"el paquete importa el probe desde {culpables}"


def test_el_probe_no_declara_dependencia_uia_en_los_manifests():
    """Ninguna dependencia UIA entró al repo: la decision espera la evidencia.

    `comtypes` se importa perezosamente DENTRO del probe (§7: no se toma una
    dependencia por costumbre, y menos una que sólo sirve a un diagnóstico).
    Si algún día entra al runtime tiene que ser una decisión explícita que
    rompa este ancla, no un `pip install` que se cuele en un lockfile.
    """
    manifest = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    proyecto = manifest.get("project", {})
    # TODAS las secciones de dependencias, no sólo la principal: cortar el texto
    # antes de `[project.optional-dependencies]` dejaba entrar un `comtypes` por
    # la puerta de al lado (hallazgo de review, Qodo).
    declaradas = list(proyecto.get("dependencies", []))
    for extra, paquetes in proyecto.get("optional-dependencies", {}).items():
        declaradas.extend(f"{extra}:{paquete}" for paquete in paquetes)
    for paquete in ("comtypes", "pywinauto", "uiautomation", "pywin32"):
        culpables = [d for d in declaradas if paquete in d.lower()]
        assert not culpables, f"{paquete} entró a las dependencias sin decisión: {culpables}"


def test_el_banner_de_la_sonda_sanea_la_ruta_del_ejecutable():
    """La primera línea no puede filtrar lo que el resto del volcado redacta.

    Hallazgo de review (Codex, P2). El banner imprimía `--exe` crudo, y una
    instalación bajo el perfil del operador lleva su nombre de usuario en la
    ruta — justo lo que `_sanear` existe para no pegar en un PR.
    """
    entorno = dict(os.environ, USERPROFILE=r"C:\Users\operador")
    completado = subprocess.run(  # noqa: S603 -- ruta del intérprete y del script, ambas del repo
        [
            sys.executable,
            str(PROBE_T5A),
            "--tool",
            "TexGen",
            "--exe",
            r"C:\Users\operador\Modding\DynDOLOD\TexGenx64.exe",
        ],
        capture_output=True,
        text=True,
        env=entorno,
        timeout=60,
        check=False,
    )
    assert "operador" not in completado.stdout, completado.stdout
    assert "<USERPROFILE>" in completado.stdout, completado.stdout


def _cargar_la_sonda():
    """Carga el probe por ruta: vive fuera del paquete y no es importable normal."""
    spec = importlib.util.spec_from_file_location("probe_t5a", PROBE_T5A)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.mark.parametrize(
    "texto",
    [
        r"C:\Games\badminton\out",
        "DynDOLOD administrator mode",
        r"C:\Modding\admin-tools\TexGen",
        "Administración de DynDOLOD",
    ],
)
def test_el_saneo_no_corrompe_texto_que_solo_contiene_el_usuario_como_substring(texto, monkeypatch):
    """Sanear de más rompe justo aquello para lo que existe el volcado.

    Hallazgo de review (Qodo). Con `USERNAME=Admin`, reemplazar por substring
    convertía `Administración` en `<USERNAME>istración` y `badminton` en
    `b<USERNAME>ton`: el árbol que hay que leer para ELEGIR el selector T5A
    quedaba ilegible, y podía inducir criterios equivocados.
    """
    monkeypatch.setenv("USERNAME", "Admin")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Admin")
    assert _cargar_la_sonda()._sanear(texto) == texto


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        (r"C:\Users\Admin\Modding\TexGenx64.exe", r"<USERPROFILE>\Modding\TexGenx64.exe"),
        (r"C:\Users\Admin", "<USERPROFILE>"),
        (r"D:\mods\Admin\out", r"D:\mods\<USERNAME>\out"),
        (r"c:\users\admin\x", r"<USERPROFILE>\x"),
    ],
)
def test_el_saneo_si_redacta_componentes_completos(texto, esperado, monkeypatch):
    """Componente completo o prefijo de ruta: ahí sí hay que redactar.

    El último caso cubre que las rutas de Windows no distinguen mayúsculas: no
    redactar por diferencia de caso sería una fuga.
    """
    monkeypatch.setenv("USERNAME", "Admin")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Admin")
    assert _cargar_la_sonda()._sanear(texto) == esperado


def test_la_sonda_traduce_un_fallo_del_sensor_de_procesos_en_vez_de_reventar():
    """Un traceback en el rig no es evidencia pegable en un PR.

    Hallazgo de review (Qodo): `main()` llamaba a `procesos()` sin borde. Un
    proceso que muere a mitad de la enumeración hace fallar a psutil, y
    `LocalizadorPsutil` lo traduce a `ObservacionUIAError`; sin atajarlo, el CLI
    reventaba justo donde el resto responde con diagnóstico y código de salida.
    """
    sonda = _cargar_la_sonda()
    with mock.patch.object(
        sonda.LocalizadorPsutil,
        "procesos",
        side_effect=ObservacionUIAError("psutil roto"),
    ):
        codigo = sonda.main(["--tool", "TexGen", "--exe", "TexGenx64.exe"])
    assert codigo == 4


def test_los_codigos_de_salida_de_la_sonda_estan_congelados():
    """Cada corte del CLI tiene su código, y son distinguibles entre sí.

    Un operador (o un script del rig) distingue "no hay nada abierto" de "no
    puedo observar" de "el sensor se rompió" por el código, no por el texto.
    """
    fuente = PROBE_T5A.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    principal = next(nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "main")
    codigos = {
        nodo.value.value
        for nodo in ast.walk(principal)
        if isinstance(nodo, ast.Return) and isinstance(nodo.value, ast.Constant)
    }
    assert codigos == {0, 2, 3, 4}


# ---------------------------------------------------------------------------
# Un pid ilegible no prueba que el control sea ajeno
# ---------------------------------------------------------------------------
#
# Hallazgo de review (Qodo). El filtro `control.pid == proceso.pid` descartaba
# como AJENOS los controles cuyo pid es negativo — que es el centinela que usa el
# adaptador cuando UIA no puede leer `ProcessId`. Con dos controles que satisfacen
# el selector, uno legible y otro no, el decisor veía UNO, lo declaraba inequívoco
# y podía emitir MATCH aunque el árbol real tuviera dos y el Output verdadero
# fuese el ilegible.
#
# Es exactamente la misma forma que la degradación de identidad del ejecutable:
# convertir "no pude probarlo" en "queda descartado". Un pid ilegible no prueba
# ajenidad; prueba que la unicidad no se puede afirmar.


def test_un_control_con_pid_ilegible_junto_a_otro_valido_da_unknown():
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(), _control(pid=-1)]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PID_NO_OBSERVABLE


def test_un_control_unico_con_pid_ilegible_tambien_da_unknown():
    """Aunque sea el único: no se puede afirmar que pertenezca a este proceso."""
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(pid=-1)]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PID_NO_OBSERVABLE


def test_una_ventana_con_pid_ilegible_da_unknown():
    """Mismo patrón en la resolución de ventana: el filtro las escondía igual."""
    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        observador=ObservadorFalso(
            ventanas=[_ventana(), _ventana(pid=-1, handle="w2")],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
            ignorar_pid=True,
        ),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PID_NO_OBSERVABLE


# ---------------------------------------------------------------------------
# pid == 0: el otro valor que significa "no lo pude leer"
# ---------------------------------------------------------------------------
#
# Hallazgo de review (Qodo), y es la mitad que faltaba del fix anterior. UIA
# devuelve `ProcessId = 0` cuando el provider no lo expone —elementos que no
# están basados en HWND—, y el adaptador lo propagaba tal cual: sólo mapeaba a
# `PID_ILEGIBLE` los casos en que la conversión a entero fallaba. Un `0` no es
# negativo, así que se colaba por el guard y lo descartaba el filtro
# `pid == proceso.pid` como si fuera de otro proceso.
#
# El resultado era exactamente la unicidad fabricada que `PID_NO_OBSERVABLE`
# existe para prevenir: dos candidatos, uno con pid 0, veredicto MATCH.


@pytest.mark.parametrize("pid_ilegible", [0, -1, -99])
def test_ningun_pid_no_positivo_cuenta_como_identidad(pid_ilegible):
    """0 y los negativos significan lo mismo: no se pudo leer."""
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control(), _control(pid=pid_ilegible)]},
        valores={"edOutput": SALIDA_ADMINISTRADA},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PID_NO_OBSERVABLE


@pytest.mark.parametrize("pid_ilegible", [0, -1])
def test_ninguna_ventana_con_pid_no_positivo_cuenta_como_identidad(pid_ilegible):
    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        observador=ObservadorFalso(
            ventanas=[_ventana(), _ventana(pid=pid_ilegible, handle="w2")],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
            ignorar_pid=True,
        ),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.PID_NO_OBSERVABLE


# ---------------------------------------------------------------------------
# Anclas de los dos guards que este PR agregó
# ---------------------------------------------------------------------------
#
# Los dos hallazgos de review que arreglan estas anclas son la MISMA forma que
# `AGENTS.md` llama "la regla que más se viola": una regla, dos lugares donde
# aplicarla. El pid ilegible se chequea en la ventana Y en el control; los
# reservados de Win32 valen para todos los componentes de la ruta. Un caso
# escrito a mano para el hermano que faltaba no ataja al tercero, así que las
# anclas enumeran: una recorre el AST del módulo entero, la otra deriva sus
# casos de la constante.


def test_ningun_guard_compara_un_pid_contra_un_literal_fuera_del_predicado():
    """La frontera "pid legible" vive en `_pid_es_legible` y en ningún otro lado.

    El bug era `pid < 0` escrito dos veces: cerraba sobre `PID_ILEGIBLE` (-1) y
    dejaba pasar el `0` que UIA devuelve para elementos no basados en HWND. Un
    test por caso arregla los dos guards de hoy; esta ancla impide que un guard
    NUEVO —de un tercer tipo de elemento— vuelva a escribir el umbral a mano.

    Comparar un pid contra OTRO pid (`control.pid == proceso.pid`) es lo que hace
    el filtro de pertenencia y no lo toca: lo prohibido es el literal.
    """
    arbol = ast.parse(MODULO_T5A.read_text(encoding="utf-8"))

    def menciona_pid(nodo):
        return (isinstance(nodo, ast.Name) and nodo.id == "pid") or (
            isinstance(nodo, ast.Attribute) and nodo.attr == "pid"
        )

    def es_literal_numerico(nodo):
        return isinstance(nodo, ast.Constant) and isinstance(nodo.value, int)

    culpables = set()
    for funcion in ast.walk(arbol):
        if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for nodo in ast.walk(funcion):
            if not isinstance(nodo, ast.Compare):
                continue
            lados = [nodo.left, *nodo.comparators]
            if any(map(menciona_pid, lados)) and any(map(es_literal_numerico, lados)):
                culpables.add(funcion.name)

    assert culpables == {"_pid_es_legible"}, (
        f"comparan un pid contra un literal fuera del predicado: {sorted(culpables - {'_pid_es_legible'})}"
    )


def test_los_reservados_de_win32_estan_congelados():
    """Igualdad literal: sacar un carácter del conjunto tiene que ser deliberado."""
    assert frozenset({"<", ">", ":", '"', "|", "?", "*"}) == _CARACTERES_RESERVADOS_WIN32


@pytest.mark.parametrize("reservado", sorted(_CARACTERES_RESERVADOS_WIN32))
def test_cada_reservado_de_win32_rechaza_la_ruta(reservado):
    """Se deriva de la constante, no de una lista paralela.

    Agregar un carácter al conjunto le agrega su caso end-to-end acá solo; una
    lista escrita a mano se habría desincronizado en el primer agregado.
    """
    assert canonicalizar_ruta_windows(f"C:\\Sky{reservado}Claw") is None


def test_la_unidad_es_la_unica_posicion_donde_dos_puntos_es_legal():
    """El complemento del test de arriba: eximir la unidad no puede eximir al resto."""
    assert canonicalizar_ruta_windows(r"C:\Sky-Claw") == r"c:\sky-claw"
    assert canonicalizar_ruta_windows(r"C:\x\D:\y") is None
    assert canonicalizar_ruta_windows(r"\\servidor\C:\x") is None


# ---------------------------------------------------------------------------
# El volcado redacta TODOS los campos de texto, no algunos
# ---------------------------------------------------------------------------
#
# Hallazgo de review (Qodo). `_volcar` saneaba el título de la ventana y el
# `Name` del control, y dejaba crudos `AutomationId`, `ClassName` y
# `ControlType`. El docstring del propio módulo dice que este volcado se pega
# en un PR: una app que derive su `AutomationId` de una ruta filtra ahí el
# perfil del operador, que es exactamente lo que las otras dos líneas redactan.
#
# No está verificado que TexGen/DynDOLOD embeban rutas en esos campos —eso sólo
# se sabrá en un rig real— pero redactar cuesta cero y cierra la superficie.
#
# El test enumera los campos de texto del control en vez de mirar uno: si
# mañana el volcado imprime un campo nuevo sin sanear, este test lo agarra.


class _ObservadorDeVolcado:
    """Observador mínimo para `_volcar`: un árbol fijo, sin UIA ni Windows."""

    def __init__(self, ventana, controles):
        self._ventana = ventana
        self._controles = controles

    def ventanas_de_proceso(self, pid):
        return [self._ventana]

    def controles_para_volcado(self, ventana):
        return list(self._controles), len(self._controles), 0

    def patrones_de_lectura(self, control):
        return "Value"


def test_el_volcado_redacta_todos_los_campos_de_texto_del_control(monkeypatch):
    monkeypatch.setenv("USERNAME", "operador")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\operador")
    sonda = _cargar_la_sonda()

    perfil = r"C:\Users\operador"
    ventana = VentanaObservada(pid=4242, titulo=f"{perfil}\\salida", class_name=f"{perfil}\\clase", handle="w1")
    # Cada campo de texto lleva el perfil: si alguno se imprime crudo, aparece.
    control = ControlObservado(
        pid=4242,
        automation_id=f"{perfil}\\idcontrol",
        nombre=f"{perfil}\\nombre",
        tipo_de_control=f"{perfil}\\tipo",
        class_name=f"{perfil}\\claseControl",
    )

    salida = io.StringIO()
    sonda._volcar(_ObservadorDeVolcado(ventana, [control]), 4242, salida)
    volcado = salida.getvalue()

    assert "operador" not in volcado, volcado
    # Y que efectivamente imprimió los campos, para que el test no pase por
    # haber volcado nada: cada uno tiene que estar, redactado.
    assert volcado.count("<USERPROFILE>") >= 6, volcado


# ---------------------------------------------------------------------------
# El umbral de longitud del saneo, y el contrato que declara de más
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("usuario", ["a", "ab", "jd"])
def test_el_saneo_redacta_tambien_los_usuarios_cortos(usuario, monkeypatch):
    """Un `USERNAME` de 1-2 caracteres es un usuario, no una excepción.

    Hallazgo de review (Qodo, security). `_sanear` traía un `len(valor) > 2`
    heredado de cuando el reemplazo era por SUBSTRING: ahí un usuario corto
    generaba falsos positivos en cualquier palabra. Con el regex de frontera
    actual eso ya no puede pasar —el usuario tiene que ser un COMPONENTE
    completo de ruta— así que el umbral dejó de proteger de nada y lo único que
    hacía era no redactar al operador que se llama `jd`.
    """
    monkeypatch.setenv("USERNAME", usuario)
    # El perfil vive en OTRO lado: la ruta bajo prueba no lo contiene, así que
    # la única regla que puede redactarla es la de USERNAME. Con el perfil
    # apuntando a la misma ruta, la redacción de USERPROFILE tapaba el hueco y
    # el test pasaba sin probar nada.
    monkeypatch.setenv("USERPROFILE", rf"C:\Users\{usuario}")
    saneado = _cargar_la_sonda()._sanear(rf"D:\{usuario}\Modding\DynDOLOD")
    assert saneado == r"D:\<USERNAME>\Modding\DynDOLOD", saneado


@pytest.mark.parametrize("usuario", ["a", "ab"])
def test_el_saneo_de_usuarios_cortos_sigue_exigiendo_frontera(usuario, monkeypatch):
    """Y bajar el umbral no puede reintroducir el sobre-saneo que el umbral tapaba."""
    monkeypatch.setenv("USERNAME", usuario)
    monkeypatch.setenv("USERPROFILE", rf"C:\Users\{usuario}")
    # `usuario` aparece como substring dentro de otros componentes, nunca solo.
    intacto = rf"D:\Games\{usuario}bcdef\x{usuario}yz\Salida"
    assert _cargar_la_sonda()._sanear(intacto) == intacto


@pytest.mark.parametrize(
    ("perfil", "intacto"),
    [
        # Cada perfil degenerado va con la ruta que SÍ destrozaría. Emparejarlos
        # importa: con una ruta cualquiera el test pasa aunque el guard no esté,
        # porque el lookahead de separador ya no matchea. Verificado midiendo
        # cuáles rompen de verdad, en vez de suponer que cualquier ruta sirve.
        ("\\", r"\\servidor\recurso\Sky-Claw"),
        ("/", "//servidor/recurso/Sky-Claw"),
        ("C:\\", "C:\\"),
        ("   ", r"D:\Games\a   \Salida"),
    ],
)
def test_un_perfil_degenerado_no_se_redacta_como_prefijo(perfil, intacto, monkeypatch):
    """Sacar el umbral de longitud no puede volver destructivo al saneo.

    `USERPROFILE=C:\\` o `HOME=/` no identifican a nadie, y redactarlos COMO
    PREFIJO se come el arranque de la ruta — que es justo lo que hay que leer
    para elegir el selector. El guard que los descarta es de FORMA (¿queda un
    componente propio bajo la raíz?) y no de longitud, porque la longitud era
    lo que dejaba sin redactar a los usuarios cortos.
    """
    monkeypatch.setenv("USERPROFILE", perfil)
    monkeypatch.setenv("HOME", perfil)
    monkeypatch.delenv("USERNAME", raising=False)
    assert _cargar_la_sonda()._sanear(intacto) == intacto


def test_el_contrato_no_declara_patrones_de_lectura_que_nadie_implementa():
    """Lo que el protocolo promete leer tiene que ser lo que el adaptador lee.

    Hallazgo de review (Qodo). El docstring de `ObservadorUIA.leer_valor`
    listaba `LegacyIAccessible` entre sus patrones de lectura y el adaptador
    real sólo prueba Value y Text: un control que expusiera SÓLO Legacy
    —frecuente en Win32/Delphi— daría `UNKNOWN` mientras el contrato afirmaba
    que se podía leer.

    El ancla se deriva de las dos fuentes en vez de repetir una lista a mano:
    lee la línea declarativa del docstring y la compara con los patrones que
    `leer_valor` del probe consulta de verdad.
    """
    doc = ObservadorUIA.leer_valor.__doc__ or ""
    declarados = set()
    for linea in doc.splitlines():
        if "Patrones de lectura implementados:" in linea:
            declarados = set(re.findall(r"``(\w+?)Pattern``", linea))
    assert declarados, (
        "el docstring de leer_valor no declara sus patrones en una línea "
        "'Patrones de lectura implementados: ...' que se pueda verificar"
    )

    fuente = PROBE_T5A.read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    # `leer_valor` delega en un lector por patrón, así que los ids viven ahí.
    # El ancla los busca en TODA la familia `_texto_por_*` además del método
    # público: si mañana se agrega un lector, entra solo.
    lectores = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and (nodo.name == "leer_valor" or nodo.name.startswith("_texto_por_"))
    ]
    assert len(lectores) >= 2, f"la familia de lectores se encogió: {[n.name for n in lectores]}"
    implementados = set()
    for lector in lectores:
        for hijo in ast.walk(lector):
            if isinstance(hijo, ast.Constant) and isinstance(hijo.value, str):
                encontrado = re.fullmatch(r"UIA_Is(\w+?)PatternAvailablePropertyId", hijo.value)
                if encontrado:
                    implementados.add(encontrado.group(1))

    assert declarados == implementados, (
        f"el contrato declara {sorted(declarados)} y el adaptador implementa {sorted(implementados)}"
    )


# ---------------------------------------------------------------------------
# Puntos y espacios finales POR COMPONENTE
# ---------------------------------------------------------------------------
#
# Hallazgo de review (Qodo). Win32 recorta los espacios y puntos FINALES de
# cada componente de una ruta no extendida, así que `...\DynDOLOD.` y
# `...\DynDOLOD` designan el MISMO directorio. El `rstrip` que había cubría
# sólo el final global de la cadena, así que un punto accidental en el campo
# Output producía `MISMATCH`/`OUTPUT_DIFIERE` — un veredicto CONCLUYENTE
# equivocado, que es peor que el `UNKNOWN` que el módulo promete cuando no
# puede normalizar con seguridad.


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        (r"C:\Sky-Claw.", r"c:\sky-claw"),
        (r"C:\Sky-Claw...", r"c:\sky-claw"),
        (r"C:\Sky \Salida", r"c:\sky\salida"),
        (r"C:\Sky.\Salida", r"c:\sky\salida"),
        (r"C:\Sky-Claw\DynDOLOD. ", r"c:\sky-claw\dyndolod"),
        (r"\\servidor\recurso\Sky-Claw.", r"\\servidor\recurso\sky-claw"),
    ],
)
def test_los_puntos_y_espacios_finales_de_cada_componente_son_neutros(crudo, esperado):
    assert canonicalizar_ruta_windows(crudo) == esperado


def test_un_punto_final_accidental_no_produce_un_mismatch_concluyente():
    """End-to-end: el campo Output con un punto de más no puede dar OUTPUT_DIFIERE."""
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": SALIDA_ADMINISTRADA + "."},
    )
    assert resultado.estado is EstadoPreflight.MATCH, resultado.razon


@pytest.mark.parametrize(
    "crudo",
    [
        r"C:\x\...\y",
        r"C:\x\   \y",
        r"C:\x\...",
        # Estos dos los pidió una guía de review TRES veces, afirmando que
        # `.. ` se canonicaliza a `..` (traversal fabricado) y `. ` a `.`.
        # No es así —`rstrip(" .")` recorta puntos Y espacios, y el componente
        # colapsa a vacío— pero no estaban cubiertos, así que se cubren: es más
        # barato un caso que volver a medirlo cada vez que se repite.
        r"C:\x\.. \y",
        r"C:\x\. \y",
        r"C:\x\ . \y",
    ],
)
def test_un_componente_que_se_queda_vacio_al_recortar_se_rechaza(crudo):
    """Recortar no puede FABRICAR un componente distinto.

    `...` recortado daría `..` (traversal) y `   ` daría vacío. Ninguno de los
    dos es una normalización neutra, así que la ruta entera se rechaza: es el
    mismo fail-closed que `..`, no una excepción nueva.
    """
    assert canonicalizar_ruta_windows(crudo) is None


# ---------------------------------------------------------------------------
# Identidad: probarla una vez no es probarla durante toda la observación
# ---------------------------------------------------------------------------


class LocalizadorQueCambia:
    """Localizador cuya fotografía cambia entre llamadas.

    Modela el reciclado de pid: la primera lectura ve el proceso esperado, la
    siguiente ve OTRO binario con el mismo pid (o ninguno).
    """

    def __init__(self, *fotografias):
        self._fotografias = list(fotografias)
        self.llamadas = 0

    def procesos(self):
        self.llamadas += 1
        indice = min(self.llamadas - 1, len(self._fotografias) - 1)
        return tuple(self._fotografias[indice])


def test_si_el_pid_se_recicla_durante_la_observacion_el_veredicto_es_unknown():
    """Hallazgo de review (Qodo, TOCTOU). Era el único hueco fail-open del pipeline.

    La identidad se probaba UNA vez sobre una fotografía de psutil y después el
    pid se usaba para enumerar ventana y control sin revalidar. Si el proceso
    muere y Windows recicla el pid, la ventana observada es de otro proceso,
    pasa los filtros `pid == proceso.pid` y produce un MATCH/MISMATCH que
    ningún proceso verificado sostiene — exactamente el modo de falla que
    `IDENTIDAD_NO_DEMOSTRABLE` existe para prevenir.
    """
    ajeno = ProcesoObservado(pid=4242, nombre_ejecutable="TexGenx64.exe", ruta_ejecutable=r"D:\Otra\TexGenx64.exe")
    localizador = LocalizadorQueCambia([_proceso()], [ajeno])
    resultado = observar_output(
        _solicitud(),
        localizador=localizador,
        observador=ObservadorFalso(
            ventanas=[_ventana()],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        ),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN, resultado.razon
    assert resultado.razon is RazonPreflight.IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION


def test_si_el_proceso_desaparece_durante_la_observacion_el_veredicto_es_unknown():
    localizador = LocalizadorQueCambia([_proceso()], [])
    resultado = observar_output(
        _solicitud(),
        localizador=localizador,
        observador=ObservadorFalso(
            ventanas=[_ventana()],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        ),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION


def test_la_identidad_se_revalida_aunque_no_haya_cambiado():
    """El camino feliz sigue dando MATCH, y se revalidó de verdad (dos lecturas)."""
    localizador = LocalizadorQueCambia([_proceso()])
    resultado = observar_output(
        _solicitud(),
        localizador=localizador,
        observador=ObservadorFalso(
            ventanas=[_ventana()],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        ),
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert localizador.llamadas >= 2, "no se revalidó la identidad después de observar"


# ---------------------------------------------------------------------------
# El saneo, en los dos huecos que quedaban
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sufijo", ["", "\\", "/"])
def test_el_saneo_redacta_el_perfil_termine_o_no_en_separador(sufijo, monkeypatch):
    """Hallazgo de review (Qodo). Con `USERPROFILE=C:\\Users\\op\\` el lookahead
    exigía OTRO separador después del valor —el suyo ya estaba adentro— así que
    una ruta que continúa no matcheaba y el perfil salía crudo.
    """
    monkeypatch.setenv("USERPROFILE", r"C:\Users\operador" + sufijo)
    monkeypatch.delenv("USERNAME", raising=False)
    saneado = _cargar_la_sonda()._sanear(r"C:\Users\operador\Modding\TexGenx64.exe")
    assert saneado == r"<USERPROFILE>\Modding\TexGenx64.exe", saneado


def test_ningun_print_de_la_sonda_emite_una_excepcion_sin_sanear():
    """Los mensajes de error son un campo de texto más del volcado.

    Hallazgo de review (Qodo). El `except` imprimía `{exc}` crudo, y esos
    mensajes se construyen con `ventana.titulo` y `control.describir()`: un
    fallo COM filtraba por el borde de error justo lo que el volcado redacta en
    el camino feliz.

    El ancla ENUMERA en vez de probar un caso: recorre todos los `print` del
    archivo y exige que cualquier interpolación de una variable capturada por
    un `except ... as` pase por `_sanear`. Un borde de error nuevo que imprima
    la excepción cruda rompe el test, aunque nadie escriba su caso.
    """
    arbol = ast.parse(PROBE_T5A.read_text(encoding="utf-8"))

    capturadas = {nodo.name for nodo in ast.walk(arbol) if isinstance(nodo, ast.ExceptHandler) and nodo.name}
    assert capturadas, "el probe no captura ninguna excepción con nombre; ¿cambió la estructura?"

    culpables = []
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "print"):
            continue
        for argumento in nodo.args:
            if not isinstance(argumento, ast.JoinedStr):
                continue
            for trozo in argumento.values:
                if not isinstance(trozo, ast.FormattedValue):
                    continue
                nombres = {h.id for h in ast.walk(trozo.value) if isinstance(h, ast.Name)}
                if not (nombres & capturadas):
                    continue
                saneado = (
                    isinstance(trozo.value, ast.Call)
                    and isinstance(trozo.value.func, ast.Name)
                    and trozo.value.func.id == "_sanear"
                )
                if not saneado:
                    culpables.append(f"línea {nodo.lineno}: {ast.unparse(trozo.value)}")

    assert not culpables, f"print() que emite una excepción sin sanear: {culpables}"


# ---------------------------------------------------------------------------
# Dos bordes que el recorte por componente dejó abiertos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("separador", ["\\", "/", "\\ ", "// "])
def test_un_separador_pelado_no_revienta(separador):
    """`canonicalizar_ruta_windows` NUNCA lanza: devuelve ``None``.

    Hallazgo de review (Qodo), y es una regresión que introduje con el recorte
    por componente: `componentes[0]` se accedía al armar la lista recortada,
    ANTES del `if not componentes` que protegía ese acceso. Un backslash solo en
    el campo Output —o `--expected-output "\\"` desde la sonda— daba
    `IndexError`. La canonicalización del esperado corre FUERA del `try` de
    `observar_output`, así que la excepción escapaba y rompía el contrato
    declarado de que el preflight nunca lanza.
    """
    assert canonicalizar_ruta_windows(separador) is None


def test_un_separador_pelado_en_el_valor_observado_da_unknown():
    """End-to-end del mismo borde: UNKNOWN, no traceback."""
    resultado = _observar(
        _solicitud(),
        procesos=[_proceso()],
        ventanas=[_ventana()],
        controles={"w1": [_control()]},
        valores={"edOutput": "\\"},
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.OBSERVADO_NO_CANONICALIZABLE


@pytest.mark.parametrize("control", ["\t", "\n", "\r", "\x0b", "\x7f"])
def test_un_caracter_de_control_al_final_rechaza_la_ruta(control):
    """Un tab pegado al final NO es whitespace neutro de Win32.

    Hallazgo de review (Qodo). El `rstrip()` corría ANTES del chequeo de
    caracteres de control, así que un `\t`/`\n`/`\r` final se borraba en
    silencio y la ruta canonicalizaba igual que la limpia — MATCH donde el
    contrato promete UNKNOWN por basura de decodificación. Win32 recorta
    espacios y puntos finales, no tabs ni saltos de línea: son destinos
    distintos, no la misma ruta escrita de otra forma.
    """
    assert canonicalizar_ruta_windows("C:\\Sky-Claw" + control) is None


def test_el_espacio_final_sigue_siendo_neutro_aunque_el_tab_no_lo_sea():
    """El complemento: cerrar el borde no puede volver a rechazar lo que Win32 sí recorta."""
    assert canonicalizar_ruta_windows("C:\\Sky-Claw  ") == r"c:\sky-claw"


@pytest.mark.parametrize(
    ("perfil", "texto"),
    [
        (r"C:\Users\operador", r"C:\Users\operador\Modding\TexGenx64.exe"),
        (r"C:\Users\operador", "C:/Users/operador/Modding/TexGenx64.exe"),
        ("C:/Users/operador", r"C:\Users\operador\Modding\TexGenx64.exe"),
        ("C:/Users/operador", "C:/Users/operador/Modding/TexGenx64.exe"),
    ],
)
def test_el_saneo_redacta_el_perfil_con_separadores_mixtos(perfil, texto, monkeypatch):
    """Win32 acepta los dos separadores, así que el saneo también tiene que hacerlo.

    Hallazgo de review (Qodo). El patrón se armaba con el valor literal de la
    variable, así que si `USERPROFILE` traía `\\` y el texto del volcado traía
    `/` —el uso documentado de la sonda es `--exe "C:/Modding/..."`, y en
    Windows el perfil viene con `\\`— el prefijo no matcheaba y la ruta del
    perfil salía entera. Los tests anteriores sólo cubrían separadores
    homogéneos, así que CI quedaba verde con la fuga adentro.
    """
    monkeypatch.setenv("USERPROFILE", perfil)
    monkeypatch.delenv("USERNAME", raising=False)
    saneado = _cargar_la_sonda()._sanear(texto)
    assert "operador" not in saneado, saneado
    assert saneado.startswith("<USERPROFILE>"), saneado


@pytest.mark.parametrize(
    "blanco",
    ["\xa0", "\x85", "\u2009", "\u3000", "\u202f", "\u2028"],
)
def test_el_whitespace_unicode_final_no_es_neutro(blanco):
    r"""Win32 recorta el espacio ASCII y el punto. Nada más.

    Hallazgo de review (Qodo), y es la mitad que me faltó del fix del tab:
    moví el chequeo de caracteres de control ANTES del recorte, pero dejé
    `rstrip()` sin argumentos, que es Unicode-aware y se lleva un NBSP
    (`U+00A0`), un NEL (`U+0085`) o un espacio fino igual que un espacio. Esos
    designan un directorio DISTINTO: `C:\Salida\xa0` canonicalizaba idéntico a
    `C:\Salida` y salía un veredicto concluyente sobre un destino que no es el
    mismo.
    """
    assert canonicalizar_ruta_windows("C:\\Salida" + blanco) is None


def test_el_espacio_ascii_final_sigue_siendo_neutro():
    """El complemento: cerrar el borde Unicode no puede rechazar lo que Win32 sí recorta."""
    assert canonicalizar_ruta_windows("C:\\Salida   ") == r"c:\salida"


def test_la_sonda_no_captura_excepciones_desnudas():
    """`except Exception` está prohibido por `coding_conventions.md` §3.

    Hallazgo de review (Qodo). El ancla existe porque **ningún gate lo cubre
    acá**: `pyproject.toml` exime `local_scripts/**` de `BLE001`, así que ruff
    pasa en verde sobre un `except Exception` en este archivo. Es exactamente el
    caso que `AGENTS.md` advierte — verificar que tu archivo no esté en la lista
    de exentos antes de confiar en el gate.

    El motivo es concreto, no de estilo: este adaptador NUNCA corrió, así que en
    la primera corrida sobre un rig hay que poder distinguir "COM falló" de "el
    adaptador tiene un typo". Un `KeyError` por un id de propiedad mal escrito
    disfrazado de "el control no expone ese patrón" haría que el operador elija
    un selector sobre evidencia inventada — justo lo que la sonda mide.
    """
    arbol = ast.parse(PROBE_T5A.read_text(encoding="utf-8"))
    desnudos = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.ExceptHandler):
            continue
        if nodo.type is None:
            desnudos.append(f"línea {nodo.lineno}: except:")
        elif isinstance(nodo.type, ast.Name) and nodo.type.id in {"Exception", "BaseException"}:
            desnudos.append(f"línea {nodo.lineno}: except {nodo.type.id}")
    assert not desnudos, f"la sonda captura excepciones desnudas: {desnudos}"


# ---------------------------------------------------------------------------
# UNC: prefijos malformados y componentes de red
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crudo",
    ["\\\\\\servidor\\recurso\\x", "///servidor/recurso/x", "\\\\\\\\servidor\\recurso\\x"],
)
def test_un_prefijo_unc_malformado_se_rechaza(crudo):
    """Tres o más separadores no son una UNC: no se colapsan a una válida.

    Hallazgo de review (CodeRabbit). El filtro de componentes vacíos hacía que
    `\\\\\\servidor\\recurso\\x` canonicalizara igual que
    `\\\\servidor\\recurso\\x`, así que una ruta malformada podía dar MATCH
    contra la salida administrada. Win32 no trata tres separadores como UNC, y
    afirmar esa igualdad excede lo que este módulo puede probar.
    """
    assert canonicalizar_ruta_windows(crudo) is None


@pytest.mark.parametrize(
    "crudo",
    [r"\\servidor.\recurso\x", r"\\servidor \recurso\x", r"\\servidor\recurso.\x", r"\\servidor\recurso \x"],
)
def test_el_recorte_no_se_aplica_al_servidor_ni_al_recurso(crudo):
    """El servidor y el share NO son componentes de ruta: no se les recorta nada.

    Hallazgo de review (Qodo). El recorte por componente se justificaba con la
    normalización de rutas de Win32, que aplica a componentes de RUTA. El
    servidor es un endpoint de red y el recurso un nombre de share: un FQDN con
    punto final no es demostrablemente el mismo host, y un share terminado en
    punto es otro share. Recortarlos afirmaba una igualdad que el contrato de
    esta función no autoriza, así que se rechaza — el mismo fail-closed de
    siempre, no una excepción nueva.
    """
    assert canonicalizar_ruta_windows(crudo) is None


def test_una_unc_normal_sigue_canonicalizando():
    """El complemento: cerrar el borde no puede romper la UNC legítima."""
    assert canonicalizar_ruta_windows(r"\\servidor\recurso\Sky-Claw.") == r"\\servidor\recurso\sky-claw"


# ---------------------------------------------------------------------------
# La revalidación tiene que usar el MISMO criterio con que se probó
# ---------------------------------------------------------------------------


def test_la_identidad_por_nombre_se_revalida_por_nombre():
    """Si la ruta nunca fue parte de la prueba, perderla no invalida nada.

    Hallazgo de review (Qodo), y es un defecto que introduje con la
    revalidación: `_huella` incluía la ruta SIEMPRE, aunque el llamador hubiera
    pedido identidad por nombre (`--exe TexGenx64.exe`). Un `AccessDenied`
    transitorio de psutil entre las dos fotos hacía que `ruta_ejecutable` pasara
    de legible a `None` y el preflight respondía UNKNOWN sobre el MISMO proceso.
    Es fail-closed, pero rompe el camino feliz legítimo y contradice el contrato
    de `_identidad_del_ejecutable`: la identidad por nombre se prueba por nombre.
    """
    con_ruta = _proceso()
    sin_ruta = ProcesoObservado(pid=4242, nombre_ejecutable="TexGenx64.exe", ruta_ejecutable=None)
    resultado = observar_output(
        _solicitud(ejecutable="TexGenx64.exe"),
        localizador=LocalizadorQueCambia([con_ruta], [sin_ruta]),
        observador=ObservadorFalso(
            ventanas=[_ventana()],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        ),
    )
    assert resultado.estado is EstadoPreflight.MATCH, resultado.razon


def test_la_identidad_por_ruta_sigue_exigiendo_la_ruta_en_la_revalidacion():
    """El complemento: aflojar el caso por nombre no puede aflojar el caso por ruta."""
    con_ruta = _proceso()
    sin_ruta = ProcesoObservado(pid=4242, nombre_ejecutable="TexGenx64.exe", ruta_ejecutable=None)
    resultado = observar_output(
        _solicitud(ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        localizador=LocalizadorQueCambia([con_ruta], [sin_ruta]),
        observador=ObservadorFalso(
            ventanas=[_ventana()],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        ),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION


def test_los_metodos_del_protocolo_materializan_por_el_helper():
    """Prohibir el nombre del recorte no alcanza: hay que exigir el CAMINO.

    Hallazgo de review (Qodo). Las anclas anteriores buscaban nombres de
    función, así que un recorte escrito INLINE —`min(total, TOPE)` dentro de la
    comprensión, llamando a `GetElement` directo— reintroducía la truncación
    silenciosa y la suite seguía entera en verde. Verificado: 205 passed con esa
    mutación.

    `controles_para_volcado` queda afuera: es diagnóstico, lo anuncia y no
    alimenta ningún veredicto.
    """
    arbol = ast.parse(PROBE_T5A.read_text(encoding="utf-8"))
    metodos = {nodo.name: nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef)}
    for nombre in ("ventanas_de_proceso", "controles_de_ventana"):
        assert nombre in metodos, f"el probe ya no implementa {nombre}: revisá este ancla"
        atributos = {hijo.attr for hijo in ast.walk(metodos[nombre]) if isinstance(hijo, ast.Attribute)}
        assert "_elementos" in atributos, f"{nombre} no materializa por `_elementos`"
        assert "GetElement" not in atributos, (
            f"{nombre} llama a GetElement directo: un recorte inline se saltea `exigir_enumeracion_completa`"
        )


@pytest.mark.parametrize(
    "texto",
    [
        r"C:\Users\operador - TexGen 3.00",
        "TexGen 3.00 - C:/Users/operador (idle)",
    ],
)
def test_el_saneo_redacta_el_perfil_aunque_lo_siga_prosa(texto, monkeypatch):
    """Un título de ventana suele pegar la ruta al nombre de la app.

    Hallazgo de review (Qodo). El lookahead exigía separador o fin de cadena
    DESPUÉS del perfil, así que `C:\\Users\\operador - TexGen 3.00` —el patrón
    típico de una app que antepone la ruta a su nombre— no matcheaba y el
    volcado, que existe para pegarse en un PR, imprimía el usuario entero.

    Se amplía a whitespace y no a "cualquier cosa": el borde sigue siendo que el
    perfil TERMINE ahí. Los cuatro casos de sobre-saneo documentados
    (`badminton`, `Administración`, `admin-tools`, `administrator`) siguen
    intactos porque después del nombre no hay ni separador ni espacio — están
    cubiertos por el test de al lado, que se corrió con este cambio puesto.
    """
    monkeypatch.setenv("USERPROFILE", r"C:\Users\operador")
    monkeypatch.delenv("USERNAME", raising=False)
    saneado = _cargar_la_sonda()._sanear(texto)
    assert "operador" not in saneado, saneado
    assert "<USERPROFILE>" in saneado, saneado


def test_el_perfil_pegado_a_puntuacion_no_se_redacta_y_es_deliberado(monkeypatch):
    r"""El límite de la frontera, anclado a propósito en vez de quedar por accidente.

    `C:\Users\operador]` NO se redacta, y no hay forma TEXTUAL de arreglarlo
    sin romper algo peor: `]` es legal en un nombre de directorio, así que
    `...\operador]` puede ser un directorio REAL distinto del perfil.
    Distinguir "acá terminó la ruta y sigue prosa" de "este es otro directorio
    cuyo nombre empieza igual" exige mirar el disco, y esta función no lo toca
    por contrato.

    La frontera elegida —separador, whitespace o fin— es donde el riesgo es
    simétrico y bajo. Este test existe para que el hueco sea una DECISIÓN
    visible: si alguien la amplía, que sea sabiendo qué está cambiando.
    """
    monkeypatch.setenv("USERPROFILE", r"C:\Users\operador")
    monkeypatch.delenv("USERNAME", raising=False)
    assert _cargar_la_sonda()._sanear(r"[C:\Users\operador]") == r"[C:\Users\operador]"


# ---------------------------------------------------------------------------
# "Nunca lanza" tiene que valer también para un bug del adaptador
# ---------------------------------------------------------------------------


class ObservadorConBugDeAdaptador:
    """Modela un typo del adaptador, no un fallo del rig.

    `_propiedad` del probe indexa `self._uia_mod.__dict__[nombre_de_id]`: un id
    mal escrito lanza `KeyError`. Desde que los `except` del adaptador dejaron
    de ser `except Exception` (a propósito), esa excepción ya no se traduce allá
    y llega hasta acá.
    """

    def __init__(self, excepcion):
        self._excepcion = excepcion

    def ventanas_de_proceso(self, pid):
        raise self._excepcion

    def controles_de_ventana(self, ventana):
        return []

    def leer_valor(self, control):
        return None


@pytest.mark.parametrize(
    "excepcion",
    [
        KeyError("UIA_IsValueXPatternAvailablePropertyId"),
        AttributeError("'module' object has no attribute 'UIA_ValueXPatternId'"),
        TypeError("int() argument must be a string"),
        ValueError("invalid literal"),
        IndexError("tuple index out of range"),
    ],
)
def test_un_bug_del_adaptador_no_escapa_de_observar_output(excepcion):
    """El contrato dice "nunca lanza" y tiene que valer para TODO.

    Hallazgo de review (Qodo), y es el costo directo de mi propio fix anterior:
    al dejar de capturar `except Exception` en el adaptador —correcto, para que
    un typo no se disfrace de "el control no expone el patrón"— esas
    excepciones pasaron a escapar de `observar_output`, que promete no lanzar.

    La promesa no es cosmética: existe para que el llamador no las trague con
    un `except` amplio y siga como si nada, que es el camino exacto por el que
    un fail-closed se vuelve fail-open. Cuando este preflight se cablee al
    runtime, la excepción rompería el pipeline en vez de cerrar en UNKNOWN.

    Traducir no es esconder: el TIPO de la excepción va en el detalle, así que
    un bug del adaptador sigue siendo diagnosticable — lo que no puede es
    disfrazarse de observación válida.
    """
    resultado = observar_output(
        _solicitud(),
        localizador=LocalizadorFalso([_proceso()]),
        observador=ObservadorConBugDeAdaptador(excepcion),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ERROR_UIA
    assert type(excepcion).__name__ in resultado.detalle, resultado.detalle


@pytest.mark.parametrize("sufijo", [" ", "\\ ", "  ", "\t"])
def test_el_saneo_tolera_un_perfil_con_whitespace_final(sufijo, monkeypatch):
    """`rstrip("\\\\/")` no se lleva un espacio final, y ahí el patrón no matchea.

    Hallazgo de review (Qodo, baja). Windows no produce ese valor en la
    práctica, pero el resto de la función existe para cerrar justamente estos
    bordes y el arreglo es una línea.
    """
    monkeypatch.setenv("USERPROFILE", r"C:\Users\operador" + sufijo)
    monkeypatch.delenv("USERNAME", raising=False)
    saneado = _cargar_la_sonda()._sanear(r"C:\Users\operador\Modding\x")
    assert "operador" not in saneado, saneado


def test_un_texto_vacio_no_termina_la_lectura():
    """Vacío NO es una lectura: se sigue probando el patrón siguiente.

    Hallazgo de review (Qodo), y es la TERCERA ancla por AST que no ataba lo que
    decía sobre esta función: la anterior contaba `return None` como única
    salida temprana y no veía que `if valor is not None: return str(valor)`
    corta con `""`. Un Edit deshabilitado que devuelve `""` por ValuePattern
    con el texto en TextPattern daba UNKNOWN.

    La respuesta no fue otra ancla más fina, fue mover el ORDEN de lectura a un
    helper puro que se puede ejecutar en Linux. Esto es conducta, no forma.
    """
    sonda = _cargar_la_sonda()
    llamados = []

    def lector(nombre, valor):
        def _leer():
            llamados.append(nombre)
            return valor

        return _leer

    # El vacío del primero no corta: se consulta el segundo y gana su texto.
    assert sonda.primer_texto_no_vacio((lector("value", ""), lector("text", r"C:\Salida"))) == r"C:\Salida"
    assert llamados == ["value", "text"]

    # Un texto real del primero SÍ corta: el segundo ni se consulta.
    llamados.clear()
    assert sonda.primer_texto_no_vacio((lector("value", r"C:\Otra"), lector("text", r"C:\Salida"))) == r"C:\Otra"
    assert llamados == ["value"], "se consultó el patrón siguiente teniendo ya una lectura válida"

    # Todos vacíos o ausentes: `None`, que el preflight traduce a UNKNOWN.
    llamados.clear()
    assert sonda.primer_texto_no_vacio((lector("value", None), lector("text", ""))) is None
    assert llamados == ["value", "text"]


def test_leer_valor_delega_el_orden_en_el_helper_puro():
    """Que no se vuelva a escribir el orden inline, donde no se puede testear."""
    arbol = ast.parse(PROBE_T5A.read_text(encoding="utf-8"))
    lector = next(nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.FunctionDef) and nodo.name == "leer_valor")
    llamadas = {
        hijo.func.id for hijo in ast.walk(lector) if isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Name)
    }
    assert "primer_texto_no_vacio" in llamadas


def test_un_control_roto_no_se_lleva_puesto_el_volcado_entero():
    """Un control *stale* no puede costar la evidencia de los que sí se leyeron.

    Hallazgo de review (Qodo). `controles_para_volcado` describía todos los
    controles dentro de un solo `try`: un repaint de la GUI que invalidara UNO
    abortaba el volcado de la ventana entera, y el operador se quedaba sin lo
    único que la sonda existe para producir.
    """
    sonda = _cargar_la_sonda()

    def describir(elemento):
        if elemento == "roto":
            raise OSError("stale element")
        return ControlObservado(
            pid=4242, automation_id=str(elemento), nombre="Output", tipo_de_control="Edit", class_name="TEdit"
        )

    descritos, fallidos = sonda.describir_tolerando_fallos(["a", "roto", "b"], describir, (OSError,))
    assert [c.automation_id for c in descritos] == ["a", "b"], "se perdió la evidencia de los controles sanos"
    assert fallidos == 1, "los que fallan se cuentan, no se esconden"


# ---------------------------------------------------------------------------
# Alias de Windows: lo que puede denotar el MISMO destino con otra cadena
# ---------------------------------------------------------------------------
#
# Hallazgo de review (Qodo). El fail-closed estaba cubierto sólo en la dirección
# del MATCH falso; el MISMATCH falso no tenía caja de ambigüedad. Si la GUI
# muestra la salida por un alias y el esperado usa la forma larga, el veredicto
# era `MISMATCH`/`OUTPUT_DIFIERE` —CONCLUYENTE— sobre dos cadenas que designan
# el mismo directorio.
#
# Se rechaza lo que se puede reconocer SIN tocar el disco. Lo que no (junctions
# y symlinks, indistinguibles de una ruta común) queda documentado como límite
# en el docstring de la función, no tapado.


@pytest.mark.parametrize(
    "crudo",
    [
        r"C:\PROGRA~1\Sky-Claw",
        r"C:\Sky-Claw\DYNDOL~1",
        r"C:\Games\TEXGEN~2\Salida",
        r"C:\%APPDATA%\Sky-Claw",
        r"C:\Games\%USERNAME%\Salida",
        r"C:\%SYSTEMDRIVE%\x",
    ],
)
def test_una_ruta_con_alias_no_puede_dar_un_veredicto_concluyente(crudo):
    """Un nombre 8.3 o una variable sin expandir pueden ser OTRA forma del mismo destino."""
    assert canonicalizar_ruta_windows(crudo) is None


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        # `~` sin dígito no es un nombre corto: `~backup` es un directorio común.
        (r"C:\~backup\Sky-Claw", r"c:\~backup\sky-claw"),
        (r"C:\Sky~Claw\x", r"c:\sky~claw\x"),
        # Un `%` suelto tampoco es una variable: hace falta el par.
        (r"C:\100% done\x", r"c:\100% done\x"),
    ],
)
def test_el_rechazo_de_alias_no_se_come_nombres_legitimos(crudo, esperado):
    """Rechazar de más rompe justo aquello para lo que existe el preflight."""
    assert canonicalizar_ruta_windows(crudo) == esperado


def test_el_volcado_distingue_un_control_ilegible_de_una_truncacion(monkeypatch):
    """Dos condiciones distintas no pueden imprimirse igual.

    Al tolerar el control roto (hallazgo de Qodo) introduje esto: con un control
    omitido `len(controles) < total`, y `_volcar` lo reportaba como `TRUNCATED`
    —que significa "el árbol no entra en la cota"—. El operador habría leído que
    se recortó el volcado cuando en realidad algo falló al leerse.
    """
    monkeypatch.setenv("USERPROFILE", r"C:\Users\operador")
    monkeypatch.delenv("USERNAME", raising=False)
    sonda = _cargar_la_sonda()

    ventana = VentanaObservada(pid=4242, titulo="TexGen", class_name="TfrmMain", handle="w1")
    control = ControlObservado(
        pid=4242, automation_id="edOutput", nombre="Output", tipo_de_control="Edit", class_name="TEdit"
    )

    class _ConUnIlegible:
        def ventanas_de_proceso(self, pid):
            return [ventana]

        def controles_para_volcado(self, ventana):
            # 3 en el árbol, 1 mostrado, 2 ilegibles: NO es truncación.
            return [control], 3, 2

        def patrones_de_lectura(self, control):
            return "Value"

    salida = io.StringIO()
    sonda._volcar(_ConUnIlegible(), 4242, salida)
    volcado = salida.getvalue()
    assert "ILEGIBLES: 2" in volcado, volcado
    assert "TRUNCATED" not in volcado, volcado
    assert "edOutput" in volcado, "se perdió la evidencia del control que sí se leyó"
