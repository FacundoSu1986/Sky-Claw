"""Seguimiento de la etapa 9: el último estado CONOCIDO en este proceso, lo que responde ``/lod_status``.

El notificador (``dyndolod_operator_notifier``) AVISA cuando pasa algo (push); este módulo RESPONDE
cuando el operador pregunta (pull). Los dos se alimentan de la misma fuente —los eventos
``pipeline.dyndolod.*`` del bus— y arman texto con los mismos formateadores (``dyndolod_mensajes``),
así que lo que el operador consulta y lo que recibió no pueden divergir.

**Qué es y qué NO es.** Es un modelo de LECTURA en memoria: guarda el último inicio, el último aviso de
actividad y el último fin, y los muestra. No ejecuta nada, no cancela nada, no lee el disco y no toca el
pipeline: ``resumen_html`` sólo lee. No es historial (sólo cuenta lo ocurrido desde el arranque de ESTE
proceso) ni es en tiempo real: el aviso de actividad que muestra es el ÚLTIMO emitido por el observador,
que avisa cada ``intervalo_de_progreso_s`` o ante un estancamiento, y por eso el resumen declara hace
cuánto llegó.

**Cuatro estados, dichos con honestidad.** Una instancia que NO ejecuta la etapa 9 (sin supervisor: el
modo ``telegram`` sin GUI) no es una instancia que «todavía no tuvo corridas»: decir lo segundo sugeriría
que mañana podría haberlas. Por eso hay un estado «conectado», que sólo ``suscribir`` activa.

**El callback del bus nunca lanza ni espera.** Un evento que no cumple el contrato del payload se registra
y se ignora (el bus lo mandaría a la DLQ). Un aviso de actividad que llega DESPUÉS del ``completed`` de su
corrida se descarta: el bus no garantiza orden entre eventos y no puede resucitar una corrida terminada.

**Todo texto dinámico va escapado** (lo hacen los formateadores; ver el ancla por enumeración en
``tests/test_dyndolod_seguimiento.py``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from pydantic import BaseModel

from sky_claw.app.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineProgressPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.app.orchestrator.dyndolod_mensajes import (
    MAX_CARACTERES_DE_NOMBRE,
    duracion_legible,
    escapar_html,
    formatear_fin,
    formatear_progreso,
    recortar,
)
from sky_claw.app.orchestrator.dyndolod_operator_notifier import (
    PATRON_DE_SUSCRIPCION,
    TOPIC_FIN,
    TOPIC_INICIO,
    TOPIC_PROGRESO,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sky_claw.app.core.event_bus import CoreEventBus, Event

logger = logging.getLogger(__name__)

_P = TypeVar("_P", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class _Visto(Generic[_P]):  # `Generic` y no PEP 695: el paquete soporta 3.11
    """Un payload y el instante (del reloj del proceso) en que llegó."""

    payload: _P
    en: float


class SeguimientoDeEtapa9:
    """Último estado conocido de la etapa 9 en este proceso. Alimentado por el bus, de solo lectura.

    Args:
        reloj: segundos (epoch). Inyectable para probar sin esperar; se usa tanto para marcar la llegada de
            cada evento como para calcular «hace cuánto» al responder.
    """

    def __init__(self, *, reloj: Callable[[], float] = time.time) -> None:
        self._reloj = reloj
        self._conectado = False
        self._inicio: _Visto[DynDOLODPipelineStartedPayload] | None = None
        self._actividad: _Visto[DynDOLODPipelineProgressPayload] | None = None
        self._fin: _Visto[DynDOLODPipelineCompletedPayload] | None = None

    # ------------------------------------------------------------------
    # Lado del bus: actualizar memoria. Sin esperas y sin excepciones.
    # ------------------------------------------------------------------

    def suscribir(self, event_bus: CoreEventBus) -> None:
        """Suscribe el seguimiento a ``pipeline.dyndolod.*`` y declara que esta instancia ejecuta la etapa 9."""
        event_bus.subscribe(PATRON_DE_SUSCRIPCION, self.on_event)
        self._conectado = True

    async def on_event(self, event: Event) -> None:
        """Suscriptor del bus. Nunca espera y nunca lanza."""
        try:
            self._registrar(event)
        except (ValueError, TypeError):
            # `ValidationError` de pydantic es un `ValueError`. Un evento que no cumple el contrato no puede
            # escapar al bus (iría a la DLQ).
            logger.warning(
                "DynDOLOD: el evento '%s' no cumple el contrato del payload; el seguimiento lo ignora",
                getattr(event, "topic", "?"),
                exc_info=True,
            )

    def _registrar(self, event: Event) -> None:
        # `strict=False`: el payload puede haber pasado por una serialización (DLQ/JSON) que convierte las tuplas
        # en listas; el modelo sigue siendo la ÚNICA autoridad del contrato.
        ahora = self._reloj()
        if event.topic == TOPIC_INICIO:
            inicio = DynDOLODPipelineStartedPayload.model_validate(event.payload, strict=False)
            self._inicio = _Visto(inicio, ahora)
            self._actividad = None
            self._fin = None
        elif event.topic == TOPIC_PROGRESO:
            # No se filtra por «hay corrida en curso»: un aviso huérfano o rezagado (el bus no garantiza orden) queda
            # guardado pero NO es observable —el `fin` tiene precedencia en `resumen_html` y un nuevo `started` lo
            # limpia—, así que la guarda sería código redundante (ver `TestOrdenYReinicio`).
            self._actividad = _Visto(DynDOLODPipelineProgressPayload.model_validate(event.payload, strict=False), ahora)
        elif event.topic == TOPIC_FIN:
            fin = DynDOLODPipelineCompletedPayload.model_validate(event.payload, strict=False)
            self._fin = _Visto(fin, ahora)

    # ------------------------------------------------------------------
    # Lado del operador: lo que muestra `/lod_status`. Sólo lee.
    # ------------------------------------------------------------------

    def resumen_html(self) -> str:
        """Texto HTML (ya escapado) del último estado conocido."""
        if not self._conectado:
            return (
                "ℹ️ Esta instancia de Sky-Claw no ejecuta la etapa 9 (no hay supervisor activo): "
                "no hay estado que mostrar."
            )
        ahora = self._reloj()
        if self._fin is not None:
            fin = self._fin
            return f"{formatear_fin(fin.payload)}\nTerminó hace {duracion_legible(ahora - fin.en)}."
        if self._inicio is None:
            return "ℹ️ Etapa 9: sin corridas registradas desde el arranque de Sky-Claw."

        inicio = self._inicio
        preset = escapar_html(recortar(inicio.payload.preset, MAX_CARACTERES_DE_NOMBRE))
        lineas = [
            f"🛠️ <b>Etapa 9 en curso</b> desde hace {duracion_legible(ahora - inicio.en)}",
            f"Preset: <code>{preset}</code> · TexGen: {'sí' if inicio.payload.run_texgen else 'no'}",
        ]
        if self._actividad is None:
            lineas.append("Todavía no hubo avisos de actividad del log.")
        else:
            actividad = self._actividad
            lineas.append(f"Último aviso de actividad, hace {duracion_legible(ahora - actividad.en)}:")
            lineas.append(formatear_progreso(actividad.payload))
        return "\n".join(lineas)
