"""Sesión de proceso vivo del broker USVFS (primitive PR-586A).

**Por qué existe.** ``VfsExecutionBroker.submit()`` es request→result terminal:
espera ``job_result`` **y** ``worker_exit`` antes de retornar. Eso alcanza a los
rituales de una sola espera (LOOT), pero no al caso que #590 ya demostró: los
consumidores necesitan el PID del tool **mientras sigue vivo** (gate UIA inicial,
HITL, gate final) y necesitan cancelar y observar el teardown causal. Esta fachada
expone exactamente eso —``pid``, ``returncode``, ``wait()``, ``result()``,
``cancel()``— sin abrir el socket ni las entrañas del job al caller.

**Qué NO es.** No es un segundo broker ni un intérprete de protocolo: el routing
por ``job_id``, la validación de frames y el fence ``worker_exit`` viven en
``vfs_broker.py``; acá sólo se conserva el ESTADO del stream de eventos y se
responde con semántica idempotente. Tampoco es una API de ejecución genérica: el
worker decide qué corre (allowlist de tool_ids en ``vfs_contracts.py``); la
sesión sólo observa y cancela procesos que el worker ya spawneó.

**Fallar cerrado.** El stream de eventos tiene un estado mínimo y coherente:
``tool_started`` una sola vez; ``tool_exit`` después de él y una sola vez. Una
violación no se ignora: se registra como error de protocolo, se falla el
resultado terminal y se dispara el teardown del worker incoherente. El código de
salida del evento y el del resultado terminal deben coincidir cuando ambos
declaran uno; si no, ``result()`` levanta :class:`VfsSessionProtocolError`.

**Cancelación.** ``cancel()`` espera el fence causal completo (cancel al worker →
Job Object del bridge → ``worker_exit``) igual que ``submit()``. Cancelar al que
espera ``result()``/``wait()`` no abandona el job: completa el mismo teardown y
recién ahí propaga la cancelación.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.local.mo2.vfs_contracts import (
    VfsJobResult,
    VfsSessionEvent,
    VfsToolExitEvent,
    VfsToolStartedEvent,
)

if TYPE_CHECKING:
    from asyncio import Future, Task

logger = logging.getLogger(__name__)


class VfsSessionError(RuntimeError):
    """La sesión no puede entregar un desenlace confiable."""


class VfsSessionProtocolError(VfsSessionError):
    """El stream de eventos o el resultado terminal violan el contrato."""


class VfsJobCancelledError(VfsSessionError):
    """La sesión fue cancelada antes de producir un resultado terminal."""


@dataclass(slots=True)
class _EstadoDeSesion:
    """Estado mínimo del stream: PID inmutable y código de salida único."""

    pid: int | None = None
    exit_code: int | None = None
    protocol_error: VfsSessionProtocolError | None = None


class VfsProcessSession:
    """Handle del daemon sobre un job brokered con tool ya spawneado.

    Construida únicamente por :meth:`VfsExecutionBroker.open_session`, que la
    entrega después de observar un ``tool_started`` válido: no existe una sesión
    con PID placeholder.
    """

    def __init__(
        self,
        *,
        job_id: str,
        result_future: Future[VfsJobResult],
        event_queue: asyncio.Queue[VfsSessionEvent],
        cancelar: Callable[[], Awaitable[None]],
    ) -> None:
        self._job_id = job_id
        self._resultado_futuro = result_future
        self._cola_eventos = event_queue
        self._cancelar_job = cancelar
        self._estado = _EstadoDeSesion()
        self._tool_started = asyncio.Event()
        self._driver: Task[VfsJobResult] | None = None
        self._colector: Task[None] | None = None
        self._cancelacion: Task[None] | None = None
        self._teardown_completo = False

    # ------------------------------------------------------------------
    # Superficie pública
    # ------------------------------------------------------------------

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def pid(self) -> int:
        """PID del tool, disponible ANTES del resultado y estable para la sesión."""
        if self._estado.pid is None:
            raise VfsSessionProtocolError(f"la sesión {self._job_id} todavía no observó un tool_started")
        return self._estado.pid

    @property
    def returncode(self) -> int | None:
        """``None`` mientras el tool vive; el código terminal sólo si es confiable.

        Un desenlace fallido, cancelado o incoherente no tiene código: devolver
        el del worker o un valor parcial sería el falso verde que el repo evita.
        """
        driver = self._driver
        if driver is None or not driver.done() or driver.cancelled() or driver.exception() is not None:
            return None
        if self._estado.protocol_error is not None or self._estado.exit_code is None:
            return None
        resultado = driver.result()
        if resultado.exit_code is not None and resultado.exit_code != self._estado.exit_code:
            return None
        return resultado.exit_code

    async def wait(self) -> int:
        """Espera el desenlace y devuelve el exit code del tool (idempotente)."""
        resultado = await self.result()
        if resultado.exit_code is None:
            raise VfsSessionError(f"la sesión {self._job_id} terminó sin exit code")
        return resultado.exit_code

    async def result(self) -> VfsJobResult:
        """Desenlace terminal (idempotente): resultado, o la causa del fallo.

        Si al caller lo cancelan mientras espera, se completa el teardown causal
        y recién entonces se propaga ``CancelledError`` — la misma política de
        ``submit()`` (ADR 0007: no se abandona un job con el árbol vivo).
        """
        driver = self._driver
        if driver is None:
            raise VfsSessionError(f"la sesión {self._job_id} no tiene driver")
        try:
            resultado = await asyncio.shield(driver)
        except asyncio.CancelledError:
            with contextlib.suppress(asyncio.CancelledError):
                await self._fence_de_teardown()
            raise
        except BaseException:
            # Un desenlace fallido no puede tapar una violación de stream que ya
            # esté encolada: se drena antes de decidir qué causa se propaga. La
            # violación manda (from None: no es consecuencia del fallo del worker).
            self._drenar_eventos_pendientes()
            if self._estado.protocol_error is not None:
                raise self._estado.protocol_error from None
            raise
        self._drenar_eventos_pendientes()
        self._exigir_coherencia(resultado)
        return resultado

    async def cancel(self) -> None:
        """Cancela el job y espera el teardown causal completo (idempotente)."""
        if self._teardown_completo:
            return
        driver = self._driver
        if driver is not None and driver.done() and self._resultado_futuro.done():
            return
        await self._fence_de_teardown()

    # ------------------------------------------------------------------
    # Cableado (sólo para el broker)
    # ------------------------------------------------------------------

    def _vincular_driver(self, driver: Task[VfsJobResult]) -> None:
        self._driver = driver

    def _iniciar_recolector(self) -> None:
        if self._colector is None:
            self._colector = asyncio.create_task(
                self._recolectar(),
                name=f"vfs-session-events-{self._job_id}",
            )

    async def _detener_recolector(self) -> None:
        colector = self._colector
        if colector is None or colector.done():
            return
        colector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await colector

    async def _esperar_tool_started(self) -> None:
        """Bloquea hasta un ``tool_started`` válido o la causa del desenlace.

        Nunca devuelve una sesión sin PID: si el driver termina antes (falla de
        manifest, attestation, bootstrap del worker, bridge caído), propaga la
        causa; si terminó sin evento, es una violación de protocolo.
        """
        driver = self._driver
        if driver is None:
            raise VfsSessionError(f"la sesión {self._job_id} no tiene driver")
        espera = asyncio.ensure_future(self._tool_started.wait())
        try:
            await asyncio.wait({espera, driver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not espera.done():
                espera.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await espera
        if self._tool_started.is_set():
            return
        self._drenar_eventos_pendientes()
        if self._estado.protocol_error is not None:
            raise self._estado.protocol_error
        if driver.cancelled():
            raise VfsSessionProtocolError(f"la sesión {self._job_id} se canceló antes de tool_started")
        error = driver.exception()
        if error is not None:
            raise error
        resultado = driver.result()
        raise VfsSessionProtocolError(
            f"la sesión {self._job_id} terminó antes de tool_started: {resultado.message or 'sin mensaje'}"
        )

    # ------------------------------------------------------------------
    # Stream de eventos
    # ------------------------------------------------------------------

    async def _recolectar(self) -> None:
        while True:
            evento = await self._cola_eventos.get()
            try:
                self._aplicar_evento(evento)
            except VfsSessionProtocolError as exc:
                self._registrar_error_de_protocolo(exc)
                return

    def _drenar_eventos_pendientes(self) -> None:
        """Aplica ya los eventos encolados: el desenlace no depende del scheduler.

        El collector es una task; entre que un evento entra a la cola y que el
        collector lo ve hay un salto de scheduler. Si el desenlace terminal llega
        en esa ventana, leerlo sin drenar permitiría que una violación de stream
        quede tapada por la causa de la muerte del worker.
        """
        while True:
            try:
                evento = self._cola_eventos.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                self._aplicar_evento(evento)
            except VfsSessionProtocolError as exc:
                self._registrar_error_de_protocolo(exc)
                return

    def _aplicar_evento(self, evento: VfsSessionEvent) -> None:
        if isinstance(evento, VfsToolStartedEvent):
            if self._estado.pid is not None:
                raise VfsSessionProtocolError(f"tool_started duplicado para el job {self._job_id}")
            self._estado.pid = evento.tool_pid
            self._tool_started.set()
            return
        if isinstance(evento, VfsToolExitEvent):
            if self._estado.pid is None:
                raise VfsSessionProtocolError(f"tool_exit antes de tool_started para el job {self._job_id}")
            if self._estado.exit_code is not None:
                raise VfsSessionProtocolError(f"tool_exit duplicado para el job {self._job_id}")
            self._estado.exit_code = evento.exit_code
            return
        raise VfsSessionProtocolError(f"evento de sesión desconocido: {evento!r}")

    def _registrar_error_de_protocolo(self, error: VfsSessionProtocolError) -> None:
        if self._estado.protocol_error is None:
            self._estado.protocol_error = error
        logger.error(
            "stream de sesión inválido; teardown del worker incoherente: %s",
            error,
            extra={"job_id": self._job_id},
        )
        if not self._resultado_futuro.done():
            self._resultado_futuro.set_exception(error)
        self._iniciar_cancelacion()

    def _exigir_coherencia(self, resultado: VfsJobResult) -> None:
        if self._estado.protocol_error is not None:
            raise self._estado.protocol_error
        if (
            self._estado.exit_code is not None
            and resultado.exit_code is not None
            and self._estado.exit_code != resultado.exit_code
        ):
            raise VfsSessionProtocolError(
                f"tool_exit reportó {self._estado.exit_code} pero el resultado declara {resultado.exit_code} "
                f"para el job {self._job_id}"
            )

    # ------------------------------------------------------------------
    # Cancelación y fence de teardown
    # ------------------------------------------------------------------

    def _iniciar_cancelacion(self) -> Task[None]:
        if self._cancelacion is None:
            self._cancelacion = asyncio.create_task(
                self._cancelar_y_confirmar(),
                name=f"vfs-session-cancel-{self._job_id}",
            )
        return self._cancelacion

    async def _cancelar_y_confirmar(self) -> None:
        try:
            await self._cancelar_job()
        except Exception as exc:  # noqa: BLE001 - el teardown nunca puede dejar la sesión colgada
            logger.error(
                "la cancelación de la sesión %s falló: %s",
                self._job_id,
                exc,
                exc_info=True,
                extra={"job_id": self._job_id},
            )
            if not self._resultado_futuro.done():
                self._resultado_futuro.set_exception(exc)
            return
        if not self._resultado_futuro.done():
            self._resultado_futuro.set_exception(VfsJobCancelledError(f"job {self._job_id} cancelado"))

    async def _fence_de_teardown(self) -> None:
        """Teardown completo que no se abandona si al caller lo cancelan."""
        cancelada = False
        limpieza = self._iniciar_cancelacion()
        while not limpieza.done():
            try:
                await asyncio.shield(limpieza)
            except asyncio.CancelledError:
                cancelada = True
        driver = self._driver
        if driver is not None:
            while not driver.done():
                try:
                    await asyncio.shield(driver)
                except asyncio.CancelledError:
                    cancelada = True
                except BaseException:
                    # El driver terminó con la causa del desenlace (timeout,
                    # worker caído, protocolo): acá sólo se espera el cleanup;
                    # el error se lee por result().
                    pass
            if not driver.cancelled():
                driver.exception()  # recuperada: el desenlace va por result()
        self._teardown_completo = True
        if cancelada:
            raise asyncio.CancelledError
