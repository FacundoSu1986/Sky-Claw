"""Contrato estricto compartido para las entradas de chat HTTP y WebSocket."""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.web.app import WebApp


@pytest.fixture
def router() -> MagicMock:
    mock = MagicMock()
    mock.chat = AsyncMock(return_value="respuesta")
    return mock


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


@pytest.fixture
async def client(router, session, aiohttp_client, monkeypatch):
    monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")
    app = WebApp(router=router, session=session).create_app()
    return await aiohttp_client(app)


@pytest.mark.parametrize("payload", [[], None, 123, "texto", True])
@pytest.mark.asyncio
async def test_http_rechaza_json_que_no_es_objeto(client, router, payload) -> None:
    # Arrange / Act
    response = await client.post("/api/chat", json=payload)

    # Assert
    assert response.status == 400
    router.chat.assert_not_awaited()


@pytest.mark.parametrize("message", [None, 123, [], {}, True])
@pytest.mark.asyncio
async def test_http_rechaza_message_que_no_es_string(client, router, message) -> None:
    # Arrange / Act
    response = await client.post("/api/chat", json={"message": message})

    # Assert
    assert response.status == 400
    router.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_rechaza_solo_espacios_sin_llamar_al_router(client, router) -> None:
    # Arrange / Act
    response = await client.post("/api/chat", json={"message": "   \t  "})

    # Assert
    assert response.status == 400
    router.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_normaliza_espacios_de_un_string_valido(client, router) -> None:
    # Arrange / Act
    response = await client.post("/api/chat", json={"message": "  hola  "})

    # Assert
    assert response.status == 200
    router.chat.assert_awaited_once()
    args, kwargs = router.chat.await_args
    assert args[0] == "hola"
    assert kwargs["chat_id"] == "web-session"


@pytest.mark.parametrize("payload", [[], 7, "texto", True])
@pytest.mark.asyncio
async def test_ws_rechaza_payload_no_objeto_sin_romper_el_handler(router, session, payload) -> None:
    # Arrange
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    raw = json.dumps({"type": "command", "command": "chat", "payload": payload})

    # Act
    await web_app._handle_ws_ui_message(ws, raw)

    # Assert
    router.chat.assert_not_awaited()
    ws.send_json.assert_awaited_once()


@pytest.mark.parametrize("text", [None, 123, [], {}, True])
@pytest.mark.asyncio
async def test_ws_no_coerciona_texto_de_otro_tipo(router, session, text) -> None:
    # Arrange
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    raw = json.dumps({"type": "command", "command": "chat", "payload": {"text": text}})

    # Act
    await web_app._handle_ws_ui_message(ws, raw)

    # Assert
    router.chat.assert_not_awaited()
    ws.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_sigue_disponible_despues_de_un_mensaje_invalido(router, session) -> None:
    # Arrange
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    invalid = json.dumps({"type": "command", "command": "chat", "payload": {"text": 42}})
    valid = json.dumps({"type": "command", "command": "chat", "payload": {"text": "  hola  "}})

    # Act
    await web_app._handle_ws_ui_message(ws, invalid)
    await web_app._handle_ws_ui_message(ws, valid)

    # Assert
    router.chat.assert_awaited_once()
    args, kwargs = router.chat.await_args
    assert args[0] == "hola"
    assert kwargs["chat_id"] == "web-session"
    assert ws.send_json.await_count == 2


def test_http_y_ws_comparten_el_mismo_validador_de_texto() -> None:
    """Ancla de familia: una superficie nueva no debe divergir silenciosamente."""

    def llamadas_de(func) -> set[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        return {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    assert "_normalize_chat_text" in llamadas_de(WebApp._handle_chat)
    assert "_normalize_chat_text" in llamadas_de(WebApp._handle_ws_ui_message)
