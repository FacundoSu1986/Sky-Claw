"""Regression tests for the UI WebSocket auth-rejection close code.

PR #120 deliberately set the ``UIBroadcastServer`` auth gate to close code
4001 so that ``AgentCommunicationClient._AUTH_REJECTION_CLOSE_CODES`` could
recognise a genuine auth rejection and drive the 5-minute brute-force
lockout.  PR #128 silently changed the server to 1008 (POLICY_VIOLATION)
without touching the client — which still keys on 4001 — disabling the
lockout entirely.

These tests pin the server/client contract so the two sides cannot drift
apart again.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ws_daemon.py imports the agent-side ``ast_guardian`` module, resolved via a
# runtime sys.path append to an agent skills directory that is not present in
# the test/CI checkout.  Stub it so ws_daemon is importable.  The class under
# test (UIBroadcastServer) never touches it — only the unrelated
# TelegramDaemon does.
sys.modules.setdefault("ast_guardian", types.ModuleType("ast_guardian"))

from sky_claw.antigravity.comms.ws_daemon import UIBroadcastServer  # noqa: E402
from sky_claw.antigravity.gui.agent_communication import AgentCommunicationClient  # noqa: E402


def _make_server() -> UIBroadcastServer:
    """Build a UIBroadcastServer with a mocked AuthTokenManager."""
    with patch("sky_claw.antigravity.comms.ws_daemon.AuthTokenManager"):
        return UIBroadcastServer()


def test_auth_rejection_close_code_matches_client_contract() -> None:
    """The server's auth-rejection close code must be one the client treats
    as an auth rejection — otherwise the 5-minute lockout never engages."""
    assert UIBroadcastServer._AUTH_REJECTION_CLOSE_CODE in AgentCommunicationClient._AUTH_REJECTION_CLOSE_CODES


@pytest.mark.asyncio
async def test_auth_gate_closes_with_auth_rejection_code() -> None:
    """An invalid token closes the socket with the auth-rejection code (4001),
    not the policy-violation code (1008) reserved for rate limiting."""
    server = _make_server()
    server._auth.validate.return_value = False

    websocket = MagicMock()
    websocket.request_headers = {"X-Auth-Token": "invalid-token"}
    websocket.remote_address = ("127.0.0.1", 5555)
    websocket.close = AsyncMock()

    await server._handler(websocket)

    websocket.close.assert_awaited_once()
    close_code = websocket.close.await_args.args[0]
    assert close_code == 4001
    assert close_code == UIBroadcastServer._AUTH_REJECTION_CLOSE_CODE
