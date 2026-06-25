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
    monkeypatch.setattr(interface_mod.asyncio, "sleep", fake_sleep)

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
    monkeypatch.setattr(interface_mod.asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.DEBUG, logger="SkyClaw.Interface"), pytest.raises(asyncio.CancelledError):
        await agent.connect()

    records = _gateway_records(caplog)
    # After the connect (call #1), drops on calls #2 and #3 are logged.
    assert len(records) == 2
    assert records[0].levelno == logging.WARNING, "first drop after a connect must re-warn"
    assert records[1].levelno == logging.DEBUG
