"""M-4: TelegramWebhook debe recibir authorized_user_id en --mode telegram y en
el hot-reload; sin él, _validate_sender falla cerrado y bloquea todo HITL.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.comms.telegram_sender import TelegramMessage


class TestTelegramModeAuthorizedUser:
    async def test_run_telegram_pasa_authorized_user_id(self) -> None:
        from sky_claw.app.modes import telegram_mode

        ctx = MagicMock()
        ctx.router = MagicMock()
        ctx.session = MagicMock()
        ctx.network.gateway = MagicMock()
        ctx.sender = MagicMock()
        ctx.sender._token = "123:ABC"
        ctx.hitl = MagicMock()
        ctx._args.operator_chat_id = 987654

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()
        fake_polling.stop = AsyncMock()

        with (
            patch.object(telegram_mode, "TelegramWebhook") as mock_webhook,
            patch.object(telegram_mode, "TelegramPolling", return_value=fake_polling),
        ):
            task = asyncio.create_task(telegram_mode._run_telegram(ctx, "127.0.0.1", 0))
            await asyncio.sleep(0.05)  # dejar que construya el webhook y arranque
            task.cancel()
            # _run_telegram captura CancelledError internamente y retorna limpio.
            await task

        assert mock_webhook.call_args.kwargs["authorized_user_id"] == 987654


class TestReloadTelegramAuthorizedUser:
    async def test_reload_pasa_authorized_user_id_como_int(self) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge

        bridge = FrontendBridge.__new__(FrontendBridge)
        ctx = MagicMock()
        ctx.polling = None
        ctx.router = MagicMock()
        ctx.session = MagicMock()
        ctx.network.gateway = MagicMock()
        ctx.hitl = MagicMock()
        bridge.ctx = ctx  # type: ignore[attr-defined]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()

        with (
            patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
            patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling),
            patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
        ):
            ok = await bridge._reload_telegram(token="123:ABC", chat_id="55501")

        assert ok is True
        # chat_id (str) debe convertirse a int para _validate_sender.
        assert mock_webhook.call_args.kwargs["authorized_user_id"] == 55501

    async def test_reload_sin_chat_id_usa_none(self) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge

        bridge = FrontendBridge.__new__(FrontendBridge)
        ctx = MagicMock()
        ctx.polling = None
        bridge.ctx = ctx  # type: ignore[attr-defined]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()

        with (
            patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
            patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling),
            patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
        ):
            ok = await bridge._reload_telegram(token="123:ABC", chat_id="")

        assert ok is True
        assert mock_webhook.call_args.kwargs["authorized_user_id"] is None


def _build_full_start_ctx(tmp_path: Path, *, mode: str, cli_operator: object) -> object:
    """Construye un AppContext aislado listo para ejecutar ``start_full()``."""
    from sky_claw.app_context import AppContext

    mo2_dir = tmp_path / "MO2"
    staging_dir = tmp_path / "staging"
    mo2_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    args = argparse.Namespace(
        db_path=str(tmp_path / "app.db"),
        mo2_root=mo2_dir,
        staging_dir=staging_dir,
        provider="ollama",
        operator_chat_id=cli_operator,
        loot_exe=None,
        install_dir=None,
        mode=mode,
    )
    ctx = AppContext(args)
    ctx.config_path = tmp_path / "config.toml"
    ctx._resolve_config_path = MagicMock()
    ctx._migrate_legacy_json = MagicMock()
    ctx.network.initialize = AsyncMock()
    ctx.network.close = AsyncMock()
    ctx.network.gateway = MagicMock()
    ctx.network.session = MagicMock(spec=aiohttp.ClientSession)
    ctx.database.initialize = AsyncMock()
    ctx.database.close = AsyncMock()
    ctx.database.registry = MagicMock()
    ctx.database.registry.is_empty = AsyncMock(return_value=False)
    return ctx


async def _start_real_ctx(
    tmp_path: Path,
    *,
    mode: str,
    cli_operator: object,
    config_chat_id: object,
) -> tuple[object, list[dict[str, object]], MagicMock, list[object]]:
    """Arranca ``AppContext.start_full()`` y registra exactamente qué identidad reciben los consumidores."""
    ctx = _build_full_start_ctx(tmp_path, mode=mode, cli_operator=cli_operator)
    sender_mock = MagicMock()
    sender_mock._token = "123:ABC"
    sender_mock.send = AsyncMock(return_value=TelegramMessage(chat_id=1, message_id=1))
    sender_mock.edit_message = AsyncMock(return_value=True)
    sender_mock.answer_callback_query = AsyncMock(return_value=True)

    notify_calls: list[dict[str, object]] = []

    class FakeHITL:
        def __init__(self, *, notify_fn=None, **_kwargs: object) -> None:
            notify_calls.append({"notify_fn": notify_fn})
            self.notify_operator = AsyncMock()
            self.respond = AsyncMock(return_value=True)
            self.terminal_decision = AsyncMock(return_value=None)

    class FakeLock:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def get_lock_info(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeSnapshot:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

    class FakeJournal:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def open(self) -> None:
            return None

        async def consultar_handoff_activo(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeRouter:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

    local_cfg = SimpleNamespace(
        mo2_root=str(tmp_path / "MO2"),
        skyrim_path=str(tmp_path),
        llm_provider="ollama",
        ollama_api_key="",
        ollama_model="",
        llm_api_key="",
        nexus_api_key="",
        telegram_bot_token="123:ABC",
        telegram_chat_id=config_chat_id,
        loot_exe="",
        xedit_exe="",
        pandora_exe="",
        bodyslide_exe="",
        install_dir="",
        allowed_tools=None,
        dyndolod_backend="standalone",
        golden_master_storage_root="",
        golden_master_mo2_instance_dir="",
        golden_master_clone_id="",
        golden_master_transition_policy="",
        golden_master_managed_source_root="",
        golden_master_outgoing_manual_runtime_root="",
        golden_master_stock_source_root="",
        golden_master_approved_stock_reference_digest_file="",
    )
    metrics_auth = MagicMock()
    metrics_auth.generate = MagicMock()
    metrics_auth.start_rotation = AsyncMock()
    metrics_auth.stop_rotation = AsyncMock()

    polling_instances: list[object] = []

    class FakePolling:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.authorized_chat_id = kwargs.get("authorized_chat_id")
            self.webhook_handler = kwargs.get("webhook_handler")
            polling_instances.append(self)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    runtime_state = tmp_path / "runtime_state"
    runtime_state.mkdir(parents=True, exist_ok=True)
    await ctx.lifecycle.initialize()  # type: ignore[attr-defined]
    ctx._push_startup_cleanup(ctx.lifecycle.close)  # type: ignore[attr-defined]

    patches = [
        patch("sky_claw.app_context.Config", return_value=local_cfg),
        patch("sky_claw.app_context.MO2Controller"),
        patch("sky_claw.app_context.MasterlistClient"),
        patch("sky_claw.app_context.TelegramSender", return_value=sender_mock),
        patch("sky_claw.app_context.HITLGuard", side_effect=FakeHITL),
        patch("sky_claw.app_context.configure_tracing"),
        patch("sky_claw.app_context.shutdown_tracing"),
        patch("sky_claw.app_context.AuthTokenManager", return_value=metrics_auth),
        patch("sky_claw.app_context.start_metrics_server", new=AsyncMock(return_value=MagicMock())),
        patch("sky_claw.app_context.stop_metrics_server", new=AsyncMock()),
        patch("sky_claw.app_context.SyncEngine"),
        patch("sky_claw.app_context.ToolsInstaller"),
        patch("sky_claw.app_context.scan_common_paths", return_value=None),
        patch("sky_claw.app_context.DistributedLockManager", FakeLock),
        patch("sky_claw.app_context.FileSnapshotManager", FakeSnapshot),
        patch("sky_claw.app_context.OperationJournal", FakeJournal),
        patch("sky_claw.app_context.AsyncToolRegistry"),
        patch("sky_claw.app_context.LLMRouter", FakeRouter),
        patch("sky_claw.app_context.TelegramPolling", FakePolling),
        patch("sky_claw.app_context._LOCK_STAGING_DIR", tmp_path / "locks"),
        patch("sky_claw.config.SystemPaths.runtime_state_dir", return_value=runtime_state),
        patch("sky_claw.app.security.governance.GovernanceManager"),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await ctx.start_full()  # type: ignore[attr-defined]

    return ctx, notify_calls, sender_mock, polling_instances


class TestMatrizOperadorTelegramCR03:
    """CR-03: matriz completa de resolución y propagación de identidad de operador."""

    @pytest.mark.parametrize(
        ("config_chat_id", "cli_operator", "esperado"),
        [
            # 1. Operador definido solamente por configuración
            ("654321", None, 654321),
            # 2. Operador definido solamente por CLI
            ("", 777111, 777111),
            # 3. Ambos definidos con valores iguales
            ("777111", 777111, 777111),
            # 4. Ambos definidos con valores diferentes: AppContext prioriza config sobre CLI
            ("111222", 333444, 111222),
            # 5. Ninguno definido -> fail-closed None
            ("", None, None),
            # 6. Identificadores inválidos o mal tipados -> fail-closed None (sin fallback permisivo)
            ("abc", None, None),
            ("abc", 333444, None),
            ("0", 333444, None),
            ("-1001234567890", 333444, None),
            (True, 333444, None),
            (123.5, 333444, None),
            ("١٢٣", 333444, None),
            ("", -1001234567890, None),
            ("", 0, None),
            ("", True, None),
            ("", "abc", None),
        ],
    )
    async def test_matriz_startup_y_run_telegram_identidad_unica(
        self,
        tmp_path: Path,
        config_chat_id: object,
        cli_operator: object,
        esperado: int | None,
    ) -> None:
        from sky_claw.app.modes import telegram_mode
        from sky_claw.app.security.hitl import HITLRequest

        ctx, notify_calls, sender_mock, _ = await _start_real_ctx(
            tmp_path,
            mode="telegram",
            cli_operator=cli_operator,
            config_chat_id=config_chat_id,
        )
        iniciado = asyncio.Event()
        captura: dict[str, object] = {}

        async def _asegurar_espia(
            contexto: object,
            *,
            sender: object,
            authorized_user_id: object,
            local_cfg: object = None,
        ) -> object:
            captura["asegurar_uid"] = authorized_user_id
            from sky_claw.app.comms.golden_master_hitl import asegurar_coordinador_golden_master

            coord = await asegurar_coordinador_golden_master(
                contexto,
                sender=sender,  # type: ignore[arg-type]
                authorized_user_id=authorized_user_id,  # type: ignore[arg-type]
                local_cfg=local_cfg,
            )
            captura["coord"] = coord
            return coord

        def _webhook_espia(**kwargs: object) -> MagicMock:
            captura["webhook_uid"] = kwargs["authorized_user_id"]
            return MagicMock()

        def _polling_espia(**kwargs: object) -> MagicMock:
            captura["polling_cid"] = kwargs["authorized_chat_id"]
            poll = MagicMock()

            async def _start() -> None:
                iniciado.set()

            poll.start = AsyncMock(side_effect=_start)
            poll.stop = AsyncMock()
            return poll

        try:
            with (
                patch.object(telegram_mode, "asegurar_coordinador_golden_master", side_effect=_asegurar_espia),
                patch.object(telegram_mode, "TelegramWebhook", side_effect=_webhook_espia),
                patch.object(telegram_mode, "TelegramPolling", side_effect=_polling_espia),
            ):
                task = asyncio.create_task(telegram_mode._run_telegram(ctx, "127.0.0.1", 0))  # type: ignore[arg-type]
                await asyncio.wait_for(iniciado.wait(), timeout=5.0)
                task.cancel()
                await asyncio.wait_for(task, timeout=5.0)

            assert getattr(ctx, "operator_chat_id", "SENTINEL") == esperado
            assert captura["asegurar_uid"] == esperado
            assert type(captura["asegurar_uid"]) is type(esperado)
            assert captura["webhook_uid"] == esperado
            assert type(captura["webhook_uid"]) is type(esperado)
            assert captura["polling_cid"] == esperado
            assert type(captura["polling_cid"]) is type(esperado)
            assert captura["coord"]._operador_autorizado == esperado  # type: ignore[attr-defined]

            # Caso 4 sin split-brain: HITL notify debe enviar al mismo operador que Webhook/Polling aceptan.
            notify_fn = notify_calls[-1]["notify_fn"]
            assert callable(notify_fn)
            await notify_fn(HITLRequest(request_id="req-matrix-1", reason="prueba"))
            if esperado is None:
                sender_mock.send.assert_not_called()
            else:
                sender_mock.send.assert_awaited_once()
                assert sender_mock.send.await_args.args[0] == esperado
        finally:
            await ctx.stop()  # type: ignore[attr-defined]

    async def test_modo_cli_y_telegram_convergen_en_misma_politica(self, tmp_path: Path) -> None:
        """Con ambos definidos distintos, modo cli y modo telegram eligen la misma identidad efectiva."""
        ctx_cli, _, _, polling_list = await _start_real_ctx(
            tmp_path / "cli",
            mode="cli",
            cli_operator=333444,
            config_chat_id="111222",
        )
        try:
            assert getattr(ctx_cli, "operator_chat_id", None) == 111222
            assert len(polling_list) == 1
            poll = polling_list[0]
            assert poll.authorized_chat_id == 111222  # type: ignore[attr-defined]
            assert poll.webhook_handler._allowed_user_id == 111222  # type: ignore[attr-defined]
            assert ctx_cli.golden_master_coordinator._operador_autorizado == 111222  # type: ignore[attr-defined]
        finally:
            await ctx_cli.stop()  # type: ignore[attr-defined]


class TestHotReloadIdentidadOperadorCR03:
    """CR-03: hot-reload no altera silenciosamente ni corrompe la identidad autorizada."""

    async def test_handle_update_config_preserva_operador_cli_cuando_config_no_define_chat_id(
        self, tmp_path: Path
    ) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge
        from sky_claw.config import Config

        ctx = _build_full_start_ctx(tmp_path, mode="cli", cli_operator=987654)
        ctx.router = MagicMock()
        ctx.hitl = MagicMock()
        ctx.polling = None
        ctx.operator_chat_id = 987654
        ctx._operator_chat_id_resuelto = True

        cfg = Config(tmp_path / "cfg_cli.toml")
        cfg._data["telegram_chat_id"] = ""

        bridge = FrontendBridge(ctx.router, MagicMock(), cfg, ctx)
        bridge._set_keyring = MagicMock()  # type: ignore[method-assign]
        bridge._send_config_result = AsyncMock()  # type: ignore[method-assign]
        cfg.save = MagicMock()  # type: ignore[method-assign]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()
        fake_polling.stop = AsyncMock()

        try:
            with (
                patch(
                    "sky_claw.app.comms.golden_master_hitl.asegurar_coordinador_golden_master", new_callable=AsyncMock
                ) as mock_gm,
                patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
                patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling) as mock_polling,
                patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
            ):
                mock_gm.return_value = MagicMock()
                await bridge._handle_update_config(
                    {"id": "m1", "content": {"telegram_bot_token": "999:NEWTOK", "telegram_chat_id": ""}}
                )

            assert mock_gm.call_args.kwargs["authorized_user_id"] == 987654
            assert mock_webhook.call_args.kwargs["authorized_user_id"] == 987654
            assert mock_polling.call_args.kwargs["authorized_chat_id"] == 987654
            assert ctx.operator_chat_id == 987654  # type: ignore[attr-defined]
        finally:
            await ctx.stop()  # type: ignore[attr-defined]

    async def test_handle_update_config_preserva_precedencia_config_sobre_cli(self, tmp_path: Path) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge
        from sky_claw.config import Config

        ctx = _build_full_start_ctx(tmp_path, mode="cli", cli_operator=333444)
        ctx.router = MagicMock()
        ctx.hitl = MagicMock()
        ctx.polling = None
        ctx.operator_chat_id = 111222
        ctx._operator_chat_id_resuelto = True

        cfg = Config(tmp_path / "cfg_prec.toml")
        cfg._data["telegram_chat_id"] = "111222"

        bridge = FrontendBridge(ctx.router, MagicMock(), cfg, ctx)
        bridge._set_keyring = MagicMock()  # type: ignore[method-assign]
        bridge._send_config_result = AsyncMock()  # type: ignore[method-assign]
        cfg.save = MagicMock()  # type: ignore[method-assign]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()
        fake_polling.stop = AsyncMock()

        try:
            with (
                patch(
                    "sky_claw.app.comms.golden_master_hitl.asegurar_coordinador_golden_master", new_callable=AsyncMock
                ) as mock_gm,
                patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
                patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling) as mock_polling,
                patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
            ):
                mock_gm.return_value = MagicMock()
                await bridge._handle_update_config(
                    {"id": "m2", "content": {"telegram_bot_token": "999:NEWTOK", "telegram_chat_id": ""}}
                )

            assert mock_gm.call_args.kwargs["authorized_user_id"] == 111222
            assert mock_webhook.call_args.kwargs["authorized_user_id"] == 111222
            assert mock_polling.call_args.kwargs["authorized_chat_id"] == 111222
            assert ctx.operator_chat_id == 111222  # type: ignore[attr-defined]
        finally:
            await ctx.stop()  # type: ignore[attr-defined]

    @pytest.mark.parametrize("chat_guardado_invalido", ["0", "abc", "-1001234567890"])
    async def test_handle_update_config_con_chat_id_invalido_en_config_y_solo_token_nuevo_falla_cerrado(
        self, tmp_path: Path, chat_guardado_invalido: str
    ) -> None:
        """CodeRabbit PRRT_kwDOR1JjU863igUb: None no debe convertirse en '' ni reactivar fallback CLI."""
        from sky_claw.app.comms.frontend_bridge import FrontendBridge
        from sky_claw.config import Config

        ctx = _build_full_start_ctx(tmp_path, mode="cli", cli_operator=987654)
        ctx.router = MagicMock()
        ctx.hitl = MagicMock()
        ctx.polling = None
        ctx.operator_chat_id = 987654
        ctx._operator_chat_id_resuelto = True

        cfg = Config(tmp_path / "cfg_invalid_stored.toml")
        cfg._data["telegram_chat_id"] = chat_guardado_invalido

        bridge = FrontendBridge(ctx.router, MagicMock(), cfg, ctx)
        bridge._set_keyring = MagicMock()  # type: ignore[method-assign]
        bridge._send_config_result = AsyncMock()  # type: ignore[method-assign]
        cfg.save = MagicMock()  # type: ignore[method-assign]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()
        fake_polling.stop = AsyncMock()

        try:
            with (
                patch(
                    "sky_claw.app.comms.golden_master_hitl.asegurar_coordinador_golden_master", new_callable=AsyncMock
                ) as mock_gm,
                patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
                patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling) as mock_polling,
                patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
            ):
                mock_gm.return_value = MagicMock()
                await bridge._handle_update_config(
                    {"id": "m2-invalid-cfg", "content": {"telegram_bot_token": "999:NEWTOK", "telegram_chat_id": ""}}
                )

            assert mock_gm.call_args.kwargs["authorized_user_id"] is None
            assert mock_webhook.call_args.kwargs["authorized_user_id"] is None
            assert mock_polling.call_args.kwargs["authorized_chat_id"] is None
            assert ctx.operator_chat_id is None  # type: ignore[attr-defined]
        finally:
            await ctx.stop()  # type: ignore[attr-defined]

    @pytest.mark.parametrize("chat_invalido", ["abc", "0", "-1001234567890"])
    async def test_handle_update_config_rechaza_chat_id_invalido_o_no_positivo(
        self, tmp_path: Path, chat_invalido: str
    ) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge
        from sky_claw.config import Config

        ctx = _build_full_start_ctx(tmp_path, mode="cli", cli_operator=987654)
        ctx.router = MagicMock()
        ctx.operator_chat_id = 987654
        ctx._operator_chat_id_resuelto = True

        cfg = Config(tmp_path / "cfg_bad.toml")
        cfg._data["telegram_chat_id"] = "987654"
        bridge = FrontendBridge(ctx.router, MagicMock(), cfg, ctx)
        bridge._send_config_result = AsyncMock()  # type: ignore[method-assign]
        cfg.save = MagicMock()  # type: ignore[method-assign]

        try:
            await bridge._handle_update_config(
                {"id": "m3", "content": {"telegram_bot_token": "999:NEWTOK", "telegram_chat_id": chat_invalido}}
            )
            bridge._send_config_result.assert_awaited_once()
            _, ok_enviado, _ = bridge._send_config_result.await_args.args
            assert ok_enviado is False
            cfg.save.assert_not_called()
            assert ctx.operator_chat_id == 987654  # type: ignore[attr-defined]
        finally:
            await ctx.stop()  # type: ignore[attr-defined]

    @pytest.mark.parametrize("chat_invalido", ["abc", "0", "-1001234567890", True])
    async def test_reload_telegram_invalido_falla_cerrado_y_sincroniza_tres_consumidores(
        self, tmp_path: Path, chat_invalido: object
    ) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge

        ctx = _build_full_start_ctx(tmp_path, mode="cli", cli_operator=987654)
        ctx.router = MagicMock()
        ctx.hitl = MagicMock()
        ctx.polling = None
        ctx.operator_chat_id = 987654
        ctx._operator_chat_id_resuelto = True

        bridge = FrontendBridge(ctx.router, MagicMock(), SimpleNamespace(telegram_chat_id=""), ctx)
        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()
        fake_polling.stop = AsyncMock()

        try:
            with (
                patch(
                    "sky_claw.app.comms.golden_master_hitl.asegurar_coordinador_golden_master", new_callable=AsyncMock
                ) as mock_gm,
                patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
                patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling) as mock_polling,
                patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
            ):
                mock_gm.return_value = MagicMock()
                ok = await bridge._reload_telegram("123:ABC", chat_invalido)  # type: ignore[arg-type]

            assert ok is True
            assert mock_gm.call_args.kwargs["authorized_user_id"] is None
            assert mock_webhook.call_args.kwargs["authorized_user_id"] is None
            assert mock_polling.call_args.kwargs["authorized_chat_id"] is None
            assert ctx.operator_chat_id is None  # type: ignore[attr-defined]
        finally:
            await ctx.stop()  # type: ignore[attr-defined]

    async def test_reload_telegram_con_chat_id_valido_pasa_int_identico_a_polling_y_webhook(self) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge

        bridge = FrontendBridge.__new__(FrontendBridge)
        ctx = MagicMock()
        ctx.polling = None
        bridge.ctx = ctx  # type: ignore[attr-defined]

        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()

        with (
            patch(
                "sky_claw.app.comms.golden_master_hitl.asegurar_coordinador_golden_master", new_callable=AsyncMock
            ) as mock_gm,
            patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
            patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling) as mock_polling,
            patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
        ):
            mock_gm.return_value = MagicMock()
            ok = await bridge._reload_telegram(token="123:ABC", chat_id="55501")

        assert ok is True
        assert mock_gm.call_args.kwargs["authorized_user_id"] == 55501
        assert type(mock_gm.call_args.kwargs["authorized_user_id"]) is int
        assert mock_webhook.call_args.kwargs["authorized_user_id"] == 55501
        assert type(mock_webhook.call_args.kwargs["authorized_user_id"]) is int
        assert mock_polling.call_args.kwargs["authorized_chat_id"] == 55501
        assert type(mock_polling.call_args.kwargs["authorized_chat_id"]) is int


class TestValidacionEstrictaTransporteCR03:
    """CR-03: ningún componente de transporte acepta bool, cero ni chat negativo de grupo."""

    @pytest.mark.parametrize("valor_invalido", [True, False, 0, -100, "-1001234567890", "abc", 1.5])
    async def test_webhook_polling_y_coordinador_rechazan_identidades_invalidas_fail_closed(
        self, valor_invalido: object
    ) -> None:
        from sky_claw.app.comms.golden_master_hitl import CoordinadorTelegramGoldenMaster
        from sky_claw.app.comms.telegram import TelegramWebhook
        from sky_claw.app.comms.telegram_polling import TelegramPolling

        sender = MagicMock()
        sender.send = AsyncMock()
        webhook = TelegramWebhook(
            router=MagicMock(),
            sender=sender,
            session=MagicMock(spec=aiohttp.ClientSession),
            authorized_user_id=valor_invalido,  # type: ignore[arg-type]
        )
        assert webhook._allowed_user_id is None
        # En Python `1 == True`; validar explícitamente que from.id=1 no pasa.
        assert webhook._validate_sender({"from": {"id": 1}, "chat": {"id": 1, "type": "private"}}) is False
        assert webhook._validate_sender({"from": {"id": 0}, "chat": {"id": 0, "type": "private"}}) is False

        handler = MagicMock()
        handler.process_update = AsyncMock()
        polling = TelegramPolling(
            token="123:ABC",
            webhook_handler=handler,
            gateway=MagicMock(),
            session=MagicMock(spec=aiohttp.ClientSession),
            authorized_chat_id=valor_invalido,  # type: ignore[arg-type]
        )
        assert polling._authorized_chat_id is None
        await polling._process_raw_update({"update_id": 1, "message": {"text": "/status", "chat": {"id": 1}}})
        handler.process_update.assert_not_called()

        coord = CoordinadorTelegramGoldenMaster(
            sender=sender,
            operador_autorizado=valor_invalido,  # type: ignore[arg-type]
        )
        try:
            assert coord._operador_autorizado is None
        finally:
            await coord.cerrar()
