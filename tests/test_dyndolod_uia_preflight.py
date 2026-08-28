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
import pathlib

import pytest

from sky_claw.local.tools.dyndolod_uia_preflight import (
    RAZONES_DE_UNKNOWN,
    TOOLS_OBSERVABLES,
    ControlObservado,
    CriteriosDeControl,
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

    def __init__(self, ventanas=(), controles=None, valores=None, error=None, error_en=None):
        self._ventanas = tuple(ventanas)
        self._controles = controles or {}
        self._valores = valores or {}
        self._error = error
        self._error_en = error_en or set()

    def _quizas_fallar(self, metodo):
        if self._error is not None and metodo in self._error_en:
            raise self._error

    def ventanas_de_proceso(self, pid):
        self._quizas_fallar("ventanas_de_proceso")
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
        "  " + SALIDA_ADMINISTRADA,
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
        # Este observador ignora el pid: devuelve la ventana de otro proceso.
        observador=ObservadorFalso(
            ventanas=[_ventana(pid=777)],
            controles={"w1": [_control()]},
            valores={"edOutput": SALIDA_ADMINISTRADA},
        ),
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VENTANA_NO_ENCONTRADA


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
        ("  C:\\Sky-Claw  ", r"c:\sky-claw"),
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
    ],
)
def test_rutas_que_no_se_pueden_canonicalizar(crudo):
    assert canonicalizar_ruta_windows(crudo) is None


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
        "UIA_UNAVAILABLE",
        "PROCESO_NO_ENCONTRADO",
        "PROCESO_AMBIGUO",
        "PID_NO_COINCIDE",
        "VENTANA_NO_ENCONTRADA",
        "VENTANA_AMBIGUA",
        "CONTROL_NO_ENCONTRADO",
        "CONTROL_AMBIGUO",
        "CONTROL_FUERA_DEL_PROCESO",
        "VALOR_NO_LEIBLE",
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
    manifest = (RAIZ / "pyproject.toml").read_text(encoding="utf-8")
    seccion = manifest.split("[project.optional-dependencies]")[0]
    for paquete in ("comtypes", "pywinauto", "uiautomation", "pywin32"):
        assert f'"{paquete}' not in seccion, f"{paquete} entró a dependencies sin decisión"
