"""Human-in-the-Loop (HITL) Guard.

When the agent encounters an action that falls outside its autonomous
scope (e.g. a mod hosted on GitHub or a request to run an unknown
patcher), :class:`HITLGuard` pauses the task queue and requests
operator authorisation via Telegram.
"""

from __future__ import annotations

import asyncio
import enum
import fnmatch
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sky_claw.config import HITL_TIMEOUT_SECONDS, OUT_OF_SCOPE_HOSTS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

HITL_OBSERVER_TIMEOUT_SECONDS = 2.0


class Decision(enum.Enum):
    """Operator decision for a pending HITL prompt."""

    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


#: Categoría HITL semántica para el protocolo T5-v2.1 de DynDOLOD/TexGen
#: (readyness humana antes de revalidar el Output). El guardrail de «Modo
#: local» (T5-v2.1 §12) NUNCA la auto-aprueba — el operador debe confirmar
#: manualmente que terminó de configurar. Es la única categoría «de
#: runtime» del pipeline que no tiene atajo: ``download`` es egress, no
#: se auto-aprueba; ``sandbox_promotion`` es revisión post-run, no se
#: auto-aprueba; ``tool_execution`` se auto-aprueba en Modo local; ESTA
#: categoría también queda fuera del auto-approve porque aprobar
#: automáticamente la confirmación de configuración reproduciría el
#: R4b del rig (#528) sin que el humano pueda siquiera ver la GUI.
CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA = "dyndolod_configuracion_lista"


@dataclass
class HITLRequest:
    """Describes a pending authorisation request."""

    request_id: str
    reason: str
    url: str | None = None
    detail: str = ""
    category: str = "scope"
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    decision: Decision = Decision.TIMEOUT
    # F6 (auditoría 2026-07-18): marca de resolución terminal. La decisión se
    # commitea una sola vez, bajo el lock del guard (primer escritor gana): una
    # vez True, ni el timeout ni un respond tardío pueden pisar ``decision``.
    _resolved: bool = field(default=False, repr=False)


def new_hitl_request_id(prefix: str) -> str:
    """Genera una identidad corta y única para un intento HITL del productor.

    El ``request_id`` sigue siendo opaco para :class:`HITLGuard`: este helper solo
    evita que productores humanos reutilicen una identidad determinista entre
    intentos, sin cambiar el protocolo de respuesta ni canonicalizar el prefijo.
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class HITLGuard:
    """Gestiona el flujo de autorización HITL.

    Parámetros
    ----------
    notify_fn:
        Callable asíncrona que envía el prompt de autorización al operador
        (por ejemplo, un mensaje de Telegram). Recibe un :class:`HITLRequest`
        y debe retornar cuando el mensaje se haya enviado.
    timeout:
        Segundos de espera de la respuesta del operador antes de comprometer
        ``TIMEOUT``.
    out_of_scope_hosts:
        Patrones de host que activan HITL.
    on_terminal:
        Observer best-effort invocado después de que gane la primera decisión terminal.
    on_cancel:
        Observer best-effort invocado cuando se cancela una solicitud no resuelta.
    observer_timeout:
        Máximo de segundos permitido para cada observer del lifecycle. Es
        independiente del timeout de respuesta humana y mantiene la presentación
        como best-effort.

    """

    def __init__(
        self,
        notify_fn: Callable[[HITLRequest], Awaitable[None]] | None = None,
        timeout: int = HITL_TIMEOUT_SECONDS,
        out_of_scope_hosts: frozenset[str] | None = None,
        on_terminal: Callable[[HITLRequest, Decision], Awaitable[None]] | None = None,
        on_cancel: Callable[[HITLRequest], Awaitable[None]] | None = None,
        observer_timeout: float = HITL_OBSERVER_TIMEOUT_SECONDS,
    ) -> None:
        self._notify = notify_fn
        self._timeout = timeout
        self._hosts = out_of_scope_hosts or OUT_OF_SCOPE_HOSTS
        if observer_timeout <= 0:
            raise ValueError("observer_timeout must be positive")
        self._on_terminal = on_terminal
        self._on_cancel = on_cancel
        self._observer_timeout = float(observer_timeout)
        self._pending: dict[str, HITLRequest] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def requires_approval(self, url: str) -> bool:
        """Return ``True`` if *url* is outside the autonomous scope."""
        hostname = (urlparse(url).hostname or "").lower()
        return any(fnmatch.fnmatch(hostname, pattern) for pattern in self._hosts)

    # ------------------------------------------------------------------
    # Request / Respond cycle
    # ------------------------------------------------------------------

    async def request_approval(
        self,
        request_id: str | None = None,
        reason: str = "",
        url: str | None = None,
        detail: str = "",
        category: str = "scope",
        timeout: float | None = None,
    ) -> Decision:
        """Pausa la ejecución y espera la autorización del operador.

        *request_id* es un identificador proporcionado por el caller (por ejemplo,
        ``"download-10-20"``). Si no se proporciona, se genera un UUID único.

        *timeout* override por-solicitud: si es ``None``, se usa el timeout
        global del guard (``self._timeout``); si es ``float``, sólo esta
        solicitud usa ese valor. Es el seam que el protocolo T5-v2.1 usa
        para acotar la espera del callable ``ConfirmadorDeConfiguracion``
        por request (delta §10, §39). El override es estrictamente
        per-solicitud: el timeout global no cambia.

        Devuelve la :class:`Decision` del operador. Según la política fail-secure,
        si no llega respuesta durante el timeout se devuelve ``Decision.TIMEOUT``
        y nunca cuenta como aprobación. Un ``notify_fn`` fallido también produce
        ``Decision.TIMEOUT`` sin crear una entrega ficticia.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        effective_timeout = float(timeout) if timeout is not None else self._timeout
        req = HITLRequest(
            request_id=request_id,
            reason=reason,
            url=url,
            detail=detail,
            category=category,
        )
        async with self._lock:
            if request_id in self._pending:
                logger.warning("HITL: duplicate request_id %s rejected", request_id)
                return Decision.DENIED
            self._pending[request_id] = req

        notify_failed = False
        try:
            try:
                if self._notify is not None:
                    await self._notify(req)
            except Exception as exc:
                notify_failed = True
                logger.error("HITL: notify_fn failed: %s", exc)
                return Decision.TIMEOUT

            logger.info("HITL: awaiting operator decision for %s", request_id)

            try:
                await asyncio.wait_for(req._event.wait(), timeout=effective_timeout)
            except TimeoutError:
                # F6: commitear el timeout bajo el lock (primer escritor gana). Si
                # un respond se coló en la ventana de la race y ya resolvió la
                # request, ``_commit`` es no-op y se honra la decisión del operador.
                if await self._commit(request_id, Decision.TIMEOUT):
                    logger.warning(
                        "HITL: timeout for %s — terminal timeout (fail-secure policy)",
                        request_id,
                    )
            return req.decision
        finally:
            # También limpiar si notify_fn se cancela: CancelledError debe
            # propagarse al caller, pero nunca dejar un pending huérfano.
            async with self._lock:
                self._pending.pop(request_id, None)

            # Cancelar una espera con un prompt ya enviado no crea una decisión
            # nueva; sólo invalida su UI para que no queden botones accionables.
            if not notify_failed and not req._resolved and self._on_cancel is not None:
                await self._run_observer_bounded(
                    self._on_cancel,
                    req,
                    observer_name="on_cancel",
                    request_id=request_id,
                )

    async def _run_observer_bounded(
        self,
        observer: Callable[..., Awaitable[None]],
        *args: object,
        observer_name: str,
        request_id: str,
    ) -> None:
        """Ejecuta un observer del lifecycle sin esperar UI indefinidamente."""
        try:
            await asyncio.wait_for(observer(*args), timeout=self._observer_timeout)
        except TimeoutError:
            logger.warning(
                "HITL lifecycle observer timed out: %s for %s",
                observer_name,
                request_id,
            )
        except asyncio.CancelledError:
            # Una cancelación externa o adicional debe seguir visible para el
            # caller; la cancelación propia del observer queda aislada como fallo
            # best-effort.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            logger.warning(
                "HITL lifecycle observer cancelled: %s for %s",
                observer_name,
                request_id,
            )
        except Exception:
            logger.exception(
                "HITL lifecycle observer failed: %s for %s",
                observer_name,
                request_id,
            )

    async def _commit(self, request_id: str, decision: Decision) -> bool:
        """F6: commitea una decisión terminal de forma atómica (primer escritor gana).

        Bajo el lock del guard: si la request sigue pendiente y no fue resuelta,
        fija ``decision``, marca ``_resolved`` y despierta al waiter. Devuelve
        ``True`` si ESTA llamada resolvió la request, ``False`` si ya estaba
        resuelta o ausente. Es el único punto donde se escribe ``decision``, así
        que el timeout y ``respond`` no pueden pisarse entre sí.
        """
        async with self._lock:
            req = self._pending.get(request_id)
            if req is None or req._resolved:
                return False
            req.decision = decision
            req._resolved = True
            req._event.set()

        if self._on_terminal is not None:
            await self._run_observer_bounded(
                self._on_terminal,
                req,
                decision,
                observer_name="on_terminal",
                request_id=request_id,
            )
        return True

    async def terminal_decision(self, request_id: str) -> Decision | None:
        """Devuelve una decisión ya comprometida para sincronizar integraciones."""
        async with self._lock:
            req = self._pending.get(request_id)
            if req is None or not req._resolved:
                return None
            return req.decision

    async def respond(self, request_id: str, approved: bool) -> bool:
        """Deliver the operator's decision for *request_id*.

        Returns ``True`` if this call committed the decision (the request was
        still pending and unresolved), ``False`` otherwise — including when a
        timeout already auto-denied it, so a late click never gets a false
        "approved" ack (F6).
        """
        return await self._commit(request_id, Decision.APPROVED if approved else Decision.DENIED)
