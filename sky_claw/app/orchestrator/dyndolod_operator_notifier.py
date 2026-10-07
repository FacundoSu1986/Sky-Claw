"""Notificador de operador para la etapa 9 (TexGen + DynDOLOD).

**Qué cierra.** ``DynDOLODPipelineService`` publicaba ``pipeline.dyndolod.started``
y ``pipeline.dyndolod.completed`` pero NADIE los consumía: un operador que seguía
la corrida desde el teléfono no recibía el aviso de fallo ni el log que lo
explica. Este módulo es el suscriptor que faltaba. No toca el pipeline ni sus
transacciones: sólo LEE eventos del bus y escribe al operador.

Además del inicio y el fin, avisa la ACTIVIDAD del log mientras la etapa corre
(``pipeline.dyndolod.progress``: sigue en curso, sin actividad, reanudada): una etapa asistida
de decenas de minutos no puede quedar muda para quien la sigue desde el teléfono.

**Tres propiedades que lo hacen seguro de enchufar a un bus compartido:**

1. *El callback no espera al canal y nunca lanza.* El bus despacha UNA tarea por
   (evento, suscriptor) y manda a la DLQ lo que lance; un ``sendMessage`` con
   backoff de 429 no puede retener a ese despacho, y un evento malformado no debe
   ensuciar la DLQ. ``on_event`` parsea y ENCOLA; nada más.
2. *Un único worker serializa los envíos.* El bus no garantiza el orden entre
   tareas; la cola sí. Si se llena, se descarta lo MÁS VIEJO y se cuenta (el
   estado terminal más reciente es el que importa) — nunca se bloquea al bus.
3. *Todo texto dinámico va escapado.* Las líneas de log de DynDOLOD son
   ``<Error: ...>`` y el modo HTML de Telegram las rechazaría con un 400: justo
   el aviso de fallo se perdería. El volumen de lo que se muestra está acotado de
   modo que el peor caso de escape (``&`` -> ``&amp;``) siga bajo los 4096
   caracteres: el sender partiría un mensaje más largo EN MEDIO de una etiqueta.

**Qué adjunta y qué no.** Ante un fallo de herramienta adjunta la COLA del log
(no el archivo entero), cortada en un límite de línea, y sólo archivos regulares
``*_log.txt`` que no sean enlaces: la ruta viaja en el payload de un evento y el
destino es un chat externo, así que el filtro es de defensa en profundidad y no
una confianza en el emisor.

**Qué NO hace.** No decide veredictos (los decide el post-check del runner), no
reintenta (el sender ya absorbe el flood-control), y no pretende entregar: si el
canal falla se registra y se sigue. La entrega a Telegram no está verificada
contra la API real en este módulo; ver los tests del adaptador.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sky_claw.app.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineProgressPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.app.security import links

if TYPE_CHECKING:
    from sky_claw.app.core.event_bus import CoreEventBus, Event

logger = logging.getLogger(__name__)

#: Topics que publica ``DynDOLODPipelineService`` (anclados por AST en
#: ``tests/test_dyndolod_operator_notifier.py``: si el servicio los renombra, este
#: módulo no puede quedar sordo en silencio).
TOPIC_INICIO = "pipeline.dyndolod.started"
TOPIC_FIN = "pipeline.dyndolod.completed"
TOPIC_PROGRESO = "pipeline.dyndolod.progress"
PATRON_DE_SUSCRIPCION = "pipeline.dyndolod.*"

#: Cuántos errores se muestran y cuánto de cada uno. El producto, con el peor
#: caso de escape (x5), más el resto del mensaje, queda bajo el límite de
#: Telegram (4096) — ver ``test_el_peor_caso_de_escape_...``.
_MAX_ERRORES_MOSTRADOS = 2
_MAX_CARACTERES_POR_ERROR = 300

_MAX_BYTES_DE_ADJUNTO_POR_DEFECTO = 200 * 1024
_MAX_ADJUNTOS_POR_DEFECTO = 2
_CAPACIDAD_DE_COLA_POR_DEFECTO = 32

#: Los logs de TexGen/DynDOLOD son ``Logs/{Tool}_{modo}_log.txt``.
_SUFIJO_DE_LOG = "_log.txt"


class CanalDeOperador(Protocol):
    """Puerto de salida: lo único que el notificador sabe del destino."""

    async def avisar(self, texto_html: str) -> None: ...

    async def adjuntar(self, *, nombre: str, contenido: bytes, descripcion: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _Notificacion:
    """Un mensaje ya formateado y sus adjuntos candidatos, listo para entregar."""

    clave: str
    texto_html: str
    logs: tuple[pathlib.Path, ...] = ()


def _esc(texto: str) -> str:
    """Escapa lo mínimo que exige el modo HTML de Telegram: ``&``, ``<`` y ``>``."""
    return html.escape(texto, quote=False)


def _recortar(texto: str, maximo: int) -> str:
    """Recorta el texto CRUDO (antes de escapar: cortar un ``&amp;`` a la mitad rompe el HTML)."""
    return texto if len(texto) <= maximo else texto[: maximo - 1] + "…"


def _duracion_legible(segundos: float) -> str:
    """``272.5`` -> ``4m 32s``. Trunca: una duración no se redondea hacia arriba."""
    total = max(0, int(segundos))
    horas, resto = divmod(total, 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas}h {minutos}m {seg}s"
    if minutos:
        return f"{minutos}m {seg}s"
    return f"{seg}s"


def _instruccion_de_cierre(run_texgen: bool) -> str:
    """Cómo cerrar cada herramienta sin perder su salida.

    Evidencia de rig commiteada: el diálogo final de cada herramienta ofrece variantes
    «Zip» cuyo PROPIO texto avisa que *"permanently delete everything in the dedicated
    output folder"* (``docs/validation/2026-09-13_pr580_real_rig/texgen-log.txt`` y
    ``dyndolod-log.txt``, la línea del diálogo final), y el de DynDOLOD además ofrece
    «Exit DynDOLOD», que sale *sin guardar*. Qué botón se eligió en esa corrida está en
    ``final-report.md`` del mismo directorio («Terminación elegida»). La corrida sólo se
    empaqueta si la carpeta de salida sobrevive.
    """
    cierre = "«Exit TexGen» (TexGen) y «Save and Exit» (DynDOLOD)" if run_texgen else "«Save and Exit» (DynDOLOD)"
    return (
        f"Al terminar, cerrá con {cierre}. Las opciones «Zip» borran la carpeta de "
        "salida y Sky-Claw ya no podría empaquetar el resultado; «Exit DynDOLOD» no guarda los plugins."
    )


def _formatear_inicio(payload: DynDOLODPipelineStartedPayload) -> str:
    return "\n".join(
        [
            "🛠️ <b>DynDOLOD · etapa 9 iniciada</b>",
            f"Preset: <code>{_esc(payload.preset)}</code> · TexGen: {'sí' if payload.run_texgen else 'no'}",
            _esc(payload.operator_instructions),
            _instruccion_de_cierre(payload.run_texgen),
        ]
    )


def _formatear_fin(payload: DynDOLODPipelineCompletedPayload) -> str:
    if payload.success:
        encabezado = "✅ <b>DynDOLOD · etapa 9 completada</b>"
    elif payload.cancelled:
        encabezado = "⏹️ <b>DynDOLOD · etapa 9 cancelada</b>"
    else:
        encabezado = "❌ <b>DynDOLOD · etapa 9 falló</b>"
    lineas = [f"{encabezado} ({_duracion_legible(payload.duration_seconds)})"]

    if payload.success:
        texgen = "no se ejecutó" if not payload.run_texgen else ("✓" if payload.texgen_success else "—")
        lineas.append(f"TexGen: {texgen} · DynDOLOD: {'✓' if payload.dyndolod_success else '—'}")
        if payload.cancelled:
            lineas.append("Nota: se canceló el post-proceso; la salida ya estaba confirmada.")
        return "\n".join(lineas)

    errores = payload.errors[:_MAX_ERRORES_MOSTRADOS]
    if errores:
        lineas.extend(f"<pre>{_esc(_recortar(error, _MAX_CARACTERES_POR_ERROR))}</pre>" for error in errores)
        omitidos = len(payload.errors) - len(errores)
        if omitidos:
            lineas.append(f"(+{omitidos} más en el log)")
    else:
        lineas.append("Sin detalle: revisá el log de la herramienta.")
    lineas.append(
        "Rollback: confirmado"
        if payload.rolled_back
        else "Rollback: NO confirmado (la transacción queda pendiente; revisá la GUI)"
    )
    return "\n".join(lineas)


_UNIDADES_DE_TAMANO = ("B", "KiB", "MiB", "GiB")

_ENCABEZADO_DE_ACTIVIDAD = {
    "progress": "⏳ <b>DynDOLOD · etapa 9 en curso</b>",
    "stalled": "⚠️ <b>DynDOLOD · sin actividad en el log</b>",
    "resumed": "▶️ <b>DynDOLOD · actividad reanudada</b>",
}


def _tamano_legible(tamano: int) -> str:
    """``4300000`` -> ``4.1 MiB``. Un tamaño negativo (no debería existir) se lee como ``0 B``."""
    valor = float(max(tamano, 0))
    indice = 0
    while valor >= 1024 and indice < len(_UNIDADES_DE_TAMANO) - 1:
        valor /= 1024
        indice += 1
    return f"{int(valor)} B" if indice == 0 else f"{valor:.1f} {_UNIDADES_DE_TAMANO[indice]}"


def _formatear_progreso(payload: DynDOLODPipelineProgressPayload) -> str:
    lineas = [f"{_ENCABEZADO_DE_ACTIVIDAD[payload.kind]} ({_duracion_legible(payload.elapsed_seconds)})"]
    if payload.log:
        lineas.append(f"Log: <code>{_esc(payload.log)}</code> · {_tamano_legible(payload.log_size_bytes)}")
    else:
        lineas.append("Todavía no hay un log de esta corrida.")
    if payload.kind == "stalled":
        lineas.append(
            f"Sin cambios hace {_duracion_legible(payload.idle_seconds)}: "
            "la herramienta puede estar esperando una acción en su ventana."
        )
    elif payload.kind == "resumed":
        lineas.append(f"Volvió a escribir tras {_duracion_legible(payload.idle_seconds)} sin cambios.")
    elif payload.idle_seconds >= 1:
        lineas.append(f"Último cambio del log hace {_duracion_legible(payload.idle_seconds)}.")
    if payload.last_line:
        lineas.append("Última línea:")
        lineas.append(f"<pre>{_esc(_recortar(payload.last_line, _MAX_CARACTERES_POR_ERROR))}</pre>")
    return "\n".join(lineas)


def _leer_cola_de_log(ruta: pathlib.Path, max_bytes: int) -> bytes:
    """Los últimos ``max_bytes`` de ``ruta``, sin la primera línea si quedó cortada."""
    with ruta.open("rb") as archivo:
        archivo.seek(0, os.SEEK_END)
        tamano = archivo.tell()
        if tamano <= max_bytes:
            archivo.seek(0)
            return archivo.read()
        archivo.seek(tamano - max_bytes)
        cola = archivo.read(max_bytes)
    # El corte cae en medio de una línea (y, en un log cp1252/utf-8, quizá de un
    # carácter): se descarta hasta el primer salto de línea.
    corte = cola.find(b"\n")
    return cola[corte + 1 :] if corte != -1 else cola


def _preparar_adjunto(ruta: pathlib.Path, max_bytes: int) -> tuple[str, bytes, bool] | None:
    """``(nombre, contenido, truncado)`` o ``None`` si ``ruta`` no es adjuntable.

    Sólo archivos regulares ``*_log.txt`` que no sean enlaces y no estén vacíos
    (Telegram rechaza un documento vacío). Bloqueante: corre en un hilo.
    """
    if not ruta.name.endswith(_SUFIJO_DE_LOG):
        return None
    # `links.is_link` y no `Path.is_symlink()`: la detección de enlaces vive en UN módulo y cubre también los
    # junctions de Windows, que la stdlib de 3.11 no ve. `is_file()` sigue enlaces, así que va DESPUÉS.
    if links.is_link(ruta) or not ruta.is_file():
        return None
    tamano = ruta.stat().st_size
    if tamano == 0:
        return None
    contenido = _leer_cola_de_log(ruta, max_bytes)
    if not contenido:
        return None
    return ruta.name, contenido, tamano > max_bytes


class DynDOLODOperatorNotifier:
    """Suscriptor del bus que avisa al operador el ciclo de vida de la etapa 9.

    Uso: ``instalar_notificador_de_dyndolod`` lo suscribe, y ``run()`` corre como
    tarea de fondo del proceso (``AppContext._track_task``).

    Args:
        canal: puerto de salida (Telegram en producción).
        max_bytes_de_adjunto: tope de la cola de log que se adjunta.
        max_adjuntos: máximo de logs adjuntados por fallo.
        capacidad_de_cola: notificaciones pendientes antes de descartar la más vieja.
    """

    def __init__(
        self,
        canal: CanalDeOperador,
        *,
        max_bytes_de_adjunto: int = _MAX_BYTES_DE_ADJUNTO_POR_DEFECTO,
        max_adjuntos: int = _MAX_ADJUNTOS_POR_DEFECTO,
        capacidad_de_cola: int = _CAPACIDAD_DE_COLA_POR_DEFECTO,
    ) -> None:
        if max_bytes_de_adjunto <= 0 or max_adjuntos < 0 or capacidad_de_cola <= 0:
            raise ValueError("max_bytes_de_adjunto y capacidad_de_cola deben ser positivos; max_adjuntos, >= 0")
        self._canal = canal
        self._max_bytes = max_bytes_de_adjunto
        self._max_adjuntos = max_adjuntos
        self._cola: asyncio.Queue[_Notificacion] = asyncio.Queue(maxsize=capacidad_de_cola)
        self._descartados = 0

    @property
    def descartados(self) -> int:
        """Notificaciones descartadas por cola llena (observabilidad)."""
        return self._descartados

    # ------------------------------------------------------------------
    # Lado del bus: parsear y encolar. Sin esperas y sin excepciones.
    # ------------------------------------------------------------------

    async def on_event(self, event: Event) -> None:
        """Suscriptor del bus. Nunca espera al canal y nunca lanza."""
        try:
            notificacion = self._construir(event)
        except (ValueError, TypeError):
            # `ValidationError` de pydantic es un `ValueError`. Un evento que no
            # cumple el contrato no puede escapar al bus (iría a la DLQ).
            logger.warning(
                "DynDOLOD: el evento '%s' no cumple el contrato del payload; el notificador lo ignora",
                getattr(event, "topic", "?"),
                exc_info=True,
            )
            return
        if notificacion is not None:
            self._encolar(notificacion)

    @staticmethod
    def _construir(event: Event) -> _Notificacion | None:
        # `strict=False`: el payload puede haber pasado por una serialización
        # (DLQ/JSON) que convierte las tuplas en listas; el modelo sigue siendo la
        # ÚNICA autoridad del contrato, sin una lista de claves paralela.
        if event.topic == TOPIC_INICIO:
            inicio = DynDOLODPipelineStartedPayload.model_validate(event.payload, strict=False)
            return _Notificacion(clave="inicio", texto_html=_formatear_inicio(inicio))
        if event.topic == TOPIC_FIN:
            fin = DynDOLODPipelineCompletedPayload.model_validate(event.payload, strict=False)
            adjuntar = not fin.success and not fin.cancelled
            return _Notificacion(
                clave="fin",
                texto_html=_formatear_fin(fin),
                logs=tuple(pathlib.Path(ruta) for ruta in fin.log_paths) if adjuntar else (),
            )
        if event.topic == TOPIC_PROGRESO:
            progreso = DynDOLODPipelineProgressPayload.model_validate(event.payload, strict=False)
            return _Notificacion(clave=f"actividad:{progreso.kind}", texto_html=_formatear_progreso(progreso))
        return None

    def _encolar(self, notificacion: _Notificacion) -> None:
        try:
            self._cola.put_nowait(notificacion)
            return
        except asyncio.QueueFull:
            pass
        # Cola llena (el canal lleva rato caído o bloqueado): lo MÁS VIEJO pierde.
        # Es una sola corrutina en un solo loop, así que el hueco que se libera acá
        # no puede ocuparlo otro productor antes del `put_nowait` siguiente.
        with contextlib.suppress(asyncio.QueueEmpty):
            self._cola.get_nowait()
        self._descartados += 1
        logger.warning(
            "DynDOLOD: cola de notificaciones llena (%d): se descarta la más vieja (%d descartadas en total)",
            self._cola.maxsize,
            self._descartados,
        )
        self._cola.put_nowait(notificacion)

    # ------------------------------------------------------------------
    # Lado del canal: UN worker, en orden.
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Entrega las notificaciones en orden hasta que se cancele la tarea."""
        while True:
            notificacion = await self._cola.get()
            try:
                await self._entregar(notificacion)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — boundary de entrega: un canal caído no puede matar al worker
                logger.warning(
                    "DynDOLOD: no se pudo entregar la notificación '%s' al operador",
                    notificacion.clave,
                    exc_info=True,
                )

    async def _entregar(self, notificacion: _Notificacion) -> None:
        await self._canal.avisar(notificacion.texto_html)
        enviados = 0
        for ruta in notificacion.logs:
            if enviados >= self._max_adjuntos:
                break
            try:
                adjunto = await asyncio.to_thread(_preparar_adjunto, ruta, self._max_bytes)
                if adjunto is None:
                    continue
                nombre, contenido, truncado = adjunto
                descripcion = (
                    f"Cola (últimos {self._max_bytes // 1024} KiB) de {nombre}"
                    if truncado
                    else f"Log completo: {nombre}"
                )
                await self._canal.adjuntar(nombre=nombre, contenido=contenido, descripcion=descripcion)
                enviados += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — un adjunto que falla no puede impedir el siguiente ni el aviso ya enviado
                logger.warning(
                    "DynDOLOD: no se pudo adjuntar el log '%s' para el operador",
                    ruta.name,
                    exc_info=True,
                )


def instalar_notificador_de_dyndolod(
    *,
    event_bus: CoreEventBus,
    canal: CanalDeOperador,
    max_bytes_de_adjunto: int = _MAX_BYTES_DE_ADJUNTO_POR_DEFECTO,
    max_adjuntos: int = _MAX_ADJUNTOS_POR_DEFECTO,
    capacidad_de_cola: int = _CAPACIDAD_DE_COLA_POR_DEFECTO,
) -> DynDOLODOperatorNotifier:
    """Crea el notificador y lo suscribe a ``pipeline.dyndolod.*``.

    Devuelve el notificador para que el caller corra ``run()`` como tarea de fondo.
    ``subscribe`` sólo agrega a la lista del bus, así que es seguro llamarlo antes
    de que el bus arranque (el mismo precedente que la telemetría de la GUI).
    """
    notificador = DynDOLODOperatorNotifier(
        canal,
        max_bytes_de_adjunto=max_bytes_de_adjunto,
        max_adjuntos=max_adjuntos,
        capacidad_de_cola=capacidad_de_cola,
    )
    event_bus.subscribe(PATRON_DE_SUSCRIPCION, notificador.on_event)
    return notificador
