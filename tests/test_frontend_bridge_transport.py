"""FrontendBridge — wiring del límite de tamaño de mensaje WS.

El bridge usa ``websockets.connect`` directo (auth por primer mensaje, no por
header). Debe pasar ``max_size`` explícito — el mismo contrato de 10 MiB que
``authenticated_connect`` aplica al resto de los clientes WS.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from sky_claw.antigravity.comms import frontend_bridge as fb
from sky_claw.antigravity.comms._transport import DEFAULT_MAX_MESSAGE_BYTES
from tests.polling_utils import poll_until


class _RefusingCtx:
    """Async ctx manager que simula gateway caído (ConnectionRefusedError)."""

    async def __aenter__(self) -> None:
        raise ConnectionRefusedError("no gateway")

    async def __aexit__(self, *args: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_bridge_passes_max_size_to_websockets_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_connect(url: str, **kwargs: object) -> _RefusingCtx:
        captured["url"] = url
        captured.update(kwargs)
        return _RefusingCtx()

    monkeypatch.setattr(fb.websockets, "connect", fake_connect)

    bridge = fb.FrontendBridge(
        router=MagicMock(),
        session=MagicMock(),
        config=MagicMock(),
        app_context=MagicMock(),
        keyring_client=MagicMock(),
    )
    task = asyncio.create_task(bridge.start(), name="bridge-start-probe")
    try:
        await poll_until(
            lambda: "url" in captured,
            timeout=3.0,
            msg="el bridge debe intentar conectar via websockets.connect",
        )
    finally:
        bridge._is_running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert captured["max_size"] == DEFAULT_MAX_MESSAGE_BYTES
