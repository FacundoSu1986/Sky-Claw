"""``/lod_status``: consulta de SOLO LECTURA del estado de la etapa 9 desde Telegram.

El operador que sigue una etapa de decenas de minutos desde el teléfono recibe avisos (push), pero no
tenía forma de PREGUNTAR cómo va. Este comando contesta con el último estado conocido; no ejecuta nada,
no cancela nada y no llega al LLM.

Propiedades que estos tests fijan:

* **Hereda la autorización del dispatcher.** ``process_update`` valida —antes de cualquier comando— que
  el remitente sea el operador, en su chat privado y sin reenviados. Un comando de lectura también filtra
  información del equipo del operador (rutas, nombres de mods en las líneas de log), así que se prueba que
  nadie más lo obtiene, incluido el fail-closed sin operador configurado.
* **Los dos transportes llegan al mismo dispatcher** (webhook HTTP y long polling): la regla del repo es no
  arreglar un camino y dejar a su gemelo.
* **La rama es de solo lectura por construcción**: un ancla por AST exige que el handler no pueda tocar el
  agente, el guard HITL ni la sesión de red.
* **Los comandos del dispatcher están congelados** y todos se evalúan DESPUÉS del gate de autorización: un
  comando nuevo colocado antes de él rompe el ancla.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.comms.telegram import TelegramWebhook
from sky_claw.app.comms.telegram_polling import TelegramPolling
from sky_claw.app.comms.telegram_sender import TelegramSendError

OPERADOR = 424242


def _webhook(*, lod_status: Any, autorizado: int | None = OPERADOR) -> tuple[TelegramWebhook, MagicMock, MagicMock]:
    router = MagicMock()
    router.chat = AsyncMock(return_value="respuesta del LLM")
    sender = MagicMock()
    sender.send = AsyncMock()
    webhook = TelegramWebhook(
        router=router,
        sender=sender,
        session=MagicMock(spec=aiohttp.ClientSession),
        authorized_user_id=autorizado,
        lod_status=lod_status,
    )
    return webhook, router, sender


def _update(
    texto: str, *, update_id: int = 1, user_id: int = OPERADOR, chat_id: int = OPERADOR, **mensaje: Any
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "text": texto,
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            **mensaje,
        },
    }


async def _drenar(webhook: TelegramWebhook) -> None:
    """Espera a que terminen las tareas de fondo que el dispatcher lanzó."""
    while webhook._tasks:
        await asyncio.gather(*list(webhook._tasks), return_exceptions=True)


class TestComando:
    async def test_el_operador_recibe_el_estado_en_html_y_la_consulta_no_llega_al_llm(self) -> None:
        webhook, router, sender = _webhook(lod_status=lambda: "<b>Etapa 9 en curso</b>")

        await webhook.process_update(_update("/lod_status"))
        await _drenar(webhook)

        sender.send.assert_awaited_once_with(OPERADOR, "<b>Etapa 9 en curso</b>", parse_mode="HTML")
        router.chat.assert_not_called()

    async def test_el_proveedor_se_consulta_sin_argumentos_y_una_sola_vez(self) -> None:
        proveedor = MagicMock(return_value="estado")
        webhook, _router, _sender = _webhook(lod_status=proveedor)

        await webhook.process_update(_update("/lod_status"))
        await _drenar(webhook)

        proveedor.assert_called_once_with()

    async def test_el_comando_tolera_espacios_alrededor_como_update_mods(self) -> None:
        proveedor = MagicMock(return_value="estado")
        webhook, router, _sender = _webhook(lod_status=proveedor)

        await webhook.process_update(_update("  /lod_status  "))
        await _drenar(webhook)

        proveedor.assert_called_once_with()
        router.chat.assert_not_called()

    async def test_sin_proveedor_responde_que_no_esta_disponible_y_no_llega_al_llm(self) -> None:
        webhook, router, sender = _webhook(lod_status=None)

        await webhook.process_update(_update("/lod_status"))
        await _drenar(webhook)

        sender.send.assert_awaited_once()
        chat, texto = sender.send.await_args.args
        assert chat == OPERADOR
        assert "no está disponible" in texto
        assert sender.send.await_args.kwargs == {}, "es texto plano fijo: sin parse_mode HTML"
        router.chat.assert_not_called()

    async def test_si_el_proveedor_falla_responde_un_error_generico_sin_filtrar_la_causa(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _roto() -> str:
            raise RuntimeError("secreto: E:\\ruta\\privada")

        webhook, router, sender = _webhook(lod_status=_roto)

        with caplog.at_level(logging.ERROR, logger="sky_claw.app.comms.telegram"):
            await webhook.process_update(_update("/lod_status"))
            await _drenar(webhook)

        sender.send.assert_awaited_once()
        _chat, texto = sender.send.await_args.args
        assert "No pude leer el estado" in texto
        assert "secreto" not in texto and "privada" not in texto, "la causa va al log, no al chat"
        assert any("lod_status" in r.getMessage() for r in caplog.records)
        router.chat.assert_not_called()

    async def test_si_el_envio_falla_no_propaga_y_queda_registrado(self, caplog: pytest.LogCaptureFixture) -> None:
        webhook, _router, sender = _webhook(lod_status=lambda: "estado")
        sender.send.side_effect = TelegramSendError("Telegram API returned 400: can't parse entities")

        with caplog.at_level(logging.ERROR, logger="sky_claw.app.comms.telegram"):
            await webhook.process_update(_update("/lod_status"))
            await _drenar(webhook)  # la tarea de fondo no puede dejar una excepción sin recoger

        assert any("lod_status" in r.getMessage() for r in caplog.records)

    async def test_un_update_repetido_responde_una_sola_vez(self) -> None:
        proveedor = MagicMock(return_value="estado")
        webhook, _router, sender = _webhook(lod_status=proveedor)

        await webhook.process_update(_update("/lod_status", update_id=7))
        await webhook.process_update(_update("/lod_status", update_id=7))
        await _drenar(webhook)

        assert sender.send.await_count == 1

    @pytest.mark.parametrize("texto", ["/lod_status extra", "/LOD_STATUS", "lod_status", "/lod_status@OtroBot"])
    async def test_solo_el_texto_exacto_es_el_comando_lo_demas_es_chat(self, texto: str) -> None:
        proveedor = MagicMock(return_value="estado")
        webhook, router, _sender = _webhook(lod_status=proveedor)

        await webhook.process_update(_update(texto))
        await _drenar(webhook)

        proveedor.assert_not_called()
        router.chat.assert_awaited_once()


class TestAutorizacion:
    @pytest.mark.parametrize(
        ("autorizado", "user_id", "chat_id", "extra"),
        [
            (OPERADOR, 999, OPERADOR, {}),
            (OPERADOR, OPERADOR, -100123, {}),
            (OPERADOR, OPERADOR, OPERADOR, {"forward_date": 1700000000}),
            (OPERADOR, OPERADOR, OPERADOR, {"forward_from": {"id": 5}}),
            (OPERADOR, OPERADOR, OPERADOR, {"reply_to_message": {"forward_date": 1700000000}}),
            (None, OPERADOR, OPERADOR, {}),
        ],
        ids=["usuario_ajeno", "chat_de_grupo", "reenviado", "reenviado_from", "respuesta_a_reenviado", "sin_operador"],
    )
    async def test_nadie_mas_que_el_operador_en_su_chat_privado_obtiene_el_estado(
        self, autorizado: int | None, user_id: int, chat_id: int, extra: dict[str, Any]
    ) -> None:
        proveedor = MagicMock(return_value="estado confidencial")
        webhook, router, sender = _webhook(lod_status=proveedor, autorizado=autorizado)

        await webhook.process_update(_update("/lod_status", user_id=user_id, chat_id=chat_id, **extra))
        await _drenar(webhook)

        proveedor.assert_not_called()
        sender.send.assert_not_called()
        router.chat.assert_not_called()


class TestSuperficies:
    """El webhook HTTP y el long polling comparten dispatcher: el comando tiene que funcionar por los dos."""

    async def test_por_el_webhook_http_llega_al_mismo_dispatcher(self) -> None:
        webhook, _router, sender = _webhook(lod_status=lambda: "estado")
        request = MagicMock()
        request.headers = {}
        request.json = AsyncMock(return_value=_update("/lod_status"))

        respuesta = await webhook.handle_update(request)
        await _drenar(webhook)

        assert respuesta.status == 200
        sender.send.assert_awaited_once_with(OPERADOR, "estado", parse_mode="HTML")

    async def test_por_long_polling_llega_al_mismo_dispatcher(self) -> None:
        webhook, _router, sender = _webhook(lod_status=lambda: "estado")
        polling = TelegramPolling(
            token="123:ABC",
            webhook_handler=webhook,
            gateway=MagicMock(),
            session=MagicMock(spec=aiohttp.ClientSession),
            interval=0,
            authorized_chat_id=OPERADOR,
        )

        await polling._process_raw_update(_update("/lod_status"))
        await _drenar(webhook)

        sender.send.assert_awaited_once_with(OPERADOR, "estado", parse_mode="HTML")

    async def test_el_polling_descarta_otro_chat_antes_de_llegar_al_dispatcher(self) -> None:
        proveedor = MagicMock(return_value="estado")
        webhook, _router, sender = _webhook(lod_status=proveedor)
        polling = TelegramPolling(
            token="123:ABC",
            webhook_handler=webhook,
            gateway=MagicMock(),
            session=MagicMock(spec=aiohttp.ClientSession),
            interval=0,
            authorized_chat_id=OPERADOR,
        )

        await polling._process_raw_update(_update("/lod_status", chat_id=-100123))
        await _drenar(webhook)

        proveedor.assert_not_called()
        sender.send.assert_not_called()


# ---------------------------------------------------------------------------
# Anclas por AST: enumeran, no muestrean
# ---------------------------------------------------------------------------


def _metodo(nombre: str) -> ast.AsyncFunctionDef:
    fuente = textwrap.dedent(inspect.getsource(getattr(TelegramWebhook, nombre)))
    [funcion] = [n for n in ast.parse(fuente).body if isinstance(n, ast.AsyncFunctionDef)]
    return funcion


def _comparaciones_de_comando(funcion: ast.AsyncFunctionDef) -> list[tuple[str, int]]:
    """``(literal, línea)`` de cada ``text == "<literal>"`` del dispatcher."""
    encontradas: list[tuple[str, int]] = []
    for nodo in ast.walk(funcion):
        if (
            isinstance(nodo, ast.Compare)
            and isinstance(nodo.left, ast.Name)
            and nodo.left.id == "text"
            and len(nodo.ops) == 1
            and isinstance(nodo.ops[0], ast.Eq)
            and isinstance(nodo.comparators[0], ast.Constant)
            and isinstance(nodo.comparators[0].value, str)
        ):
            encontradas.append((nodo.comparators[0].value, nodo.lineno))
    return encontradas


def test_los_comandos_exactos_del_dispatcher_estan_congelados() -> None:
    comandos = {literal for literal, _linea in _comparaciones_de_comando(_metodo("process_update"))}

    assert comandos == {"/update_mods", "/lod_status"}, (
        "cambió el conjunto de comandos del dispatcher: un comando nuevo es una superficie de entrada con "
        "autorización propia; decidí qué filtra y escribí sus tests antes de congelarlo acá"
    )


def test_todo_comando_se_evalua_despues_del_gate_de_autorizacion() -> None:
    funcion = _metodo("process_update")
    gate = [
        n.lineno
        for n in ast.walk(funcion)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_validate_private_operator"
    ]
    assert len(gate) == 1, "el dispatcher tiene que validar al operador exactamente una vez"

    antes_del_gate = [literal for literal, linea in _comparaciones_de_comando(funcion) if linea < gate[0]]

    assert antes_del_gate == [], f"comandos evaluados ANTES de autorizar al remitente: {antes_del_gate}"


def test_la_rama_de_solo_lectura_no_puede_tocar_el_agente_ni_ejecutar_nada() -> None:
    """El handler de ``/lod_status`` sólo puede usar el proveedor de estado y el sender.

    Sin acceso a ``_router`` (agente LLM), ``_hitl`` (aprobaciones) ni ``_session`` (red), esta rama no
    puede ejecutar la etapa ni mutar nada aunque alguien la edite sin cuidado: se rompería este ancla.
    """
    handler = _metodo("_handle_lod_status_command")
    usados = {
        n.attr
        for n in ast.walk(handler)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"
    }

    assert usados == {"_lod_status", "_sender"}, f"el handler de solo lectura toca: {sorted(usados)}"
