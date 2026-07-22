"""La attestation preview ocurre antes de esperar la decisión humana."""

from __future__ import annotations

import asyncio
from typing import Any

from sky_claw.antigravity.orchestrator.tool_strategies.middleware import HitlGateMiddleware
from sky_claw.antigravity.security.hitl import HITLGuard


async def test_prepare_for_approval_termina_antes_de_notificar_hitl() -> None:
    order: list[str] = []
    registered = asyncio.Event()
    captured: list[Any] = []

    class _Strategy:
        name = "execute_loot_sorting"

        async def prepare_for_approval(self, payload: dict[str, Any]) -> None:
            assert payload == {"profile_name": "Default"}
            order.append("prepared")

        def clear_approval_preparation(self, payload: dict[str, Any]) -> None:
            assert payload == {"profile_name": "Default"}
            order.append("cleared")

    async def notify(request: Any) -> None:
        order.append("notified")
        captured.append(request)
        registered.set()

    async def next_call() -> dict[str, Any]:
        order.append("executed")
        return {"status": "ok"}

    guard = HITLGuard(notify_fn=notify, timeout=2)
    gate = HitlGateMiddleware(hitl_guard=guard)
    pending = asyncio.create_task(gate(_Strategy(), {"profile_name": "Default"}, next_call))
    await asyncio.wait_for(registered.wait(), timeout=1)
    assert order == ["prepared", "notified"]

    await guard.respond(captured[0].request_id, approved=True)
    assert await pending == {"status": "ok"}
    assert order == ["prepared", "notified", "executed", "cleared"]
