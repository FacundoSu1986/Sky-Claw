"""Regression guard for InterfaceAgent Gateway reconnect log hygiene (Issue D).

When the Node.js Gateway (``ws://127.0.0.1:18789``) is not running, the daemon
reconnects forever — by design. But it logged a WARNING on *every* attempt,
flooding the startup log indefinitely. The fix keeps the reconnect loop intact
and only changes log levels: WARNING on the first drop (state change), DEBUG on
subsequent retries while still down, re-arming to WARNING after a reconnect.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from sky_claw.antigravity.comms import interface as interface_mod
from sky_claw.antigravity.comms.interface import InterfaceAgent


class _EmptyWS:
    """Async-iterable websocket stub that yields nothing (connection then closes)."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _AsyncioProxy:
    """Proxy to the real ``asyncio`` that overrides only ``sleep``.

    Swapping ``interface_mod.asyncio`` for this keeps the sleep patch local to
    the module under test, instead of mutating ``asyncio.sleep`` globally (which
    would affect every other coroutine on the loop during the test).
    """

    def __init__(self, sleep) -> None:
        self.sleep = sleep  # instance attr shadows the real asyncio.sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


def _gateway_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "Gateway perdido" in r.getMessage()]


async def test_reconnect_logs_warning_once_then_debug(monkeypatch, caplog):
    """First disconnect → WARNING; subsequent retries while down → DEBUG."""
    agent = InterfaceAgent(gateway_url="ws://127.0.0.1:18789")

    async def fake_connect(*_a, **_k):
        raise ConnectionRefusedError("[WinError 1225] connection refused")

    sleeps = {"n": 0}

    async def fake_sleep(_delay):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError  # break the infinite loop after 2 drops

    monkeypatch.setattr(interface_mod, "authenticated_connect", fake_connect)
    monkeypatch.setattr(interface_mod, "asyncio", _AsyncioProxy(fake_sleep))

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.Interface"), pytest.raises(asyncio.CancelledError):
        await agent.connect()

    records = _gateway_records(caplog)
    assert len(records) == 2, f"expected 2 reconnect logs, got {len(records)}"
    assert records[0].levelno == logging.WARNING
    assert records[1].levelno == logging.DEBUG


async def test_reconnect_rewarns_after_successful_connect(monkeypatch, caplog):
    """A successful connect re-arms the warning: the next drop is WARNING again."""
    agent = InterfaceAgent(gateway_url="ws://127.0.0.1:18789")

    calls = {"n": 0}

    async def fake_connect(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _EmptyWS()  # connects, then _listen returns immediately
        raise ConnectionRefusedError("[WinError 1225] connection refused")

    sleeps = {"n": 0}

    async def fake_sleep(_delay):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(interface_mod, "authenticated_connect", fake_connect)
    monkeypatch.setattr(interface_mod, "asyncio", _AsyncioProxy(fake_sleep))

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.Interface"), pytest.raises(asyncio.CancelledError):
        await agent.connect()

    records = _gateway_records(caplog)
    # After the connect (call #1), drops on calls #2 and #3 are logged.
    assert len(records) == 2
    assert records[0].levelno == logging.WARNING, "first drop after a connect must re-warn"
    assert records[1].levelno == logging.DEBUG


class _ScriptedWS:
    """WS stub que emite una lista fija de mensajes y luego cierra."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


async def test_listen_ignora_frame_no_json_sin_crashear():
    """M-5: un frame que no es JSON no debe propagar JSONDecodeError."""
    agent = InterfaceAgent(gateway_url="ws://127.0.0.1:18789")
    delivered: list[dict] = []

    async def _cb(data: dict) -> None:
        delivered.append(data)

    agent._command_callbacks = [_cb]

    agent.ws_connection = _ScriptedWS(["esto no es json {", '{"type": "EJECUTAR", "cmd": "x"}'])

    # No debe lanzar; el frame válido posterior sigue procesándose.
    await agent._listen_to_gateway()
    await asyncio.sleep(0.01)  # dejar correr el callback de EJECUTAR

    assert delivered and delivered[0]["cmd"] == "x"


async def test_listen_ignora_hitl_sin_request_id():
    """M-5: un hitl_response sin request_id no debe lanzar KeyError."""
    agent = InterfaceAgent(gateway_url="ws://127.0.0.1:18789")
    agent.ws_connection = _ScriptedWS(['{"type": "hitl_response", "decision": "approved"}'])

    # No debe lanzar.
    await agent._listen_to_gateway()


async def test_hitl_sin_decision_no_resuelve_la_espera():
    """T5 (review PR #257): un hitl_response con request_id pero sin decision NO despierta."""
    agent = InterfaceAgent(gateway_url="ws://127.0.0.1:18789")
    event = asyncio.Event()
    agent._pending_hitl["req-1"] = {"event": event, "decision": None}

    # Frame malformado: request_id válido, sin decision.
    agent.ws_connection = _ScriptedWS(['{"type": "hitl_response", "request_id": "req-1"}'])
    await agent._listen_to_gateway()

    # La espera NO debe haberse resuelto ni la decisión almacenada.
    assert not event.is_set()
    assert agent._pending_hitl["req-1"]["decision"] is None


async def test_hitl_decision_invalida_no_resuelve():
    """T5: una decision fuera del conjunto válido se ignora."""
    agent = InterfaceAgent(gateway_url="ws://127.0.0.1:18789")
    event = asyncio.Event()
    agent._pending_hitl["req-2"] = {"event": event, "decision": None}

    agent.ws_connection = _ScriptedWS(['{"type": "hitl_response", "request_id": "req-2", "decision": "maybe"}'])
    await agent._listen_to_gateway()

    assert not event.is_set()


async def test_hitl_decision_valida_resuelve():
    """T5: una decision válida sí resuelve la espera y almacena el valor."""
    agent = InterfaceAgent(gateway_url="ws://127.0.0.1:18789")
    event = asyncio.Event()
    agent._pending_hitl["req-3"] = {"event": event, "decision": None}

    agent.ws_connection = _ScriptedWS(['{"type": "hitl_response", "request_id": "req-3", "decision": "denied"}'])
    await agent._listen_to_gateway()

    assert event.is_set()
    assert agent._pending_hitl["req-3"]["decision"] == "denied"
