"""T5-v2 — adapter HITL del protocolo de readiness de DynDOLOD/TexGen.

**Qué responde.** *"El operador terminó de configurar a mano esta GUI y declara
que está lista para la verificación final."* No es "permito ejecutar una tool":
esa es la pregunta del middleware, pre-run, con categoría ``tool_execution``.
Ésta ocurre MID-RUN, con el proceso ya spawneado y sus drains vivos, y su
respuesta habilita el FINAL gate sobre la MISMA instancia.

**Por qué es un adapter y no código del gate.** ``dyndolod_uia_gate`` define
SÓLO el puerto (:class:`ConfirmadorDeConfiguracion`): un protocolo de dos métodos
asíncronos, sin dependencias de NiceGUI ni de Telegram. La traducción a una
superficie humana vive acá, en ``app.orchestrator``, y reutiliza
:class:`HITLGuard` — el mismo guard, la misma cola de pendientes, el mismo
lifecycle de cancelación que el resto del repo. El runner no conoce ninguna de
las dos capas.

**Read-only, otra vez.** Este adapter no pulsa Start, no escribe el preset, no
inyecta mouse ni teclado, no toca la ventana. Sólo pide una decisión humana y
la traduce a un valor del enum del puerto.

**Categoría propia y NO auto-aprobable.** ``CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA``
se trata como ``download`` y ``sandbox_promotion``: nunca se auto-aprueba en
Modo local. Si se reutilizara ``tool_execution`` —que sí se auto-aprueba— el
operador jamás declararía que terminó de configurar, y el final gate verificaría
una configuración que nadie confirmó.

**Timeout por solicitud, no global.** La espera mid-run se mide en minutos de
trabajo humano, no en la escala de un prompt de scope; el adapter pide su plazo
por request (``solicitud.timeout_seconds``) para no alargar el timeout de TODOS
los prompts del proceso. El runner además acota el callable desde afuera
(defensa en profundidad: un adapter roto no puede colgar al runner de por vida).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sky_claw.app.security.hitl import (
    CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
    Decision,
    HITLGuard,
    new_hitl_request_id,
)
from sky_claw.local.tools.dyndolod_uia_gate import (
    OperatorConfigurationReadyRequest,
    ResultadoConfirmacion,
)

logger = logging.getLogger(__name__)


class ConfirmadorHITL:
    """Puente real entre :class:`HITLGuard` y el puerto del gate.

    ``hitl_guard=None`` NO es "aprobado por omisión": es el caso "no hay canal
    humano cableado" (preview, rig sin GUI, tests) y devuelve
    ``CANAL_NO_DISPONIBLE``, que el runner corta igual que un deny.
    """

    def __init__(
        self,
        *,
        hitl_guard: HITLGuard | None,
        on_informar: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._guard = hitl_guard
        self._on_informar = on_informar

    @staticmethod
    def _traducir(decision: object) -> ResultadoConfirmacion:
        """``Decision`` → ``ResultadoConfirmacion``, fail-closed ante lo inesperado.

        Un valor fuera del contrato (un adapter roto, un enum nuevo) se traduce a
        ``CANAL_NO_DISPONIBLE`` y no a "seguir": la única traducción que habilita
        el final gate es ``APPROVED``.
        """
        if decision is Decision.APPROVED:
            return ResultadoConfirmacion.APROBADA
        if decision is Decision.DENIED:
            return ResultadoConfirmacion.DENEGADA
        if decision is Decision.TIMEOUT:
            return ResultadoConfirmacion.TIMEOUT
        return ResultadoConfirmacion.CANAL_NO_DISPONIBLE

    async def confirmar(
        self,
        solicitud: OperatorConfigurationReadyRequest,
    ) -> ResultadoConfirmacion:
        """Bloquea hasta que el operador decide (o el canal falla).

        El texto del prompt es el CONTRATO de la corrección humana: cita el
        Output observado, el esperado (la misma raíz del ``-o:``) y la
        instrucción explícita de corregir el campo ANTES de aprobar. La versión
        anterior decía "dejá el campo Output como está", y el rig real la
        refutó: TexGen arranca con el preset rancio precargado, así que el
        operador tiene que poder corregirlo — y el final gate verifica que lo
        haya hecho. La GUI sigue siendo read-only para Sky-Claw: la corrección la
        hace el humano, no este adapter.
        """
        if self._guard is None:
            logger.warning(
                "readiness de %s sin canal HITL cableado: se corta fail-closed",
                solicitud.tool_name,
            )
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE

        observado = solicitud.observed_output or "(no disponible)"
        esperado = str(solicitud.expected_output)
        # Literal y no una constante del módulo: el ancla de productores de
        # `tests/test_hitl.py` exige que el prefijo sea un `ast.Constant` — es la
        # propiedad que garantiza que la identidad del intento no se derive de
        # nada del operador. El prefijo declarado ahí es "dyndolod-readiness".
        request_id = new_hitl_request_id("dyndolod-readiness")
        try:
            decision = await self._guard.request_approval(
                request_id=request_id,
                reason=(
                    f"{solicitud.tool_name} (pid={solicitud.pid}) pide confirmación de configuración. "
                    f"Output observado: {observado}. Output esperado: {esperado}. "
                    f"Antes de aprobar, corregí el campo Output para que sea exactamente: {esperado}. "
                    "Después elegí el preset y los worldspaces en la GUI y aprobá sólo cuando la configuración esté lista."
                ),
                detail=(
                    f"pid={solicitud.pid} output_observado={observado} output_administrado={esperado} "
                    f"timeout={solicitud.timeout_seconds:.0f}s"
                ),
                category=CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
                timeout=float(solicitud.timeout_seconds),
            )
        except asyncio.CancelledError:
            # La cancelación externa (deadline del runner, shutdown) debe seguir
            # visible: el `finally` de `request_approval` ya limpió el pendiente y
            # disparó `on_cancel`, así que la UI no queda accionable.
            raise
        except Exception as exc:  # noqa: BLE001 -- canal humano: cualquier fallo es "no disponible"
            logger.warning(
                "el canal HITL de readiness de %s falló: %s",
                solicitud.tool_name,
                exc,
                exc_info=True,
            )
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE

        return self._traducir(decision)

    async def informar(self, *, tool: str, mensaje: str) -> None:
        """Aviso best-effort POST-final-MATCH ("Output verificado, podés dar Start").

        Best-effort SOLO porque la instrucción contractual completa ya viajó en el
        texto de la confirmación que el operador leyó para aprobar. Un fallo acá
        no puede cortar una corrida que ya pasó los dos gates.
        """
        if self._on_informar is None:
            logger.info("readiness de %s: %s", tool, mensaje)
            return
        try:
            await self._on_informar(tool, mensaje)
        except Exception:  # noqa: BLE001 -- aviso best-effort, nunca gatea
            logger.warning("no se pudo informar al operador sobre %s", tool, exc_info=True)
