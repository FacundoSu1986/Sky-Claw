"""T5A — preflight READ-ONLY de UI Automation sobre TexGen/DynDOLOD.

**La pregunta que este módulo responde, entera.** *"¿Qué valor de Output muestra
AHORA esta instancia concreta de TexGen/DynDOLOD, y canonicaliza igual que la
salida administrada que Sky-Claw le pasaría por ``-o:``?"* Nada más. La
observación es de solo lectura: no se pulsa Start, no se escribe el preset, no
se toca ``OutputPath=``, no se inyecta teclado ni mouse.

**El límite epistémico es la parte importante, y es una propiedad del mecanismo,
no una advertencia de prosa.** ``MATCH`` dice *"el valor que la GUI muestra
canonicaliza igual que el esperado"*. **No** dice *"la próxima corrida va a
escribir ahí"*: entre lo que muestra la ventana y dónde aterrizan los bytes hay
un preset en disco y un ``-o:`` en la línea de comandos cuya autoridad relativa
todavía no está establecida (eso es T5-v2). Por eso:

* el resultado no tiene ``success: bool``. El contrato ``success``/``message`` de
  ``AGENTS.md`` es para TOOLS, y esto no lo es —no está cableado en ningún
  dispatcher (ni ``tool_dispatcher`` ni ``AsyncToolRegistry``), a propósito—; y
  un ``success=True`` sobre un preflight de GUI se lee como una garantía de
  escritura física que ninguna evidencia de acá sostiene. El estado va en
  :class:`EstadoPreflight`, que tiene tres valores y no dos.
* no existe un estado "verificado". La tercera caja es ``UNKNOWN`` y es adonde
  cae **toda** ambigüedad: cero candidatos, dos candidatos, un pid que no
  corresponde, un patrón de lectura ausente, un error COM, una ruta que no se
  puede canonicalizar sin tocar el disco.

**Por qué el observador es un puerto y no una llamada a COM.** Dos razones que
se sostienen solas. (1) El paquete tiene que importarse en el CI de Ubuntu sin
COM ni ``UIAutomationCore`` ni escritorio interactivo, así que acá no entra un
import Windows-only ni siquiera perezoso —el ancla
``test_el_modulo_no_importa_nada_de_windows`` lo congela—. (2) Todavía **no hay
evidencia** de que TexGen/DynDOLOD expongan el campo *Output* por UIA: nadie
midió su árbol. Inventar el selector antes de verlo sería exactamente el defecto
que este módulo existe para evitar, así que los criterios del control son un
INPUT (:class:`CriteriosDeControl`) y no una constante escrita de memoria. El
backend Windows que produce esa evidencia vive fuera del runtime, en
``local_scripts/scripts/probe_dyndolod_uia_readonly.py``.

**Read-only enunciado como propiedad, no como recordatorio.** El protocolo
:class:`ObservadorUIA` declara TRES métodos y los tres devuelven datos; no
existe —ni puede agregarse sin romper un test— una primitiva que pulse, escriba,
enfoque o seleccione. La superficie completa de T5A (este módulo y el probe) no
puede siquiera NOMBRAR una primitiva mutante: el ancla por AST de
``tests/test_dyndolod_uia_preflight.py`` enumera atributos, identificadores y
literales, e incluye ``getattr``/``setattr``/``eval``/``exec`` porque son el
despacho dinámico que dejaría llamar a una mutante sin que su nombre aparezca en
el árbol. Un guard que sólo mirara los nombres directos sería una muestra; este
es una enumeración.

Fuera de alcance (T5-v2 y posteriores): autoridad final entre preset y ``-o:``,
limpieza del preset rancio, modificación de argumentos, ejecución de la
generación, atribución de artefactos físicos.
"""

from __future__ import annotations

import enum
import ntpath
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import psutil

#: Las dos únicas herramientas que este preflight observa. Se congela por
#: igualdad literal en los tests: son los dos binarios de la familia DynDOLOD que
#: reciben la raíz administrada por ``-o:`` (``output_targets.dyndolod_output_target``),
#: y son hermanos —cablear uno y dejar el otro es el defecto dominante del repo—.
TOOLS_OBSERVABLES: frozenset[str] = frozenset({"TexGen", "DynDOLOD"})

#: Unidad de Windows como componente completo: ``C:``. Un valor como ``1:`` o
#: ``CD:`` no es una unidad y la ruta no se canonicaliza.
_ES_UNIDAD = re.compile(r"^[A-Za-z]:$")

#: Prefijos de espacio de nombres de Win32 (``\\?\`` extendido, ``\\.\`` de
#: dispositivo). Se rechazan en vez de traducirse: ``\\?\C:\x`` designa el mismo
#: lugar que ``C:\x``, así que tratarlo como UNC daría un MISMATCH falso y
#: traducirlo sería justo la transformación agresiva que el contrato prohíbe.
#: Fail-closed: UNKNOWN.
_PREFIJOS_DE_NAMESPACE = frozenset({"?", "."})


class EstadoPreflight(enum.Enum):
    """Las tres cajas del veredicto. Son tres y no dos, y ese es el contrato."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class RazonPreflight(enum.Enum):
    """Por qué el veredicto es el que es.

    Cada rama del pipeline declara la suya: reciclar una razón vecina deja el
    diagnóstico mintiendo sobre dónde se cortó la observación. Congelado por
    igualdad literal en los tests.
    """

    OUTPUT_COINCIDE = "OUTPUT_COINCIDE"
    OUTPUT_DIFIERE = "OUTPUT_DIFIERE"
    #: Validación de la solicitud, ANTES de generar tráfico UIA.
    TOOL_DESCONOCIDA = "TOOL_DESCONOCIDA"
    SELECTOR_SIN_CRITERIOS = "SELECTOR_SIN_CRITERIOS"
    ESPERADO_NO_CANONICALIZABLE = "ESPERADO_NO_CANONICALIZABLE"
    #: El backend no puede observar en esta plataforma/proceso.
    UIA_NO_DISPONIBLE = "UIA_UNAVAILABLE"
    #: Identidad de proceso y ventana.
    PROCESO_NO_ENCONTRADO = "PROCESO_NO_ENCONTRADO"
    PROCESO_AMBIGUO = "PROCESO_AMBIGUO"
    PID_NO_COINCIDE = "PID_NO_COINCIDE"
    VENTANA_NO_ENCONTRADA = "VENTANA_NO_ENCONTRADA"
    VENTANA_AMBIGUA = "VENTANA_AMBIGUA"
    #: Resolución del control de Output dentro de esa ventana.
    CONTROL_NO_ENCONTRADO = "CONTROL_NO_ENCONTRADO"
    CONTROL_AMBIGUO = "CONTROL_AMBIGUO"
    CONTROL_FUERA_DEL_PROCESO = "CONTROL_FUERA_DEL_PROCESO"
    #: Lectura y comparación.
    VALOR_NO_LEIBLE = "VALOR_NO_LEIBLE"
    ERROR_UIA = "ERROR_UIA"
    OBSERVADO_NO_CANONICALIZABLE = "OBSERVADO_NO_CANONICALIZABLE"


#: Todo lo que NO es un veredicto sobre el valor es ``UNKNOWN``. Se deriva por
#: complemento y no se escribe a mano: una razón nueva cae en ``UNKNOWN`` por
#: construcción, que es la dirección segura. Si alguna vez una razón nueva
#: tuviera que ser concluyente, hay que sacarla explícitamente de acá.
RAZONES_DE_UNKNOWN: frozenset[RazonPreflight] = frozenset(RazonPreflight) - {
    RazonPreflight.OUTPUT_COINCIDE,
    RazonPreflight.OUTPUT_DIFIERE,
}


class ObservacionUIAError(Exception):
    """Cualquier fallo de OBSERVACIÓN: error COM, elemento *stale*, sensor roto.

    El pipeline lo traduce a ``UNKNOWN``; nunca escapa de :func:`observar_output`.
    """


class UIANoDisponibleError(ObservacionUIAError):
    """No hay backend capaz de observar acá (plataforma, dependencia, sesión).

    Es SUBCLASE de :class:`ObservacionUIAError` a propósito: la red general del pipeline
    atrapa ``ObservacionUIAError``, así que una plataforma sin backend no puede escaparse
    del fail-closed por olvido de un ``except`` específico. La distinción existe
    sólo para que el diagnóstico separe *"no pude mirar"* de *"miré y se rompió"*.
    """


@dataclass(frozen=True)
class ProcesoObservado:
    """Identidad de un proceso candidato, tal como la reporta el sensor.

    ``ruta_ejecutable`` es ``None`` cuando el sensor no la pudo leer (permisos):
    ahí la identidad se prueba sólo por nombre de binario y eso queda registrado
    en la evidencia, no escondido.
    """

    pid: int
    nombre_ejecutable: str
    ruta_ejecutable: str | None = None


@dataclass(frozen=True)
class VentanaObservada:
    """Ventana top-level de un proceso. ``handle`` es opaco para el pipeline."""

    pid: int
    titulo: str
    class_name: str = ""
    handle: object = None


@dataclass(frozen=True)
class ControlObservado:
    """Un control dentro de una ventana, con las propiedades que UIA expone.

    ``pid`` viaja en el control y no se asume heredado de la ventana: es lo que
    hace que un ``AutomationId`` homónimo de otro proceso no pueda colarse como
    candidato.
    """

    pid: int
    automation_id: str = ""
    nombre: str = ""
    tipo_de_control: str = ""
    class_name: str = ""
    handle: object = None

    def describir(self) -> str:
        """Descriptor estable para la evidencia del resultado."""
        return (
            f"pid={self.pid} automation_id={self.automation_id!r} "
            f"nombre={self.nombre!r} tipo={self.tipo_de_control!r} clase={self.class_name!r}"
        )


@dataclass(frozen=True)
class CriteriosDeControl:
    """Cómo se reconoce el control de *Output*, provisto por el llamador.

    **No hay un selector por defecto y eso es deliberado.** Nadie midió todavía
    el árbol UIA de TexGen/DynDOLOD, así que cualquier constante acá sería una
    invención con apariencia de evidencia. Los criterios se combinan con AND y
    al menos uno tiene que estar puesto: un selector vacío matchea todos los
    controles de la ventana y, con una sola caja de texto, daría un ``MATCH``
    que no prueba nada.

    ``AutomationId`` no basta por sí solo como autoridad —puede no existir, es
    único sólo entre hermanos y no se garantiza estable entre builds—, por eso
    los tres criterios son opcionales y combinables en vez de haber un campo
    obligatorio.
    """

    automation_id: str | None = None
    nombre: str | None = None
    tipo_de_control: str | None = None

    def esta_vacio(self) -> bool:
        return self.automation_id is None and self.nombre is None and self.tipo_de_control is None

    def coincide(self, control: ControlObservado) -> bool:
        """AND de los criterios presentes. Comparación exacta, sin heurística."""
        if self.automation_id is not None and control.automation_id != self.automation_id:
            return False
        if self.nombre is not None and control.nombre != self.nombre:
            return False
        return not (self.tipo_de_control is not None and control.tipo_de_control != self.tipo_de_control)

    def describir(self) -> str:
        partes = []
        if self.automation_id is not None:
            partes.append(f"automation_id={self.automation_id!r}")
        if self.nombre is not None:
            partes.append(f"nombre={self.nombre!r}")
        if self.tipo_de_control is not None:
            partes.append(f"tipo={self.tipo_de_control!r}")
        return " ".join(partes) if partes else "(sin criterios)"


@dataclass(frozen=True)
class SolicitudPreflightUIA:
    """Todo lo que el preflight necesita, sin cablearse a ninguna config global.

    ``ejecutable_esperado`` es ``str`` y no ``pathlib.Path`` a propósito: en
    Linux, ``PurePosixPath(r"C:\\Modding\\TexGenx64.exe")`` es UN componente y el
    basename saldría mal. Acá se parsea siempre con :mod:`ntpath`, que interpreta
    semántica Windows en cualquier plataforma — es lo que hace que estos tests
    corran en el CI de Ubuntu sin mentir.

    ``pid`` opcional: si viene, la identidad se ata a ESE proceso y un pid que no
    corresponde es ``UNKNOWN``, nunca "busquemos otro parecido".
    """

    tool: str
    ejecutable_esperado: str
    salida_administrada_esperada: str
    criterios_del_control: CriteriosDeControl
    pid: int | None = None


@dataclass(frozen=True)
class ResultadoPreflightUIA:
    """Veredicto + evidencia suficiente para diagnosticar sin volver al rig."""

    estado: EstadoPreflight
    razon: RazonPreflight
    tool: str
    detalle: str
    valor_esperado: str
    pid: int | None = None
    ventana: str | None = None
    valor_observado: str | None = None
    valor_observado_canonico: str | None = None
    valor_esperado_canonico: str | None = None
    evidencia: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class LocalizadorDeProcesos(Protocol):
    """Sensor de procesos vivos. Solo enumera; no abre, no señaliza, no mata."""

    def procesos(self) -> Sequence[ProcesoObservado]: ...


@runtime_checkable
class ObservadorUIA(Protocol):
    """Puerto de observación UI Automation. **Tres métodos, los tres de lectura.**

    Que las primitivas mutantes no estén acá no es un olvido a completar después:
    es el contrato. ``tests/test_dyndolod_uia_preflight.py`` congela esta lista
    por igualdad literal, así que agregar ``click()`` rompe la suite aunque
    ningún llamador lo use.

    Cualquier implementación puede lanzar :class:`ObservacionUIAError` (o
    :class:`UIANoDisponibleError`); el pipeline las traduce a ``UNKNOWN``.
    """

    def ventanas_de_proceso(self, pid: int) -> Sequence[VentanaObservada]: ...

    def controles_de_ventana(self, ventana: VentanaObservada) -> Sequence[ControlObservado]: ...

    def leer_valor(self, control: ControlObservado) -> str | None: ...


class LocalizadorPsutil:
    """Identidad de proceso vía :mod:`psutil` — ya es dependencia del repo.

    La mitad de §9 (¿existe UNA instancia de este binario y cuál es su pid?) no
    necesita nada Windows-only, así que no lo toma: psutil enumera igual en el CI
    de Linux, lo que hace que esta pieza sea testeable de verdad en vez de
    quedar detrás del mismo muro que el árbol UIA.
    """

    def procesos(self) -> Sequence[ProcesoObservado]:
        salida: list[ProcesoObservado] = []
        try:
            # `ad_value=None` sustituye los atributos que el sensor no puede leer
            # (permisos) en vez de propagar AccessDenied: un proceso ajeno del
            # sistema no debe abortar la enumeración entera. Los procesos que
            # mueren durante la iteración los saltea `process_iter`.
            for proceso in psutil.process_iter(["pid", "name", "exe"], ad_value=None):
                info = proceso.info
                nombre = info.get("name") or ""
                if not nombre:
                    continue
                ruta = info.get("exe") or None
                salida.append(
                    ProcesoObservado(
                        pid=int(info["pid"]),
                        nombre_ejecutable=str(nombre),
                        ruta_ejecutable=str(ruta) if ruta else None,
                    )
                )
        except psutil.Error as exc:
            raise ObservacionUIAError(f"el sensor de procesos falló: {exc}") from exc
        return tuple(salida)


class ObservadorNoDisponible:
    """Observador que falla cerrado. Es el backend por defecto, y a propósito.

    T5A no cablea un backend Windows en el runtime porque falta la evidencia que
    lo justificaría: nadie midió el árbol UIA de TexGen/DynDOLOD, así que ni el
    selector ni la decisión de dependencia (comtypes / pywinauto / uiautomation)
    tienen todavía sobre qué apoyarse. Hasta entonces el pipeline responde
    ``UNKNOWN`` con razón ``UIA_UNAVAILABLE``, que es la respuesta honesta, en
    vez de un backend a medias que devolvería veredictos inventados.
    """

    def __init__(self, motivo: str = "no hay backend de UI Automation cableado (T5A: REAL_RIG_REQUIRED)") -> None:
        self._motivo = motivo

    def ventanas_de_proceso(self, pid: int) -> Sequence[VentanaObservada]:
        raise UIANoDisponibleError(self._motivo)

    def controles_de_ventana(self, ventana: VentanaObservada) -> Sequence[ControlObservado]:
        raise UIANoDisponibleError(self._motivo)

    def leer_valor(self, control: ControlObservado) -> str | None:
        raise UIANoDisponibleError(self._motivo)


def observador_por_defecto() -> ObservadorUIA:
    """El observador que usa quien no inyecta uno: el que falla cerrado."""
    return ObservadorNoDisponible()


def canonicalizar_ruta_windows(valor: str | None) -> str | None:
    """Forma canónica comparable de una ruta Windows, o ``None`` si no la hay.

    **Puramente textual: no toca el filesystem.** Ni ``resolve()``, ni
    ``exists()``, ni creación de directorios. Una comparación que dependiera del
    disco daría veredictos distintos según qué existiera en el rig en ese
    instante, y el encargo prohíbe explícitamente arreglar la ruta tocando el
    disco.

    Cada transformación se aplica sólo si es demostrablemente neutra en semántica
    Windows; lo que no lo es, se rechaza. Rechazar cuesta un ``UNKNOWN``
    revisable; normalizar de más cuesta un ``MATCH`` falso, que es el defecto que
    todo este módulo existe para no cometer:

    * se aceptan ``/`` y ``\\`` como separadores (Win32 acepta los dos) y se
      colapsan los repetidos;
    * se descarta el separador final y el whitespace de los extremos (los campos
      de texto de una GUI los arrastran sin cambiar de destino);
    * se compara sin distinguir mayúsculas, que es la semántica por defecto de
      NTFS. Se usa ``lower()`` y no ``casefold()``: ``casefold()`` pliega ``ß``
      a ``ss`` y ahí sí inventaría equivalencias que Windows no hace;
    * un componente ``.`` se descarta: nunca traversa, así que quitarlo es neutro
      incluso con junctions de por medio;
    * un componente ``..`` **rechaza la ruta**. Win32 lo resuelve léxicamente,
      pero un valor no normalizado en el campo Output es señal de un estado que
      no vamos a interpretar por el operador — y ninguna salida administrada
      legítima lo contiene, así que rechazarlo no le cuesta nada a nadie;
    * se exige ruta ABSOLUTA (unidad ``X:\\…`` o UNC ``\\\\servidor\\recurso\\…``).
      Una relativa depende del ``cwd`` del binario, que UIA no reporta: sin ese
      dato no hay comparación posible, sólo una adivinanza;
    * los prefijos de namespace (:data:`_PREFIJOS_DE_NAMESPACE`) se rechazan: ver
      la nota de esa constante.
    """
    if valor is None:
        return None
    texto = valor.strip()
    if not texto:
        return None
    # Un carácter de control adentro de la ruta es basura de decodificación o de
    # copiado, no una ruta: no se limpia, se rechaza.
    if any(ord(caracter) < 0x20 or ord(caracter) == 0x7F for caracter in texto):
        return None

    texto = texto.replace("/", "\\")
    es_unc = texto.startswith("\\\\")
    cuerpo = texto[2:] if es_unc else texto
    componentes = [parte for parte in cuerpo.split("\\") if parte]

    if es_unc and componentes and componentes[0] in _PREFIJOS_DE_NAMESPACE:
        return None
    if ".." in componentes:
        return None
    componentes = [parte for parte in componentes if parte != "."]

    if es_unc:
        # Servidor + recurso como mínimo: `\\servidor` sola no designa un árbol.
        if len(componentes) < 2:
            return None
        return ("\\\\" + "\\".join(componentes)).lower()

    if not componentes:
        return None
    unidad = componentes[0]
    # `texto.startswith(unidad + "\\")` es lo que separa `C:\x` (absoluta) de
    # `C:x` (relativa a la unidad) y de `\C:\x` (que no empieza por la unidad).
    if not _ES_UNIDAD.match(unidad) or not texto.startswith(unidad + "\\"):
        return None
    return "\\".join([unidad, *componentes[1:]]).lower() if componentes[1:] else unidad.lower() + "\\"


def _nombre_de_binario(ruta: str) -> str:
    """Basename en semántica Windows, en minúsculas. Vale en cualquier plataforma."""
    return ntpath.basename(ruta.replace("/", "\\")).lower()


def _es_ruta_completa(valor: str) -> bool:
    """¿El llamador nombró una instalación concreta o sólo el binario?"""
    normalizado = valor.replace("/", "\\")
    return "\\" in normalizado or ":" in normalizado


def _resultado(
    estado: EstadoPreflight,
    razon: RazonPreflight,
    solicitud: SolicitudPreflightUIA,
    detalle: str,
    *,
    pid: int | None = None,
    ventana: str | None = None,
    valor_observado: str | None = None,
    valor_observado_canonico: str | None = None,
    valor_esperado_canonico: str | None = None,
    evidencia: Sequence[str] = (),
) -> ResultadoPreflightUIA:
    return ResultadoPreflightUIA(
        estado=estado,
        razon=razon,
        tool=solicitud.tool,
        detalle=detalle,
        valor_esperado=solicitud.salida_administrada_esperada,
        pid=pid,
        ventana=ventana,
        valor_observado=valor_observado,
        valor_observado_canonico=valor_observado_canonico,
        valor_esperado_canonico=valor_esperado_canonico,
        evidencia=tuple(evidencia),
    )


def _candidatos_de_proceso(
    solicitud: SolicitudPreflightUIA,
    procesos: Sequence[ProcesoObservado],
) -> tuple[list[ProcesoObservado], list[str]]:
    """Procesos cuya identidad es compatible con la solicitud, más su evidencia.

    Dos niveles de prueba, y el resultado dice cuál se usó. Si el llamador nombró
    una instalación completa y el sensor pudo leer la ruta del proceso, la
    identidad se prueba contra la ruta ENTERA: dos copias del mismo TexGen en
    discos distintos no son la misma instancia. Si el sensor no pudo leer la ruta
    (permisos), queda el nombre del binario y eso se registra — la identidad más
    débil no se disfraza de la fuerte.
    """
    nombre_esperado = _nombre_de_binario(solicitud.ejecutable_esperado)
    exige_ruta = _es_ruta_completa(solicitud.ejecutable_esperado)
    ruta_esperada = canonicalizar_ruta_windows(solicitud.ejecutable_esperado) if exige_ruta else None

    candidatos: list[ProcesoObservado] = []
    evidencia: list[str] = []
    for proceso in procesos:
        if _nombre_de_binario(proceso.nombre_ejecutable) != nombre_esperado:
            continue
        if ruta_esperada is not None and proceso.ruta_ejecutable:
            observada = canonicalizar_ruta_windows(proceso.ruta_ejecutable)
            if observada != ruta_esperada:
                evidencia.append(f"pid={proceso.pid} descartado: ruta {proceso.ruta_ejecutable!r} != esperada")
                continue
            evidencia.append(f"pid={proceso.pid} identidad por ruta completa")
        else:
            evidencia.append(f"pid={proceso.pid} identidad sólo por nombre de binario")
        candidatos.append(proceso)
    return candidatos, evidencia


def _resolver_proceso(
    solicitud: SolicitudPreflightUIA,
    localizador: LocalizadorDeProcesos,
) -> tuple[ProcesoObservado | None, ResultadoPreflightUIA | None]:
    """Una instancia concreta, o el ``UNKNOWN`` que explica por qué no.

    **Nunca desempata.** Ni por "el primero", ni por el pid más bajo, ni por el
    más reciente: con dos instancias plausibles la observación no puede decir a
    cuál se refiere, y elegir una sería fabricar la certeza que falta.
    """
    candidatos, evidencia = _candidatos_de_proceso(solicitud, localizador.procesos())

    if solicitud.pid is not None:
        con_pid = [proceso for proceso in candidatos if proceso.pid == solicitud.pid]
        if not con_pid:
            return None, _resultado(
                EstadoPreflight.UNKNOWN,
                RazonPreflight.PID_NO_COINCIDE,
                solicitud,
                f"ningún proceso {solicitud.ejecutable_esperado!r} corre con pid {solicitud.pid}",
                evidencia=evidencia,
            )
        candidatos = con_pid

    if not candidatos:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.PROCESO_NO_ENCONTRADO,
            solicitud,
            f"no hay ninguna instancia de {solicitud.ejecutable_esperado!r} corriendo",
            evidencia=evidencia,
        )
    if len(candidatos) > 1:
        pids = ", ".join(str(proceso.pid) for proceso in candidatos)
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.PROCESO_AMBIGUO,
            solicitud,
            f"hay {len(candidatos)} instancias plausibles (pids {pids}); la observación no puede elegir",
            evidencia=evidencia,
        )
    return candidatos[0], None


def _resolver_ventana(
    solicitud: SolicitudPreflightUIA,
    observador: ObservadorUIA,
    proceso: ProcesoObservado,
    evidencia: list[str],
) -> tuple[VentanaObservada | None, ResultadoPreflightUIA | None]:
    """La ventana top-level del proceso, si hay exactamente una.

    Se revalida ``ventana.pid`` contra el proceso resuelto en vez de confiar en
    que el backend haya filtrado: el pid es la única identidad verificable que
    tenemos, y un adapter que devuelva de más no debe poder ampliar el alcance de
    la observación sin que se note.
    """
    ventanas = [ventana for ventana in observador.ventanas_de_proceso(proceso.pid) if ventana.pid == proceso.pid]
    evidencia.extend(f"ventana titulo={ventana.titulo!r} clase={ventana.class_name!r}" for ventana in ventanas)

    if not ventanas:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.VENTANA_NO_ENCONTRADA,
            solicitud,
            f"el pid {proceso.pid} no expone ninguna ventana top-level propia",
            pid=proceso.pid,
            evidencia=evidencia,
        )
    if len(ventanas) > 1:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.VENTANA_AMBIGUA,
            solicitud,
            f"el pid {proceso.pid} expone {len(ventanas)} ventanas top-level; no se elige una",
            pid=proceso.pid,
            evidencia=evidencia,
        )
    return ventanas[0], None


def _resolver_control(
    solicitud: SolicitudPreflightUIA,
    observador: ObservadorUIA,
    proceso: ProcesoObservado,
    ventana: VentanaObservada,
    evidencia: list[str],
) -> tuple[ControlObservado | None, ResultadoPreflightUIA | None]:
    """El control de Output, si los criterios lo señalan sin ambigüedad.

    La búsqueda está acotada al subtree de la ventana del proceso esperado (no se
    recorre el Desktop), y los candidatos se filtran además por pid: un
    ``AutomationId`` homónimo que viva fuera de este proceso no puede crear
    ambigüedad ni ser elegido.
    """
    criterios = solicitud.criterios_del_control
    controles = observador.controles_de_ventana(ventana)
    coinciden = [control for control in controles if criterios.coincide(control)]
    evidencia.extend(control.describir() for control in coinciden)

    if not coinciden:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.CONTROL_NO_ENCONTRADO,
            solicitud,
            f"ningún control de la ventana satisface {criterios.describir()}",
            pid=proceso.pid,
            ventana=ventana.titulo,
            evidencia=evidencia,
        )

    del_proceso = [control for control in coinciden if control.pid == proceso.pid]
    if not del_proceso:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.CONTROL_FUERA_DEL_PROCESO,
            solicitud,
            f"los {len(coinciden)} candidatos pertenecen a otro proceso que el pid {proceso.pid}",
            pid=proceso.pid,
            ventana=ventana.titulo,
            evidencia=evidencia,
        )
    if len(del_proceso) > 1:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.CONTROL_AMBIGUO,
            solicitud,
            f"{len(del_proceso)} controles satisfacen {criterios.describir()}; el selector no es inequívoco",
            pid=proceso.pid,
            ventana=ventana.titulo,
            evidencia=evidencia,
        )
    return del_proceso[0], None


def observar_output(
    solicitud: SolicitudPreflightUIA,
    *,
    localizador: LocalizadorDeProcesos,
    observador: ObservadorUIA,
) -> ResultadoPreflightUIA:
    """Lee el Output que muestra la GUI y lo compara con la salida administrada.

    Es **síncrona** a propósito: las llamadas COM de UI Automation son
    bloqueantes y apartamento-dependientes, así que envolverlas en una corrutina
    acá sólo escondería que el trabajo pasa igual en el hilo del llamador. Quien
    la use desde código async la manda a ``asyncio.to_thread`` y decide él el
    apartamento, que es donde esa decisión pertenece.

    El orden de los cortes no es casual: primero la validación de la SOLICITUD
    (barata, local, y una solicitud mal formada no debe generar tráfico UIA sobre
    la GUI del operador), después la identidad de proceso, después la ventana,
    después el control, y sólo al final la lectura y la comparación. Cada corte
    devuelve ``UNKNOWN`` con su propia razón.

    Nunca lanza: :class:`ObservacionUIAError` y :class:`UIANoDisponibleError` se traducen a
    ``UNKNOWN``. Un preflight que propague excepciones invita a que el llamador
    las trague con un ``except`` amplio y siga como si nada, que es el camino
    exacto por el que un fail-closed se convierte en un fail-open.
    """
    if solicitud.tool not in TOOLS_OBSERVABLES:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.TOOL_DESCONOCIDA,
            solicitud,
            f"{solicitud.tool!r} no está entre las tools observables {sorted(TOOLS_OBSERVABLES)}",
        )
    if solicitud.criterios_del_control.esta_vacio():
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.SELECTOR_SIN_CRITERIOS,
            solicitud,
            "el selector no tiene ningún criterio: matchearía cualquier control de la ventana",
        )
    esperado_canonico = canonicalizar_ruta_windows(solicitud.salida_administrada_esperada)
    if esperado_canonico is None:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.ESPERADO_NO_CANONICALIZABLE,
            solicitud,
            f"la salida administrada esperada {solicitud.salida_administrada_esperada!r} no es una ruta Windows absoluta",
        )

    evidencia: list[str] = []
    try:
        proceso, corte = _resolver_proceso(solicitud, localizador)
        if corte is not None:
            return corte
        assert proceso is not None  # noqa: S101 -- garantizado por _resolver_proceso
        evidencia.append(f"proceso pid={proceso.pid} exe={proceso.ruta_ejecutable or proceso.nombre_ejecutable!r}")

        ventana, corte = _resolver_ventana(solicitud, observador, proceso, evidencia)
        if corte is not None:
            return corte
        assert ventana is not None  # noqa: S101 -- garantizado por _resolver_ventana

        control, corte = _resolver_control(solicitud, observador, proceso, ventana, evidencia)
        if corte is not None:
            return corte
        assert control is not None  # noqa: S101 -- garantizado por _resolver_control

        observado = observador.leer_valor(control)
    except UIANoDisponibleError as exc:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.UIA_NO_DISPONIBLE,
            solicitud,
            f"no hay observación de UI Automation disponible: {exc}",
            valor_esperado_canonico=esperado_canonico,
            evidencia=evidencia,
        )
    except ObservacionUIAError as exc:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.ERROR_UIA,
            solicitud,
            f"la observación de UI Automation falló: {exc}",
            valor_esperado_canonico=esperado_canonico,
            evidencia=evidencia,
        )

    comunes = {
        "pid": proceso.pid,
        "ventana": ventana.titulo,
        "valor_esperado_canonico": esperado_canonico,
        "evidencia": evidencia,
    }

    if observado is None:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.VALOR_NO_LEIBLE,
            solicitud,
            "el control no expone ningún patrón de LECTURA de UIA; no se fuerza el valor por otra vía",
            **comunes,  # type: ignore[arg-type]
        )

    observado_canonico = canonicalizar_ruta_windows(observado)
    if observado_canonico is None:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.OBSERVADO_NO_CANONICALIZABLE,
            solicitud,
            f"el valor observado {observado!r} no es una ruta Windows absoluta canonicalizable",
            valor_observado=observado,
            **comunes,  # type: ignore[arg-type]
        )

    if observado_canonico == esperado_canonico:
        return _resultado(
            EstadoPreflight.MATCH,
            RazonPreflight.OUTPUT_COINCIDE,
            solicitud,
            "el Output que muestra la GUI canonicaliza igual que la salida administrada "
            "(no prueba dónde escribirá una corrida futura)",
            valor_observado=observado,
            valor_observado_canonico=observado_canonico,
            **comunes,  # type: ignore[arg-type]
        )
    return _resultado(
        EstadoPreflight.MISMATCH,
        RazonPreflight.OUTPUT_DIFIERE,
        solicitud,
        f"la GUI muestra {observado_canonico!r} y la salida administrada es {esperado_canonico!r}",
        valor_observado=observado,
        valor_observado_canonico=observado_canonico,
        **comunes,  # type: ignore[arg-type]
    )
