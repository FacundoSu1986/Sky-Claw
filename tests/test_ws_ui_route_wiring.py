"""Regression tests for the GUI↔daemon WebSocket wiring (HTTP 404 on /ws/ui).

The packaged exe logged `aiohttp.access ... "GET /ws/ui" 404` in a reconnect
loop because the daemon's aiohttp ``WebApp`` was built **without** an
``event_bus`` (so the Operations Hub WS route was never registered) and, even
when registered, mounts at ``/api/status`` while the GUI client
(``AgentCommunicationClient``) connects to ``/ws/ui``. Auth already matches
(both use ``X-Auth-Token``).

These pin the fix: ``WebApp`` accepts a ``ws_route_path`` so the bootloader can
mount the Operations Hub at ``/ws/ui`` (default stays ``/api/status`` so the
existing standalone tests are untouched), and ``SupervisorAgent`` exposes its
``event_bus`` so the bootloader can wire it in.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from sky_claw.antigravity.core.event_bus import CoreEventBus
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.antigravity.web.app import WebApp


@pytest.fixture
async def bus() -> CoreEventBus:
    b = CoreEventBus()
    await b.start()
    try:
        yield b
    finally:
        await b.stop()


@pytest.fixture
async def http_session() -> aiohttp.ClientSession:
    async with aiohttp.ClientSession() as s:
        yield s


async def _start_client(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_webapp_mounts_ws_at_ws_ui_when_configured(bus, http_session, monkeypatch):
    """With ws_route_path='/ws/ui' the GUI client path serves the WS (no 404)."""
    monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")  # this test covers routing, not auth
    web_app = WebApp(router=None, session=http_session, event_bus=bus, ws_route_path="/ws/ui")
    app = web_app.create_app()
    assert web_app.ops_hub_handler is not None  # route registered when event_bus is provided
    await web_app.ops_hub_handler.start()
    client = await _start_client(app)
    try:
        async with client.ws_connect("/ws/ui") as ws:
            frame = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=1.0))
            assert frame["event_type"] == "snapshot"
            assert frame["payload"]["connected"] is True
        # The old default path must no longer answer when remapped.
        resp = await client.get("/api/status")
        assert resp.status == 404
    finally:
        await web_app.ops_hub_handler.stop()
        await client.close()


@pytest.mark.asyncio
async def test_webapp_default_ws_route_path_unchanged(bus, http_session, monkeypatch):
    """Default (no ws_route_path) still mounts at /api/status — existing contract."""
    monkeypatch.setenv("SKY_CLAW_DEV_NO_AUTH", "1")
    web_app = WebApp(router=None, session=http_session, event_bus=bus)
    app = web_app.create_app()
    assert web_app.ops_hub_handler is not None  # route registered when event_bus is provided
    await web_app.ops_hub_handler.start()
    client = await _start_client(app)
    try:
        async with client.ws_connect("/api/status") as ws:
            frame = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=1.0))
            assert frame["event_type"] == "snapshot"
    finally:
        await web_app.ops_hub_handler.stop()
        await client.close()


@pytest.mark.parametrize("bad_path", ["", "ws/ui", "api/status"])
def test_webapp_rejects_non_absolute_ws_route_path(bad_path):
    """A misconfigured ws_route_path fails fast at construction, not deep in aiohttp."""
    with pytest.raises(ValueError, match="absolute path"):
        WebApp(router=None, session=None, ws_route_path=bad_path)  # type: ignore[arg-type]


@pytest.fixture
def mo2_root(tmp_path, monkeypatch):
    """Throwaway MO2 layout so SupervisorAgent.__init__ runs (mirrors construction test)."""
    monkeypatch.chdir(tmp_path)
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))
    return mo2


def test_supervisor_exposes_event_bus(mo2_root, tmp_path):
    """The bootloader needs the supervisor's bus to wire the Operations Hub WS."""
    sup = SupervisorAgent(path_validator=PathValidator(roots=[mo2_root, tmp_path]))
    assert sup.event_bus is sup._event_bus
    assert isinstance(sup.event_bus, CoreEventBus)
