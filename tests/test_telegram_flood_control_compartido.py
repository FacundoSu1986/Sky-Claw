"""``TelegramSender``: cooldown COMPARTIDO del flood-control (HTTP 429) — #706.

Diagnóstico de la issue: el reintento de un 429 dormía **sólo** a la corrutina que
falló. Una ``send()`` concurrente pasaba ``_wait_for_rate_limit`` de inmediato (por
contrato, un 429 no registra envío) y volvía a golpear Telegram **dentro de la
misma ventana de flood-control**, así que una ráfaga podía agotar los reintentos
acotados y perder avisos.

Lo que se ancla acá, criterio por criterio de #706:

1. autoridad compartida de cooldown basada en ``time.monotonic``;
2. coordinación del ``retry_after`` entre corrutinas del mismo chat;
3. alcance global del bot (un 429 no distingue el límite del chat del del bot: se
   converge al mayor);
4. consulta antes de CADA intento HTTP, no sólo antes del primer mensaje;
5. un 429 nunca se registra ni se devuelve como mensaje entregado;
6. esperas acotadas, cancelables y sin locks sostenidos durante una espera;
7. límites de reintento y backpressure intactos;
11. tests deterministas con dos o más envíos concurrentes.

El tiempo es SIMULADO: el reloj del módulo y ``asyncio.sleep`` se reemplazan por un
reloj falso que avanza exactamente lo que el código pidió dormir. Ningún test mide
tiempo real ni depende del orden de scheduling salvo donde se sincroniza de forma
explícita.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.comms import telegram_sender as sender_mod
from sky_claw.app.comms.telegram import (
    TelegramHITLMessageRegistry,
    TelegramWebhook,
    build_hitl_callback_data,
)
from sky_claw.app.comms.telegram_sender import (
    TelegramMessage,
    TelegramRateLimitError,
    TelegramSender,
)

_TOKEN = "123:ABC"


# ---------------------------------------------------------------------------
# Harness: reloj falso, sleeps espiados y gateway simulado
# ---------------------------------------------------------------------------


class _RelojFalso:
    """Reloj ``monotonic`` simulado: avanza sólo cuando el código duerme."""

    def __init__(self) -> None:
        self.ahora = 0.0

    def monotonic(self) -> float:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += segundos


class _Tiempo:
    """Tiempo simulado del sender: reloj propio + esperas observadas.

    No se parchea el módulo ``time`` real (el event loop lo usa para sus propios
    timers) ni se escribe un ``sleep`` de mentira que no avance el reloj: eso
    dejaría al cooldown esperando para siempre o haría leer la espera como vencida.
    El sueño simulado cede el control ANTES de avanzar —de modo que otra corrutina
    puede observar la espera en curso— y al reanudar lleva el reloj a
    ``inicio + segundos``, nunca hacia atrás: dos esperas concurrentes que arrancan
    en el mismo instante despiertan en el mismo instante.
    """

    def __init__(self) -> None:
        self.reloj = _RelojFalso()
        self.dormidos: list[float] = []
        self.llamadas: list[tuple[int | None, float]] = []
        self._al_dormir: list[Callable[[float], None]] = []
        self._sleep_real = asyncio.sleep

    def al_dormir(self, hook: Callable[[float], None]) -> None:
        """Registra un hook que corre mientras una corrutina está durmiendo."""
        self._al_dormir.append(hook)

    async def dormir(self, segundos: float) -> None:
        self.dormidos.append(segundos)
        inicio = self.reloj.ahora
        await self._sleep_real(0)
        self.reloj.ahora = max(self.reloj.ahora, inicio + segundos)
        for hook in list(self._al_dormir):
            hook(segundos)

    async def ceder(self) -> None:
        """Cede el control SIN contar como espera del sender."""
        await self._sleep_real(0)

    async def hasta(self, condicion: Callable[[], bool], *, limite: int = 5000) -> None:
        """Espera determinista a una condición, cediendo el control."""
        for _ in range(limite):
            if condicion():
                return
            await self.ceder()
        raise AssertionError("la condición de sincronización no se cumplió")


@pytest.fixture
def tiempo(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Tiempo]:
    """Reloj falso + ``asyncio.sleep`` espiado del módulo del sender."""
    t = _Tiempo()
    monkeypatch.setattr(sender_mod, "time", t.reloj)
    with patch.object(sender_mod.asyncio, "sleep", t.dormir):
        yield t


def _respuesta(status: int, cuerpo: dict[str, Any] | str, headers: dict[str, str] | None = None) -> AsyncMock:
    """Respuesta falsa de ``gateway.request`` (soporta ``async with``)."""
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {}
    texto = cuerpo if isinstance(cuerpo, str) else json.dumps(cuerpo)
    resp.text = AsyncMock(return_value=texto)
    resp.json = AsyncMock(return_value=json.loads(texto) if not isinstance(cuerpo, str) else {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _ok(message_id: int = 7) -> AsyncMock:
    return _respuesta(200, {"ok": True, "result": {"message_id": message_id}})


def _429(retry_after: int | None = 2) -> AsyncMock:
    cuerpo: dict[str, Any] = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 2",
    }
    if retry_after is not None:
        cuerpo["parameters"] = {"retry_after": retry_after}
    return _respuesta(429, cuerpo)


class _GatewaySimulado:
    """Gateway que registra ``(chat, instante simulado)`` por intento.

    ``respuestas`` es una cola fija (un elemento por intento esperado) y
    ``respondedor`` permite decidir por instante simulado, para las ráfagas donde el
    número de intentos depende del interleaving.
    """

    def __init__(self, tiempo: _Tiempo, respuestas: list[AsyncMock], respondedor: Callable[[float], AsyncMock] | None):
        self._tiempo = tiempo
        self._pendientes = collections.deque(respuestas)
        self._respondedor = respondedor
        self.siguiente_id = 1
        mock = MagicMock()

        async def _request(method: str, url: str, session: Any, **kwargs: Any) -> Any:
            peticion = kwargs.get("json", {})
            self._tiempo.llamadas.append((peticion.get("chat_id"), self._tiempo.reloj.ahora))
            if self._respondedor is not None:
                return self._respondedor(self._tiempo.reloj.ahora)
            if not self._pendientes:
                raise AssertionError("el sender hizo más intentos HTTP de los esperados")
            return self._pendientes.popleft()

        mock.request = AsyncMock(side_effect=_request)
        self.mock = mock


def _sender(tiempo: _Tiempo, *respuestas: AsyncMock) -> tuple[TelegramSender, MagicMock]:
    gateway = _GatewaySimulado(tiempo, list(respuestas), None)
    sender = TelegramSender(bot_token=_TOKEN, gateway=gateway.mock, session=MagicMock(spec=aiohttp.ClientSession))
    return sender, gateway.mock


def _sender_limitado_por_tiempo(tiempo: _Tiempo, *, hasta: float) -> tuple[TelegramSender, MagicMock]:
    """Gateway que responde 429 mientras el instante simulado sea ``< hasta``."""
    gateway = _GatewaySimulado(
        tiempo,
        [],
        lambda ahora: _429(retry_after=4) if ahora < hasta else _ok(),
    )
    sender = TelegramSender(bot_token=_TOKEN, gateway=gateway.mock, session=MagicMock(spec=aiohttp.ClientSession))
    return sender, gateway.mock


def _tiempos(tiempo: _Tiempo) -> list[float]:
    return [instante for _chat, instante in tiempo.llamadas]


# ---------------------------------------------------------------------------
# Criterios 1/3/6/9 — la autoridad compartida del cooldown
# ---------------------------------------------------------------------------


class TestAutoridadDeCooldown:
    """``CooldownCompartido``: deadline monotónico, acotado y sin locks."""

    def test_sin_aviso_publicado_no_hay_espera(self, tiempo: _Tiempo) -> None:
        # Nadie publicó un 429: la consulta no puede inventar una espera.
        cooldown = sender_mod.CooldownCompartido()

        assert cooldown.restante() == 0.0

    def test_lo_publicado_vale_para_todo_el_bot(self, tiempo: _Tiempo) -> None:
        # Criterio 3: el 429 no distingue el límite del chat del límite global del bot,
        # así que el aviso tiene alcance de BOT — lo honra cualquier chat — y no existe
        # una segunda fuente de verdad (un deadline por chat) que el global, por ser el
        # máximo de todos los avisos, fuera a dominar siempre.
        cooldown = sender_mod.CooldownCompartido()

        cooldown.publicar(5.0)

        assert cooldown.restante() == 5.0

    def test_el_deadline_no_retrocede_con_una_publicacion_menor(self, tiempo: _Tiempo) -> None:
        # Criterio 9: la actualización es un ``max``, así que un aviso más corto que
        # llega después NO acorta la espera ya anunciada a las demás corrutinas.
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(5.0)

        tiempo.reloj.avanzar(1.0)
        cooldown.publicar(1.0)

        assert cooldown.restante() == pytest.approx(4.0), "el deadline de 5.0s sigue vigente"

    def test_un_aviso_mas_largo_extiende_el_deadline(self, tiempo: _Tiempo) -> None:
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(2.0)

        tiempo.reloj.avanzar(1.0)
        cooldown.publicar(5.0)

        assert cooldown.restante() == pytest.approx(5.0)

    @pytest.mark.parametrize("segundos", [60.0, 600.0])
    def test_un_retry_after_largo_se_retiene_completo_en_autoridad(self, tiempo: _Tiempo, segundos: float) -> None:
        # La autoridad compartida retiene el deadline real devuelto por Telegram:
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(segundos)
        assert cooldown.restante() == pytest.approx(segundos)

    @pytest.mark.parametrize("absurdo", [100000.0, 999999.0])
    def test_un_retry_after_mayor_a_sanidad_se_acota_a_24h(self, tiempo: _Tiempo, absurdo: float) -> None:
        # Criterio de sanidad: valores absurdos se acotan al tope de 24 horas:
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(absurdo)
        assert cooldown.restante() == pytest.approx(sender_mod._SANIDAD_MAX_COOLDOWN_SEGUNDOS)
        assert cooldown.restante() <= sender_mod._SANIDAD_MAX_COOLDOWN_SEGUNDOS

    async def test_sin_aviso_esperar_no_duerme(self, tiempo: _Tiempo) -> None:
        cooldown = sender_mod.CooldownCompartido()

        await cooldown.esperar()

        assert tiempo.dormidos == []

    async def test_esperar_duerme_hasta_el_deadline(self, tiempo: _Tiempo) -> None:
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(3.0)

        await cooldown.esperar()

        assert tiempo.dormidos == [3.0], "una sola espera por el mismo aviso"
        assert tiempo.reloj.ahora == pytest.approx(3.0)
        assert cooldown.restante() == 0.0, "el aviso vencido no vuelve a esperar"

    async def test_una_publicacion_durante_la_espera_se_honra_en_la_misma_consulta(self, tiempo: _Tiempo) -> None:
        # Criterio 2: si otra corrutina publica un aviso MÁS LEJANO mientras esta
        # duerme, la espera en curso se extiende — no se despierta y golpea la red
        # dentro de la ventana nueva. Acotado: una extensión por consulta.
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(3.0)
        ya_extension = [False]

        def _extender(_segundos: float) -> None:
            if not ya_extension[0]:
                ya_extension[0] = True
                cooldown.publicar(5.0)

        tiempo.al_dormir(_extender)

        await cooldown.esperar()

        assert tiempo.dormidos == [3.0, 5.0], "la extensión se espera una sola vez"
        assert tiempo.reloj.ahora == pytest.approx(8.0)

    async def test_tres_extensiones_progresivas_intercaladas_reevaluan_deadline_completo(self, tiempo: _Tiempo) -> None:
        """A: CooldownCompartido.esperar() no debe detenerse tras dos sleeps si el

        deadline fue extendido una tercera vez; debe reevaluar en bucle hasta vencimiento.
        """
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(3.0)  # t=0, deadline=3.0
        extensiones = 0

        def _extender(_segundos: float) -> None:
            nonlocal extensiones
            if extensiones == 0:
                extensiones = 1
                cooldown.publicar(4.0)  # t=3, deadline=7.0
            elif extensiones == 1:
                extensiones = 2
                cooldown.publicar(5.0)  # t=7, deadline=12.0

        tiempo.al_dormir(_extender)

        await cooldown.esperar()

        assert tiempo.dormidos == [3.0, 4.0, 5.0]
        assert tiempo.reloj.ahora == pytest.approx(12.0)
        assert cooldown.restante() == 0.0

    async def test_la_espera_es_cancelable(self, tiempo: _Tiempo) -> None:
        # Criterio 7: la cancelación se propaga; el ``asyncio.sleep`` no la traga.
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(5.0)

        tarea = asyncio.create_task(cooldown.esperar())
        await tiempo.hasta(lambda: tiempo.dormidos == [5.0])
        tarea.cancel()

        with pytest.raises(asyncio.CancelledError):
            await tarea


# ---------------------------------------------------------------------------
# Criterios 2/3/11 — dos envíos concurrentes
# ---------------------------------------------------------------------------


class TestEnviosConcurrentes:
    """La ventana que abre un 429 frena a las corrutinas que comparten el sender."""

    async def test_un_429_en_un_chat_espera_al_envio_concurrente_del_mismo_chat(self, tiempo: _Tiempo) -> None:
        # Escenario textual de #706: ``send()`` A come un 429 y la ``send()`` B,
        # concurrente, pasaba el rate limit local y volvía a golpear la API dentro
        # de la ventana. Ahora B consulta el cooldown compartido ANTES de intentar.
        sender, gateway = _sender(tiempo, _429(retry_after=5), _ok(1), _ok(2))

        primera = asyncio.create_task(sender.send(456, "primer aviso"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante() > 0)
        segunda = asyncio.create_task(sender.send(456, "segundo aviso"))

        assert await primera == TelegramMessage(chat_id=456, message_id=1)
        assert await segunda == TelegramMessage(chat_id=456, message_id=2)
        assert gateway.request.await_count == 3
        assert _tiempos(tiempo) == [0.0, 5.0, 5.0], "ningún intento HTTP dentro de la ventana de flood-control"

    async def test_un_429_en_un_chat_espera_al_envio_concurrente_de_otro_chat(self, tiempo: _Tiempo) -> None:
        # Criterio 3: el alcance del bot es conservador a propósito — no se puede
        # distinguir desde el 429 si el límite fue el del chat o el del bot, y golpear
        # la API dentro de la ventana anunciada es más caro que esperar.
        sender, _gateway = _sender(tiempo, _429(retry_after=5), _ok(1), _ok(2))

        primera = asyncio.create_task(sender.send(456, "aviso del chat A"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante() > 0)
        segunda = asyncio.create_task(sender.send(789, "aviso del chat B"))

        await asyncio.gather(primera, segunda)

        assert _tiempos(tiempo) == [0.0, 5.0, 5.0]
        assert [chat for chat, _t in tiempo.llamadas] == [456, 456, 789]

    async def test_una_rafaga_concurrente_converge_en_el_deadline_compartido(self, tiempo: _Tiempo) -> None:
        # Cuatro envíos al mismo chat: los primeros intentos pueden coincidir (todavía
        # no hay aviso que publicar), pero a partir del 429 TODOS los reintentos caen
        # en los deadlines compartidos en vez de repetir cada uno su propio backoff.
        sender, gateway = _sender_limitado_por_tiempo(tiempo, hasta=12.0)

        tareas = [asyncio.create_task(sender.send(456, f"aviso {i}")) for i in range(4)]
        resultados = await asyncio.gather(*tareas)

        assert all(isinstance(m, TelegramMessage) for m in resultados)
        assert gateway.request.await_count <= 4 * (1 + sender_mod._MAX_REINTENTOS_429), "backpressure intacto"
        # Todos los intentos caen en la rejilla de la ventana (0 = antes de que el
        # primer 429 la abriera; 4·k = deadlines compartidos). Sin el cooldown
        # publicado, cada hermana reintentaría dentro de la ventana y la ráfaga
        # terminaría en errores en vez de entregar los cuatro avisos.
        assert all(t % 4.0 == 0.0 for t in _tiempos(tiempo)), "ningún intento cae dentro de la ventana"

    async def test_ninguna_corrutina_llega_al_gateway_antes_del_vencimiento_efectivo_con_tres_extensiones(
        self, tiempo: _Tiempo
    ) -> None:
        """A: Exigir que ninguna corrutina llegue a NetworkGateway.request() antes

        del vencimiento efectivo cuando tres publicaciones intercaladas extienden el cooldown.
        """
        sender, _gw = _sender(tiempo, _ok(1))
        sender._cooldown.publicar(3.0)  # t=0, d=3.0
        extensiones = 0

        def _extender(_segundos: float) -> None:
            nonlocal extensiones
            if extensiones == 0:
                extensiones = 1
                sender._cooldown.publicar(4.0)  # t=3, d=7.0
            elif extensiones == 1:
                extensiones = 2
                sender._cooldown.publicar(5.0)  # t=7, d=12.0

        tiempo.al_dormir(_extender)

        await sender.send(123, "mensaje")

        # Con main (2 sleeps), despierta a t=7.0 y llama al gateway antes del vencimiento efectivo (12.0s):
        assert len(tiempo.llamadas) == 1
        _chat, instante = tiempo.llamadas[0]
        assert instante >= 12.0, f"Llamada prematura al gateway en t={instante}s cuando el deadline efectivo era 12.0s"

    async def test_cancelar_una_espera_no_deja_a_la_otra_corrutina_bloqueada(self, tiempo: _Tiempo) -> None:
        # Criterios 7/10: cancelar una tarea durante la espera compartida ni retiene
        # el aviso ni impide que la hermana complete.
        sender, _gateway = _sender(tiempo, _429(retry_after=5), _ok(9), _ok(10))

        primera = asyncio.create_task(sender.send(456, "se cancela"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante() > 0)
        segunda = asyncio.create_task(sender.send(456, "sobrevive"))
        await tiempo.hasta(lambda: tiempo.dormidos == [5.0])

        primera.cancel()
        with pytest.raises(asyncio.CancelledError):
            await primera
        # La cancelada NO consume el intento aceptado: la hermana se lleva el primer
        # ``ok`` pendiente y completa ella sola.
        assert await segunda == TelegramMessage(chat_id=456, message_id=9)
        assert _tiempos(tiempo) == [0.0, 5.0], "la hermana no golpeó la API dentro de la ventana"


# ---------------------------------------------------------------------------
# Las vías de control son el MISMO recurso: mismo cooldown (#706)
# ---------------------------------------------------------------------------


class TestViasDeControl:
    """``editMessageText`` y ``answerCallbackQuery`` también participan del cooldown.

    Son el hermano del camino de envío en el mismo archivo: si no consultaran la
    ventana publicada, una edición de HITL o el cierre de un botón golpearían la API
    dentro del flood-control anunciado (y una edición perdida deja botones viejos en
    el prompt del operador).
    """

    async def test_edit_message_espera_la_ventana_publicada_por_un_envio(self, tiempo: _Tiempo) -> None:
        sender, gateway = _sender(tiempo, _429(retry_after=5), _ok(7), _ok(7))

        envio = asyncio.create_task(sender.send(456, "aviso"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante() > 0)
        await sender.edit_message(456, 1, "editado")

        assert await envio == TelegramMessage(chat_id=456, message_id=7)
        assert gateway.request.await_count == 3
        assert _tiempos(tiempo) == [0.0, 5.0, 5.0], "la edición no puede salir dentro de la ventana"

    async def test_un_429_de_edit_message_publica_la_ventana_para_las_demas_corrutinas(self, tiempo: _Tiempo) -> None:
        # Una vía de control no reintenta, pero SÍ publica el aviso que recibe: si no,
        # sus hermanas seguirían golpeando la misma ventana.
        sender, _gateway = _sender(tiempo, _429(retry_after=5), _ok(7))

        await sender.edit_message(456, 1, "editado")
        assert sender._cooldown.restante() > 0, "el 429 de la edición debe publicarse"

        assert await sender.send(456, "aviso") == TelegramMessage(chat_id=456, message_id=7)
        assert _tiempos(tiempo) == [0.0, 5.0]

    async def test_answer_callback_query_consulta_el_alcance_del_bot(self, tiempo: _Tiempo) -> None:
        # ``answerCallbackQuery`` no conoce el chat del callback: participa del
        # cooldown del bot, que es el único alcance que el 429 permite atribuir.
        sender, _gateway = _sender(tiempo, _429(retry_after=5), _ok(7), _ok(7))

        envio = asyncio.create_task(sender.send(456, "aviso"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante() > 0)
        await sender.answer_callback_query("cb-1")

        assert await envio == TelegramMessage(chat_id=456, message_id=7)
        assert _tiempos(tiempo) == [0.0, 5.0, 5.0]

    async def test_un_429_de_answer_callback_query_se_publica_para_el_bot(self, tiempo: _Tiempo) -> None:
        sender, _gateway = _sender(tiempo, _429(retry_after=5), _ok(7))

        await sender.answer_callback_query("cb-1")

        assert sender._cooldown.restante() > 0
        assert await sender.send(456, "aviso") == TelegramMessage(chat_id=456, message_id=7)
        assert _tiempos(tiempo) == [0.0, 5.0]


# ---------------------------------------------------------------------------
# Criterios 5/7/10/12 — ni éxito fabricado ni reintentos ilimitados
# ---------------------------------------------------------------------------


class TestContratoDeFallo:
    async def test_una_rafaga_de_429_agota_los_reintentos_y_no_registra_envios(self, tiempo: _Tiempo) -> None:
        # Criterios 5 y 12: un 429 NUNCA ocupa un slot del rate limit local (no es un
        # mensaje entregado) y nunca se devuelve una identidad de mensaje inventada.
        intentos = sender_mod._MAX_REINTENTOS_429 + 1
        sender, gateway = _sender(tiempo, *[_429(retry_after=1) for _ in range(intentos)])

        with pytest.raises(TelegramRateLimitError) as excinfo:
            await sender.send(456, "no llega")

        assert excinfo.value.retry_after == 1.0
        assert gateway.request.await_count == intentos, "el límite de reintentos sigue acotado"
        assert not sender._send_times.get(456), "un 429 no puede ocupar un slot de envío"

    async def test_dos_envios_concurrentes_limitados_no_entregan_exito_fabricado(self, tiempo: _Tiempo) -> None:
        sender, gateway = _sender_limitado_por_tiempo(tiempo, hasta=float("inf"))

        tareas = [asyncio.create_task(sender.send(456, f"aviso {i}")) for i in range(2)]
        resultados = await asyncio.gather(*tareas, return_exceptions=True)

        assert all(isinstance(r, TelegramRateLimitError) for r in resultados)
        assert gateway.request.await_count == 2 * (1 + sender_mod._MAX_REINTENTOS_429), "cada envío conserva su tope"
        assert not sender._send_times.get(456), "ningún 429 puede registrarse como entrega"

    async def test_un_429_seguido_de_exito_solo_registra_el_envio_aceptado(self, tiempo: _Tiempo) -> None:
        sender, _gateway = _sender(tiempo, _429(retry_after=1), _ok(3))

        resultado = await sender.send(456, "hola")

        assert resultado == TelegramMessage(chat_id=456, message_id=3)
        assert len(sender._send_times[456]) == 1

    async def test_retry_after_600_retiene_deadline_completo_y_falla_rapido_sin_dormir_600s(
        self, tiempo: _Tiempo
    ) -> None:
        """B: retry_after=600 no debe truncarse a 30s en la autoridad compartida; debe

        retener 600s y los callers con presupuesto insuficiente (90s) deben fallar rápido sin colgarse 600s,
        evitando además que solicitudes a los 35s golpeen la red prematuramente.
        """
        sender, gw = _sender(tiempo, _429(retry_after=600))

        with pytest.raises(TelegramRateLimitError) as exc_info:
            await sender.send(123, "primer intento")

        assert exc_info.value.retry_after == 600.0
        assert gw.request.await_count == 1
        # La autoridad retiene los 600s íntegros:
        assert sender._cooldown.restante() == pytest.approx(600.0)

        # En main (donde el cooldown expiró a los 30s), una llamada a los 35s golpeaba la red:
        tiempo.reloj.avanzar(35.0)  # restan 565s > 90s
        with pytest.raises(TelegramRateLimitError):
            await sender.send(123, "intento subsecuente dentro de los 600s")

        # El gateway no debió recibir llamadas a los 35s:
        assert gw.request.await_count == 1

    async def test_mensaje_multichunk_comparte_presupuesto_total_y_reporta_entregas_parciales(
        self, tiempo: _Tiempo
    ) -> None:
        """C: Una operación send() dividida en fragmentos comparte un presupuesto único (90s).

        Si fragmentos iniciales se entregan y un fragmento posterior agota el presupuesto,
        debe fallar con TelegramRateLimitError (sin fabricar éxito) e informar entregas parciales.
        """
        # Mensaje de 3 chunks (>4096 caracteres cada división)
        texto_largo = "A" * 4000 + "\n" + "B" * 4000 + "\n" + "C" * 1000
        # Chunk 1: recibe 429(30s), duerme 30s, reintenta -> éxito (id=101). Consumió 30s.
        # Chunk 2: recibe 429(30s), duerme 30s, reintenta -> éxito (id=102). Consumió 60s acumulados.
        # Chunk 3: recibe 429(20s), duerme 20s -> consumió 80s acumulados.
        # Chunk 3 (intento 2): recibe 429(20s). Como el presupuesto restante es 10s (90s - 80s),
        # 20s excede el presupuesto y debe fallar sin dormir otros 20s ni entregar el tercer chunk.
        respuestas = [
            _429(retry_after=30),
            _ok(101),
            _429(retry_after=30),
            _ok(102),
            _429(retry_after=20),
            _429(retry_after=20),
            _ok(103),  # no debe alcanzarse si el presupuesto es compartido
        ]
        sender, gw = _sender(tiempo, *respuestas)

        with pytest.raises(TelegramRateLimitError) as exc_info:
            await sender.send(123, texto_largo)

        assert exc_info.value.retry_after == 20.0
        # No fabricó éxito: lanzó excepción
        # Y reporta que los chunks 1 y 2 fueron entregados:
        assert exc_info.value.mensajes_entregados == (
            TelegramMessage(chat_id=123, message_id=101),
            TelegramMessage(chat_id=123, message_id=102),
        )

    async def test_send_document_con_cooldown_excediendo_cota_lanza_upload_timeout(self, tiempo: _Tiempo) -> None:
        """D: Si el cooldown activo supera el tiempo remanente de MAX_UPLOAD_TOTAL_SECONDS,

        send_document debe clasificar el fallo como TelegramUploadTimeoutError.
        """
        sender, _ = _sender(tiempo, _ok(99))

        async def _rate_limit_lento(chat_id: int) -> None:
            # Simula que la espera previa consumió casi toda la cota
            tiempo.reloj.avanzar(sender_mod.MAX_UPLOAD_TOTAL_SECONDS - 5.0)
            # Cooldown activo de 10s (mayor a los 5s restantes)
            sender._cooldown.publicar(10.0)

        sender._wait_for_rate_limit = _rate_limit_lento  # type: ignore[assignment]

        with pytest.raises(sender_mod.TelegramUploadTimeoutError) as exc_info:
            await sender.send_document(123, b"datos de prueba", "log.txt")

        assert "cota total" in str(exc_info.value)

    async def test_vias_hitl_tienen_presupuesto_acotado_y_no_se_bloquean_en_cooldown_largo(
        self, tiempo: _Tiempo
    ) -> None:
        """D: edit_message y answer_callback_query usan presupuesto acotado por defecto

        y no se bloquean durante un cooldown largo (ej. 600s).
        """
        sender, gw = _sender(tiempo, _ok(1))
        sender._cooldown.publicar(600.0)

        with pytest.raises(TelegramRateLimitError):
            await sender.edit_message(123, 10, "edit")

        with pytest.raises(TelegramRateLimitError):
            await sender.answer_callback_query("cb_1", text="ack")

        assert gw.request.await_count == 0, "no debieron llamar al gateway durante cooldown que excede presupuesto"

    async def test_answer_callback_query_usa_presupuesto_efectivo_de_10_segundos_y_falla_rapido(
        self, tiempo: _Tiempo
    ) -> None:
        """P2-A: answer_callback_query debe usar un presupuesto efectivo de 10.0s,
        fallando rápido sin esperar ni golpear el gateway cuando el cooldown lo supera,
        pero esperando y completando si el cooldown cabe en los 10.0s.
        """
        sender, gw = _sender(tiempo, _ok(1))
        # 1. Cooldown de 12s supera el presupuesto de 10s -> falla rápido en 0s
        sender._cooldown.publicar(12.0)
        with pytest.raises(TelegramRateLimitError) as exc_info:
            await sender.answer_callback_query("cb_1", text="ack")

        assert exc_info.value.presupuesto_agotado is True
        assert exc_info.value.retry_after == 12.0
        assert gw.request.await_count == 0, "no debe llamar al gateway cuando el cooldown supera el presupuesto"
        assert tiempo.dormidos == [], "debe fallar de inmediato sin dormir"

        # 2. Tras vencer el cooldown anterior, un cooldown de 5s cabe dentro de los 10s -> espera los 5s y ejecuta
        tiempo.reloj.avanzar(12.0)
        sender._cooldown.publicar(5.0)
        await sender.answer_callback_query("cb_2", text="ack2")
        assert gw.request.await_count == 1
        assert 5.0 in tiempo.dormidos

    async def test_callback_hitl_con_cooldown_activo_ejecuta_decision_y_no_genera_segundo_intento(
        self, tiempo: _Tiempo
    ) -> None:
        """P2-A: Escenario HITL completo con cooldown activo:
        1. Publicar cooldown largo antes del ack.
        2. Ejecutar procesamiento del callback.
        3. Decisión del guard se compromete exactamente una vez.
        4. Fallo de ack por cooldown se observa pero no aborta el flujo.
        5. El flujo terminal/fallback continúa sin generar segundo intento HTTP.
        """
        sender, gw = _sender(tiempo, _ok(1))
        # Publicar cooldown de 15s (excede los 10s de callback query)
        sender._cooldown.publicar(15.0)

        hitl = MagicMock()
        hitl.respond = AsyncMock(return_value=True)
        registry = TelegramHITLMessageRegistry()
        token = await registry.reserve_token("req-p2a")
        await registry.register("req-p2a", 123, 9, sender, token=token)

        webhook = TelegramWebhook(
            router=MagicMock(),
            sender=sender,
            session=MagicMock(spec=aiohttp.ClientSession),
            hitl=hitl,
            authorized_user_id=123,
            hitl_registry=registry,
        )

        update = {
            "update_id": 1,
            "callback_query": {
                "id": "cb-hitl-1",
                "from": {"id": 123},
                "message": {"chat": {"id": 123}, "message_id": 9},
                "data": build_hitl_callback_data("approve", token),
            },
        }

        # Procesa el update: no debe fallar ni escapar TelegramRateLimitError
        await webhook.process_update(update)

        # La decisión del guard se comprometió exactamente una vez
        hitl.respond.assert_awaited_once_with("req-p2a", True)
        # El callback intentó confirmar pero falló en CooldownCompartido sin llamar al gateway
        # (cero llamadas de answerCallbackQuery al gateway dentro del cooldown)
        llamadas_cb = [c for c in gw.llamadas if "answerCallbackQuery" in getattr(c, "url", "")]
        assert len(llamadas_cb) == 0, "no debe generar peticiones HTTP de callbackquery dentro del cooldown"

    async def test_send_document_con_nuevo_429_que_excede_presupuesto_restante_pero_menor_al_total_lanza_upload_timeout(
        self, tiempo: _Tiempo
    ) -> None:
        """P2-B Escenario B: Un documento consume parte de su plazo y recibe un HTTP 429 NUEVO
        con retry_after > 30s y > tiempo_restante, pero menor a MAX_UPLOAD_TOTAL_SECONDS.
        Debe fallar específicamente como TelegramUploadTimeoutError.
        """
        sender, gw = _sender(tiempo, _429(retry_after=35))

        async def _rate_limit_lento(chat_id: int) -> None:
            # Simula que trabajo previo consumió casi toda la cota (quedan 20s de presupuesto restante)
            tiempo.reloj.avanzar(sender_mod.MAX_UPLOAD_TOTAL_SECONDS - 20.0)

        sender._wait_for_rate_limit = _rate_limit_lento  # type: ignore[assignment]

        with pytest.raises(sender_mod.TelegramUploadTimeoutError) as exc_info:
            await sender.send_document(123, b"datos de prueba", "log.txt")

        assert "cota total" in str(exc_info.value)
        assert "35" in str(exc_info.value)
        assert sender._cooldown.restante() == 35.0

    async def test_send_document_con_reintentos_agotados_sin_consumir_presupuesto_conserva_rate_limit_error(
        self, tiempo: _Tiempo
    ) -> None:
        """P2-B: Cuando se agotan los reintentos pero el cooldown cabe en el presupuesto
        restante, send_document debe conservar TelegramRateLimitError según el contrato.
        """
        # 4 respuestas consecutivas 429 con retry_after=2s (agota _MAX_REINTENTOS_429 = 3)
        sender, gw = _sender(tiempo, _429(retry_after=2), _429(retry_after=2), _429(retry_after=2), _429(retry_after=2))

        with pytest.raises(TelegramRateLimitError) as exc_info:
            await sender.send_document(123, b"datos de prueba", "log.txt")

        assert not isinstance(exc_info.value, sender_mod.TelegramUploadTimeoutError)
        assert exc_info.value.retry_after == 2.0
        assert exc_info.value.presupuesto_agotado is False


# ---------------------------------------------------------------------------
# Anclas estructurales del mecanismo
# ---------------------------------------------------------------------------


def _metodo(arbol: ast.Module, clase: str, nombre: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == clase:
            for hijo in nodo.body:
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)) and hijo.name == nombre:
                    return hijo
    raise AssertionError(f"no se encontró {clase}.{nombre} en el módulo del sender")


def _es_consulta_de_espera(nodo: ast.Call) -> bool:
    return isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "esperar"


def _es_llamada_al_gateway(nodo: ast.AST) -> bool:
    return (
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "request"
        and isinstance(nodo.func.value, ast.Attribute)
        and nodo.func.value.attr == "_gateway"
    )


def _fuente() -> ast.Module:
    return ast.parse(Path(sender_mod.__file__).read_text(encoding="utf-8"))


class TestAnclasEstructurales:
    """El ancla no muestrea: enumera el mecanismo en el módulo real."""

    def test_la_consulta_del_cooldown_vive_en_el_bucle_de_intentos(self) -> None:
        """Criterio 4: la consulta precede a CADA intento HTTP, no al primero.

        Sobre el AST real: hay UNA sola consulta en ``_enviar`` y vive dentro del
        ``for`` que enumera los intentos. Moverla fuera del bucle rompe el test.
        """
        enviar = _metodo(_fuente(), "TelegramSender", "_enviar")
        bucles = [nodo for nodo in ast.walk(enviar) if isinstance(nodo, (ast.For, ast.AsyncFor, ast.While))]
        assert len(bucles) == 1, "se espera un único bucle de intentos en _enviar"
        en_el_bucle = [
            nodo for nodo in ast.walk(bucles[0]) if isinstance(nodo, ast.Call) and _es_consulta_de_espera(nodo)
        ]
        assert len(en_el_bucle) == 1, "el bucle de intentos consulta el cooldown exactamente una vez"
        todas = [nodo for nodo in ast.walk(enviar) if isinstance(nodo, ast.Call) and _es_consulta_de_espera(nodo)]
        assert len(todas) == 1, "no puede haber una consulta fuera del bucle de intentos"

    def test_todo_camino_http_del_modulo_consulta_el_cooldown(self) -> None:
        """Criterio 4 sobre la FAMILIA de caminos HTTP, no sobre uno.

        Todo método que pega a la API consulta el cooldown ANTES de su petición, y el
        conjunto de esos métodos se congela por igualdad literal: un camino nuevo
        (una vía de control que salga sin consultar) rompe el ancla —y el PR— hasta
        que se le escriba su receta. Es la regla que este repo más viola: arreglar un
        hermano y dejar al otro.
        """
        metodos = [
            nodo
            for nodo in ast.walk(_fuente())
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_es_llamada_al_gateway(hijo) for hijo in ast.walk(nodo))
        ]
        assert sorted(metodo.name for metodo in metodos) == ["_enviar", "answer_callback_query", "edit_message"]
        for metodo in metodos:
            peticion = min(hijo.lineno for hijo in ast.walk(metodo) if _es_llamada_al_gateway(hijo))
            consultas = [
                hijo.lineno for hijo in ast.walk(metodo) if isinstance(hijo, ast.Call) and _es_consulta_de_espera(hijo)
            ]
            assert consultas, f"{metodo.name} pega a la API sin consultar el cooldown compartido"
            assert min(consultas) < peticion, f"{metodo.name} consulta el cooldown después de su petición"

    def test_el_mecanismo_no_usa_locks(self) -> None:
        """Criterio 8: no hay lock que pueda quedar sostenido durante una espera.

        La coordinación es por deadline con mutación síncrona (sin ``await`` entre
        leer y escribir). Si alguien introduce un lock acá, el ancla lo nombra para
        que la revisión verifique que no se sostiene durante un ``await``.
        """
        prohibidos = {"Lock", "Semaphore", "BoundedSemaphore", "Condition"}
        encontrados = sorted(
            nodo.attr for nodo in ast.walk(_fuente()) if isinstance(nodo, ast.Attribute) and nodo.attr in prohibidos
        )
        assert encontrados == [], f"el cooldown compartido debe ser lock-free, se encontró {encontrados}"

    def test_el_cooldown_usa_reloj_monotonico(self) -> None:
        """Criterio 1: deadline monotónico, inmune a saltos del reloj de pared."""
        usos = sorted(
            nodo.attr
            for nodo in ast.walk(_fuente())
            if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "time"
        )
        assert set(usos) == {"monotonic"}, "el sender sólo puede leer el reloj monotónico"
