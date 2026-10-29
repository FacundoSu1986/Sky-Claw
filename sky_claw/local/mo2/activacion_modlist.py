"""¿Quedó activado en el ``modlist.txt`` del perfil el lote de mods que se acaba de instalar?

Instalar bajo ``<mo2>/mods/`` no activa: sin ``+Nombre`` en el ``modlist.txt`` USVFS no monta el mod y el
juego nunca lo ve (p. ej. ``CommunityShaders.dll``). Quien instala tiene que activar, y hay DOS superficies
que lo hacen sobre el mismo recurso: el agente LLM (``setup_tools``, NGIO y Community Shaders) y el ritual
de la GUI (``run_ritual_install``, Community Shaders). Cada una tenía su copia del bloque, y repartirlo fue
exactamente el defecto que dejó a NGIO reportando éxito sin activar mientras Community Shaders ya lo había
corregido ("arreglar un hermano y no al otro", ver ``AGENTS.md``).

Este módulo contesta **una sola vez** la pregunta de la que dependen las dos: *dado un lote de nombres de mod,
¿quedaron todos habilitados en el perfil?* Vive en la capa neutral (``sky_claw.local.mo2``) a propósito: la GUI
no puede importar al agente ni al revés, y las dos importan de acá. Cada superficie traduce el
:class:`ResultadoActivacion` a SU salida (el agente a ``success``/``message``; la GUI al cartel del operador),
pero ninguna decide por su cuenta si hubo éxito.

El estado es un vocabulario CERRADO (:data:`EstadoActivacion`): ``ok`` es el único éxito; ``vacio`` impide que
``0 == 0`` se lea como «todo activado». Un estado nuevo obliga a decidir qué hace cada superficie con él
(``tests/test_cs_activacion_modlist.py`` congela el vocabulario y el cartel de la GUI por igualdad literal).

**Lo que NO hace:** no instala, no persiste la config del instalador ni reintenta. Tampoco decide la política de
posición del mod: delega en ``MO2Controller.add_mod_to_modlist`` sin ``highest_priority`` (un ``-Mod`` previo se
habilita en su lugar; un mod ausente se agrega al final). Quién más llama a ese mutador, y desde dónde, lo
enumera el ancla de ``tests/test_cs_activacion_modlist.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

__all__ = [
    "ActivadorDeModlist",
    "EstadoActivacion",
    "ResultadoActivacion",
    "activar_en_modlist",
]

logger = logging.getLogger(__name__)

#: Vocabulario cerrado de la activación. ``ok`` es el único éxito.
EstadoActivacion = Literal["ok", "parcial", "fallo", "no_disponible", "vacio"]

_MOTIVO_VACIO: Final = "el instalador no devolvió componentes"
_MOTIVO_NO_DISPONIBLE: Final = "no hay MO2 o perfil en el contexto de esta sesión"


class ActivadorDeModlist(Protocol):
    """Lo único que se le pide al controlador de MO2: habilitar un mod en el modlist de un perfil."""

    async def add_mod_to_modlist(self, mod_name: str, *, profile: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ResultadoActivacion:
    """Veredicto de la activación de un lote.

    ``activados`` son los nombres que quedaron escritos, en orden (en ``parcial`` es el prefijo que se llegó a
    escribir antes de la falla). ``error`` es el motivo crudo —el texto de la excepción, o la razón de
    ``vacio``/``no_disponible``—; es ``None`` solo en ``ok``.
    """

    estado: EstadoActivacion
    activados: tuple[str, ...] = ()
    error: str | None = None

    @property
    def exito(self) -> bool:
        """``True`` exactamente cuando ``estado == "ok"``."""
        return self.estado == "ok"

    @property
    def mensaje(self) -> str:
        """Mensaje canónico del contrato de tools: vacío SOLO en éxito."""
        if self.exito:
            return ""
        sufijo = f": {self.error}" if self.error else ""
        return f"Instalado en disco pero no activado en el perfil ({self.estado}){sufijo}"

    def como_campos(self) -> dict[str, object]:
        """Campos del contrato de tools (``success``/``message``) más los estructurados de la activación."""
        return {
            "activados": list(self.activados),
            "activacion": self.estado,
            "success": self.exito,
            "message": self.mensaje,
        }


async def activar_en_modlist(
    nombres: Sequence[str],
    mo2: ActivadorDeModlist | None,
    profile: str | None,
    herramienta: str,
) -> ResultadoActivacion:
    """Habilita ``nombres`` en el ``modlist.txt`` de ``profile`` y devuelve el veredicto.

    Es el ÚNICO bucle de producción sobre ``add_mod_to_modlist`` para lotes de mods recién instalados. Corta en la
    primera falla: los posteriores no se tocan, así que ``parcial`` es siempre un prefijo de ``nombres``.

    Args:
        nombres: nombres de carpeta de mod (``ModInstallResult.mod_name``), en el orden a activar.
        mo2: controlador de la instancia en la que se instaló, o ``None`` si la sesión no lo tiene.
        profile: perfil de la sesión, o ``None``/vacío si no hay.
        herramienta: nombre legible de lo instalado, solo para el log.

    Returns:
        ``vacio`` si ``nombres`` está vacío (se decide ANTES que la falta de MO2), ``no_disponible`` si falta ``mo2`` o
        el perfil, ``ok`` si se escribieron todos, y ``parcial``/``fallo`` si la escritura lanzó con/sin
        entradas ya escritas. ``asyncio.CancelledError`` no se captura: se propaga.
    """
    if not nombres:
        return ResultadoActivacion("vacio", error=_MOTIVO_VACIO)
    if mo2 is None or not profile:
        return ResultadoActivacion("no_disponible", error=_MOTIVO_NO_DISPONIBLE)
    activados: list[str] = []
    try:
        for nombre in nombres:
            await mo2.add_mod_to_modlist(nombre, profile=profile)
            activados.append(nombre)
    except Exception as exc:  # noqa: BLE001 — degradar a fallo parcial, no perder el resultado de la instalación
        logger.exception("Activación en modlist falló para %s", herramienta)
        return ResultadoActivacion("parcial" if activados else "fallo", tuple(activados), str(exc))
    return ResultadoActivacion("ok", tuple(activados))
