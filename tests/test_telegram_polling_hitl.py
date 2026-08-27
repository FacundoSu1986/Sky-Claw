"""Pruebas de transporte de Telegram y lifecycle de HITL por polling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.comms.telegram import TelegramWebhook
from sky_claw.app.comms.telegram_polling import TelegramPolling
from sky_claw.app.security.hitl import Decision, HITLGuard


async def _esperar_notificacion(notificacion_enviada: asyncio.Event) -> None:
    await asyncio.wait_for(notificacion_enviada.wait(), timeout=1)


def _crear_update_callback(
    update_id: int,
    accion: str,
    request_id: str,
    *,
    user_id: int = 123,
    chat_id: int = 123,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}, "message_id": 77},
            "data": f"hitl:{accion}:{request_id}",
        },
    }


def _crear_transporte(hitl: HITLGuard) -> tuple[TelegramPolling, MagicMock, MagicMock]:
    router = MagicMock()
    sender = MagicMock()
    sender.answer_callback_query = AsyncMock()
    sender.edit_message = AsyncMock()
    sender.send = AsyncMock()
    session = MagicMock(spec=aiohttp.ClientSession)
    webhook = TelegramWebhook(
        router=router,
        sender=sender,
        session=session,
        hitl=hitl,
        authorized_user_id=123,
    )
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=webhook,
        gateway=MagicMock(),
        session=session,
        interval=0,
        authorized_chat_id=123,
    )
    return polling, sender, session


@pytest.mark.asyncio
async def test_callback_approve_por_polling_resuelve_hitl_pendiente() -> None:
    notificacion_enviada = asyncio.Event()

    async def notificar(_request) -> None:
        notificacion_enviada.set()

    hitl = HITLGuard(notify_fn=notificar, timeout=1)
    polling, sender, _session = _crear_transporte(hitl)
    pendiente = asyncio.create_task(hitl.request_approval(request_id="req-approve"))

    await _esperar_notificacion(notificacion_enviada)
    await polling._process_raw_update(_crear_update_callback(1, "approve", "req-approve"))

    assert await pendiente is Decision.APPROVED
    sender.answer_callback_query.assert_awaited_once_with("callback-1", text=None)
    sender.edit_message.assert_awaited_once_with(
        123,
        77,
        "Request 'req-approve' approved by operator.",
        reply_markup=None,
    )


@pytest.mark.asyncio
async def test_callback_deny_por_polling_resuelve_hitl_pendiente() -> None:
    notificacion_enviada = asyncio.Event()

    async def notificar(_request) -> None:
        notificacion_enviada.set()

    hitl = HITLGuard(notify_fn=notificar, timeout=5)
    polling, sender, _session = _crear_transporte(hitl)
    pendiente = asyncio.create_task(hitl.request_approval(request_id="req-deny"))

    await _esperar_notificacion(notificacion_enviada)
    await polling._process_raw_update(_crear_update_callback(2, "deny", "req-deny"))

    sender.answer_callback_query.assert_awaited_once_with("callback-2", text=None)
    assert await pendiente is Decision.DENIED
    sender.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_accion_callback_desconocida_por_polling_no_resuelve_hitl() -> None:
    notificacion_enviada = asyncio.Event()

    async def notificar(_request) -> None:
        notificacion_enviada.set()

    hitl = HITLGuard(notify_fn=notificar, timeout=1)
    polling, sender, _session = _crear_transporte(hitl)
    pendiente = asyncio.create_task(hitl.request_approval(request_id="req-unknown"))

    await _esperar_notificacion(notificacion_enviada)
    await polling._process_raw_update(_crear_update_callback(3, "escalate", "req-unknown"))

    assert not pendiente.done()
    sender.answer_callback_query.assert_awaited_once_with("callback-3", text="Invalid HITL action")
    pendiente.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pendiente


@pytest.mark.asyncio
async def test_callback_no_autorizado_por_polling_no_resuelve_hitl() -> None:
    notificacion_enviada = asyncio.Event()

    async def notificar(_request) -> None:
        notificacion_enviada.set()

    hitl = HITLGuard(notify_fn=notificar, timeout=1)
    polling, sender, _session = _crear_transporte(hitl)
    pendiente = asyncio.create_task(hitl.request_approval(request_id="req-unauthorized"))

    await _esperar_notificacion(notificacion_enviada)
    await polling._process_raw_update(
        _crear_update_callback(4, "approve", "req-unauthorized", user_id=999, chat_id=999)
    )

    assert not pendiente.done()
    # El polling rechaza chats no autorizados antes de entregar el update al dispatcher.
    sender.answer_callback_query.assert_not_awaited()
    pendiente.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pendiente


@pytest.mark.asyncio
async def test_callback_duplicado_por_polling_solo_tiene_un_efecto() -> None:
    notificacion_enviada = asyncio.Event()

    async def notificar(_request) -> None:
        notificacion_enviada.set()

    hitl = HITLGuard(notify_fn=notificar, timeout=1)
    polling, sender, _session = _crear_transporte(hitl)
    pendiente = asyncio.create_task(hitl.request_approval(request_id="req-duplicate"))
    update = _crear_update_callback(5, "approve", "req-duplicate")

    await _esperar_notificacion(notificacion_enviada)
    await polling._process_raw_update(update)
    await polling._process_raw_update(update)

    assert await pendiente is Decision.APPROVED
    sender.edit_message.assert_awaited_once()
    sender.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_no_permite_aprobacion_tardia() -> None:
    notificacion_enviada = asyncio.Event()

    async def notificar(_request) -> None:
        notificacion_enviada.set()

    hitl = HITLGuard(notify_fn=notificar, timeout=0.01)
    polling, sender, _session = _crear_transporte(hitl)
    pendiente = asyncio.create_task(hitl.request_approval(request_id="req-timeout"))

    await _esperar_notificacion(notificacion_enviada)
    assert await pendiente is Decision.TIMEOUT

    await polling._process_raw_update(_crear_update_callback(6, "approve", "req-timeout"))

    sender.edit_message.assert_not_awaited()
    sender.send.assert_awaited_once_with(123, "Error: Request 'req-timeout' not found or already processed.")


@pytest.mark.asyncio
async def test_polling_preserva_la_sesion_inyectada() -> None:
    hitl = HITLGuard()
    polling, _sender, session = _crear_transporte(hitl)
    polling._running = True

    async def detener_despues_del_poll() -> None:
        polling._running = False

    polling._poll_once = AsyncMock(side_effect=detener_despues_del_poll)
    await polling._run_loop()

    assert polling._session is session
    assert polling._owns_session is False
    session.close.assert_not_called()


@pytest.mark.asyncio
async def test_polling_cierra_la_sesion_propietaria() -> None:
    handler = MagicMock()
    gateway = MagicMock()
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=handler,
        gateway=gateway,
        session=None,
        interval=0,
        authorized_chat_id=123,
    )
    sesion_propietaria = MagicMock(spec=aiohttp.ClientSession)
    sesion_propietaria.closed = False
    sesion_propietaria.close = AsyncMock()
    polling._running = True

    async def detener_despues_del_poll() -> None:
        polling._running = False

    polling._poll_once = AsyncMock(side_effect=detener_despues_del_poll)
    with (
        patch("sky_claw.app.comms.telegram_polling.aiohttp.ClientSession", return_value=sesion_propietaria),
        patch("sky_claw.app.security.network_gateway.GatewayTCPConnector", return_value=MagicMock()),
    ):
        await polling._run_loop()

    sesion_propietaria.close.assert_awaited_once()
    assert polling._session is None
    assert polling._owns_session is False


@pytest.mark.asyncio
async def test_stop_espera_la_tarea_y_es_idempotente() -> None:
    limpieza_completa = asyncio.Event()
    tarea_iniciada = asyncio.Event()

    async def tarea_de_polling() -> None:
        tarea_iniciada.set()
        try:
            await asyncio.Event().wait()
        finally:
            limpieza_completa.set()

    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=MagicMock(spec=aiohttp.ClientSession),
        authorized_chat_id=123,
    )
    polling._running = True
    polling._polling_task = asyncio.create_task(tarea_de_polling())
    await tarea_iniciada.wait()

    await polling.stop()
    await polling.stop()

    assert limpieza_completa.is_set()
    assert polling._polling_task is None
    assert polling._running is False


@pytest.mark.asyncio
async def test_stop_propaga_fallo_inesperado_y_limpia_la_referencia() -> None:
    async def tarea_fallida() -> None:
        raise RuntimeError("polling failed")

    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=MagicMock(spec=aiohttp.ClientSession),
        authorized_chat_id=123,
    )
    polling._running = True
    polling._polling_task = asyncio.create_task(tarea_fallida())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="polling failed"):
        await polling.stop()

    assert polling._polling_task is None
    assert polling._running is False


@pytest.mark.asyncio
async def test_stop_desde_la_propia_tarea_no_se_autoespera() -> None:
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=MagicMock(spec=aiohttp.ClientSession),
        authorized_chat_id=123,
    )
    tarea_actual = asyncio.current_task()
    assert tarea_actual is not None
    polling._running = True
    polling._polling_task = tarea_actual

    await polling.stop()

    assert polling._running is False
    assert polling._polling_task is tarea_actual


@pytest.mark.asyncio
async def test_cancelar_stop_repropaga_cancelled_error_y_recolecta_la_tarea() -> None:
    limpieza_completa = asyncio.Event()
    tarea_iniciada = asyncio.Event()

    async def tarea_de_polling() -> None:
        tarea_iniciada.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            limpieza_completa.set()

    sesion_inyectada = MagicMock(spec=aiohttp.ClientSession)
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=sesion_inyectada,
        authorized_chat_id=123,
    )
    polling._running = True
    polling._polling_task = asyncio.create_task(tarea_de_polling())
    await tarea_iniciada.wait()

    tarea_stop = asyncio.create_task(polling.stop())
    await asyncio.sleep(0)
    tarea_stop.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea_stop

    assert limpieza_completa.is_set()
    assert polling._polling_task is None
    assert polling._running is False
    sesion_inyectada.close.assert_not_called()


@pytest.mark.asyncio
async def test_start_no_crea_segunda_tarea_durante_self_stop() -> None:
    primera_tarea_iniciada = asyncio.Event()
    primera_tarea_puede_terminar = asyncio.Event()
    tareas_observadas: list[asyncio.Task[None]] = []
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=MagicMock(spec=aiohttp.ClientSession),
        interval=0,
        authorized_chat_id=123,
    )

    async def poll_once() -> None:
        tarea_actual = asyncio.current_task()
        assert tarea_actual is not None
        tareas_observadas.append(tarea_actual)
        if len(tareas_observadas) == 1:
            primera_tarea_iniciada.set()
            await polling.stop()
            await primera_tarea_puede_terminar.wait()
            polling._running = False
        else:
            polling._running = False

    polling._poll_once = AsyncMock(side_effect=poll_once)
    await polling.start()
    await primera_tarea_iniciada.wait()
    primera_tarea = polling._polling_task
    assert primera_tarea is not None

    await polling.start()

    assert polling._polling_task is primera_tarea
    assert not primera_tarea.done()
    assert tareas_observadas == [primera_tarea]

    primera_tarea_puede_terminar.set()
    await primera_tarea
    await asyncio.sleep(0)

    assert polling._polling_task is None
    assert polling._running is False


@pytest.mark.asyncio
async def test_restart_es_posible_despues_de_terminar_task_anterior() -> None:
    primera_tarea_terminada = asyncio.Event()
    segunda_tarea_iniciada = asyncio.Event()
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=MagicMock(spec=aiohttp.ClientSession),
        interval=0,
        authorized_chat_id=123,
    )
    llamadas = 0

    async def poll_once() -> None:
        nonlocal llamadas
        llamadas += 1
        polling._running = False
        if llamadas == 1:
            primera_tarea_terminada.set()
        else:
            segunda_tarea_iniciada.set()

    polling._poll_once = AsyncMock(side_effect=poll_once)
    await polling.start()
    await primera_tarea_terminada.wait()
    primera_tarea = polling._polling_task
    assert primera_tarea is not None
    await primera_tarea
    await asyncio.sleep(0)

    await polling.start()
    segunda_tarea = polling._polling_task
    assert segunda_tarea is not None
    assert segunda_tarea is not primera_tarea
    await segunda_tarea
    await asyncio.sleep(0)

    assert segunda_tarea_iniciada.is_set()
    assert polling._polling_task is None


@pytest.mark.asyncio
async def test_start_sin_gateway_falla_antes_de_publicar_estado_activo() -> None:
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=None,
        session=None,
        interval=0,
        authorized_chat_id=123,
    )

    with pytest.raises(RuntimeError, match="NetworkGateway"):
        await polling.start()

    assert polling._running is False
    assert polling._polling_task is None


@pytest.mark.asyncio
async def test_polling_falla_sin_gateway_y_limpia_el_estado_interno() -> None:
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=None,
        session=None,
        interval=0,
        authorized_chat_id=123,
    )
    polling._running = True

    with pytest.raises(RuntimeError, match="NetworkGateway"):
        await polling._run_loop()

    assert polling._running is False
    assert polling._session is None
    assert polling._owns_session is False


@pytest.mark.asyncio
async def test_fallo_durante_polling_cierra_la_sesion_propietaria() -> None:
    polling = TelegramPolling(
        token="123:ABC",
        webhook_handler=MagicMock(),
        gateway=MagicMock(),
        session=None,
        interval=0,
        authorized_chat_id=123,
    )
    sesion_propietaria = MagicMock(spec=aiohttp.ClientSession)
    sesion_propietaria.closed = False
    sesion_propietaria.close = AsyncMock()

    async def fallar_y_detener() -> None:
        polling._running = False
        raise RuntimeError("poll once failed")

    polling._running = True
    polling._poll_once = AsyncMock(side_effect=fallar_y_detener)
    with (
        patch("sky_claw.app.comms.telegram_polling.aiohttp.ClientSession", return_value=sesion_propietaria),
        patch("sky_claw.app.security.network_gateway.GatewayTCPConnector", return_value=MagicMock()),
    ):
        await polling._run_loop()

    sesion_propietaria.close.assert_awaited_once()
    assert polling._session is None
    assert polling._running is False
