"""T5-v2 — el gate productivo del preflight UIA: fail-closed sobre la corrida.

**La pregunta que responde, entera.** *"El control Output que ESTA instancia
recién lanzada muestra por UIA, ¿coincide con la salida administrada que
Sky-Claw le pasó por ``-o:``?"* La observación pura devuelve ``MATCH`` /
``MISMATCH`` / ``UNKNOWN``; el protocolo de readiness (T5-v2.1) la clasifica en
dos paradas distintas —initial y final— y NUNCA continúa el pipeline sobre una
observación divergente o inconclusa sin pasar por ellas.

**Initial gate (readiness, no autorización).** :func:`clasificar_initial` decide
si la GUI está lista para que un humano la configure:

* ``MATCH`` + ``OUTPUT_COINCIDE`` → ``INITIAL_MATCH`` → HITL (el campo Output es
  editable y todavía falta elegir preset/worldspaces).
* ``MISMATCH`` + ``OUTPUT_DIFIERE`` con identidad, ventana, control y valor
  válidos → ``INITIAL_CONFIGURABLE_MISMATCH`` → HITL para la corrección humana
  (el rig real midió un preset rancio precargado). NO autoriza Start.
* ``UNKNOWN``, ambigüedad, identidad no demostrable o sensor no disponible →
  ``INITIAL_BLOCKED`` → fail-closed (el humano no lo resuelve desde esta GUI).

**Final gate (autorización).** Tras la confirmación humana se RE-OBSERVA desde
cero: sólo ``EstadoPreflight.MATCH`` deja continuar; ``MISMATCH`` o ``UNKNOWN``
fallan CERRADO y el runner termina el proceso.

**Contrato DÉBIL, y dicho así adrede (F2, requisito del dueño del rig).** La
garantía es contractual: *Sky-Claw no continúa hasta ``MATCH`` y el operador
no debe interactuar con Start durante el gate*. NO es la garantía fuerte —
*"el humano físicamente no puede pulsar Start antes del ``MATCH``"*— porque la
GUI pertenece al binario recién lanzado y acepta input nativo desde el spawn;
bloquearla exigiría mutar una ventana ajena (``EnableWindow``, hooks de input
o similares), que el alcance read-only de T5-v2 prohíbe. La carrera humano vs
``MATCH`` es una limitación documentada de una etapa asistida, no un
invariante de este módulo. ``MATCH`` habilita la espera normal; **no** es un
comprobante de escritura física (eso sigue siendo del post-check de
artefactos, que es independiente y posterior).

**Qué es este módulo y qué no.** Es un lazo *acotado* alrededor de
:func:`sky_claw.local.tools.dyndolod_uia_preflight.observar_output`: la decisión
MATCH / MISMATCH / UNKNOWN vive entera en el preflight (T5A) y este módulo sólo
le agrega dos cosas que el preflight declaradamente no tiene: un **deadline
monotónico** y la **clasificación de razones transitorias de arranque**. No
conoce COM, ni procesos.

**Cómo se ejecuta la observación (T5-v2.1).** El seam es
:class:`EjecutorDeGateUIA`, con dos implementaciones que NO son intercambiables
por comodidad:

* PRODUCCIÓN — ``EjecutorGatePorHelper``: la observación corre en un **proceso
  descartable** con el COM aislado adentro; el deadline del padre es DURO
  (``kill``/reap del helper), así que una llamada COM que nunca retorna no puede
  colgar al runner. Es el que cablea el composition root.
* TESTS/RIG — ``EjecutorGateEnProceso``: corre el gate en ``asyncio.to_thread``
  y sólo es honesto con **observadores cooperativos** (``to_thread`` no cancela
  el hilo subyacente; una llamada COM real colgada lo bloquearía). El censo de
  wiring exige el helper en todo constructor productivo.

La cancelación externa viaja como ``CancelledError`` en el await de afuera; en
producción esa cancelación mata y reapea el helper antes de propagar.

**Por qué reintentar ALGO y no todo.** Una GUI tarda entre milisegundos y lo
que el operador tarde con un modal en aparecer *lista*; cortar a la primera
lectura haría del arranque normal un ``UNKNOWN`` espurio. Pero reintentar un
``UNKNOWN`` estructural (ambigüedad, identidad, ruta incanonizable) es la
definición de un timeout inútil: ninguna de esas condiciones cambia porque el
reloj avance. La partición exacta vive en :data:`RAZONES_TRANSITORIAS_DE_INICIO`
y está **congelada por igualdad literal** en los tests: mover una razón de caja
es una decisión, no un detalle.

**Razones transitorias y su evidencia.** Cada una fue observada o se deriva de
un estado medido del rig T5A (2026-08-29, evidencia externa al repo):

* ``PROCESO_NO_ENCONTRADO`` / ``PID_NO_COINCIDE`` — entre ``CreateProcess`` y el
  próximo sondeo de psutil puede no existir todavía la fila del proceso propio.
* ``VENTANA_NO_ENCONTRADA`` — el proceso existe y todavía no creó su ventana
  top-level.
* ``CONTROL_NO_ENCONTRADO`` — **medido**: el modal ``LOD billboard(s) not
  found.`` de DynDOLOD (``#32770``) deja el árbol en 34 controles SIN el
  ``Edit``/``TEdit`` del wizard; sólo tras el ``Ignore`` humano aparece (52
  controles). Y además aplica al arranque ordinario anterior al wizard.
* ``VALOR_NO_LEIBLE`` — un ``TEdit`` recién creado puede no tener texto todavía
  (vacío no es lectura, ver ``primer_texto_no_vacio``).

Razones terminalmente NO transitorias, con el motivo por omisión: un
``MISMATCH`` es un veredicto *concluyente* sobre el estado actual del preset
(reintentar sólo dilataría el bloqueo); ``CONTROL_AMBIGUO`` /
``VENTANA_AMBIGUA`` / ``PROCESO_AMBIGUO`` son estructurales; toda la familia
``*_NO_CANONICALIZABLE`` / ``IDENTIDAD_*`` es determinista sobre la solicitud;
``ENUMERACION_INCOMPLETA`` es evidencia parcial prohibida por diseño;
``ERROR_UIA`` y ``UIA_NO_DISPONIBLE`` son del sensor, no del timing de la GUI.
"""

from __future__ import annotations

import enum
import json
import logging
import math
import pathlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sky_claw.local.tools.dyndolod_uia_preflight import (
    CriteriosDeControl,
    EstadoPreflight,
    LocalizadorDeProcesos,
    ObservacionUIAError,
    ObservadorLiberable,
    ObservadorUIA,
    RazonPreflight,
    ResultadoPreflightUIA,
    SolicitudPreflightUIA,
    observar_output,
    par_estado_razon_valido,
)

#: Cota del "GUI lista" del gate. **No** es el timeout de 4 h de la etapa — eso
#: cubre la generación entera; esto cubre nacer hasta exponer el Output. El valor
#: es deliberadamente generoso con un HUMANO en frente: el rig T5A midió que
#: DynDOLOD abre un modal (``LOD billboard(s) not found.``) que exige un clic de
#: ``Ignore`` antes de que el wizard —y su campo Output— exista, y la etapa 9 es
#: asistida por diseño (el operador está en la máquina). 5 minutos cubren esa
#: interacción sin reutilizar ciegamente la ventana de la corrida. Si el plazo
#: se agota, el gate falla CERRADO y el proceso se termina: nada queda huérfano
#: a la espera de un modal que nadie resolvió.
GATE_UIA_TIMEOUT_SEGUNDOS = 300.0

#: Cadencia de sondeo dentro del deadline. Cada ronda cuesta una enumeración UIA
#: completa (ventana + árbol de descendientes: ~50-115 elementos en el rig
#: medido), así que un segundo es el punto entre latencia de detección y tráfico
#: COM cross-process.
GATE_UIA_INTERVALO_SEGUNDOS = 1.0

#: Deadline del FINAL gate (posterior a la confirmación humana). **Corto a
#: propósito**, y distinto del inicial: el operador ya declaró "configuración
#: lista" y la GUI está idle, así que lo único que puede quedar pendiente es que
#: un ``TEdit`` redibujado al aplicar el preset vuelva a exponer texto
#: (``VALOR_NO_LEIBLE`` es la ÚNICA razón transitoria final). Regalar los 300 s
#: del inicial a un cuelgue del sensor posterior a la aprobación sólo dilata el
#: fail-closed; 30 s cubren de sobra un redraw.
GATE_UIA_TIMEOUT_FINAL_SEGUNDOS = 30.0

#: Razones que PUEDEN desaparecer solas durante el arranque de la GUI. Ver el
#: docstring del módulo para la evidencia de cada una. Congelado por igualdad
#: literal en ``tests/test_dyndolod_uia_gate.py``.
RAZONES_TRANSITORIAS_DE_INICIO: frozenset[RazonPreflight] = frozenset(
    {
        RazonPreflight.PROCESO_NO_ENCONTRADO,
        RazonPreflight.PID_NO_COINCIDE,
        RazonPreflight.VENTANA_NO_ENCONTRADA,
        RazonPreflight.CONTROL_NO_ENCONTRADO,
        RazonPreflight.VALOR_NO_LEIBLE,
    }
)


class VeredictoInitial(enum.Enum):
    """La clasificación del INITIAL gate: readiness de la GUI, no autorización.

    **Por qué no alcanza con ``EstadoPreflight``.** El pipeline contesta *"¿el
    valor que la GUI muestra coincide?"* con tres cajas (MATCH / MISMATCH /
    UNKNOWN). El protocolo de readiness necesita una pregunta DISTINTA en su
    primera parada: *"¿la GUI de ESTA instancia está lista para que un humano la
    configure y después confirmemos?"*. Un ``MISMATCH`` concluyente —proceso
    correcto, identidad probada, ventana única, control único, valor legible y
    canonicalizable que simplemente DIFIERE— es exactamente el estado inicial del
    rig real: TexGen pre-carga un preset rancio y el operador corrige el campo
    antes de Start (rig T5-v2, 2026-09-10). Bloquear ahí mata el proceso antes de
    que el único que puede corregirlo —el humano— sea consultado.

    Los tres valores NO son una escala de severidad ni un bool con adornos:
    ``CONFIGURABLE_MISMATCH`` habilita la corrección humana y **no autoriza
    Start**; el final gate re-observa desde cero y su única autorización es
    ``MATCH`` (``EstadoPreflight.MATCH``). ``BLOCKED`` conserva el fail-closed
    para todo lo que un humano no puede resolver en esta instancia: sensor caído,
    identidad no demostrable, ambigüedad o enumeración incompleta.
    """

    MATCH = "INITIAL_MATCH"
    CONFIGURABLE_MISMATCH = "INITIAL_CONFIGURABLE_MISMATCH"
    BLOCKED = "INITIAL_BLOCKED"


def clasificar_initial(resultado: ResultadoPreflightUIA) -> VeredictoInitial:
    """Policy del initial gate: qué resultado permite entrar a la fase HITL.

    **No toca la semántica de** :func:`observar_output`: un ``MISMATCH`` sigue
    siendo ``MISMATCH``. Es una policy SEPARADA sobre el par ``(estado, razón)``,
    y eso es deliberado: el día que una razón nueva caiga en la caja equivocada,
    la clasificación sigue siendo fail-closed por construcción (todo lo que no
    esté enumerado abajo es ``BLOCKED``).

    Las dos únicas entradas configurable/verde son las que el pipeline emite
    después de probar identidad por pid + ruta, unicidad de ventana y de control,
    y canonicalización del valor leído:

    * ``MATCH`` + ``OUTPUT_COINCIDE`` → ``MATCH``: la GUI ya muestra la salida
      administrada; igual se convoca al operador (el campo es editable y todavía
      falta que elija preset/worldspaces).
    * ``MISMATCH`` + ``OUTPUT_DIFIERE`` → ``CONFIGURABLE_MISMATCH``: la GUI está
      inequívocamente identificada y el valor es legible; sólo hay que corregirlo
      a mano.

    Todo lo demás —``UNKNOWN`` de cualquier razón, y cualquier combinación
    desconocida— es ``BLOCKED``. Congelado por enumeración de las 21 razones en
    ``tests/test_dyndolod_uia_gate.py``.
    """
    if resultado.estado is EstadoPreflight.MATCH and resultado.razon is RazonPreflight.OUTPUT_COINCIDE:
        return VeredictoInitial.MATCH
    if resultado.estado is EstadoPreflight.MISMATCH and resultado.razon is RazonPreflight.OUTPUT_DIFIERE:
        return VeredictoInitial.CONFIGURABLE_MISMATCH
    return VeredictoInitial.BLOCKED


def resultado_proceso_muerto(solicitud: SolicitudPreflightUIA) -> ResultadoPreflightUIA:
    """El ``UNKNOWN`` honesto de "el proceso observado ya no está".

    Existe porque la observación corre en un helper descartable y la muerte de la
    instancia se detecta en el PADRE (la vigilia del runner), no como una razón
    del pipeline: cuando la carrera la gana la muerte, no hay último resultado
    que devolver. ``PROCESO_NO_ENCONTRADO`` describe exactamente lo observado y
    cae en ``BLOCKED`` por la policy del initial gate.
    """
    return ResultadoPreflightUIA(
        estado=EstadoPreflight.UNKNOWN,
        razon=RazonPreflight.PROCESO_NO_ENCONTRADO,
        tool=solicitud.tool,
        detalle="el proceso observado terminó durante el gate de Output: no se re-observa un binario muerto",
        valor_esperado=solicitud.salida_administrada_esperada,
        pid=solicitud.pid,
    )


def resultado_sin_backend(
    solicitud: SolicitudPreflightUIA,
    exc: ObservacionUIAError,
) -> ResultadoPreflightUIA:
    """El ``UNKNOWN`` de "no se pudo construir el observador", mismo fail-closed.

    La fábrica tira ``UIANoDisponibleError`` (plataforma, comtypes ausente, COM
    no inicializable) ANTES de la primera observación: no hay resultado del
    pipeline que reenviar, así que se sintetiza el equivalente exacto de lo que
    el preflight habría respondido si el primer ``ventanas_de_proceso`` fallara
    — ``UNKNOWN`` con ``UIA_NO_DISPONIBLE`` — en vez de una excepción que el
    runner tendría que adivinar.

    Es público desde T5-v2.1 porque el runner lo reusa para el corte por
    GRACIA EXTERNA (un COM colgado más allá del deadline del gate): el
    veredicto honesto ahí también es "el sensor no respondió", y duplicar el
    constructor en el runner volvería a abrir la divergencia
    ``(estado, razón)`` que este helper existe para cerrar.
    """
    return ResultadoPreflightUIA(
        estado=EstadoPreflight.UNKNOWN,
        razon=RazonPreflight.UIA_NO_DISPONIBLE,
        tool=solicitud.tool,
        detalle=f"no se pudo construir el observador de UI Automation: {exc}",
        valor_esperado=solicitud.salida_administrada_esperada,
        pid=solicitud.pid,
    )


def exigir_plazo_positivo_finito(
    valor: object,
    nombre: str,
    *,
    error: type[Exception] = ValueError,
) -> float:
    """Rechaza 0, negativos, bool, NaN e infinitos en fronteras de deadline.

    ``bool`` es subclase de ``int`` y no es un presupuesto de tiempo.
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise error(f"{nombre} tiene que ser un número finito > 0; llegó {type(valor).__name__}")
    numero = float(valor)
    if not math.isfinite(numero) or numero <= 0:
        raise error(f"{nombre} tiene que ser un número finito > 0; llegó {valor!r}")
    return numero


def ejecutar_gate_sincrono(
    solicitud: SolicitudPreflightUIA,
    *,
    fabrica_observador: Callable[[], ObservadorUIA],
    localizador: LocalizadorDeProcesos,
    timeout_segundos: float,
    intervalo_segundos: float,
    reloj: Callable[[], float] = time.monotonic,
    dormir: Callable[[float], None] = time.sleep,
    proceso_vivo: Callable[[], bool] | None = None,
    al_progreso: Callable[[ResultadoPreflightUIA], None] | None = None,
    razones_transitorias: frozenset[RazonPreflight] = RAZONES_TRANSITORIAS_DE_INICIO,
) -> ResultadoPreflightUIA:
    """Ejecuta el gate hasta veredicto concluyente, deadline o muerte del proceso.

    **Síncrono a propósito** (mismo motivo que ``observar_output``): las llamadas
    COM son bloqueantes y de apartamento; el runner manda la llamada entera a un
    ``asyncio.to_thread`` y la fábrica construye el observador **dentro de ese
    hilo**, que es donde el apartamento STA queda atado.

    Política, en orden — cada rama sale con el resultado que la produjo, jamás
    con uno inventado:

    1. ``MATCH`` o ``MISMATCH`` → retorno inmediato (veredicto concluyente).
    2. ``UNKNOWN`` con razón NO transitoria → inmediato (estructural).
    3. ``UNKNOWN`` transitorio con el proceso muerto → inmediato: otra ronda de
       UIA no resucita al binario, y esperar al deadline sólo dilata el rojo.
    4. ``UNKNOWN`` transitorio con deadline agotado → el ÚLTIMO resultado, con su
       razón: es la evidencia diagnóstica del corte ("se esperó y seguía sin
       aparecer la ventana", no "algo raro pasó").
    5. En cualquier otro estado → dormir **contra el deadline** y reintentar.

    ``al_progreso`` se invoca sólo cuando la razón CAMBIA (una GUI sana no debe
    generar un log por segundo), y nunca antes de la primera observación.

    ``razones_transitorias`` selecciona la POLÍTICA de reintento: el gate
    inicial (T5-v2) usa ``RAZONES_TRANSITORIAS_DE_INICIO`` (la GUI puede estar
    todavía naciendo y hay un modal HITL arrancando); el final gate (T5-v2.1,
    tras la confirmación humana) usa ``RAZONES_TRANSITORIAS_FINAL`` — quien ya
    dijo "listo" no vuelve a abrir la ventana de arriba. Es un parámetro del
    seam y no una bifurcación del loop: el poll/deadline/cleanup son el mismo
    mecanismo para los dos.
    """
    # Error de programación/configuración, no del rig: el gate no puede
    # decidir con un presupuesto no finito. Se lanza (no se traduce a
    # UNKNOWN) porque un NaN/inf/cero acá es un bug del caller, y un
    # UNKNOWN lo disfrazaría de "la GUI no respondió".
    exigir_plazo_positivo_finito(timeout_segundos, "timeout_segundos")
    exigir_plazo_positivo_finito(intervalo_segundos, "intervalo_segundos")

    try:
        observador = fabrica_observador()
    except ObservacionUIAError as exc:
        return resultado_sin_backend(solicitud, exc)

    limite = reloj() + timeout_segundos
    razon_anterior: RazonPreflight | None = None
    try:
        while True:
            resultado = observar_output(solicitud, localizador=localizador, observador=observador)
            if resultado.estado is not EstadoPreflight.UNKNOWN:
                return resultado
            if resultado.razon not in razones_transitorias:
                return resultado
            if proceso_vivo is not None and not proceso_vivo():
                return resultado
            restante = limite - reloj()
            if restante <= 0:
                return resultado
            if al_progreso is not None and resultado.razon is not razon_anterior:
                al_progreso(resultado)
                razon_anterior = resultado.razon
            # El sueño NUNCA se pasa del deadline: un `time.sleep(1)` sin cota sería
            # la forma barata de que "acotado" dejara de ser verdad por un intervalo.
            dormir(min(intervalo_segundos, restante))
    finally:
        # F4 — el observador que abrió recursos en ESTE hilo los cierra acá, en
        # TODO camino (MATCH/MISMATCH/UNKNOWN/excepción). La capacidad es
        # optativa (``ObservadorLiberable``): un observador sin ella no deja
        # nada pendiente por construcción. ``isinstance`` con Protocol
        # ``runtime_checkable``: nada de despacho dinámico — el ancla read-only
        # de la superficie prohíbe ``getattr``.
        if isinstance(observador, ObservadorLiberable):
            observador.liberar()


# =============================================================================
# T5-v2.1 — Readyness del operador + final gate sobre la MISMA instancia
# =============================================================================
#
# El ligado entre la espera de readyness y los dos gates UIA lo implementa el
# runner en ``DynDOLODRunner._protocolo_de_readiness``: initial gate →
# instrucciones al operador → confirmación HITL (con carrera explícita contra
# la muerte del proceso) → final gate → aviso "Start habilitado". Este módulo
# define SÓLO los tipos del puerto (la pregunta "el humano terminó de
# configurar") para que un adaptador nuevo no tenga que conocer el runner.
#
# **Por qué no es un ``bool``.** Un bool aplasta cuatro cases distintos:
# True (el operador aprobó), False (rechazó), timeout (nunca respondió)
# y canal no disponible (ninguna superficie del rig puede preguntarle).
# Los últimos tres deben ser fail-closed con distinta evidencia — aplastarlos
# recrea el fallo mudo que esta etapa cierra.


@dataclass(frozen=True)
class OperatorConfigurationReadyRequest:
    """Lo que el runner sabe sobre la instancia cuando pide la readyness.

    **Inmutable y estrecho a propósito**: el confirmador no debe pedirle al
    runner que recalcule nada. El pid es el de la instancia ORIGINAL
    (``proc.pid``) — el mismo que el initial gate verificó; el
    ``expected_output`` es el MISMO que la solicitud del initial gate llevaba —
    la única fuente de "lo que se esperaba": el caller lo deriva del layout
    del workspace y lo cita por string, igual que el ``-o:`` del argv.

    ``observed_output`` es el valor CRUDO que la GUI mostraba en el initial gate
    (lo que el operador tiene que corregir si difiere del esperado). Es
    ``None`` sólo si el pipeline no llegó a leer un valor — que en la práctica
    no ocurre para MATCH/MISMATCH, porque ambos exigen una lectura
    canonicalizable; el adaptador lo muestra como "no disponible" en vez de
    inventarlo.

    ``timeout_seconds`` es el deadline de la confirmación que el runner pide
    que el adaptador respete con su propio reloj; el runner ADEMÁS acota la
    espera del callable con su ``asyncio.wait_for`` (defensa en profundidad:
    un adaptador roto no puede colgar al runner de por vida).
    """

    tool_name: str
    pid: int
    executable: pathlib.Path
    expected_output: pathlib.Path
    timeout_seconds: float
    observed_output: str | None = None


class ResultadoConfirmacion(enum.Enum):
    """Lo que el puerto de confirmación puede responder.

    **APROBADA** es el único valor que habilita el final gate. Las demás son
    razones de CORTE del protocolo. ``TIMEOUT`` lo emite el canal humano (el
    HITL no llegó en el plazo) y el runner también lo produce localmente si el
    callable excede su presupuesto. ``CANAL_NO_DISPONIBLE`` cubre tanto "no
    hay confirmador cableado" como "el canal existía pero falló": la corrida
    se corta igual y la evidencia conserva el detalle.
    """

    APROBADA = "aprobada"
    DENEGADA = "denegada"
    TIMEOUT = "timeout"
    CANAL_NO_DISPONIBLE = "canal_no_disponible"


class ResolucionProtocoloReadiness(enum.Enum):
    """El desenlace con el que el runner cierra el protocolo de readyness.

    Incluye los mismos desenlaces del canal humano (tras traducir
    ``ResultadoConfirmacion``) más ``PROCESO_TERMINO``: la muerte del proceso
    DURANTE la espera de la confirmación. No es una decisión humana ni del
    confirmador — es lifecycle del runner, y vive separado a propósito para
    que ``ResultadoConfirmacion`` jamás pueda mentir con un valor que el
    humano no eligió.
    """

    APROBADA = "aprobada"
    DENEGADA = "denegada"
    TIMEOUT = "timeout"
    CANAL_NO_DISPONIBLE = "canal_no_disponible"
    PROCESO_TERMINO = "proceso_termino"


class ConfirmadorDeConfiguracion(Protocol):
    """Puerto estrecho del runner hacia la superficie que conoce al operador.

    El runner NO conoce NiceGUI ni Telegram: la traducción la hace el
    adaptador (``ConfirmadorHITL`` en ``sky_claw.app.orchestrator``), que
    reutiliza :class:`HITLGuard`. Un valor de retorno fuera de
    ``ResultadoConfirmacion`` es bug del adaptador: el runner lo traduce a
    ``CANAL_NO_DISPONIBLE``, nunca a un bool ni a un "continue".
    """

    async def confirmar(
        self,
        solicitud: OperatorConfigurationReadyRequest,
    ) -> ResultadoConfirmacion:
        """Bloquea hasta que el operador decide (o el canal falla).

        DEBE respetar ``solicitud.timeout_seconds`` con su propio reloj;
        el runner lo acota además desde afuera (defensa en profundidad).
        """
        ...

    async def informar(self, *, tool: str, mensaje: str) -> None:
        """Aviso POST-final-MATCH ("Output verificado, puede pulsar Start").

        Es best-effort SOLO porque la instrucción contractual completa ya está
        en el texto de la confirmación que el operador leyó para aprobar.
        """
        ...


class ConfirmadorNoDisponible:
    """confirmador fail-closed explícito para runners sin canal cableado.

    Existe para que un llamador ("no tengo HITL disponible acá") pueda
    declararlo explícitamente — y la evidencia diga ``canal_no_disponible``
    en vez de confiar en la rama del runner para ``None``. El runner corta en
    ``None`` igual; esta clase es la afirmación de intención, no la defensa.
    """

    async def confirmar(
        self,
        solicitud: OperatorConfigurationReadyRequest,
    ) -> ResultadoConfirmacion:
        return ResultadoConfirmacion.CANAL_NO_DISPONIBLE

    async def informar(self, *, tool: str, mensaje: str) -> None:
        # No hay superficie: la instrucción no llega a nadie; el fail-closed
        # de ``confirmar`` ya corta antes de que esto importe. Solo log.
        logging.getLogger(__name__).info("ConfirmadorNoDisponible.informar (sin superficie): %s — %s", tool, mensaje)


#: Deadline por defecto de la ESPERA de readyness. Acotado y explícito a
#: propósito: una readyness que nunca llega tiene que cortar, no colgar la
#: GUI de DynDOLOD abierta para siempre. Cubre un preset normal sin reutilizar
#: el deadline de 4 h de la corrida.
DEFAULT_READINESS_TIMEOUT_SEGUNDOS = 600.0

#: Margen que el CALLER agrega por fuera del deadline del gate antes de declarar
#: que el helper de observación se colgó. El gate respeta su propio deadline
#: entre observaciones, pero una sola llamada COM puede bloquearse sin retorno:
#: sin esta cota, "acotado" dejaría de ser verdad por una llamada del sistema.
#: Agotada la gracia, el veredicto honesto es ``UIA_NO_DISPONIBLE`` — el sensor
#: no respondió — y el proceso se termina igual.
GRACIA_EXTERNA_DEL_GATE_SEGUNDOS = 15.0

#: Cadencia de la vigilia que corre EN PARALELO a la confirmación humana (y a la
#: observación del gate), para cortar en cuanto el proceso muere en vez de
#: esperar el deadline entero.
INTERVALO_VIGILIA_SEGUNDOS = 0.5


class PoliticaDeReintento(enum.Enum):
    """Qué razones transitorias rigen cada observación del protocolo.

    El initial gate tolera que la GUI esté naciendo; el final, después de que el
    operador declaró "lista", sólo tolera un Edit que todavía no expone texto.
    Es la MISMA partición de :data:`RAZONES_TRANSITORIAS_DE_INICIO` y
    :data:`RAZONES_TRANSITORIAS_FINAL`, con nombre enumerable para cruzar la
    frontera del helper (que recibe el nombre, no el frozenset).
    """

    INICIO = "inicio"
    FINAL = "final"


class EjecutorDeGateUIA(Protocol):
    """Puerto de EJECUCIÓN de la observación acotada, con una sola promesa.

    Corre el gate hasta veredicto concluyente, deadline o muerte, y devuelve el
    ``ResultadoPreflightUIA`` — nunca una excepción de dominio: los fallos del
    sensor y del mecanismo de ejecución se sintetizan con
    :func:`resultado_sin_backend`, que la policy del initial gate clasifica como
    ``BLOCKED``. La única excepción que puede propagar es ``CancelledError``, y
    el contrato de cancelación depende de la implementación:

    * el ejecutor PRODUCTIVO (``EjecutorGatePorHelper``) aísla la observación en
      un proceso descartable y, al cancelarlo, lo mata y lo reapea antes de
      propagar: una llamada COM que nunca retorna no puede dejar nada vivo;
    * el ejecutor de tests/rig (``EjecutorGateEnProceso``) corre en un hilo del
      pool y NO puede interrumpir una llamada COM colgada — es honesto sólo
      porque su uso está acotado a fakes cooperativos.

    El runner hace la carrera contra la muerte de la instancia observada del
    lado del padre; el ejecutor no conoce al proceso.
    """

    async def ejecutar(
        self,
        solicitud: SolicitudPreflightUIA,
        *,
        politica: PoliticaDeReintento,
        timeout_segundos: float,
        intervalo_segundos: float,
    ) -> ResultadoPreflightUIA:
        """Observa hasta veredicto o deadline. Nunca lanza por fallo del sensor."""
        ...


@dataclass(frozen=True)
class CapacidadDeReadinessUIA:
    """Todo lo que un consumidor necesita para correr el protocolo de readiness.

    Es el paquete INYECTABLE que separa "la capa pura sabe decidir" de "el
    runtime tiene un backend". Agrupa los COLABORADORES del protocolo —el
    ejecutor del gate y el canal humano— en un objeto que se cablea UNA vez en
    el composition root, para que un call site nuevo no tenga que recordar
    cuatro parámetros sueltos.

    **La ausencia de esta capacidad NO es una autorización.** El runner exige
    elegir explícitamente entre esta capacidad y ``ReadinessMode.DISABLED_FOR_TEST``
    (sin default silencioso), y un censo exige que TODO constructor productivo
    pase la capacidad: el default permisivo es exactamente lo que el censo
    existe para prohibir.

    Los colaboradores son Protocolos, no clases concretas: el ejecutor
    productivo corre un helper descartable con el backend Windows real, y los
    tests inyectan el ejecutor en proceso con observadores falsos sin tocar el
    runner.
    """

    ejecutor: EjecutorDeGateUIA
    confirmador: ConfirmadorDeConfiguracion
    gate_timeout_segundos: float = GATE_UIA_TIMEOUT_SEGUNDOS
    gate_intervalo_segundos: float = GATE_UIA_INTERVALO_SEGUNDOS
    gate_final_timeout_segundos: float = GATE_UIA_TIMEOUT_FINAL_SEGUNDOS
    gate_final_intervalo_segundos: float = GATE_UIA_INTERVALO_SEGUNDOS
    readiness_timeout_segundos: float = DEFAULT_READINESS_TIMEOUT_SEGUNDOS
    gracia_externa_segundos: float = GRACIA_EXTERNA_DEL_GATE_SEGUNDOS
    intervalo_vigilia_segundos: float = INTERVALO_VIGILIA_SEGUNDOS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gate_timeout_segundos",
            exigir_plazo_positivo_finito(self.gate_timeout_segundos, "gate_timeout_segundos"),
        )
        object.__setattr__(
            self,
            "gate_intervalo_segundos",
            exigir_plazo_positivo_finito(self.gate_intervalo_segundos, "gate_intervalo_segundos"),
        )
        object.__setattr__(
            self,
            "gate_final_timeout_segundos",
            exigir_plazo_positivo_finito(self.gate_final_timeout_segundos, "gate_final_timeout_segundos"),
        )
        object.__setattr__(
            self,
            "gate_final_intervalo_segundos",
            exigir_plazo_positivo_finito(self.gate_final_intervalo_segundos, "gate_final_intervalo_segundos"),
        )
        object.__setattr__(
            self,
            "readiness_timeout_segundos",
            exigir_plazo_positivo_finito(self.readiness_timeout_segundos, "readiness_timeout_segundos"),
        )
        object.__setattr__(
            self,
            "gracia_externa_segundos",
            exigir_plazo_positivo_finito(self.gracia_externa_segundos, "gracia_externa_segundos"),
        )
        object.__setattr__(
            self,
            "intervalo_vigilia_segundos",
            exigir_plazo_positivo_finito(self.intervalo_vigilia_segundos, "intervalo_vigilia_segundos"),
        )


#: El FINAL gate (post-confirmación humana) no puede tolerar las razones
#: transitorias del initial: una vez el operador declaró "configuración
#: lista", un wizard sin ventana o sin control NO va a "resolverse solo" — el
#: estado ya tendría que existir. Sólo ``VALOR_NO_LEIBLE`` queda transitorio
#: (un Edit recién redibujado al aplicar el preset puede no exponer texto un
#: latido), y el deadline final es corto. Es decisión de política: ante la
#: duda, el final gate falla CERRADO (UNKNOWN) y la corrida muere; reintentar
#: indefinidamente tras la confirmación abriría la ventana que T5-v2.1 cierra.
#: Congelado por igualdad literal en los tests.
RAZONES_TRANSITORIAS_FINAL: frozenset[RazonPreflight] = frozenset(
    {
        RazonPreflight.VALOR_NO_LEIBLE,
    }
)

#: Mapa política → razones transitorias. Es la ÚNICA traducción entre el nombre
#: que cruza la frontera del helper y el frozenset que consume el gate;
#: congelado por igualdad literal en los tests, así que una política nueva sin
#: entrada rompe el ancla en vez de degradar en silencio a "sin transitorias".
RAZONES_TRANSITORIAS_POR_POLITICA: dict[PoliticaDeReintento, frozenset[RazonPreflight]] = {
    PoliticaDeReintento.INICIO: RAZONES_TRANSITORIAS_DE_INICIO,
    PoliticaDeReintento.FINAL: RAZONES_TRANSITORIAS_FINAL,
}


# =============================================================================
# Contrato serializable del helper descartable (T5-v2.1)
# =============================================================================
#
# El gate productivo corre en un PROCESO aparte: el padre le manda la solicitud
# y la política como JSON y el helper le devuelve el resultado en el MISMO
# formato. Estas funciones son la única implementación de esa traducción —las
# usan las dos puntas— y por eso viven acá y no duplicadas en cada lado: un
# campo que viaje por un camino y se pierda en el otro reconstruye una
# divergencia que este módulo existe para no tener.
#
# Dirección del fail-closed: CUALQUIER dato que no encaje es
# :class:`ContratoDeHelperError` (ValueError), que el padre traduce a
# ``UIA_NO_DISPONIBLE``. Una respuesta ilegible no puede convertirse en un
# veredicto verde por accidente.

#: Nombres de los dos archivos del canal padre ↔ helper. Viven acá —en el
#: contrato— y no en cada punta: el padre los escribe y el helper los lee, y dos
#: constantes divergentes serían un hang silencioso hasta el deadline.
NOMBRE_DEL_PEDIDO = "pedido.json"
NOMBRE_DEL_RESULTADO = "resultado.json"


class ContratoDeHelperError(ValueError):
    """La solicitud o la respuesta del helper no respeta el contrato serializable."""


def _texto_requerido(datos: dict[str, object], clave: str) -> str:
    valor = datos.get(clave)
    if not isinstance(valor, str):
        raise ContratoDeHelperError(f"{clave!r} debe ser un string; llegó {type(valor).__name__}")
    return valor


def _texto_opcional(datos: dict[str, object], clave: str) -> str | None:
    valor = datos.get(clave)
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ContratoDeHelperError(f"{clave!r} debe ser null o un string; llegó {type(valor).__name__}")
    return valor


def _entero_opcional(datos: dict[str, object], clave: str) -> int | None:
    valor = datos.get(clave)
    if valor is None:
        return None
    # ``bool`` es subclase de ``int`` y no es un pid: excluirlo explícitamente.
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ContratoDeHelperError(f"{clave!r} debe ser null o un entero; llegó {type(valor).__name__}")
    return valor


def _decimal(datos: dict[str, object], clave: str) -> float:
    valor = datos.get(clave)
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ContratoDeHelperError(f"{clave!r} debe ser numérico; llegó {type(valor).__name__}")
    return exigir_plazo_positivo_finito(valor, clave, error=ContratoDeHelperError)


def _diccionario_requerido(datos: dict[str, object], clave: str) -> dict[str, object]:
    valor = datos.get(clave)
    if not isinstance(valor, dict):
        raise ContratoDeHelperError(f"{clave!r} debe ser un objeto; llegó {type(valor).__name__}")
    return {str(k): v for k, v in valor.items()}


def _criterios_a_diccionario(criterios: CriteriosDeControl) -> dict[str, object]:
    return {
        "automation_id": criterios.automation_id,
        "nombre": criterios.nombre,
        "tipo_de_control": criterios.tipo_de_control,
        "class_name": criterios.class_name,
    }


def solicitud_a_json(solicitud: SolicitudPreflightUIA) -> str:
    """Serializa la solicitud para el helper. Un solo camino: el de la clase."""
    return json.dumps(
        {
            "tool": solicitud.tool,
            "ejecutable_esperado": solicitud.ejecutable_esperado,
            "salida_administrada_esperada": solicitud.salida_administrada_esperada,
            "criterios_del_control": _criterios_a_diccionario(solicitud.criterios_del_control),
            "pid": solicitud.pid,
        },
        ensure_ascii=False,
    )


def solicitud_desde_json(texto: str) -> SolicitudPreflightUIA:
    """Reconstruye la solicitud del helper; cualquier dato raro LANZA (fail-closed)."""
    try:
        crudo = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ContratoDeHelperError(f"la solicitud no es JSON válido: {exc}") from exc
    if not isinstance(crudo, dict):
        raise ContratoDeHelperError(f"la solicitud debe ser un objeto JSON; llegó {type(crudo).__name__}")
    datos = {str(clave): valor for clave, valor in crudo.items()}
    criterios = _diccionario_requerido(datos, "criterios_del_control")
    return SolicitudPreflightUIA(
        tool=_texto_requerido(datos, "tool"),
        ejecutable_esperado=_texto_requerido(datos, "ejecutable_esperado"),
        salida_administrada_esperada=_texto_requerido(datos, "salida_administrada_esperada"),
        criterios_del_control=CriteriosDeControl(
            automation_id=_texto_opcional(criterios, "automation_id"),
            nombre=_texto_opcional(criterios, "nombre"),
            tipo_de_control=_texto_opcional(criterios, "tipo_de_control"),
            class_name=_texto_opcional(criterios, "class_name"),
        ),
        pid=_entero_opcional(datos, "pid"),
    )


def resultado_a_json(resultado: ResultadoPreflightUIA) -> str:
    """Serializa el veredicto del helper con TODA su evidencia."""
    return json.dumps(
        {
            "estado": resultado.estado.value,
            "razon": resultado.razon.value,
            "tool": resultado.tool,
            "detalle": resultado.detalle,
            "valor_esperado": resultado.valor_esperado,
            "pid": resultado.pid,
            "ventana": resultado.ventana,
            "valor_observado": resultado.valor_observado,
            "valor_observado_canonico": resultado.valor_observado_canonico,
            "valor_esperado_canonico": resultado.valor_esperado_canonico,
            "evidencia": list(resultado.evidencia),
        },
        ensure_ascii=False,
    )


def resultado_desde_json(texto: str) -> ResultadoPreflightUIA:
    """Reconstruye el veredicto; un estado/razón fuera del contrato LANZA.

    Los valores de ``EstadoPreflight`` y ``RazonPreflight`` se reconstruyen por
    membresía de enum: una razón que este binario no conoce (versión cruzada)
    es un contrato roto, no un UNKNOWN silencioso — el veredicto que llegó no
    se puede interpretar, así que no hay veredicto. Y la membresía no alcanza:
    el PAR también se valida contra ``par_estado_razon_valido`` (la autoridad
    estado ↔ razón que el pipeline usa para emitir), porque un ``MATCH`` con
    ``OUTPUT_DIFIERE`` deserializaría como autorización del FINAL gate.
    """
    try:
        crudo = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ContratoDeHelperError(f"el resultado no es JSON válido: {exc}") from exc
    if not isinstance(crudo, dict):
        raise ContratoDeHelperError(f"el resultado debe ser un objeto JSON; llegó {type(crudo).__name__}")
    datos = {str(clave): valor for clave, valor in crudo.items()}
    try:
        estado = EstadoPreflight(_texto_requerido(datos, "estado"))
        razon = RazonPreflight(_texto_requerido(datos, "razon"))
    except ValueError as exc:
        raise ContratoDeHelperError(f"estado/razón fuera del contrato: {exc}") from exc
    # La membresía de cada enum no basta: el PAR tiene que ser uno que el
    # pipeline pueda emitir. Un ``MATCH`` + ``OUTPUT_DIFIERE`` deserializa como
    # ``MATCH`` y el FINAL gate autoriza por ``estado is MATCH`` — aceptarlo acá
    # es convertir datos corruptos del canal en una corrida que sigue. La única
    # autoridad es ``par_estado_razon_valido`` (misma tabla que el pipeline usa
    # para EMITIR): el par imposible se rechaza en la frontera y el padre lo
    # traduce a ``UNKNOWN``/``UIA_NO_DISPONIBLE`` por el camino fail-closed que
    # ya existe para toda respuesta ilegible.
    if not par_estado_razon_valido(estado, razon):
        raise ContratoDeHelperError(f"par estado/razón inconsistente: {estado.value} + {razon.value}")
    evidencia_cruda = datos.get("evidencia")
    if not isinstance(evidencia_cruda, list) or not all(isinstance(linea, str) for linea in evidencia_cruda):
        raise ContratoDeHelperError("'evidencia' debe ser una lista de strings")
    return ResultadoPreflightUIA(
        estado=estado,
        razon=razon,
        tool=_texto_requerido(datos, "tool"),
        detalle=_texto_requerido(datos, "detalle"),
        valor_esperado=_texto_requerido(datos, "valor_esperado"),
        pid=_entero_opcional(datos, "pid"),
        ventana=_texto_opcional(datos, "ventana"),
        valor_observado=_texto_opcional(datos, "valor_observado"),
        valor_observado_canonico=_texto_opcional(datos, "valor_observado_canonico"),
        valor_esperado_canonico=_texto_opcional(datos, "valor_esperado_canonico"),
        evidencia=tuple(str(linea) for linea in evidencia_cruda),
    )


def politica_desde_nombre(nombre: str) -> PoliticaDeReintento:
    """``"inicio"``/``"final"`` → enum; cualquier otra cosa LANZA (fail-closed)."""
    try:
        return PoliticaDeReintento(nombre)
    except ValueError as exc:
        raise ContratoDeHelperError(f"política de reintento desconocida: {nombre!r}") from exc


@dataclass(frozen=True)
class PedidoDeGate:
    """La unidad de trabajo que el padre le encarga al helper descartable.

    Agrupa las cuatro cosas que el helper necesita para correr EXACTAMENTE la
    observación que el runner pidió: la solicitud, la política de reintento y
    los dos presupuestos de tiempo. Un pedido serializable es el contrato; el
    helper no recibe parámetros sueltos que puedan desincronizarse entre
    caminos.
    """

    solicitud: SolicitudPreflightUIA
    politica: PoliticaDeReintento
    timeout_segundos: float
    intervalo_segundos: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timeout_segundos",
            exigir_plazo_positivo_finito(self.timeout_segundos, "timeout_segundos"),
        )
        object.__setattr__(
            self,
            "intervalo_segundos",
            exigir_plazo_positivo_finito(self.intervalo_segundos, "intervalo_segundos"),
        )


def pedido_a_json(pedido: PedidoDeGate) -> str:
    """Serializa el pedido completo, con la MISMA solicitud que usa el gate puro."""
    return json.dumps(
        {
            "solicitud": json.loads(solicitud_a_json(pedido.solicitud)),
            "politica": pedido.politica.value,
            "timeout_segundos": pedido.timeout_segundos,
            "intervalo_segundos": pedido.intervalo_segundos,
        },
        ensure_ascii=False,
    )


def pedido_desde_json(texto: str) -> PedidoDeGate:
    """Reconstruye el pedido; cualquier dato raro LANZA (fail-closed)."""
    try:
        crudo = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ContratoDeHelperError(f"el pedido no es JSON válido: {exc}") from exc
    if not isinstance(crudo, dict):
        raise ContratoDeHelperError(f"el pedido debe ser un objeto JSON; llegó {type(crudo).__name__}")
    datos = {str(clave): valor for clave, valor in crudo.items()}
    return PedidoDeGate(
        solicitud=solicitud_desde_json(json.dumps(_diccionario_requerido(datos, "solicitud"))),
        politica=politica_desde_nombre(_texto_requerido(datos, "politica")),
        timeout_segundos=_decimal(datos, "timeout_segundos"),
        intervalo_segundos=_decimal(datos, "intervalo_segundos"),
    )
