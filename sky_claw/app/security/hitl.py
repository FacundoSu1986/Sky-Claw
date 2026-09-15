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

#: Categoría de la confirmación MID-RUN de configuración de DynDOLOD/TexGen
#: (T5-v2). Semánticamente NO es "permito ejecutar una tool": es "terminé de
#: configurar a mano esta GUI y su Output está listo para la verificación final".
#:
#: Por eso NO se reutiliza ``"tool_execution"``: esa categoría se auto-aprueba en
#: Modo local (``make_gui_hitl_notify``), y auto-aprobar ésta haría que el
#: operador nunca confirme lo que la categoría significa — el gate final
#: verificaría una configuración que nadie declaró terminada. Mismo trato que
#: ``download`` (egress) y ``sandbox_promotion`` (post-run): siempre manual.
CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA = "dyndolod_configuracion_lista"


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
        notice_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._notify = notify_fn
        self._timeout = timeout
        self._hosts = out_of_scope_hosts or OUT_OF_SCOPE_HOSTS
        if observer_timeout <= 0:
            raise ValueError("observer_timeout must be positive")
        self._on_terminal = on_terminal
        self._on_cancel = on_cancel
        self._observer_timeout = float(observer_timeout)
        #: Superficie de AVISOS informativos (no decisiones): el reader que ya
        #: presenta los prompts puede recibir mensajes de una línea sin crear un
        #: pendiente. Se asigna en el wiring (`_bootloader` para la GUI; Telegram
        #: puede dejarlo en None) y `notify_operator` lo trata como best-effort.
        self.notice_fn = notice_fn
        self._pending: dict[str, HITLRequest] = {}
        self._lock = asyncio.Lock()

    async def notify_operator(self, mensaje: str) -> None:
        """Entrega un aviso informativo por la superficie del guard, si hay una.

        **No es una decisión**: no crea pendiente, no espera respuesta y no
        participa del ciclo request/respond. Best-effort declarado: un fallo de
        la superficie se loguea y NUNCA se propaga — un aviso no puede gatear ni
        tumbar la corrida que lo emite.
        """
        if self.notice_fn is None:
            return
        try:
            await self.notice_fn(mensaje)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- aviso informativo, nunca gatea
            logger.warning("HITL: notice_fn failed", exc_info=True)

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

        Devuelve la :class:`Decision` del operador. Según la política fail-secure,
        si no llega respuesta durante el timeout se devuelve ``Decision.TIMEOUT``
        y nunca cuenta como aprobación. Un ``notify_fn`` fallido también produce
        ``Decision.TIMEOUT`` sin crear una entrega ficticia.

        *timeout* es un override ESTRICTAMENTE por solicitud (T5-v2): el timeout
        global del guard no cambia y ``None`` conserva exactamente el
        comportamiento previo. Existe porque una espera mid-run —el operador
        configurando a mano la GUI de DynDOLOD/TexGen— tiene una escala distinta
        de la de un prompt de scope, y alargar el global alargaría TODOS los
        prompts del proceso. Sigue siendo fail-secure: agotado el plazo se
        commitea ``TIMEOUT``, nunca una aprobación.

        Un *timeout* EXPLÍCITO no positivo se RECHAZA acá: ``asyncio.wait_for``
        con un plazo no positivo corta de inmediato, así que un cero por error de
        configuración del caller convertiría cada prompt en un ``TIMEOUT``
        instantáneo y el fail-closed taparía el bug en vez de exponerlo. El
        ``timeout`` GLOBAL del constructor no se toca: su ``0`` es un modo
        "corta ya" con semántica establecida (los tests lo usan) y no es parte de
        este override.
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        if timeout is not None:
            effective_timeout = float(timeout)
            if effective_timeout <= 0:
                # Bug de configuración/caller, no del operador: se lanza ANTES
                # de registrar el pendiente para no dejar una entrada colgada.
                raise ValueError(f"HITL timeout debe ser > 0; llegó {effective_timeout}")
        else:
            effective_timeout = self._timeout
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
