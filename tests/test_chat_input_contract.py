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


@pytest.mark.parametrize("raw_json", ["[]", "null", "123", '"texto"', "true"])
@pytest.mark.asyncio
async def test_http_rechaza_json_que_no_es_objeto(client, router, raw_json) -> None:
    # Preparación / Actuar
    response = await client.post(
        "/api/chat",
        data=raw_json,
        headers={"Content-Type": "application/json"},
    )

    # Verificación
    assert response.status == 400
    router.chat.assert_not_awaited()


@pytest.mark.parametrize("message", [None, 123, [], {}, True])
@pytest.mark.asyncio
async def test_http_rechaza_message_que_no_es_string(client, router, message) -> None:
    # Preparación / Actuar
    response = await client.post("/api/chat", json={"message": message})

    # Verificación
    assert response.status == 400
    router.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_rechaza_solo_espacios_sin_llamar_al_router(client, router) -> None:
    # Preparación / Actuar
    response = await client.post("/api/chat", json={"message": "   \t  "})

    # Verificación
    assert response.status == 400
    router.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_normaliza_espacios_de_un_string_valido(client, router) -> None:
    # Preparación / Actuar
    response = await client.post("/api/chat", json={"message": "  hola  "})

    # Verificación
    assert response.status == 200
    router.chat.assert_awaited_once()
    args, kwargs = router.chat.await_args
    assert args[0] == "hola"
    assert kwargs["chat_id"] == "web-session"


@pytest.mark.asyncio
async def test_http_rechaza_cuerpo_con_charset_invalido(client, router) -> None:
    # Preparación / Actuar: bytes no-UTF-8 hacen que aiohttp Request.text()
    # levante UnicodeDecodeError (subclase de ValueError) antes del json.loads.
    response = await client.post(
        "/api/chat",
        data=b'\xff\xfe{"message": "hola"}',
        headers={"Content-Type": "application/json"},
    )

    # Verificación
    assert response.status == 400
    router.chat.assert_not_awaited()


@pytest.mark.parametrize("root", [[], None, 7, "texto", True])
@pytest.mark.asyncio
async def test_ws_rechaza_json_raiz_no_objeto(router, session, root) -> None:
    # Preparación
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()

    # Actuar
    await web_app._handle_ws_ui_message(ws, json.dumps(root))

    # Verificación
    router.chat.assert_not_awaited()
    ws.send_json.assert_awaited_once()


@pytest.mark.parametrize("payload", [[], None, 7, "texto", True])
@pytest.mark.asyncio
async def test_ws_rechaza_payload_no_objeto_sin_romper_el_handler(router, session, payload) -> None:
    # Preparación
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    raw = json.dumps({"type": "command", "command": "chat", "payload": payload})

    # Actuar
    await web_app._handle_ws_ui_message(ws, raw)

    # Verificación
    router.chat.assert_not_awaited()
    ws.send_json.assert_awaited_once()


@pytest.mark.parametrize("text", [None, 123, [], {}, True])
@pytest.mark.asyncio
async def test_ws_no_coerciona_texto_de_otro_tipo(router, session, text) -> None:
    # Preparación
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    raw = json.dumps({"type": "command", "command": "chat", "payload": {"text": text}})

    # Actuar
    await web_app._handle_ws_ui_message(ws, raw)

    # Verificación
    router.chat.assert_not_awaited()
    ws.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_rechaza_texto_de_solo_espacios_con_mensaje_de_error(router, session) -> None:
    # Preparación
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    raw = json.dumps({"type": "command", "command": "chat", "payload": {"text": " \t "}})

    # Actuar
    await web_app._handle_ws_ui_message(ws, raw)

    # Verificación
    router.chat.assert_not_awaited()
    ws.send_json.assert_awaited_once_with({"type": "response", "payload": {"response": "⚠️ Empty message."}})


@pytest.mark.asyncio
async def test_ws_sigue_disponible_despues_de_un_mensaje_invalido(router, session) -> None:
    # Preparación
    web_app = WebApp(router=router, session=session)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    invalid = json.dumps({"type": "command", "command": "chat", "payload": {"text": 42}})
    valid = json.dumps({"type": "command", "command": "chat", "payload": {"text": "  hola  "}})

    # Actuar
    await web_app._handle_ws_ui_message(ws, invalid)
    await web_app._handle_ws_ui_message(ws, valid)

    # Verificación
    router.chat.assert_awaited_once()
    args, kwargs = router.chat.await_args
    assert args[0] == "hola"
    assert kwargs["chat_id"] == "web-session"
    assert ws.send_json.await_count == 2


def test_toda_superficie_que_despacha_al_router_normaliza_el_texto() -> None:
    """Ancla enumerante: congela la familia completa de despachadores de chat.

    Detecta por AST todos los métodos de ``WebApp`` que llaman a
    ``self._router.chat``. Un despachador nuevo rompe la igualdad literal
    hasta que se escriba su cobertura, y debe usar ``_normalize_chat_text``
    para no divergir silenciosamente del contrato HTTP/WS.
    """
    arbol = ast.parse(textwrap.dedent(inspect.getsource(WebApp)))
    clase = next(nodo for nodo in arbol.body if isinstance(nodo, ast.ClassDef))
    despachadores: dict[str, set[str]] = {}
    for metodo in clase.body:
        if not isinstance(metodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        llamadas = {ast.unparse(nodo.func) for nodo in ast.walk(metodo) if isinstance(nodo, ast.Call)}
        if "self._router.chat" in llamadas:
            despachadores[metodo.name] = llamadas

    assert set(despachadores) == {"_handle_chat", "_handle_ws_ui_message"}
    for nombre, llamadas in despachadores.items():
        assert "_normalize_chat_text" in llamadas, f"{nombre} despacha al router sin normalizar el texto"
