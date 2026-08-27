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


class Decision(enum.Enum):
    """Operator decision for a pending HITL prompt."""

    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


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
    """Manages the HITL authorisation flow.

    Parameters
    ----------
    notify_fn:
        Async callable that sends the authorisation prompt to the
        operator (e.g. Telegram message).  Receives a :class:`HITLRequest`
        and should return when the message has been sent.
    timeout:
        Seconds to wait for operator response before committing ``TIMEOUT``.
    out_of_scope_hosts:
        Host patterns that trigger HITL.
    on_terminal:
        Best-effort observer invoked after the first terminal decision wins.
    on_cancel:
        Best-effort observer invoked when a sent request is cancelled before resolution.

    """

    def __init__(
        self,
        notify_fn: Callable[[HITLRequest], Awaitable[None]] | None = None,
        timeout: int = HITL_TIMEOUT_SECONDS,
        out_of_scope_hosts: frozenset[str] | None = None,
        on_terminal: Callable[[HITLRequest, Decision], Awaitable[None]] | None = None,
        on_cancel: Callable[[HITLRequest], Awaitable[None]] | None = None,
    ) -> None:
        self._notify = notify_fn
        self._timeout = timeout
        self._hosts = out_of_scope_hosts or OUT_OF_SCOPE_HOSTS
        self._on_terminal = on_terminal
        self._on_cancel = on_cancel
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
    ) -> Decision:
        """Pause execution and wait for operator authorisation.

        *request_id* is a caller-supplied identifier (e.g. ``"download-10-20"``).
        If not provided, a unique UUID is generated automatically.

        Returns the :class:`Decision` made by the operator. Per the fail-secure
        policy, if no response arrives within the timeout ``Decision.TIMEOUT`` is
        returned and never counts as approval. A failed ``notify_fn`` likewise
        yields ``Decision.TIMEOUT`` without creating a fictitious delivery.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
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
                await asyncio.wait_for(req._event.wait(), timeout=self._timeout)
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
                try:
                    await self._on_cancel(req)
                except Exception:
                    logger.exception("HITL cancellation UI hook failed for %s", request_id)

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
            try:
                await self._on_terminal(req, decision)
            except Exception:
                # La UI es best-effort: nunca puede convertir una decisión ya
                # comprometida en un fallo de seguridad ni reabrir la request.
                logger.exception("HITL terminal UI hook failed for %s", request_id)
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
