"""T5-v2 — el gate productivo del preflight UIA: fail-closed sobre la corrida.

**La pregunta que responde, entera.** *"El control Output que ESTA instancia
recién lanzada muestra por UIA, ¿coincide con la salida administrada que
Sky-Claw le pasó por ``-o:``?"* Si la respuesta no es ``MATCH``, Sky-Claw no
considera la instancia lista y el runner termina el proceso — el pipeline no
continúa sobre una observación divergente o inconclusa.

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
conoce COM, ni procesos, ni asyncio — el runner lo ejecuta dentro de un
``asyncio.to_thread`` (las llamadas UIA son bloqueantes y de apartamento) y la
cancelación viaja como ``CancelledError`` en el await de afuera, con el proceso
aún bajo el Job Object del runner.

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

import time
from collections.abc import Callable

from sky_claw.local.tools.dyndolod_uia_preflight import (
    EstadoPreflight,
    LocalizadorDeProcesos,
    ObservacionUIAError,
    ObservadorLiberable,
    ObservadorUIA,
    RazonPreflight,
    ResultadoPreflightUIA,
    SolicitudPreflightUIA,
    observar_output,
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


def _resultado_sin_backend(
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
    """
    return ResultadoPreflightUIA(
        estado=EstadoPreflight.UNKNOWN,
        razon=RazonPreflight.UIA_NO_DISPONIBLE,
        tool=solicitud.tool,
        detalle=f"no se pudo construir el observador de UI Automation: {exc}",
        valor_esperado=solicitud.salida_administrada_esperada,
        pid=solicitud.pid,
    )


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
    """
    if timeout_segundos <= 0 or intervalo_segundos <= 0:
        # Error de programación/configuración, no del rig: el gate no puede
        # decidir con un presupuesto de tiempo negativo. Se lanza (no se
        # traduce a UNKNOWN) porque un cero acá es un bug del caller, y un
        # UNKNOWN lo disfrazaría de "la GUI no respondió".
        raise ValueError(
            f"timeout_segundos e intervalo_segundos tienen que ser > 0; "
            f"llegaron {timeout_segundos=} {intervalo_segundos=}"
        )

    try:
        observador = fabrica_observador()
    except ObservacionUIAError as exc:
        return _resultado_sin_backend(solicitud, exc)

    limite = reloj() + timeout_segundos
    razon_anterior: RazonPreflight | None = None
    try:
        while True:
            resultado = observar_output(solicitud, localizador=localizador, observador=observador)
            if resultado.estado is not EstadoPreflight.UNKNOWN:
                return resultado
            if resultado.razon not in RAZONES_TRANSITORIAS_DE_INICIO:
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
