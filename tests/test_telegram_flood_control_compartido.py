"""``TelegramSender``: cooldown COMPARTIDO del flood-control (HTTP 429) — #706.

Diagnóstico de la issue: el reintento de un 429 dormía sólo a la corrutina que
falló. Una ``send()`` concurrente al mismo chat pasaba ``_wait_for_rate_limit`` de
inmediato y volvía a golpear Telegram dentro de la misma ventana de flood-control,
agotando reintentos acotados y perdiendo mensajes.

Propiedades ancladas en esta suite (R1–R10 + anclas estructurales):
1. R1: Dos tareas al mismo chat — la segunda espera el cooldown compartido.
2. R2: Dos chats distintos — el 429 de un chat no bloquea al otro.
3. R3: Extensión monotónica — el deadline nunca retrocede.
4. R4: Reintento de la propia tarea — respeta el mismo cooldown compartido.
5. R5: Mensaje y documento — comparten el cooldown del mismo chat.
6. R6: Ningún 429 se registra como envío exitoso.
7. R7: Cancelación — cancelar una tarea en espera no borra el cooldown para las demás.
8. R8: Reintentos agotados — produce TelegramRateLimitError.
9. R9: Retry-after excesivo o inválido — se acota y rechaza según política.
10. R10: Límite temporal de documentos — la espera compartida cuenta dentro del timeout.
11. Presupuesto acumulado — las esperas son finitas y no crecen indefinidamente.
12. Limpieza de memoria — chats vencidos se purgan sin timers de fondo.
13. Vías de control — answer_callback_query y edit_message consultan y publican.
14. Anclas AST — sin locks, reloj monotónico, consulta en bucle de intentos.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import contextlib
import json
from collections.abc import Iterator
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
    TelegramSendError,
)

_TOKEN = "123:ABC"


# ---------------------------------------------------------------------------
# Harness: reloj simulado, sleep espiado y gateway determinista
# ---------------------------------------------------------------------------


class _RelojFalso:
    """Reloj monotonic simulado."""

    def __init__(self, inicio: float = 100.0) -> None:
        self.ahora = inicio

    def monotonic(self) -> float:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += segundos


class _Tiempo:
    """Coordinador de tiempo simulado para concurrencia determinista."""

    def __init__(self) -> None:
        self.reloj = _RelojFalso()
        self.dormidos: list[float] = []
        self.llamadas: list[tuple[int | None, str, float]] = []
        self._sleep_real = asyncio.sleep
        self._barrera: asyncio.Event | None = None

    def pausar_sleep(self) -> asyncio.Event:
        """Pausa los sleeps simulados con una barrera hasta llamar a reanudar_sleep()."""
        self._barrera = asyncio.Event()
        return self._barrera

    def reanudar_sleep(self) -> None:
        """Libera la barrera de sleep simulado."""
        if self._barrera is not None:
            self._barrera.set()
            self._barrera = None

    async def dormir(self, segundos: float) -> None:
        self.dormidos.append(segundos)
        if self._barrera is not None:
            await self._barrera.wait()
        inicio = self.reloj.ahora
        await self._sleep_real(0)
        self.reloj.ahora = max(self.reloj.ahora, inicio + segundos)

    async def ceder(self) -> None:
        await self._sleep_real(0)

    async def hasta(self, condicion: Any, *, limite: int = 5000) -> None:
        """Espera determinista a una condición, cediendo el control."""
        for _ in range(limite):
            if condicion():
                return
            await self.ceder()
        raise AssertionError("la condición de sincronización no se cumplió")


@pytest.fixture
def tiempo(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Tiempo]:
    """Inyecta reloj simulado y sleep espiado en el módulo telegram_sender."""
    t = _Tiempo()
    monkeypatch.setattr(sender_mod, "time", t.reloj)
    with patch.object(sender_mod.asyncio, "sleep", t.dormir):
        yield t


def _respuesta(status: int, cuerpo: dict[str, Any] | str, headers: dict[str, str] | None = None) -> AsyncMock:
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
        "description": f"Too Many Requests: retry after {retry_after}",
    }
    if retry_after is not None:
        cuerpo["parameters"] = {"retry_after": retry_after}
    return _respuesta(429, cuerpo)


def _sender(tiempo: _Tiempo, *respuestas: AsyncMock) -> tuple[TelegramSender, MagicMock]:
    pendientes = collections.deque(respuestas)
    mock = MagicMock()

    async def _request(method: str, url: str, session: Any, **kwargs: Any) -> Any:
        endpoint = url.rsplit("/", 1)[-1]
        chat_id = None
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            chat_id = kwargs["json"].get("chat_id")
        elif "data" in kwargs and isinstance(kwargs["data"], aiohttp.FormData):
            for campo in kwargs["data"]._fields:
                if campo[0].get("name") == "chat_id":
                    with contextlib.suppress(ValueError, TypeError):
                        chat_id = int(campo[2])
        tiempo.llamadas.append((chat_id, endpoint, tiempo.reloj.ahora))
        if not pendientes:
            raise AssertionError(f"Intento HTTP inesperado en {endpoint} para {chat_id}")
        return pendientes.popleft()

    mock.request = AsyncMock(side_effect=_request)
    sender = TelegramSender(bot_token=_TOKEN, gateway=mock, session=MagicMock(spec=aiohttp.ClientSession))
    return sender, mock


# ---------------------------------------------------------------------------
# R1 & R2: Concurrencia entre tareas y aislamiento por chat
# ---------------------------------------------------------------------------


class TestConcurrenciaYAislamiento:
    async def test_r1_dos_tareas_mismo_chat_segunda_espera_cooldown(self, tiempo: _Tiempo) -> None:
        """R1: Tarea A recibe 429 (retry_after=5). Tarea B al mismo chat NO llama

        al gateway antes de que venza el cooldown compartido.
        """
        sender, gateway = _sender(tiempo, _429(retry_after=5), _ok(101), _ok(102))

        tiempo.pausar_sleep()
        primera = asyncio.create_task(sender.send(123, "primer aviso"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante(123) > 0)
        segunda = asyncio.create_task(sender.send(123, "segundo aviso"))

        for _ in range(5):
            await tiempo.ceder()
        assert len(tiempo.llamadas) == 1, "la segunda tarea no debió llamar al gateway durante el cooldown"

        tiempo.reanudar_sleep()
        res_a = await primera
        res_b = await segunda
        assert res_a == TelegramMessage(chat_id=123, message_id=101)
        assert res_b == TelegramMessage(chat_id=123, message_id=102)
        assert gateway.request.await_count == 3
        instantes = [t for _cid, _ep, t in tiempo.llamadas]
        assert instantes == [100.0, 105.0, 105.0], "la segunda tarea no debió llamar al gateway antes de 105.0"

    async def test_r2_dos_chats_distintos_429_en_chat_a_no_bloquea_chat_b(self, tiempo: _Tiempo) -> None:
        """R2: 429 en chat 123 no bloquea a chat 456."""
        sender, gateway = _sender(tiempo, _429(retry_after=5), _ok(202), _ok(201))

        tiempo.pausar_sleep()
        primera = asyncio.create_task(sender.send(123, "chat A"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante(123) > 0)

        segunda = asyncio.create_task(sender.send(456, "chat B"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 2)
        assert tiempo.llamadas[1] == (456, "sendMessage", 100.0), "chat B debió llamar a t=100.0"

        tiempo.reanudar_sleep()
        res_a, res_b = await asyncio.gather(primera, segunda)
        assert res_a.message_id == 201
        assert res_b.message_id == 202

        chats_y_tiempos = [(cid, t) for cid, _ep, t in tiempo.llamadas]
        assert chats_y_tiempos == [(123, 100.0), (456, 100.0), (123, 105.0)]


# ---------------------------------------------------------------------------
# R3 & R4: Extensión monotónica y reintento propio
# ---------------------------------------------------------------------------


class TestExtensionYReintento:
    def test_r3_extension_monotonica_deadline_no_retrocede(self, tiempo: _Tiempo) -> None:
        """R3: Publicar un retry_after menor después de uno mayor no acorta el deadline."""
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(123, 5.0)  # deadline = 105.0

        tiempo.reloj.avanzar(1.0)  # t = 101.0
        cooldown.publicar(123, 2.0)  # nuevo candidato = 103.0 < 105.0

        assert cooldown.restante(123) == pytest.approx(4.0), "el deadline mayor de 105.0s debe mantenerse"

    async def test_r4_reintento_de_la_propia_tarea_respeta_extension_concurrente(self, tiempo: _Tiempo) -> None:
        """R4: Tarea A recibe 429(2s). Mientras espera, una respuesta concurrente

        extiende el cooldown a 4s. A debe esperar el cooldown extendido.
        """
        sender, gateway = _sender(tiempo, _429(retry_after=2), _ok(11))

        tiempo.pausar_sleep()
        tarea = asyncio.create_task(sender.send(123, "intento con reintento"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante(123) > 0)

        sender._cooldown.publicar(123, 4.0)

        tiempo.reanudar_sleep()
        res = await tarea
        assert res.message_id == 11
        assert gateway.request.await_count == 2
        instantes = [t for _cid, _ep, t in tiempo.llamadas]
        assert instantes == [100.0, 104.0], "el reintento no esperó la extensión concurrente del cooldown"


# ---------------------------------------------------------------------------
# R5 & R6: Mensaje y Documento; Ningún 429 como éxito
# ---------------------------------------------------------------------------


class TestMensajeYDocumento:
    async def test_r5_send_document_y_send_comparten_cooldown_en_mismo_chat(self, tiempo: _Tiempo) -> None:
        """R5: 429 originado por send_document afecta a send concurrente del mismo chat."""
        sender, gateway = _sender(tiempo, _429(retry_after=4), _ok(301), _ok(302))

        tiempo.pausar_sleep()
        tarea_doc = asyncio.create_task(sender.send_document(123, b"log data", "log.txt"))
        await tiempo.hasta(lambda: len(tiempo.llamadas) == 1)
        await tiempo.hasta(lambda: sender._cooldown.restante(123) > 0)

        # En este punto sendDocument publicó 429(4s). send() al mismo chat no debe entrar a gw.request.
        tarea_msg = asyncio.create_task(sender.send(123, "texto concurrente"))
        for _ in range(5):
            await tiempo.ceder()

        assert len(tiempo.llamadas) == 1, "sendMessage no respetó el cooldown de sendDocument"

        tiempo.reanudar_sleep()
        res_doc, res_msg = await asyncio.gather(tarea_doc, tarea_msg)
        assert res_doc.message_id == 301
        assert res_msg.message_id == 302
        assert gateway.request.await_count == 3
        instantes = [t for _cid, _ep, t in tiempo.llamadas]
        assert instantes == [100.0, 104.0, 104.0]

    async def test_r6_ningun_429_se_registra_como_exito_ni_consume_slot(self, tiempo: _Tiempo) -> None:
        """R6: Una respuesta 429 no incrementa _send_times ni produce TelegramMessage."""
        sender, _ = _sender(tiempo, _429(retry_after=1), _ok(401))

        res = await sender.send(123, "hola")
        assert res.message_id == 401
        assert len(sender._send_times[123]) == 1


# ---------------------------------------------------------------------------
# R7 & R8: Cancelación y Reintentos agotados
# ---------------------------------------------------------------------------


class TestCancelacionYReintentosAgotados:
    async def test_r7_cancelar_tarea_en_espera_no_borra_cooldown_compartido(self, tiempo: _Tiempo) -> None:
        """R7: Cancelar B durante la espera libera B sin borrar el cooldown para C."""
        sender, _ = _sender(tiempo, _429(retry_after=5), _ok(501))

        sender._cooldown.publicar(123, 5.0)

        tarea_b = asyncio.create_task(sender.send(123, "mensaje B"))
        await tiempo.ceder()
        tarea_b.cancel()

        with pytest.raises(asyncio.CancelledError):
            await tarea_b

        assert sender._cooldown.restante(123) > 0.0

    async def test_r8_reintentos_agotados_lanza_telegram_rate_limit_error(self, tiempo: _Tiempo) -> None:
        """R8: Ráfaga persistente de 429 agota _MAX_REINTENTOS_429 y lanza error tipado."""
        assert sender_mod._MAX_REINTENTOS_429 == 3, "el contrato normativo fija exactamente 3 reintentos"
        respuestas = [_429(retry_after=1) for _ in range(4)]
        sender, gw = _sender(tiempo, *respuestas)

        with pytest.raises(TelegramRateLimitError) as exc_info:
            await sender.send(123, "hola")

        assert exc_info.value.retry_after == 1.0
        assert issubclass(TelegramRateLimitError, TelegramSendError)
        assert gw.request.await_count == 4


# ---------------------------------------------------------------------------
# R9 & R10: Retry-after excesivo y límite temporal de documentos
# ---------------------------------------------------------------------------


class TestLimitesYAcotacion:
    async def test_r9_retry_after_excesivo_no_se_espera_y_se_acota_en_publicacion(self, tiempo: _Tiempo) -> None:
        """R9: retry_after > 30s falla de inmediato en _enviar y se acota a 30s en publicación."""
        sender, gw = _sender(tiempo, _429(retry_after=600))

        with pytest.raises(TelegramRateLimitError):
            await sender.send(123, "hola")

        assert gw.request.await_count == 1
        assert sender._cooldown.restante(123) <= sender_mod._RETRY_AFTER_MAX_SEGUNDOS

    async def test_r10_espera_cooldown_cuenta_dentro_del_deadline_de_documentos(
        self, tiempo: _Tiempo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R10: send_document falla con TelegramUploadTimeoutError si el cooldown acumulado

        excede MAX_UPLOAD_TOTAL_SECONDS.
        """
        sender, _ = _sender(tiempo, _429(retry_after=20), _ok(601))
        monkeypatch.setattr(sender_mod, "MAX_UPLOAD_TOTAL_SECONDS", 5.0)

        with pytest.raises(sender_mod.TelegramUploadTimeoutError):
            await sender.send_document(123, b"data", "test.txt")


# ---------------------------------------------------------------------------
# Presupuesto de espera acumulada y Limpieza de memoria
# ---------------------------------------------------------------------------


class TestPresupuestoYLimpieza:
    async def test_espera_acumulada_acotada_por_presupuesto(self, tiempo: _Tiempo) -> None:
        """Una corrutina no espera indefinidamente si extensiones sucesivas superan el presupuesto."""
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(123, 20.0)

        with pytest.raises(TelegramRateLimitError, match="presupuesto"):
            await cooldown.esperar(123, presupuesto=10.0)

    def test_limpieza_de_chats_vencidos(self, tiempo: _Tiempo) -> None:
        """Los chats inactivos cuyo cooldown ya venció se purgan sin dejar memoria infinita."""
        cooldown = sender_mod.CooldownCompartido()
        cooldown.publicar(101, 2.0)
        cooldown.publicar(102, 10.0)

        tiempo.reloj.avanzar(5.0)
        cooldown.publicar(103, 2.0)

        assert 101 not in cooldown._deadlines
        assert 102 in cooldown._deadlines
        assert 103 in cooldown._deadlines


# ---------------------------------------------------------------------------
# Vías de control: edit_message y answer_callback_query
# ---------------------------------------------------------------------------


class TestViasDeControl:
    async def test_edit_message_espera_cooldown_y_publica_429(self, tiempo: _Tiempo) -> None:
        sender, gw = _sender(tiempo, _429(retry_after=3))

        await sender.edit_message(123, 55, "nuevo texto")

        assert gw.request.await_count == 1
        assert sender._cooldown.restante(123) == pytest.approx(3.0)

    async def test_answer_callback_query_espera_y_publica_a_nivel_bot(self, tiempo: _Tiempo) -> None:
        sender, gw = _sender(tiempo, _429(retry_after=4))

        await sender.answer_callback_query("cb_123", text="ack")

        assert gw.request.await_count == 1
        assert sender._cooldown.restante(None) == pytest.approx(4.0)
        assert sender._cooldown.restante(999) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Anclas estructurales (AST)
# ---------------------------------------------------------------------------


def _fuente() -> ast.AST:
    ruta = Path(sender_mod.__file__)
    return ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))


class TestAnclasEstructurales:
    def test_la_consulta_del_cooldown_vive_en_el_bucle_de_intentos(self) -> None:
        """La consulta de cooldown debe ocurrir antes de CADA intento HTTP en _enviar."""
        arbol = _fuente()
        enviar_fn = next(
            nodo
            for nodo in ast.walk(arbol)
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == "_enviar"
        )
        bucles = [nodo for nodo in ast.walk(enviar_fn) if isinstance(nodo, (ast.For, ast.AsyncFor, ast.While))]
        assert len(bucles) == 1, "se espera un único bucle de intentos en _enviar"
        bucle = bucles[0]
        consultas = [
            nodo
            for nodo in ast.walk(bucle)
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "esperar"
        ]
        assert len(consultas) >= 1, "el bucle de intentos debe consultar esperar() del cooldown"
        llamada_incondicional = any(
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Await)
            and isinstance(stmt.value.value, ast.Call)
            and isinstance(stmt.value.value.func, ast.Attribute)
            and stmt.value.value.func.attr == "esperar"
            for stmt in bucle.body
        )
        assert llamada_incondicional, "la llamada a esperar() debe estar al nivel del bucle sin condiciones"

    def test_todo_camino_http_del_modulo_consulta_el_cooldown(self) -> None:
        """Todo método que llama al gateway consulta esperar() antes de request()."""
        arbol = _fuente()
        metodos = [
            nodo
            for nodo in ast.walk(arbol)
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute) and hijo.func.attr == "request"
                for hijo in ast.walk(nodo)
            )
        ]
        assert sorted(metodo.name for metodo in metodos) == ["_enviar", "answer_callback_query", "edit_message"]
        for metodo in metodos:
            consultas = [
                hijo.lineno
                for hijo in ast.walk(metodo)
                if isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute) and hijo.func.attr == "esperar"
            ]
            assert consultas, f"{metodo.name} no consulta el cooldown compartido"

    def test_el_mecanismo_no_usa_locks(self) -> None:
        """El cooldown debe ser lock-free para no sostener bloqueos durante awaits."""
        arbol = _fuente()
        prohibidos = {"Lock", "Semaphore", "BoundedSemaphore", "Condition"}
        encontrados = sorted(
            nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute) and nodo.attr in prohibidos
        )
        assert encontrados == [], f"el cooldown compartido debe ser lock-free, se encontró {encontrados}"

    def test_el_cooldown_usa_reloj_monotonico(self) -> None:
        """El sender debe usar time.monotonic exclusivamente."""
        arbol = _fuente()
        usos = sorted(
            nodo.attr
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "time"
        )
        assert set(usos) == {"monotonic"}, f"el sender debe usar sólo time.monotonic, se encontró {usos}"
