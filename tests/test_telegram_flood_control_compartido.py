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

    @pytest.mark.parametrize("absurdo", [60.0, 600.0, 9999.0])
    def test_un_retry_after_mayor_al_tope_se_acota(self, tiempo: _Tiempo, absurdo: float) -> None:
        # Criterio 6: no hay esperas indefinidas. El tope que ya usa el reintento
        # acota TAMBIÉN lo que se publica para las demás corrutinas.
        cooldown = sender_mod.CooldownCompartido()

        cooldown.publicar(absurdo)

        assert cooldown.restante() == pytest.approx(sender_mod._RETRY_AFTER_MAX_SEGUNDOS)
        assert cooldown.restante() <= sender_mod._RETRY_AFTER_MAX_SEGUNDOS

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
