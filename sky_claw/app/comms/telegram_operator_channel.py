"""Adaptador Telegram del canal de operador (puerto ``CanalDeOperador``).

El notificador de DynDOLOD (``app/orchestrator/dyndolod_operator_notifier.py``)
no conoce Telegram: habla con un puerto de dos métodos (``avisar`` y
``adjuntar``). Este adaptador lo traduce a :class:`TelegramSender`.

**Sender y chat se resuelven en CADA envío**, no al construir. ``AppContext``
publica ``sender`` y ``operator_chat_id`` al final del arranque y los limpia al
cerrar: un adaptador que los capturara al construirse podría hablarle a un sender
ya cerrado, o quedarse sin canal para siempre si se construyó antes de la
publicación.

**Sin canal es un no-op, no un error.** A diferencia del HITL —fail-CLOSED: sin
canal de operador deniega la solicitud—, una notificación informativa no decide
nada: no hay acción que proteger y un aviso nunca puede gatear una corrida. La
ausencia se declara UNA vez (no en cada notificación) para no inundar el log.

Los errores de transporte (``TelegramSendError``, ``TelegramRateLimitError``, red)
se PROPAGAN: quien notifica es el que sabe qué hacer con ellos (registrarlos y
seguir). El sender ya reintentó el flood-control; reintentar acá lo duplicaría.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.app.comms.telegram_sender import TelegramSender

logger = logging.getLogger(__name__)


class TelegramCanalDeOperador:
    """Entrega avisos y adjuntos al chat privado del operador por Telegram.

    Args:
        sender: proveedor del ``TelegramSender`` vigente (``None`` si no hay).
        chat_id: proveedor del chat del operador (``None`` si no está configurado).
    """

    def __init__(
        self,
        *,
        sender: Callable[[], TelegramSender | None],
        chat_id: Callable[[], int | None],
    ) -> None:
        self._sender = sender
        self._chat_id = chat_id
        self._sin_canal_declarado = False

    def _destino(self) -> tuple[TelegramSender, int] | None:
        sender = self._sender()
        chat_id = self._chat_id()
        if sender is None or chat_id is None:
            if not self._sin_canal_declarado:
                self._sin_canal_declarado = True
                logger.info(
                    "DynDOLOD: sin canal de operador (Telegram sin configurar o ya cerrado): "
                    "las notificaciones no se entregan."
                )
            return None
        return sender, chat_id

    async def avisar(self, texto_html: str) -> None:
        """Envía ``texto_html`` (ya escapado por el notificador) al operador."""
        destino = self._destino()
        if destino is None:
            return
        sender, chat_id = destino
        await sender.send(chat_id, texto_html, parse_mode="HTML")

    async def adjuntar(self, *, nombre: str, contenido: bytes, descripcion: str) -> None:
        """Envía ``contenido`` como documento. ``descripcion`` va como texto plano."""
        destino = self._destino()
        if destino is None:
            return
        sender, chat_id = destino
        await sender.send_document(chat_id, contenido, nombre, caption=descripcion)
