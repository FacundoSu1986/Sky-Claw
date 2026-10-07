"""Formato de los mensajes al operador sobre la etapa 9 (TexGen + DynDOLOD): UNA sola fuente.

Dos consumidores arman texto con estas funciones: el notificador de operador
(``dyndolod_operator_notifier``: avisos PUSH de inicio, fin y actividad) y el seguimiento
(``dyndolod_seguimiento``: la respuesta a ``/lod_status``, PULL). Si cada uno formateara por su
cuenta, el mensaje que el operador recibe y el que consulta divergirían en el primer cambio.

**Todo texto dinámico va escapado.** Las líneas de log de DynDOLOD son ``<Error: ...>`` y el modo
HTML de Telegram las rechazaría con un 400: justo el aviso de fallo se perdería. El volumen de lo
que se muestra está acotado de modo que el peor caso de escape (``&`` -> ``&amp;``, x5) siga bajo
los 4096 caracteres: el sender partiría un mensaje más largo EN MEDIO de una etiqueta HTML. El
recorte se hace sobre el texto CRUDO, antes de escapar: cortar un ``&amp;`` a la mitad rompe el HTML.

Son funciones PURAS (sin I/O ni reloj): se prueban sin canal ni bus.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.app.core.event_payloads import (
        DynDOLODPipelineCompletedPayload,
        DynDOLODPipelineProgressPayload,
        DynDOLODPipelineStartedPayload,
    )

#: Cuántos errores se muestran y cuánto de cada uno. El producto, con el peor caso de escape (x5), más el
#: resto del mensaje, queda bajo el límite de Telegram (4096) — ver ``test_el_peor_caso_de_escape_...``.
_MAX_ERRORES_MOSTRADOS = 2
_MAX_CARACTERES_POR_ERROR = 300
#: Tope de los NOMBRES que se muestran (preset, archivo de log): vienen del sistema y son cortos en la práctica,
#: pero el peor caso de longitud es una propiedad del mecanismo y no de que lo sean.
MAX_CARACTERES_DE_NOMBRE = 100


def escapar_html(texto: str) -> str:
    """Escapa lo mínimo que exige el modo HTML de Telegram: ``&``, ``<`` y ``>``."""
    return html.escape(texto, quote=False)


def recortar(texto: str, maximo: int) -> str:
    """Recorta el texto CRUDO (antes de escapar: cortar un ``&amp;`` a la mitad rompe el HTML)."""
    return texto if len(texto) <= maximo else texto[: maximo - 1] + "…"


def duracion_legible(segundos: float) -> str:
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


def formatear_inicio(payload: DynDOLODPipelineStartedPayload) -> str:
    return "\n".join(
        [
            "🛠️ <b>DynDOLOD · etapa 9 iniciada</b>",
            f"Preset: <code>{escapar_html(payload.preset)}</code> · TexGen: {'sí' if payload.run_texgen else 'no'}",
            escapar_html(payload.operator_instructions),
            _instruccion_de_cierre(payload.run_texgen),
        ]
    )


def formatear_fin(payload: DynDOLODPipelineCompletedPayload) -> str:
    if payload.success:
        encabezado = "✅ <b>DynDOLOD · etapa 9 completada</b>"
    elif payload.cancelled:
        encabezado = "⏹️ <b>DynDOLOD · etapa 9 cancelada</b>"
    else:
        encabezado = "❌ <b>DynDOLOD · etapa 9 falló</b>"
    lineas = [f"{encabezado} ({duracion_legible(payload.duration_seconds)})"]

    if payload.success:
        texgen = "no se ejecutó" if not payload.run_texgen else ("✓" if payload.texgen_success else "—")
        lineas.append(f"TexGen: {texgen} · DynDOLOD: {'✓' if payload.dyndolod_success else '—'}")
        if payload.cancelled:
            lineas.append("Nota: se canceló el post-proceso; la salida ya estaba confirmada.")
        return "\n".join(lineas)

    errores = payload.errors[:_MAX_ERRORES_MOSTRADOS]
    if errores:
        lineas.extend(f"<pre>{escapar_html(recortar(error, _MAX_CARACTERES_POR_ERROR))}</pre>" for error in errores)
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


def tamano_legible(tamano: int) -> str:
    """``4300000`` -> ``4.1 MiB``. Un tamaño negativo (no debería existir) se lee como ``0 B``."""
    valor = float(max(tamano, 0))
    indice = 0
    while valor >= 1024 and indice < len(_UNIDADES_DE_TAMANO) - 1:
        valor /= 1024
        indice += 1
    return f"{int(valor)} B" if indice == 0 else f"{valor:.1f} {_UNIDADES_DE_TAMANO[indice]}"


def formatear_progreso(payload: DynDOLODPipelineProgressPayload) -> str:
    lineas = [f"{_ENCABEZADO_DE_ACTIVIDAD[payload.kind]} ({duracion_legible(payload.elapsed_seconds)})"]
    if payload.log:
        nombre = escapar_html(recortar(payload.log, MAX_CARACTERES_DE_NOMBRE))
        lineas.append(f"Log: <code>{nombre}</code> · {tamano_legible(payload.log_size_bytes)}")
    else:
        lineas.append("Todavía no hay un log de esta corrida.")
    if payload.kind == "stalled":
        lineas.append(
            f"Sin cambios hace {duracion_legible(payload.idle_seconds)}: "
            "la herramienta puede estar esperando una acción en su ventana."
        )
    elif payload.kind == "resumed":
        lineas.append(f"Volvió a escribir tras {duracion_legible(payload.idle_seconds)} sin cambios.")
    elif payload.idle_seconds >= 1:
        lineas.append(f"Último cambio del log hace {duracion_legible(payload.idle_seconds)}.")
    if payload.last_line:
        lineas.append("Última línea:")
        lineas.append(f"<pre>{escapar_html(recortar(payload.last_line, _MAX_CARACTERES_POR_ERROR))}</pre>")
    return "\n".join(lineas)
