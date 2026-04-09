"""Tests for the HITL wiring: AppContext, TelegramWebhook commands, end-to-end flow."""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.comms.telegram import TelegramWebhook, _parse_hitl_command
from sky_claw.comms.telegram_sender import TelegramSender
from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import SyncEngine
from sky_claw.scraper.masterlist import MasterlistClient
from sky_claw.scraper.nexus_downloader import FileInfo, NexusDownloader
from sky_claw.security.hitl import Decision, HITLGuard
from sky_claw.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.security.path_validator import PathValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(update_id: int, chat_id: int = 999, text: str = "hello") -> dict:
    return {
        "update_id": update_id,
        "message": {"text": text, "chat": {"id": chat_id}, "from": {"id": chat_id}},
    }


def _make_sender() -> AsyncMock:
    sender = MagicMock(spec=TelegramSender)
    sender.send = AsyncMock()
    return sender


def _make_router(response: str = "ok") -> MagicMock:
    router = MagicMock()
    router.chat = AsyncMock(return_value=response)
    return router


def _make_webhook(
    hitl: HITLGuard | None = None,
    router_response: str = "llm response",
    authorized_user_id: int | None = 999,
) -> tuple[TelegramWebhook, MagicMock, AsyncMock]:
    router = _make_router(router_response)
    sender = _make_sender()
    session = MagicMock(spec=aiohttp.ClientSession)
    webhook = TelegramWebhook(
        router=router,
        sender=sender,
        session=session,
        hitl=hitl,
        authorized_user_id=authorized_user_id,
    )
    return webhook, router, sender


def _make_file_info(
    nexus_id: int = 10,
    file_id: int = 20,
    file_name: str = "mod.zip",
) -> FileInfo:
    return FileInfo(
        nexus_id=nexus_id,
        file_id=file_id,
        file_name=file_name,
        size_bytes=1024,
        md5="",
        download_url=f"https://premium-files.nexusmods.com/{file_name}",
    )


# ---------------------------------------------------------------------------
# _parse_hitl_command
# ---------------------------------------------------------------------------


class TestParseHITLCommand:
    def test_approve_returns_true_and_id(self) -> None:
        result = _parse_hitl_command("/approve download-10-20")
        assert result == (True, "download-10-20")

    def test_deny_returns_false_and_id(self) -> None:
        result = _parse_hitl_command("/deny download-10-20")
        assert result == (False, "download-10-20")

    def test_approve_strips_whitespace(self) -> None:
        result = _parse_hitl_command("  /approve   my-req-id  ")
        assert result == (True, "my-req-id")

    def test_deny_strips_whitespace(self) -> None:
        result = _parse_hitl_command("  /deny   my-req-id  ")
        assert result == (False, "my-req-id")

    def test_regular_text_returns_none(self) -> None:
        assert _parse_hitl_command("hello world") is None

    def test_approve_without_id_returns_none(self) -> None:
        assert _parse_hitl_command("/approve") is None

    def test_approve_empty_id_returns_none(self) -> None:
        assert _parse_hitl_command("/approve   ") is None

    def test_deny_without_id_returns_none(self) -> None:
        assert _parse_hitl_command("/deny") is None

    def test_deny_empty_id_returns_none(self) -> None:
        assert _parse_hitl_command("/deny   ") is None

    def test_id_with_spaces_preserved(self) -> None:
        result = _parse_hitl_command("/approve some complex id")
        assert result == (True, "some complex id")

    def test_unrelated_slash_command_returns_none(self) -> None:
        assert _parse_hitl_command("/start") is None
        assert _parse_hitl_command("/help") is None


# ---------------------------------------------------------------------------
# TelegramWebhook — HITL command routing
# ---------------------------------------------------------------------------


class TestWebhookHITLApprove:
    @pytest.mark.asyncio
    async def test_approve_found_calls_respond_and_confirms(
        self, aiohttp_client
    ) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        webhook, router, sender = _make_webhook(hitl=guard)

        # Register a pending request manually.
        req_task = asyncio.create_task(
            guard.request_approval("download-10-20", "test")
        )
        await asyncio.sleep(0)  # Let the coroutine register the request.

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        resp = await client.post(
            "/webhook", json=_make_update(1, text="/approve download-10-20")
        )
        assert resp.status == 200
        await asyncio.sleep(0.05)

        decision = await req_task
        assert decision is Decision.APPROVED
        sender.send.assert_awaited_once_with(999, "Request 'download-10-20' approved.")

    @pytest.mark.asyncio
    async def test_deny_found_calls_respond_and_confirms(
        self, aiohttp_client
    ) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        webhook, router, sender = _make_webhook(hitl=guard)

        req_task = asyncio.create_task(
            guard.request_approval("download-5-6", "test")
        )
        await asyncio.sleep(0)

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        await client.post("/webhook", json=_make_update(2, text="/deny download-5-6"))
        await asyncio.sleep(0.05)

        decision = await req_task
        assert decision is Decision.DENIED
        sender.send.assert_awaited_once_with(999, "Request 'download-5-6' denied.")

    @pytest.mark.asyncio
    async def test_approve_unknown_id_sends_not_found(self, aiohttp_client) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        webhook, router, sender = _make_webhook(hitl=guard)

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        await client.post(
            "/webhook", json=_make_update(3, text="/approve nonexistent-id")
        )
        await asyncio.sleep(0.05)

        router.chat.assert_not_awaited()
        sender.send.assert_awaited_once()
        msg = sender.send.call_args[0][1]
        assert "No pending" in msg
        assert "nonexistent-id" in msg

    @pytest.mark.asyncio
    async def test_deny_unknown_id_sends_not_found(self, aiohttp_client) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        webhook, router, sender = _make_webhook(hitl=guard)

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        await client.post(
            "/webhook", json=_make_update(4, text="/deny ghost-req")
        )
        await asyncio.sleep(0.05)

        router.chat.assert_not_awaited()
        msg = sender.send.call_args[0][1]
        assert "No pending" in msg

    @pytest.mark.asyncio
    async def test_hitl_command_does_not_reach_llm_router(
        self, aiohttp_client
    ) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        webhook, router, sender = _make_webhook(hitl=guard)

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        await client.post(
            "/webhook", json=_make_update(5, text="/approve some-req")
        )
        await asyncio.sleep(0.05)

        router.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_regular_text_still_routes_to_llm(self, aiohttp_client) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        webhook, router, sender = _make_webhook(hitl=guard, router_response="42 mods")

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        await client.post("/webhook", json=_make_update(6, text="search Requiem"))
        await asyncio.sleep(0.05)

        router.chat.assert_awaited_once()
        sender.send.assert_awaited_once_with(999, "42 mods")

    @pytest.mark.asyncio
    async def test_approve_without_hitl_falls_through_to_llm(
        self, aiohttp_client
    ) -> None:
        """When hitl=None, /approve is treated as normal text by the LLM."""
        webhook, router, sender = _make_webhook(hitl=None)

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        await client.post(
            "/webhook", json=_make_update(7, text="/approve some-req")
        )
        await asyncio.sleep(0.05)

        router.chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approve_with_empty_id_falls_through_to_llm(
        self, aiohttp_client
    ) -> None:
        """'/approve' with no ID is not a valid HITL command — goes to LLM."""
        guard = HITLGuard(notify_fn=None, timeout=5)
        webhook, router, sender = _make_webhook(hitl=guard)

        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        await client.post("/webhook", json=_make_update(8, text="/approve"))
        await asyncio.sleep(0.05)

        router.chat.assert_awaited_once()


# ---------------------------------------------------------------------------
# HITLGuard notify_fn (the closure built in AppContext.start)
# ---------------------------------------------------------------------------


class TestHITLNotifyFn:
    @pytest.mark.asyncio
    async def test_notify_sends_to_operator_chat(self) -> None:
        sender = _make_sender()
        operator_chat_id = 777

        # Replicate the closure logic from AppContext.start().
        async def _hitl_notify(req: Any) -> None:
            msg = (
                f"HITL Approval Required\n\n"
                f"{req.reason}\n\n"
                f"{req.detail}\n\n"
                f"Approve: /approve {req.request_id}\n"
                f"Deny:    /deny {req.request_id}"
            )
            await sender.send(operator_chat_id, msg)

        guard = HITLGuard(notify_fn=_hitl_notify, timeout=5)

        # Trigger request but immediately respond so it doesn't block.
        async def _respond() -> None:
            await asyncio.sleep(0.01)
            await guard.respond("req-notify-test", approved=True)

        asyncio.create_task(_respond())
        await guard.request_approval("req-notify-test", "Download mod X", detail="file.zip")

        sender.send.assert_awaited_once()
        call = sender.send.call_args
        assert call[0][0] == operator_chat_id
        msg = call[0][1]
        assert "HITL Approval Required" in msg
        assert "/approve req-notify-test" in msg
        assert "/deny req-notify-test" in msg

    @pytest.mark.asyncio
    async def test_notify_noop_when_sender_is_none(self) -> None:
        """notify_fn must not raise when sender is None."""
        _sender: TelegramSender | None = None

        async def _hitl_notify(req: Any) -> None:
            if _sender is None:
                return
            await _sender.send(0, "msg")

        guard = HITLGuard(notify_fn=_hitl_notify, timeout=5)

        async def _respond() -> None:
            await asyncio.sleep(0.01)
            await guard.respond("safe-req", approved=False)

        asyncio.create_task(_respond())
        decision = await guard.request_approval("safe-req", "test")
        assert decision is Decision.DENIED

    @pytest.mark.asyncio
    async def test_notify_noop_when_operator_chat_id_is_none(self) -> None:
        """notify_fn must not raise when operator_chat_id is None."""
        sender = _make_sender()
        operator_chat_id: int | None = None

        async def _hitl_notify(req: Any) -> None:
            if sender is None or operator_chat_id is None:
                return
            await sender.send(operator_chat_id, "msg")

        guard = HITLGuard(notify_fn=_hitl_notify, timeout=5)

        async def _respond() -> None:
            await asyncio.sleep(0.01)
            await guard.respond("no-chat-req", approved=True)

        asyncio.create_task(_respond())
        decision = await guard.request_approval("no-chat-req", "test")

        assert decision is Decision.APPROVED
        sender.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_crash_request_approval(self) -> None:
        """If the notify_fn raises, request_approval still completes."""
        async def _bad_notify(req: Any) -> None:
            raise RuntimeError("Telegram is down")

        guard = HITLGuard(notify_fn=_bad_notify, timeout=5)

        async def _respond() -> None:
            await asyncio.sleep(0.05)
            await guard.respond("bad-notify-req", approved=True)

        asyncio.create_task(_respond())
        # Should propagate the exception from notify_fn — callers wrap it.
        with pytest.raises(RuntimeError, match="Telegram is down"):
            await guard.request_approval("bad-notify-req", "test")


# ---------------------------------------------------------------------------
# AppContext wiring — unit-level checks without starting a real server
# ---------------------------------------------------------------------------


class TestAppContextWiring:
    @pytest.mark.asyncio
    async def test_hitl_and_downloader_wired_when_keys_set(
        self, tmp_path: pathlib.Path
    ) -> None:
        """start() wires hitl + downloader when NEXUS_API_KEY is set."""
        import argparse
        from sky_claw.__main__ import AppContext

        args = argparse.Namespace(
            db_path=tmp_path / "test.db",
            mo2_root=tmp_path,
            loot_exe=pathlib.Path("loot.exe"),
            operator_chat_id=None,
            staging_dir=tmp_path / "staging",
            provider=None,
            xedit_exe=None,
            install_dir=tmp_path / "tools",
        )

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "test-key",
                "NEXUS_API_KEY": "nexus-key",
                "TELEGRAM_BOT_TOKEN": "",
            },
        ):
            ctx = AppContext(args)
            await ctx.start()

        try:
            assert ctx.hitl is not None, "HITLGuard should always be created"
            assert ctx.network.downloader is not None, "NexusDownloader should be created when NEXUS_API_KEY is set"
            assert ctx.network.downloader.staging_dir == tmp_path / "staging"
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_downloader_is_none_without_nexus_key(
        self, tmp_path: pathlib.Path
    ) -> None:
        """start() leaves downloader=None when NEXUS_API_KEY is absent."""
        import argparse
        from sky_claw.__main__ import AppContext

        args = argparse.Namespace(
            db_path=tmp_path / "test.db",
            mo2_root=tmp_path,
            loot_exe=pathlib.Path("loot.exe"),
            operator_chat_id=None,
            staging_dir=tmp_path / "staging",
            provider=None,
            xedit_exe=None,
            install_dir=tmp_path / "tools",
        )

        clean_config = tmp_path / "config.toml"
        clean_config.write_text("")

        with patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "test-key", "NEXUS_API_KEY": "", "TELEGRAM_BOT_TOKEN": ""},
        ), patch("keyring.get_password", return_value=None), \
           patch("keyring.set_password"):
            ctx = AppContext(args)
            await ctx.start_minimal()
            ctx.config_path = clean_config
            await ctx.start_full()

        try:
            assert ctx.network.downloader is None
            assert ctx.hitl is not None
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_sender_created_when_bot_token_set(
        self, tmp_path: pathlib.Path
    ) -> None:
        import argparse
        from sky_claw.__main__ import AppContext

        args = argparse.Namespace(
            db_path=tmp_path / "test.db",
            mo2_root=tmp_path,
            loot_exe=pathlib.Path("loot.exe"),
            operator_chat_id=None,
            staging_dir=tmp_path / "staging",
            provider=None,
            xedit_exe=None,
            install_dir=tmp_path / "tools",
        )

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "test-key",
                "NEXUS_API_KEY": "",
                "TELEGRAM_BOT_TOKEN": "123:TOKEN",
            },
        ):
            ctx = AppContext(args)
            await ctx.start()

        try:
            assert ctx.sender is not None
        finally:
            await ctx.stop()

    @pytest.mark.asyncio
    async def test_sender_none_without_bot_token(
        self, tmp_path: pathlib.Path
    ) -> None:
        import argparse
        from sky_claw.__main__ import AppContext

        args = argparse.Namespace(
            db_path=tmp_path / "test.db",
            mo2_root=tmp_path,
            loot_exe=pathlib.Path("loot.exe"),
            operator_chat_id=None,
            staging_dir=tmp_path / "staging",
            provider=None,
            xedit_exe=None,
            install_dir=tmp_path / "tools",
        )

        clean_config = tmp_path / "config.toml"
        clean_config.write_text("")

        with patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "test-key", "NEXUS_API_KEY": "", "TELEGRAM_BOT_TOKEN": ""},
        ), patch("keyring.get_password", return_value=None), \
           patch("keyring.set_password"):
            ctx = AppContext(args)
            await ctx.start_minimal()
            ctx.config_path = clean_config
            await ctx.start_full()

        try:
            assert ctx.sender is None
        finally:
            await ctx.stop()


# ---------------------------------------------------------------------------
# Argparse — new flags
# ---------------------------------------------------------------------------


class TestArgparse:
    def test_operator_chat_id_parsed(self) -> None:
        from sky_claw.__main__ import _parse_args

        args = _parse_args(["--operator-chat-id", "12345"])
        assert args.operator_chat_id == 12345

    def test_operator_chat_id_defaults_to_none(self) -> None:
        from sky_claw.__main__ import _parse_args

        with patch.dict("os.environ", {"SKY_CLAW_OPERATOR_CHAT_ID": ""}):
            args = _parse_args([])
        assert args.operator_chat_id is None

    def test_operator_chat_id_from_env(self) -> None:
        from sky_claw.__main__ import _parse_args

        with patch.dict("os.environ", {"SKY_CLAW_OPERATOR_CHAT_ID": "99"}):
            args = _parse_args([])
        assert args.operator_chat_id == 99

    def test_staging_dir_parsed(self) -> None:
        from sky_claw.__main__ import _parse_args

        args = _parse_args(["--staging-dir", "/tmp/mods"])
        assert args.staging_dir == pathlib.Path("/tmp/mods")

    def test_staging_dir_from_env(self) -> None:
        from sky_claw.__main__ import _parse_args

        with patch.dict("os.environ", {"SKY_CLAW_STAGING_DIR": "/env/staging"}):
            args = _parse_args([])
        assert args.staging_dir == pathlib.Path("/env/staging")


# ---------------------------------------------------------------------------
# End-to-end HITL flow: download request → notify → /approve → enqueued
# ---------------------------------------------------------------------------


class TestEndToEndHITLFlow:
    @pytest.mark.asyncio
    async def test_download_request_then_approve_enqueues(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        """Full flow: tool calls request_approval → webhook delivers /approve → enqueued."""
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.security.path_validator import PathValidator

        # ---- Setup ----
        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

        db = AsyncModRegistry(db_path=tmp_path / "e2e.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        # HITLGuard with a sender mock as notify_fn.
        operator_chat_id = 555
        notifications: list[str] = []

        async def _notify(req: Any) -> None:
            notifications.append(req.request_id)

        guard = HITLGuard(notify_fn=_notify, timeout=10)

        downloader = NexusDownloader(
            api_key="nexus-key",
            gateway=gw,
            staging_dir=tmp_path / "staging",
        )
        tool_registry = AsyncToolRegistry(
            registry=db,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=guard,
            downloader=downloader,
        )

        sender = _make_sender()
        router = _make_router()
        session = MagicMock(spec=aiohttp.ClientSession)
        webhook = TelegramWebhook(
            router=router,
            sender=sender,
            session=session,
            hitl=guard,
            authorized_user_id=operator_chat_id,
        )
        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        fi = _make_file_info(nexus_id=42, file_id=7)

        # Capture what gets enqueued.
        enqueued: list[Any] = []

        def _fake_enqueue(coro: Any, **kwargs) -> asyncio.Task:
            task = asyncio.create_task(coro)
            enqueued.append(task)
            return task

        # ---- Trigger tool call in a background task ----
        tool_result: dict[str, Any] = {}

        async def _run_tool() -> None:
            with patch("sky_claw.agent.tools.nexus_tools.aiohttp.ClientSession") as mock_cls:
                mock_sess = AsyncMock()
                mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
                mock_sess.__aexit__ = AsyncMock(return_value=False)
                mock_cls.return_value = mock_sess

                with patch.object(downloader, "get_file_info", return_value=fi):
                    with patch.object(sync_engine, "enqueue_download", side_effect=_fake_enqueue):
                        result_str = await tool_registry.execute(
                            "download_mod", {"nexus_id": 42, "file_id": 7}
                        )
            tool_result.update(json.loads(result_str))

        tool_task = asyncio.create_task(_run_tool())

        # Let the tool reach request_approval and register the pending request.
        await asyncio.sleep(0.05)

        # ---- Operator sends /approve via Telegram ----
        expected_request_id = "download-42-7"
        await client.post(
            "/webhook",
            json=_make_update(100, chat_id=operator_chat_id, text=f"/approve {expected_request_id}"),
        )
        await asyncio.sleep(0.1)

        # ---- Await tool completion ----
        await tool_task

        # ---- Assertions ----
        assert tool_result["status"] == "enqueued", f"Got: {tool_result}"
        assert tool_result["nexus_id"] == 42
        assert tool_result["file_id"] == 7
        assert len(enqueued) == 1

        # Notification was sent.
        assert expected_request_id in notifications

        # Confirmation was sent to operator.
        sender.send.assert_awaited()
        confirm_calls = [c for c in sender.send.call_args_list if "approved" in str(c)]
        assert confirm_calls, "Operator should receive an 'approved' confirmation"

        await db.close()

    @pytest.mark.asyncio
    async def test_download_request_then_deny_returns_denied(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        """Flow: tool calls request_approval → /deny → tool returns denied status."""
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.security.path_validator import PathValidator

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

        db = AsyncModRegistry(db_path=tmp_path / "e2e_deny.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        guard = HITLGuard(notify_fn=None, timeout=10)
        downloader = NexusDownloader(
            api_key="nexus-key", gateway=gw, staging_dir=tmp_path / "staging"
        )
        tool_registry = AsyncToolRegistry(
            registry=db, mo2=mo2, sync_engine=sync_engine, hitl=guard, downloader=downloader
        )

        sender = _make_sender()
        webhook = TelegramWebhook(
            router=_make_router(), sender=sender,
            session=MagicMock(spec=aiohttp.ClientSession), hitl=guard,
            authorized_user_id=999,
        )
        app = aiohttp.web.Application()
        app.router.add_post("/webhook", webhook.handle_update)
        client = await aiohttp_client(app)

        fi = _make_file_info(nexus_id=1, file_id=2)
        tool_result: dict[str, Any] = {}

        async def _run_tool() -> None:
            with patch("sky_claw.agent.tools.nexus_tools.aiohttp.ClientSession") as mock_cls:
                mock_sess = AsyncMock()
                mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
                mock_sess.__aexit__ = AsyncMock(return_value=False)
                mock_cls.return_value = mock_sess
                with patch.object(downloader, "get_file_info", return_value=fi):
                    result_str = await tool_registry.execute(
                        "download_mod", {"nexus_id": 1, "file_id": 2}
                    )
            tool_result.update(json.loads(result_str))

        tool_task = asyncio.create_task(_run_tool())
        await asyncio.sleep(0.05)

        await client.post(
            "/webhook",
            json=_make_update(200, text="/deny download-1-2"),
        )
        await asyncio.sleep(0.1)
        await tool_task

        assert tool_result["status"] == "denied"
        assert tool_result["decision"] == "denied"

        await db.close()
