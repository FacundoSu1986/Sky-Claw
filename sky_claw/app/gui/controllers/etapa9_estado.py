"""Estado de la etapa 9 para la GUI: progreso de la corrida y cuarentena del broker VFS (P-3c-2, H-11 y H-4).

Antes solo lo veía el agente (``lods_status``). La GUI lee la MISMA fuente (``servicio.progreso_actual`` y la consulta de
cuarentena del contexto) y arma los hechos con la MISMA función (``progreso_en_hechos``). Es solo lectura: liberar la
cuarentena sigue siendo ``--mode vfs-quarantine`` con Sky-Claw cerrado, y acá solo se muestra el procedimiento.

Son HECHOS: ningún texto de este módulo emite un veredicto «colgado».
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sky_claw.local.tools.dyndolod_progreso import progreso_en_hechos

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EstadoDeLaEtapa9:
    """Lo que la GUI puede mostrar hoy de la etapa 9. ``None`` = nada que mostrar de ese lado."""

    progreso: dict[str, Any] | None
    cuarentena: dict[str, Any] | None


def leer_estado_de_la_etapa9(
    *,
    supervisor: object | None,
    consultar_cuarentena: Callable[[], dict[str, Any] | None] | None,
    ahora: float,
) -> EstadoDeLaEtapa9:
    """Lee progreso y cuarentena. Best-effort: ninguna falla de lectura rompe el panel (se loguea y se omite).

    El progreso lo decide el CICLO DE VIDA DEL SERVICIO, común a toda superficie: ``progreso_actual`` se descarta al
    iniciar y al terminar cada corrida, así que una muestra presente es de una corrida en curso, la haya lanzado la GUI o
    el agente (Telegram, ``/api/chat``). Por eso NO se condiciona al single-flight de la GUI, que no ve lo remoto; la edad
    de la muestra queda a la vista. La cuarentena se informa SIEMPRE, también sin corrida: es de la instancia.
    """
    return EstadoDeLaEtapa9(progreso=_progreso(supervisor, ahora), cuarentena=_cuarentena(consultar_cuarentena))


def _progreso(supervisor: object | None, ahora: float) -> dict[str, Any] | None:
    try:
        servicio = None if supervisor is None else getattr(supervisor, "dyndolod_service", None)
        muestra = None if servicio is None else getattr(servicio, "progreso_actual", None)
        return None if muestra is None else progreso_en_hechos(muestra, ahora=ahora)
    except Exception:
        logger.warning("no se pudo leer el progreso de la etapa 9 para la GUI", exc_info=True)
        return None


def _cuarentena(consultar: Callable[[], dict[str, Any] | None] | None) -> dict[str, Any] | None:
    if consultar is None:
        return None
    try:
        return consultar()
    except Exception:
        logger.warning("no se pudo consultar la cuarentena del broker VFS para la GUI", exc_info=True)
        return None


def _duracion(segundos: float) -> str:
    total = int(max(0.0, segundos))
    horas, resto = divmod(total, 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas} h {minutos:02d} min"
    if minutos:
        return f"{minutos} min {seg:02d} s"
    return f"{seg} s"


def _tamano(cantidad: int) -> str:
    if cantidad < 1024:
        return f"{cantidad} B"
    valor = float(cantidad)
    for unidad in ("KiB", "MiB", "GiB"):
        valor /= 1024
        if valor < 1024 or unidad == "GiB":
            return f"{valor:.1f}".replace(".", ",") + f" {unidad}"
    raise AssertionError("inalcanzable")  # pragma: no cover


def lineas_de_progreso(hechos: Mapping[str, Any]) -> tuple[str, ...]:
    """Las líneas que ve el operador: hechos de la muestra y cuán vieja es. Sin veredicto de «colgado»."""
    lineas = [f"{hechos['herramienta']} · {_duracion(hechos['transcurrido_segundos'])} desde que arrancó"]
    log_bytes = hechos["log_bytes"]
    if log_bytes is None:
        lineas.append("Sin log medible todavía: el tamaño y la antigüedad no se pudieron leer.")
    else:
        sin_cambios = hechos["log_sin_cambios_segundos"]
        detalle = "" if sin_cambios is None else f" · sin cambios hace {_duracion(sin_cambios)}"
        lineas.append(f"Log: {_tamano(log_bytes)}{detalle}")
    lineas.append(f"Muestra de hace {_duracion(hechos['muestra_hace_segundos'])}")
    return tuple(lineas)


def texto_de_cuarentena(cuarentena: Mapping[str, Any]) -> str:
    """Qué le pasa a la instancia, por qué y cómo se libera (el procedimiento lo redacta ``vfs_quarantine``)."""
    return (
        "La instancia de MO2 está en cuarentena: Sky-Claw no lanza nada en ella hasta que la liberes. "
        f"Motivo: {cuarentena['razon']}. {cuarentena['como_liberar']}"
    )
