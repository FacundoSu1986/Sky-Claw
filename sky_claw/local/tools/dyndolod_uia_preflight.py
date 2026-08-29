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

**Dos formas de mentir que este módulo se prohíbe explícitamente.** Las dos las
encontró la review de la ronda 1, y las dos comparten forma: convertir *"no
pude comprobarlo"* en *"lo comprobé"*, más débilmente, sin decirlo.

1. *Degradar la identidad.* Si el llamador nombra una instalación
   (``C:\\Modding\\DynDOLOD\\TexGenx64.exe``) y el sensor no puede leer la ruta del
   proceso, comparar sólo el NOMBRE del binario responde una pregunta distinta
   de la que se hizo. Hoy eso es ``IDENTIDAD_NO_DEMOSTRABLE``; la identidad por
   nombre existe únicamente cuando el llamador pidió un nombre.
2. *Recortar la evidencia.* Una colección de UIA truncada en silencio hace que
   dos candidatos parezcan uno, y un candidato único es justo lo que el decisor
   toma como inequívoco. Por eso :func:`exigir_enumeracion_completa` **lanza** en
   vez de recortar: no hay una variante que devuelva una secuencia parcial, así
   que ningún adaptador puede alimentar un veredicto con evidencia incompleta ni
   siquiera por descuido.

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

#: Caracteres que Win32 prohíbe dentro de un componente de ruta. ``\\`` y ``/``
#: no están porque son separadores y se resuelven antes de partir; los de control
#: (``< 0x20`` y ``0x7F``) tampoco, porque se rechazan sobre el texto completo.
#: El ``:`` está acá y se exime SÓLO en el designador de unidad, que valida
#: :data:`_ES_UNIDAD`: en cualquier otra posición abre un Alternate Data Stream
#: (``C:\\x\\a:b`` escribe en el stream ``b`` de ``a``, no en un archivo ``a:b``),
#: así que compararlo como si fuera un nombre de directorio es afirmar un destino
#: que no es el que Win32 resolvería.
_CARACTERES_RESERVADOS_WIN32 = frozenset('<>:"|?*')

#: Cota de elementos que un adaptador materializa por consulta a UIA. Cada
#: elemento cuesta un ``GetElement`` más una lectura POR propiedad, y todas son
#: llamadas COM que cruzan el límite de proceso: el costo está ahí, no en el
#: ``FindAll`` (que arma la colección entera antes de que nadie la recorra). Por
#: eso la cota acota el MARSHALLING y no el recorrido del árbol.
TOPE_DE_ELEMENTOS_UIA = 400


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
    EJECUTABLE_NO_CANONICALIZABLE = "EJECUTABLE_NO_CANONICALIZABLE"
    #: El backend no puede observar en esta plataforma/proceso.
    UIA_NO_DISPONIBLE = "UIA_UNAVAILABLE"
    #: Identidad de proceso y ventana.
    PROCESO_NO_ENCONTRADO = "PROCESO_NO_ENCONTRADO"
    PROCESO_AMBIGUO = "PROCESO_AMBIGUO"
    PID_NO_COINCIDE = "PID_NO_COINCIDE"
    IDENTIDAD_NO_DEMOSTRABLE = "IDENTIDAD_NO_DEMOSTRABLE"
    IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION = "IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION"
    PID_NO_OBSERVABLE = "PID_NO_OBSERVABLE"
    VENTANA_NO_ENCONTRADA = "VENTANA_NO_ENCONTRADA"
    VENTANA_AMBIGUA = "VENTANA_AMBIGUA"
    #: Resolución del control de Output dentro de esa ventana.
    CONTROL_NO_ENCONTRADO = "CONTROL_NO_ENCONTRADO"
    CONTROL_AMBIGUO = "CONTROL_AMBIGUO"
    CONTROL_FUERA_DEL_PROCESO = "CONTROL_FUERA_DEL_PROCESO"
    #: Lectura y comparación.
    VALOR_NO_LEIBLE = "VALOR_NO_LEIBLE"
    ENUMERACION_INCOMPLETA = "ENUMERACION_INCOMPLETA"
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


class EnumeracionIncompletaError(ObservacionUIAError):
    """La colección de UIA no entró entera en la cota: la evidencia es parcial.

    Existe porque un recorte SILENCIOSO fabrica certeza. Si un adaptador
    devuelve los primeros N de N+1 controles, el decisor ve un único candidato
    donde el árbol real tenía dos, lo declara inequívoco y puede emitir un
    ``MATCH`` que nada sostiene — evidencia parcial tratada como completa, que
    es el modo de fallo más caro que tiene este módulo.

    Es SUBCLASE de :class:`ObservacionUIAError` por el mismo motivo que
    :class:`UIANoDisponibleError`: la red general del pipeline la atrapa aunque
    un camino nuevo se olvide de mencionarla.
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


#: Centinela que un adaptador usa cuando no pudo leer ``ProcessId``. NUNCA el
#: pid esperado: el pipeline trata la identidad ilegible como no observable, que
#: es distinto de "pertenece a otro proceso".
#:
#: No es el único valor ilegible que llega, y por eso los guards preguntan por
#: :func:`_pid_es_legible` y no por esta constante — ver su docstring.
PID_ILEGIBLE = -1


@dataclass(frozen=True)
class VentanaObservada:
    """Ventana top-level de un proceso. ``handle`` es opaco para el pipeline.

    ``pid`` no positivo = el backend no pudo leerlo (ver :func:`_pid_es_legible`).
    """

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

    def _criterios(self) -> dict[str, str]:
        """Sólo los criterios con EVIDENCIA: los vacíos o en blanco no cuentan.

        Un `--automation-id ""` desde la CLI parece un criterio y no lo es: un
        ``AutomationId`` vacío no identifica a nadie, y si la ventana tuviera un
        único control con ese campo vacío quedaría elegido sobre evidencia nula.
        Un valor en blanco es "no lo pasé", no "quiero el que esté vacío".
        """
        candidatos = {
            "automation_id": self.automation_id,
            "nombre": self.nombre,
            "tipo_de_control": self.tipo_de_control,
        }
        return {campo: valor for campo, valor in candidatos.items() if valor is not None and valor.strip()}

    def esta_vacio(self) -> bool:
        """``True`` si no queda NINGÚN criterio con evidencia.

        No es "los tres campos son ``None``": un selector de puros blancos
        (``automation_id=""``) también está vacío, porque `_criterios` ya los
        descartó. El pipeline lo traduce a ``UNKNOWN`` antes de mirar el árbol.
        """
        return not self._criterios()

    def coincide(self, control: ControlObservado) -> bool:
        """AND de los criterios CON EVIDENCIA. Comparación exacta, sin heurística.

        Los campos observados se enumeran a mano y no por acceso dinámico: el
        ancla read-only de la suite prohíbe `getattr` en esta superficie, y con
        razón — es el hueco por el que una llamada mutante entraría sin que su
        nombre aparezca en el árbol sintáctico. La enumeración explícita cuesta
        tres líneas y mantiene el guard completo.
        """
        observados = {
            "automation_id": control.automation_id,
            "nombre": control.nombre,
            "tipo_de_control": control.tipo_de_control,
        }
        return all(observados[campo] == valor for campo, valor in self._criterios().items())

    def describir(self) -> str:
        """Los criterios con evidencia, tal como se aplicaron."""
        partes = [f"{campo}={valor!r}" for campo, valor in self._criterios().items()]
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

    def procesos(self) -> Sequence[ProcesoObservado]:
        """Los procesos vivos que el sensor alcanza a ver, SIN filtrar.

        El filtrado por identidad lo hace el pipeline, no la implementación: si
        el sensor decidiera a quién devolver, la prueba de identidad quedaría
        fuera de lo auditable. Un proceso ilegible se OMITE; no se inventa un
        placeholder.

        Lanza :class:`ObservacionUIAError` si el sensor falla como un todo — eso
        es ``UNKNOWN``, distinto de "no hay ninguna instancia corriendo".
        """
        ...


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

    def ventanas_de_proceso(self, pid: int) -> Sequence[VentanaObservada]:
        """Ventanas top-level de ``pid``. **No las enfoca, no las activa, no las ordena.**

        Traer una ventana al frente es una mutación que ven el usuario y la app:
        cambia el foco de teclado. La observación no lo necesita, así que no lo
        hace.

        Puede devolver ventanas de más: el pipeline revalida cada ``pid`` contra
        el proceso resuelto, justamente para que un adapter no pueda ampliar el
        alcance sin que se note. Si el ``ProcessId`` no se pudo leer va
        :data:`PID_ILEGIBLE` y NUNCA el pid pedido (ver :func:`_pid_es_legible`).
        """
        ...

    def controles_de_ventana(self, ventana: VentanaObservada) -> Sequence[ControlObservado]:
        """Descendientes de ``ventana``, **completos o ninguno**.

        Si el árbol no entra en :data:`TOPE_DE_ELEMENTOS_UIA` la implementación
        lanza :class:`EnumeracionIncompletaError` en vez de recortar: los
        primeros N de N+1 le darían al decisor una unicidad que el árbol real no
        tiene, y ése es exactamente el ``MATCH`` falso que este módulo existe
        para no emitir. Ver :func:`exigir_enumeracion_completa`.
        """
        ...

    def leer_valor(self, control: ControlObservado) -> str | None:
        """El valor que el control MUESTRA hoy, o ``None`` si no lo expone.

        Patrones de lectura implementados: ``ValuePattern`` y ``TextPattern``.
        Sólo de lectura: ``ValuePattern`` también tiene operación de escritura y
        acá no se usa. Las primitivas mutantes están fuera del contrato — no
        aparecen en este protocolo, y el ancla por AST de la suite prohíbe
        nombrarlas en toda esta superficie.

        **``LegacyIAccessible`` NO es un camino de lectura**, aunque la sonda lo
        reporte cuando el control lo expone. Es a propósito y es un hueco
        conocido: los controles Win32/Delphi a veces exponen sólo ese patrón, y
        ahí este método devuelve ``None`` y el preflight responde ``UNKNOWN``
        aunque el valor exista. Implementarlo a ciegas sería escribir una rama
        COM que nadie puede ejercitar hasta que haya rig, y un valor leído mal
        es peor que un ``UNKNOWN``: la sonda lo REPORTA justamente para que el
        rig diga si hace falta. Cerrarlo es trabajo de T5-v2, no de acá.
        Hallazgo de review (Qodo); el contrato decía leerlo y no lo leía.

        ``None`` es "no lo expone", que termina en ``UNKNOWN``. No se confunde
        con ``""``, que es un valor leído y vacío.
        """
        ...


class LocalizadorPsutil:
    """Identidad de proceso vía :mod:`psutil` — ya es dependencia del repo.

    La mitad de §9 (¿existe UNA instancia de este binario y cuál es su pid?) no
    necesita nada Windows-only, así que no lo toma: psutil enumera igual en el CI
    de Linux, lo que hace que esta pieza sea testeable de verdad en vez de
    quedar detrás del mismo muro que el árbol UIA.
    """

    def procesos(self) -> Sequence[ProcesoObservado]:
        """Enumera con ``psutil`` y traduce su fallo al error del puerto.

        Un proceso sin nombre legible se omite en vez de entrar con el campo
        vacío: entraría como candidato de un ejecutable que nadie pudo confirmar.
        """
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
      la nota de esa constante;
    * un componente con un carácter reservado de Win32
      (:data:`_CARACTERES_RESERVADOS_WIN32`) **rechaza la ruta**. Es la misma
      regla que ``..``, por el mismo motivo: el sistema no puede abrir esa ruta,
      así que no tiene un destino con el cual comparar. Bajarla a minúsculas y
      compararla como cadena sería afirmar una igualdad entre dos cosas de las
      cuales al menos una no designa nada.
    """
    if valor is None:
        return None
    # La asimetría entre los dos extremos es de Win32, no una preferencia: la
    # normalización de rutas del sistema recorta los espacios FINALES del último
    # componente, así que quitarlos es neutro. Los LÍDERES no: con un espacio
    # adelante la ruta deja de ser absoluta a la unidad —`" C:"` no es una
    # unidad— y el sistema la resuelve como relativa o la rechaza. Es un destino
    # distinto, no el mismo escrito de otra forma, así que se rechaza en vez de
    # limpiarse. Recortar los dos daba MATCH entre `" C:\x"` y `"C:\x"`.
    if valor[:1].isspace():
        return None
    # ANTES del recorte, y el orden es el fix: `rstrip()` se lleva `\t`, `\n` y
    # `\r` igual que un espacio, así que mirando el texto YA recortado un tab
    # final se borraba en silencio y la ruta canonicalizaba igual que la limpia
    # — MATCH donde el contrato promete UNKNOWN. Win32 recorta espacios y puntos
    # finales; tabs y saltos de línea NO. Hallazgo de review (Qodo).
    if any(ord(caracter) < 0x20 or ord(caracter) == 0x7F for caracter in valor):
        return None
    texto = valor.rstrip()
    if not texto:
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
    # Antes de cualquier acceso posicional: un separador pelado (`"\\"`, `"/"`)
    # deja la lista vacía, y armar la lista recortada mira `componentes[0]`. Sin
    # este corte eso era un `IndexError` que escapaba de `observar_output` —la
    # canonicalización del esperado corre fuera de su `try`— y rompía el
    # contrato de que el preflight NUNCA lanza. Hallazgo de review (Qodo).
    if not componentes:
        return None

    # El designador de unidad es el único componente donde `:` es legal, y ahí lo
    # valida `_ES_UNIDAD` más abajo (`^[A-Za-z]:$`), que es más estricto que esta
    # comprobación: eximirlo no le abre la puerta a nada.
    interiores = componentes if es_unc else componentes[1:]
    if any(_CARACTERES_RESERVADOS_WIN32 & set(parte) for parte in interiores):
        return None

    # Win32 recorta los espacios y puntos FINALES de CADA componente de una ruta
    # no extendida, así que `…\DynDOLOD.` y `…\DynDOLOD` son el mismo
    # directorio. El `rstrip` de arriba sólo cubre el final global de la cadena:
    # con un punto de más en el campo Output el pipeline emitía OUTPUT_DIFIERE,
    # un veredicto CONCLUYENTE equivocado — peor que el UNKNOWN que este módulo
    # promete cuando no puede normalizar con seguridad. Hallazgo de review (Qodo).
    #
    # El designador de unidad queda afuera del recorte para no perturbar la
    # prueba de `texto.startswith(unidad + "\\")` de más abajo, que es lo que
    # distingue `C:\x` de `C:x`; un `C:` no lleva puntos ni espacios finales.
    recortados = []
    for parte in interiores:
        limpio = parte.rstrip(" .")
        # Recortar no puede FABRICAR un componente distinto del que había: `...`
        # se quedaría en nada y `   ` también. Eso no es una normalización
        # neutra, así que se rechaza la ruta entera — mismo fail-closed que `..`.
        if not limpio:
            return None
        recortados.append(limpio)
    componentes = recortados if es_unc else [componentes[0], *recortados]

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


def exigir_enumeracion_completa(total: int, *, tope: int = TOPE_DE_ELEMENTOS_UIA, contexto: str) -> int:
    """Devuelve ``total`` si la colección entra entera; si no, LANZA.

    La forma importa: no existe una variante que recorte. Un adaptador no puede
    entregarle al decisor una secuencia parcial "por las dudas", porque la única
    respuesta honesta a una colección que no entra es dejar de responder. Un
    modo diagnóstico sí puede mostrar los primeros N, pero entonces tiene que
    decir que truncó, y esa lista no alimenta ningún veredicto.
    """
    if total > tope:
        raise EnumeracionIncompletaError(
            f"la enumeración de {contexto} devolvió {total} elementos y la cota es {tope}: "
            f"la evidencia sería parcial y un recorte podría esconder un segundo candidato"
        )
    return total


def _nombre_de_binario(ruta: str) -> str:
    """Basename en semántica Windows, en minúsculas. Vale en cualquier plataforma."""
    return ntpath.basename(ruta.replace("/", "\\")).lower()


def _es_ruta_completa(valor: str) -> bool:
    """¿El llamador nombró una instalación concreta o sólo el binario?"""
    normalizado = valor.replace("/", "\\")
    return "\\" in normalizado or ":" in normalizado


def _pid_es_legible(pid: int) -> bool:
    """``True`` sólo si el pid identifica a un proceso.

    Hay DOS valores que significan "no lo pude leer" y ninguno es una excepción:
    UIA devuelve ``ProcessId = 0`` cuando el provider no lo expone —elementos que
    no están basados en HWND—, y el adaptador pone :data:`PID_ILEGIBLE` cuando la
    conversión a entero falla. Ningún proceso real tiene pid ``<= 0``, así que la
    frontera es el SIGNO y no una constante: un guard escrito contra
    ``PID_ILEGIBLE`` deja pasar el ``0``, que después descarta el filtro
    ``pid == proceso.pid`` como si el elemento fuera ajeno. Eso fabrica la
    unicidad que :data:`RazonPreflight.PID_NO_OBSERVABLE` existe para prevenir.

    Vive en un solo lugar a propósito: los dos guards (ventana y control) son
    hermanos y el defecto de este repo es arreglar uno y no el otro.
    """
    return pid > 0


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
    """Constructor ÚNICO de todo veredicto: cada rama del pipeline sale por acá.

    Que sea uno solo es lo que impide que el par ``(estado, razon)`` diverja
    entre caminos, y lo que obliga a una rama nueva a elegir una razón declarada
    en vez de reciclar la de al lado.
    """
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


def _identidad_del_ejecutable(valor: str) -> tuple[str, str | None] | None:
    """``(basename, ruta_canónica | None)`` de lo que pidió el llamador, o ``None``.

    ``None`` significa que la solicitud no nombra un ejecutable comparable, y es
    un corte de VALIDACIÓN, no un fallback: un nombre vacío, o una ruta que
    parece completa pero no se puede canonicalizar. Aceptar esa segunda forma y
    compararla por nombre era el defecto de la ronda 1: convertía "no puedo
    probar la ruta" en "la identidad es el basename", que es una afirmación
    distinta y más débil que la que el llamador pidió.
    """
    nombre = _nombre_de_binario(valor)
    if not nombre.strip():
        return None
    if not _es_ruta_completa(valor):
        # El llamador pidió un binario, no una instalación: identidad por
        # nombre porque es LO QUE PIDIÓ, no porque algo falló.
        return nombre, None
    ruta = canonicalizar_ruta_windows(valor)
    if ruta is None:
        return None
    return nombre, ruta


def _probar_identidad(
    procesos: Sequence[ProcesoObservado],
    ruta_esperada: str,
    evidencia: list[str],
) -> tuple[list[ProcesoObservado], str | None]:
    """Filtra por RUTA COMPLETA. Devuelve ``(candidatos, motivo_de_corte)``.

    Tres desenlaces por proceso, y ninguno es "seguí igual":

    * la ruta observada canonicaliza y coincide → candidato;
    * canonicaliza y difiere → NO es candidato (otra instalación del mismo
      binario), se registra en la evidencia y se sigue;
    * no hay ruta observable (``AccessDenied``) o no canonicaliza → la identidad
      de ESTE proceso no se puede probar ni refutar, así que tampoco se puede
      afirmar cuántas instancias de la instalación pedida hay. Corta.
    """
    candidatos: list[ProcesoObservado] = []
    for proceso in procesos:
        if not proceso.ruta_ejecutable:
            evidencia.append(f"pid={proceso.pid}: el sensor no expone la ruta del ejecutable")
            return [], f"no se puede probar que el pid {proceso.pid} sea {ruta_esperada!r}"
        observada = canonicalizar_ruta_windows(proceso.ruta_ejecutable)
        if observada is None:
            evidencia.append(f"pid={proceso.pid}: ruta {proceso.ruta_ejecutable!r} no canonicalizable")
            return [], f"la ruta observada del pid {proceso.pid} no se puede comparar"
        if observada != ruta_esperada:
            evidencia.append(f"pid={proceso.pid} descartado: {proceso.ruta_ejecutable!r} es otra instalación")
            continue
        evidencia.append(f"pid={proceso.pid} identidad probada por ruta completa")
        candidatos.append(proceso)
    return candidatos, None


def _resolver_proceso(
    solicitud: SolicitudPreflightUIA,
    localizador: LocalizadorDeProcesos,
) -> tuple[ProcesoObservado | None, ResultadoPreflightUIA | None, list[str]]:
    """Una instancia concreta, o el ``UNKNOWN`` que explica por qué no.

    **Nunca desempata** —ni por "el primero", ni por el pid más bajo— y **nunca
    degrada la identidad**: si el llamador nombró una instalación, la respuesta
    sale de comparar la instalación, o no sale.

    El orden importa: el filtro por ``pid`` va ANTES de la prueba de identidad.
    Un tercero homónimo con ``exe`` ilegible no puede invalidar una observación
    que el llamador ya ató a un pid concreto; sin pid, en cambio, ese mismo
    tercero sí hace imposible afirmar que hay exactamente una instancia de lo
    que se pidió.
    """
    evidencia: list[str] = []
    identidad = _identidad_del_ejecutable(solicitud.ejecutable_esperado)
    if identidad is None:
        return (
            None,
            _resultado(
                EstadoPreflight.UNKNOWN,
                RazonPreflight.EJECUTABLE_NO_CANONICALIZABLE,
                solicitud,
                f"el ejecutable esperado {solicitud.ejecutable_esperado!r} no nombra un binario comparable",
            ),
            evidencia,
        )
    nombre_esperado, ruta_esperada = identidad

    candidatos = [p for p in localizador.procesos() if _nombre_de_binario(p.nombre_ejecutable) == nombre_esperado]

    if solicitud.pid is not None:
        con_pid = [proceso for proceso in candidatos if proceso.pid == solicitud.pid]
        if not con_pid:
            return (
                None,
                _resultado(
                    EstadoPreflight.UNKNOWN,
                    RazonPreflight.PID_NO_COINCIDE,
                    solicitud,
                    f"ningún proceso {nombre_esperado!r} corre con pid {solicitud.pid}",
                    evidencia=evidencia,
                ),
                evidencia,
            )
        candidatos = con_pid

    if ruta_esperada is not None:
        candidatos, corte = _probar_identidad(candidatos, ruta_esperada, evidencia)
        if corte is not None:
            return (
                None,
                _resultado(
                    EstadoPreflight.UNKNOWN,
                    RazonPreflight.IDENTIDAD_NO_DEMOSTRABLE,
                    solicitud,
                    corte,
                    evidencia=evidencia,
                ),
                evidencia,
            )
    else:
        evidencia.extend(f"pid={p.pid} identidad sólo por nombre de binario (es lo pedido)" for p in candidatos)

    if not candidatos:
        return (
            None,
            _resultado(
                EstadoPreflight.UNKNOWN,
                RazonPreflight.PROCESO_NO_ENCONTRADO,
                solicitud,
                f"no hay ninguna instancia de {solicitud.ejecutable_esperado!r} corriendo",
                evidencia=evidencia,
            ),
            evidencia,
        )
    if len(candidatos) > 1:
        pids = ", ".join(str(proceso.pid) for proceso in candidatos)
        return (
            None,
            _resultado(
                EstadoPreflight.UNKNOWN,
                RazonPreflight.PROCESO_AMBIGUO,
                solicitud,
                f"hay {len(candidatos)} instancias plausibles (pids {pids}); la observación no puede elegir",
                evidencia=evidencia,
            ),
            evidencia,
        )
    return candidatos[0], None, evidencia


def _huella(proceso: ProcesoObservado) -> tuple[str, str | None]:
    """Lo que hace que un pid siga denotando lo mismo que cuando se probó."""
    ruta = canonicalizar_ruta_windows(proceso.ruta_ejecutable) if proceso.ruta_ejecutable else None
    return proceso.nombre_ejecutable.lower(), ruta


def _revalidar_identidad(
    solicitud: SolicitudPreflightUIA,
    localizador: LocalizadorDeProcesos,
    proceso: ProcesoObservado,
    evidencia: list[str],
) -> ResultadoPreflightUIA | None:
    """Vuelve a probar la identidad DESPUÉS de observar, o corta con ``UNKNOWN``.

    Probar la identidad una vez no es probarla durante toda la observación. El
    pid se demuestra contra una fotografía de psutil y después se usa para
    enumerar la ventana, el control y leer el valor: si en el medio el proceso
    muere y Windows RECICLA el pid, todo lo observado es de otro proceso, pasa
    los filtros ``pid == proceso.pid`` y sale un veredicto concluyente que
    ningún proceso verificado sostiene. Era el único hueco fail-open del
    pipeline — una degradación de identidad por TIEMPO, no por sensor.

    La revalidación va DESPUÉS y no antes a propósito: chequear antes sólo
    corre la ventana de carrera unos microsegundos, mientras que chequear
    después la ENCIERRA — cubre el reciclado que haya ocurrido durante la
    observación, que es cuando puede ocurrir.

    **No la elimina, y decirlo importa:** el pid podría reciclarse justo después
    de esta segunda lectura. Cerrar la carrera del todo exige un handle del
    sistema operativo que mantenga vivo el pid mientras se observa, y eso es
    trabajo de T5-v2 con un backend real. Lo que esto garantiza es que un
    reciclado que ocurra DURANTE la observación no puede producir un veredicto
    concluyente. Hallazgo de review (Qodo).
    """
    try:
        actuales = list(localizador.procesos())
    except ObservacionUIAError as exc:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION,
            solicitud,
            f"no se pudo revalidar la identidad del pid {proceso.pid} después de observar: {exc}",
            pid=proceso.pid,
            evidencia=evidencia,
        )

    vigentes = [candidato for candidato in actuales if candidato.pid == proceso.pid]
    if len(vigentes) != 1 or _huella(vigentes[0]) != _huella(proceso):
        motivo = (
            f"el pid {proceso.pid} ya no existe"
            if not vigentes
            else f"el pid {proceso.pid} ya no denota {proceso.ruta_ejecutable or proceso.nombre_ejecutable!r}"
        )
        evidencia.append(f"revalidación: {motivo}")
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.IDENTIDAD_CAMBIO_DURANTE_LA_OBSERVACION,
            solicitud,
            f"{motivo}: lo observado no lo sostiene ningún proceso verificado",
            pid=proceso.pid,
            evidencia=evidencia,
        )
    return None


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
    observadas = list(observador.ventanas_de_proceso(proceso.pid))
    evidencia.extend(f"ventana titulo={ventana.titulo!r} clase={ventana.class_name!r}" for ventana in observadas)

    # ANTES de filtrar por pid: el filtro descartaría estas como "ajenas", y no
    # lo son — su identidad simplemente no se pudo leer. Ver `PID_ILEGIBLE`.
    ilegibles = [ventana for ventana in observadas if not _pid_es_legible(ventana.pid)]
    if ilegibles:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.PID_NO_OBSERVABLE,
            solicitud,
            f"{len(ilegibles)} ventana(s) sin ProcessId legible: no se puede afirmar a qué proceso pertenecen",
            pid=proceso.pid,
            evidencia=evidencia,
        )

    ventanas = [ventana for ventana in observadas if ventana.pid == proceso.pid]

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

    ilegibles = [control for control in coinciden if not _pid_es_legible(control.pid)]
    if ilegibles:
        return None, _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.PID_NO_OBSERVABLE,
            solicitud,
            f"{len(ilegibles)} de {len(coinciden)} candidatos no exponen ProcessId: "
            "un pid ilegible no prueba ajenidad, prueba que la unicidad no se puede afirmar",
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
        proceso, corte, evidencia = _resolver_proceso(solicitud, localizador)
        if corte is not None:
            return corte
        assert proceso is not None  # noqa: S101 -- garantizado por _resolver_proceso

        ventana, corte = _resolver_ventana(solicitud, observador, proceso, evidencia)
        if corte is not None:
            return corte
        assert ventana is not None  # noqa: S101 -- garantizado por _resolver_ventana

        control, corte = _resolver_control(solicitud, observador, proceso, ventana, evidencia)
        if corte is not None:
            return corte
        assert control is not None  # noqa: S101 -- garantizado por _resolver_control

        observado = observador.leer_valor(control)

        # Recién acá: la identidad tiene que seguir siendo la misma que se probó
        # ANTES de mirar la GUI. Ver `_revalidar_identidad`.
        corte = _revalidar_identidad(solicitud, localizador, proceso, evidencia)
        if corte is not None:
            return corte
    except EnumeracionIncompletaError as exc:
        return _resultado(
            EstadoPreflight.UNKNOWN,
            RazonPreflight.ENUMERACION_INCOMPLETA,
            solicitud,
            f"la observación fue parcial y no se puede afirmar unicidad: {exc}",
            valor_esperado_canonico=esperado_canonico,
            evidencia=evidencia,
        )
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
