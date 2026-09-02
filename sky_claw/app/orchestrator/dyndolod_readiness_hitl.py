"""Adaptador :class:`ConfirmadorHITL` del puerto T5-v2.1.

Convierte un :class:`HITLGuard` existente al protocolo estrecho
:class:`ConfirmadorDeConfiguracion` que el runner necesita. La traducción
queda en un solo lugar, visible y testeable:

* ``Decision.APPROVED`` → :class:`ResultadoConfirmacion.APROBADA`
* ``Decision.DENIED``   → :class:`ResultadoConfirmacion.DENEGADA`
* ``Decision.TIMEOUT``  → :class:`ResultadoConfirmacion.TIMEOUT`
* HITLGuard ``None``    → :class:`ResultadoConfirmacion.CANAL_NO_DISPONIBLE`

Es el ÚNICO camino válido para que el canal humano se conecte al runner
de T5-v2.1. ``ConfirmadorNoDisponible`` (en el módulo ``dyndolod_uia_gate``)
es el atajo fail-closed explícito para cuando no hay HITLGuard disponible;
este adapter es el puente real.

**No se hace el click de Start, no se inyecta mouse/teclado, no se modifica
el preset.** Es estrictamente un traductor. El click de Start lo hace el
operador sobre la GUI nativa de DynDOLOD/TexGen, fuera del runner.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sky_claw.app.security.hitl import CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA, HITLGuard
from sky_claw.local.tools.dyndolod_uia_gate import (
    OperatorConfigurationReadyRequest,
    ResultadoConfirmacion,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ConfirmadorHITL:
    """Adaptador :class:`HITLGuard` → :class:`ConfirmadorDeConfiguracion`.

    Reutiliza el guard existente — no introduce otro sistema de aprobación.
    El guard ya respeta la categoría ``dyndolod_configuracion_lista`` (delta
    §12): el ``make_gui_hitl_notify`` la parkea con modal manual y NUNCA
    la auto-aprueba con «Modo local». La ruta Telegram (delegate) que
    también pasa por este guard cae al camino por defecto para categorías
    no blancas — pero como la categoría es específica del runner, el
    delegate NO se invoca para esta confirmación (la rama del
    ``make_gui_hitl_notify`` para esa categoría retorna antes).

    Args:
        hitl_guard: El :class:`HITLGuard` compartido del supervisor. ``None``
            es válido (el adapter responderá ``CANAL_NO_DISPONIBLE`` en
            cada llamada, lo cual el runner traduce al fail-closed
            :class:`ResolucionProtocoloReadiness.CANAL_NO_DISPONIBLE`).
        prefix: Prefijo para ``request_id`` (identifica la operación en
            los logs de HITL sin filtrar el pid del usuario).
        on_informar: Callable opcional que recibe el aviso de "Start
            habilitado" / instrucciones previas. Por defecto ``None``:
            el adapter los loguea. La superficie GUI/Telegram puede
            inyectar aquí su propio notificador para mantener el
            mismo canal de la confirmación.
    """

    def __init__(
        self,
        *,
        hitl_guard: HITLGuard | None,
        prefix: str = "dyndolod-readiness",
        on_informar: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._hitl_guard = hitl_guard
        # El prefijo se mantiene como atributo de instancia para que un
        # ``ConfirmadorHITL`` específico pueda identificar la operación
        # (logger), pero el prefijo de ``new_hitl_request_id`` es el valor
        # por default que usa la familia de productores humanos (delta
        # §15, anclado en ``tests/test_hitl.py::_EXPECTED_PREFIXES``).
        self._prefix = prefix
        self._on_informar = on_informar

    @staticmethod
    def _translate(decision: object) -> ResultadoConfirmacion:
        """Traduce ``Decision`` de HITLGuard a :class:`ResultadoConfirmacion`."""
        # Import perezoso para no introducir dependencia circular en tiempo
        # de carga (hitl.py no debe importar este módulo).
        from sky_claw.app.security.hitl import Decision

        if decision is Decision.APPROVED:
            return ResultadoConfirmacion.APROBADA
        if decision is Decision.DENIED:
            return ResultadoConfirmacion.DENEGADA
        if decision is Decision.TIMEOUT:
            return ResultadoConfirmacion.TIMEOUT
        # ``HITLGuard`` no define un valor "canal no disponible" — pero el
        # guard con notify_fn fallido o cancelado cae en ``TIMEOUT``. La
        # no-disponibilidad de canal puro (guard ``None``) la resuelve el
        # adapter arriba con su propio atajo.
        return ResultadoConfirmacion.CANAL_NO_DISPONIBLE

    async def confirmar(self, solicitud: OperatorConfigurationReadyRequest) -> ResultadoConfirmacion:
        """Bloquea hasta la decisión del operador.

        El callable del runner no llama ``HITLGuard.request_approval``
        directamente: lo hace el adapter, que conoce la categoría
        específica, el ``request_id`` y la traducción del ``Decision``.
        El guard NUNCA devuelve ``APPROVED`` sin que el humano haya
        confirmado, ni siquiera con «Modo local» ON (delta §12:
        la categoría ``dyndolod_configuracion_lista`` rompe la rama
        de auto-approve del ``make_gui_hitl_notify``).
        """
        if self._hitl_guard is None:
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        # request_id determinista + único. new_hitl_request_id añade un
        # uuid corto para distinguir intentos distintos.
        from sky_claw.app.security.hitl import new_hitl_request_id

        request_id = new_hitl_request_id("dyndolod-readiness")
        try:
            decision = await self._hitl_guard.request_approval(
                request_id=request_id,
                reason=(
                    f"{solicitud.tool_name} pide confirmación de configuración: "
                    "el operador terminó de cargar el preset/worldspaces y "
                    "solicita re-validación del Output antes de pulsar Start."
                ),
                detail=(
                    f"pid={solicitud.pid} output_administrado={solicitud.expected_output} "
                    f"timeout={solicitud.timeout_seconds:.0f}s"
                ),
                category=CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
                timeout=float(solicitud.timeout_seconds),
            )
        except Exception as exc:
            # El guard no debería lanzar (devuelve TIMEOUT/DENIED en su
            # lugar), pero un adaptador externo o un bug del path de
            # notificación podría. Fail-closed: CANAL_NO_DISPONIBLE.
            logger.warning(
                "ConfirmadorHITL.confirmar: el guard lanzó %s; CANAL_NO_DISPONIBLE",
                exc,
                exc_info=True,
            )
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        return self._translate(decision)

    async def informar(self, *, tool: str, mensaje: str) -> None:
        """Best-effort: aviso de «Start habilitado» o de instrucción previa.

        Si el callable ``on_informar`` fue inyectado, se invoca con el
        mismo ``(tool, mensaje)``. Si falla, se loguea: el runner no
        depende de este canal para nada crítico (la instrucción completa
        viaja con la confirmación).
        """
        if self._on_informar is None:
            logger.info("ConfirmadorHITL.informar (sin on_informar): %s — %s", tool, mensaje)
            return
        try:
            await self._on_informar(tool, mensaje)
        except Exception:
            logger.warning("ConfirmadorHITL.informar: on_informar falló (tool=%s)", tool, exc_info=True)
