"""Round-trip + auth tests for the GUI↔daemon /ws/ui chat handler.

PR #195 shipped green because nothing tested this round-trip; these are that
missing coverage. The handler routes ``command/chat`` -> ``LLMRouter`` and
replies ``{"type":"response","payload":{"response": ...}}``.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from sky_claw.antigravity.web.app import WebApp


class _StubRouter:
    def __init__(self, reply: str = "pong-from-llm") -> None:
        self._reply = reply
        self.messages: list[str] = []

    async def chat(self, message: str, session, *, chat_id: str) -> str:
        self.messages.append(message)
        return self._reply


class _StubAuth:
    """Minimal AuthTokenManager stub: validates exactly one known token."""

    def __init__(self, valid_token: str = "good-token") -> None:
        self._valid = valid_token
        self.calls: list[str] = []

    def validate(self, token: str) -> bool:
        self.calls.append(token)
        return token == self._valid

    def register_rotation_callback(self, _cb) -> None:  # create_app may call this
        pass


@pytest.fixture
async def client_router(monkeypatch):
    monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")  # bypass auth for round-trip
    router = _StubRouter()
    app = WebApp(router=router, session=None).create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, router
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_command_returns_response_frame(client_router):
    client, router = client_router
    async with client.ws_connect("/ws/ui") as ws:
        await ws.send_json({"type": "command", "command": "chat", "payload": {"text": "hola"}})
        reply = await ws.receive_json(timeout=5)

    assert reply == {"type": "response", "payload": {"response": "pong-from-llm"}}
    assert router.messages == ["hola"]


@pytest.mark.asyncio
async def test_ws_ui_rejects_unauthed_with_4001(monkeypatch):
    monkeypatch.delenv("SKY_CLAW_DEV_NO_AUTH", raising=False)  # no bypass, no auth_manager -> fail closed
    app = WebApp(router=_StubRouter(), session=None).create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/ws/ui") as ws:
            msg = await ws.receive(timeout=5)
            assert msg.type == aiohttp.WSMsgType.CLOSE
            assert msg.data == 4001
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_ui_rejects_invalid_token_with_4001(monkeypatch):
    """Production auth path: auth_manager present + invalid X-Auth-Token -> 4001."""
    monkeypatch.delenv("SKY_CLAW_DEV_NO_AUTH", raising=False)
    auth = _StubAuth(valid_token="good-token")
    app = WebApp(router=_StubRouter(), session=None, auth_manager=auth).create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/ws/ui", headers={"X-Auth-Token": "WRONG"}) as ws:
            msg = await ws.receive(timeout=5)
            assert msg.type == aiohttp.WSMsgType.CLOSE
            assert msg.data == 4001
        assert "WRONG" in auth.calls  # validate() was actually exercised
    finally:
        await client.close()


class _BoomRouter(_StubRouter):
    async def chat(self, message, session, *, chat_id):
        raise RuntimeError("llm exploded")


@pytest.mark.asyncio
async def test_ws_ui_router_error_returns_warning_frame(monkeypatch):
    monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")
    app = WebApp(router=_BoomRouter(), session=None).create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.ws_connect("/ws/ui") as ws:
            await ws.send_json({"type": "command", "command": "chat", "payload": {"text": "x"}})
            reply = await ws.receive_json(timeout=5)
            assert reply["type"] == "response"
            assert "⚠️" in reply["payload"]["response"]
            # socket still usable: a second message also gets a frame
            await ws.send_json({"type": "command", "command": "chat", "payload": {"text": "y"}})
            reply2 = await ws.receive_json(timeout=5)
            assert reply2["type"] == "response"
    finally:
        await client.close()
