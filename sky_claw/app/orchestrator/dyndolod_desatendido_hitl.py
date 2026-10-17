"""#661 — adapter HITL REMOTO de la etapa 9 desatendida (Telegram vía ``HITLGuard``).

**Qué responde.** Las dos únicas decisiones humanas que quedan cuando nadie está
frente a la PC:

1. *"¿Apruebo que TexGen/DynDOLOD corran solos con ESTA configuración?"* — antes
   del spawn, con el build verificado, la huella de cada preset y el Output
   administrado. Reemplaza formalmente al HITL asistido ("terminé de configurar
   la GUI"): la configuración ya no se elige en la ventana, se aprueba tal como
   está en disco.
2. *"¿Continúo este modal de arranque medido?"* — mid-run, con la instrucción
   exacta del diálogo y su explicación. Sin aprobación el driver no lo toca.

**Categoría propia y NO auto-aprobable** (``CATEGORIA_DYNDOLOD_DESATENDIDO``):
la GUI no la intercepta, así que viaja por el canal remoto del guard.
``hitl_guard=None`` no es "aprobado": es ``CANAL_NO_DISPONIBLE`` y la corrida se
corta.

**No conoce al runner ni a COM**: traduce el puerto ``AprobadorDesatendido`` a
``HITLGuard.request_approval`` y nada más.
"""

from __future__ import annotations

import asyncio
import logging

from sky_claw.app.security.hitl import (
    CATEGORIA_DYNDOLOD_DESATENDIDO,
    Decision,
    HITLGuard,
    new_hitl_request_id,
)
from sky_claw.local.tools.dyndolod_uia_driver import SolicitudAprobacionDesatendida, SolicitudModalConocido
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion

logger = logging.getLogger(__name__)


def _traducir(decision: object) -> ResultadoConfirmacion:
    """``Decision`` → ``ResultadoConfirmacion``; lo inesperado es ``CANAL_NO_DISPONIBLE``."""
    if decision is Decision.APPROVED:
        return ResultadoConfirmacion.APROBADA
    if decision is Decision.DENIED:
        return ResultadoConfirmacion.DENEGADA
    if decision is Decision.TIMEOUT:
        return ResultadoConfirmacion.TIMEOUT
    return ResultadoConfirmacion.CANAL_NO_DISPONIBLE


def texto_de_configuracion(solicitud: SolicitudAprobacionDesatendida) -> tuple[str, str]:
    """``(reason, detail)`` del prompt de configuración: lo que el operador aprueba, entero."""
    cadena = "TexGen → DynDOLOD" if solicitud.run_texgen else "DynDOLOD"
    reason = (
        f"Etapa 9 DESATENDIDA ({cadena}): Sky-Claw va a operar las ventanas por UI Automation, "
        "sin intervención en la PC, con la configuración que cada herramienta tiene guardada en su "
        "preset (Sky-Claw no la edita y restaura el preset al terminar). Aprobá sólo si esa es la "
        "configuración que querés generar."
    )
    lineas = [f"preset pedido: {solicitud.preset_pedido}"]
    for herramienta in solicitud.herramientas:
        calidad = f" · Preset={herramienta.preset_de_calidad}" if herramienta.preset_de_calidad else ""
        lineas.append(
            f"{herramienta.tool} {herramienta.version} (sha256 {herramienta.exe_sha256[:12]}…){calidad} · "
            f"preset {herramienta.preset} (huella {herramienta.preset_huella[:12]}…) · "
            f"Output {herramienta.output_esperado}"
        )
    lineas.append(f"timeout de esta aprobación: {solicitud.timeout_seconds:.0f}s")
    return reason, "\n".join(lineas)


def texto_de_modal(solicitud: SolicitudModalConocido) -> tuple[str, str]:
    """``(reason, detail)`` del prompt de modal: la instrucción EXACTA y su explicación."""
    reason = (
        f"{solicitud.tool} (pid={solicitud.pid}) mostró un aviso de arranque conocido. "
        "Aprobá para que Sky-Claw pulse 'Ignore' y continúe; denegá para cortar la corrida."
    )
    detail = (
        f"aviso: {solicitud.instruccion}\n"
        f"qué significa: {solicitud.descripcion}\n"
        f"id: {solicitud.modal_id} · timeout: {solicitud.timeout_seconds:.0f}s"
    )
    return reason, detail


class AprobadorDesatendidoHITL:
    """Implementación productiva de ``AprobadorDesatendido`` sobre ``HITLGuard``."""

    def __init__(self, *, hitl_guard: HITLGuard | None) -> None:
        self._guard = hitl_guard

    async def aprobar_configuracion(self, solicitud: SolicitudAprobacionDesatendida) -> ResultadoConfirmacion:
        if self._guard is None:
            logger.warning("etapa 9 desatendida sin canal HITL cableado: se corta fail-closed")
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        reason, detail = texto_de_configuracion(solicitud)
        # Prefijo literal: el ancla de productores de `tests/test_hitl.py` lo
        # exige `ast.Constant` (la identidad del intento no deriva del operador).
        request_id = new_hitl_request_id("dyndolod-desatendido")
        try:
            decision = await self._guard.request_approval(
                request_id=request_id,
                reason=reason,
                detail=detail,
                category=CATEGORIA_DYNDOLOD_DESATENDIDO,
                timeout=float(solicitud.timeout_seconds),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- canal humano: cualquier fallo es "no disponible"
            logger.warning("el canal HITL de la aprobación desatendida falló", exc_info=True)
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        return _traducir(decision)

    async def aprobar_modal(self, solicitud: SolicitudModalConocido) -> ResultadoConfirmacion:
        if self._guard is None:
            logger.warning("modal de %s sin canal HITL cableado: se corta fail-closed", solicitud.tool)
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        reason, detail = texto_de_modal(solicitud)
        request_id = new_hitl_request_id("dyndolod-modal")
        try:
            decision = await self._guard.request_approval(
                request_id=request_id,
                reason=reason,
                detail=detail,
                category=CATEGORIA_DYNDOLOD_DESATENDIDO,
                timeout=float(solicitud.timeout_seconds),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- canal humano: cualquier fallo es "no disponible"
            logger.warning("el canal HITL del modal de %s falló", solicitud.tool, exc_info=True)
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        return _traducir(decision)

    async def informar(self, *, tool: str, mensaje: str) -> None:
        """Aviso de progreso al operador remoto. Best-effort (el guard absorbe el fallo)."""
        if self._guard is None:
            logger.info("etapa 9 desatendida (%s): %s", tool, mensaje)
            return
        await self._guard.notify_operator(mensaje)
