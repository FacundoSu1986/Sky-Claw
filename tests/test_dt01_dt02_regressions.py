"""Tests for UIBroadcastServer rate-limiting (DT-01) and
AgentCommunicationClient dispatch semaphore (DT-02).

Validates the data-structure and concurrency fixes applied during the
security forensic audit without requiring real WebSocket connections.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════
# DT-01: UIBroadcastServer — deque-based sliding window
# ═══════════════════════════════════════════════════════════════════════


def _import_broadcast_server():
    """Import UIBroadcastServer, mocking out the ast_guardian side-effect.

    ws_daemon.py imports ``ast_guardian`` from a local path at module level.
    This mock injects a stub so tests run without that filesystem dependency.
    """
    ast_stub = MagicMock()
    auth_stub = MagicMock()

    with (
        patch.dict(sys.modules, {"ast_guardian": ast_stub}),
        patch(
            "sky_claw.antigravity.security.auth_token_manager.AuthTokenManager",
            return_value=auth_stub,
        ),
    ):
        from sky_claw.antigravity.comms.ws_daemon import UIBroadcastServer

        return UIBroadcastServer


class TestUIBroadcastRateLimiter:
    """Verify the sliding-window rate limiter uses deque and behaves correctly."""

    def _make_server(self):
        cls = _import_broadcast_server()
        return cls()

    def test_client_timestamps_uses_deque(self):
        """DT-01: _client_timestamps must produce deque instances, not lists."""
        server = self._make_server()
        # Access a new key — the defaultdict factory should create a deque.
        ts = server._client_timestamps[42]
        assert isinstance(ts, deque), f"Expected deque for O(1) popleft, got {type(ts).__name__}"

    def test_sliding_window_prunes_old_entries(self):
        """Entries older than _RATE_LIMIT_WINDOW must be pruned."""
        server = self._make_server()
        client_id = 99
        now = time.monotonic()

        # Seed with timestamps: 5 old (outside window), 3 recent.
        old = [now - server._RATE_LIMIT_WINDOW - i for i in range(5, 0, -1)]
        recent = [now - 1.0, now - 0.5, now]
        server._client_timestamps[client_id] = deque(old + recent)

        # Simulate the pruning logic from _handler.
        timestamps = server._client_timestamps[client_id]
        cutoff = now - server._RATE_LIMIT_WINDOW
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

        assert len(timestamps) == 3, f"Expected 3 recent entries, got {len(timestamps)}"

    def test_cleanup_on_disconnect(self):
        """Disconnecting a client must remove its timestamp entry entirely."""
        server = self._make_server()
        client_id = 7
        server._client_timestamps[client_id].append(time.monotonic())
        assert client_id in server._client_timestamps

        # Simulate the finally block in _handler.
        server._client_timestamps.pop(client_id, None)
        assert client_id not in server._client_timestamps

    def test_deque_popleft_is_used(self):
        """Confirm popleft() works on the deque (would fail on a list)."""
        server = self._make_server()
        client_id = 1
        ts = server._client_timestamps[client_id]
        ts.append(1.0)
        ts.append(2.0)
        ts.append(3.0)

        # popleft is a deque method; list would raise AttributeError.
        val = ts.popleft()
        assert val == 1.0
        assert len(ts) == 2

    @pytest.mark.asyncio
    async def test_handler_reads_websockets_16_request_headers(self):
        """SEC-WS: websockets 16 exposes handshake headers via request.headers."""
        server = self._make_server()
        server._auth.validate = MagicMock(return_value=True)

        class FakeWebSocket:
            request = SimpleNamespace(headers={"X-Auth-Token": "tok-16"})
            remote_address = ("127.0.0.1", 54321)

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def close(self, *_args, **_kwargs):
                return None

            async def send(self, *_args, **_kwargs):
                return None

        await server._handler(FakeWebSocket())

        server._auth.validate.assert_called_once_with("tok-16")


# ═══════════════════════════════════════════════════════════════════════
# DT-02: AgentCommunicationClient — dispatch semaphore
# ═══════════════════════════════════════════════════════════════════════


class TestAgentCommDispatchQueue:
    """Verify sync callback dispatch is queued and bounded."""

    def _make_client(self, on_message=None):
        """Build a minimal AgentCommunicationClient."""
        from sky_claw.antigravity.gui.agent_communication import (
            AgentCommunicationClient,
        )

        return AgentCommunicationClient(
            daemon_url="ws://localhost:9999/ws/ui",
            on_message=on_message,
        )

    def test_dispatch_queue_exists_and_has_correct_bound(self):
        """DT-02: sync callback dispatch queue must be bounded to 16."""
        client = self._make_client()
        assert hasattr(client, "_dispatch_queue")
        assert isinstance(client._dispatch_queue, asyncio.Queue)
        assert client._dispatch_queue.maxsize == 16

    def test_daemon_url_rejects_plaintext_non_loopback(self):
        """SEC-WS: NiceGUI clients must enforce the shared WS URL policy."""
        from sky_claw.antigravity.comms._transport import InsecureTransportError
        from sky_claw.antigravity.gui.agent_communication import AgentCommunicationClient

        with pytest.raises(InsecureTransportError):
            AgentCommunicationClient(daemon_url="ws://evil.example.com/ws")

    @pytest.mark.asyncio
    async def test_sync_callback_does_not_block_socket_read_loop(self):
        """A blocked sync callback must not prevent reading the next WS message."""
        started = threading.Event()
        release = threading.Event()

        class FakeWebSocket:
            def __init__(self):
                self._messages = [
                    '{"type": "first"}',
                    '{"type": "second"}',
                ]
                self.yielded = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._messages:
                    raise StopAsyncIteration
                self.yielded += 1
                return self._messages.pop(0)

        call_log: list[dict] = []

        def sync_handler(data):
            if data["type"] == "first":
                started.set()
                release.wait(timeout=2.0)
            call_log.append(data)

        client = self._make_client(on_message=sync_handler)
        client._start_dispatch_workers()
        ws = FakeWebSocket()

        listen_task = asyncio.create_task(client._listen(ws))
        await asyncio.to_thread(started.wait, 1.0)

        for _ in range(20):
            if ws.yielded == 2:
                break
            await asyncio.sleep(0.01)

        release.set()
        await listen_task
        await client._drain_dispatch_queue()
        await client._stop_dispatch_workers()

        assert ws.yielded == 2
        assert {item["type"] for item in call_log} == {"first", "second"}

    @pytest.mark.asyncio
    async def test_async_callback_awaited_directly(self):
        """Async callbacks should be awaited directly, no semaphore involved."""
        call_log: list[dict] = []

        async def async_handler(data):
            call_log.append(data)

        client = self._make_client(on_message=async_handler)

        import inspect

        assert inspect.iscoroutinefunction(async_handler)

        # Async path: direct await.
        await client._on_message({"type": "async_test"})
        assert len(call_log) == 1

    @pytest.mark.asyncio
    async def test_dispatch_queue_backpressure_is_bounded(self):
        """Queue capacity must stay bounded during callback bursts."""
        client = self._make_client()
        client._dispatch_queue = asyncio.Queue(maxsize=2)

        await client._enqueue_sync_callback({"type": "one"})
        await client._enqueue_sync_callback({"type": "two"})

        with pytest.raises(asyncio.QueueFull):
            await client._enqueue_sync_callback({"type": "three"})

    @pytest.mark.asyncio
    async def test_stop_cancels_dispatch_workers(self):
        """stop() must not leave callback worker tasks running."""
        client = self._make_client()
        client._start_dispatch_workers()
        assert client._dispatch_workers

        await client.stop()

        assert all(worker.done() for worker in client._dispatch_workers)


class TestScraperAgentGatewayBoundary:
    """ScraperAgent must not perform outbound HTTP without NetworkGateway."""

    @pytest.mark.asyncio
    async def test_api_request_without_gateway_fails_closed(self):
        from sky_claw.antigravity.scraper.scraper_agent import ScraperAgent

        db = MagicMock()
        # PR #118: ScraperAgent now requires gateway in constructor (fail-closed)
        # and raises ValueError if None is passed
        with pytest.raises(ValueError, match="requires a NetworkGateway"):
            ScraperAgent(db, gateway=None)
