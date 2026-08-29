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
import os
import pathlib
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
        valores={"": SALIDA_ADMINISTRADA},
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
        valores={"": SALIDA_ADMINISTRADA},
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
        valores={"edOutput": SALIDA_ADMINISTRADA},
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
    # Ninguna de estas rutas existe; si la canonicalización llamara a resolve()
    # o exists() sobre ellas el resultado dependería del disco del rig.
    monkeypatch.chdir(tmp_path)
    assert canonicalizar_ruta_windows(r"Z:\no\existe") == r"z:\no\existe"
    assert not (tmp_path / "no").exists()


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
