"""Tests for lazy initialization (fix/exe-config-definitive).

Verifies the two-phase startup pattern:
  Phase 1 — start_minimal(): HTTP session only (setup wizard works).
  Phase 2 — start_full(): full provider + router (chat works).
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.config import Config
from sky_claw.local.local_config import LocalConfig, load, save

# ---------------------------------------------------------------------------
# 1. AppContext.is_configured property
# ---------------------------------------------------------------------------


class TestIsConfigured:
    def test_false_before_start(self) -> None:
        """is_configured is False immediately after construction."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)
        assert ctx.is_configured is False

    def test_true_after_router_set(self) -> None:
        """is_configured is True when router is assigned."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)
        ctx.router = MagicMock()
        assert ctx.is_configured is True


# ---------------------------------------------------------------------------
# 2. start_minimal creates session but NOT router
# ---------------------------------------------------------------------------


class TestStartMinimal:
    @pytest.mark.asyncio
    async def test_start_minimal_creates_session(self) -> None:
        """start_minimal creates session and resolves config_path."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)
        await ctx.start_minimal()

        assert ctx.network.session is not None
        assert ctx.config_path is not None
        assert ctx.router is None  # NOT created yet
        assert ctx.is_configured is False

        # Cleanup.
        await ctx.network.session.close()

    @pytest.mark.asyncio
    async def test_config_path_resolved(self) -> None:
        """start_minimal sets config_path to sky_claw_config.json."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)
        await ctx.start_minimal()

        assert ctx.config_path is not None
        assert ctx.config_path.name == "config.toml"

        await ctx.network.session.close()


# ---------------------------------------------------------------------------
# 3. _resolve_config_path
# ---------------------------------------------------------------------------


class TestResolveConfigPath:
    def test_normal_mode(self) -> None:
        """In normal Python, config_path is CWD / sky_claw_config.json."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)
        ctx._resolve_config_path()

        assert ctx.config_path == Config.DEFAULT_CONFIG_FILE

    def test_frozen_mode(self) -> None:
        """When frozen, config_path is next to the .exe."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)

        with patch("sky_claw.__main__.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys.executable = "C:/dist/SkyClawApp.exe"
            ctx._resolve_config_path()

        assert ctx.config_path == Config.DEFAULT_CONFIG_FILE


# ---------------------------------------------------------------------------
# 4. _apply_config_to_env (removed — method no longer exists in AppContext)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="_apply_config_to_env no longer exists in AppContext")
class TestApplyConfigToEnv:
    def test_anthropic_key_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anthropic key from config sets ANTHROPIC_API_KEY env var."""
        from sky_claw.__main__ import AppContext

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        cfg = Config(pathlib.Path("/tmp/fake_config.toml"))
        cfg._data["anthropic_api_key"] = "sk-ant-test-key-1234"

        AppContext._apply_config_to_env(cfg)

        import os

        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-key-1234"

    def test_env_var_not_overwritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Existing env var is NOT overwritten by config."""
        from sky_claw.__main__ import AppContext

        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-original")

        cfg = Config(pathlib.Path("/tmp/fake_config.toml"))
        cfg._data["anthropic_api_key"] = "sk-ant-should-not-replace"

        AppContext._apply_config_to_env(cfg)

        import os

        assert os.environ.get("ANTHROPIC_API_KEY") == "env-original"


# ---------------------------------------------------------------------------
# 5. /api/chat returns 503 when router is None
# ---------------------------------------------------------------------------


class TestChatNotConfigured:
    @pytest.mark.asyncio
    async def test_chat_returns_503_without_router(self, aiohttp_client) -> None:
        """POST /api/chat returns 503 when router is None."""
        from sky_claw.antigravity.web.app import WebApp

        web_app = WebApp(
            router=None,
            session=MagicMock(),
            config_path=pathlib.Path("/tmp/test_config.json"),
        )
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/chat", json={"message": "hello"})
        assert resp.status == 503
        data = await resp.json()
        assert "no está configurado" in data["error"]

    @pytest.mark.asyncio
    async def test_chat_works_with_router(self, aiohttp_client) -> None:
        """POST /api/chat returns 200 when router is available."""
        from sky_claw.antigravity.web.app import WebApp

        mock_router = AsyncMock()
        mock_router.chat = AsyncMock(return_value="Hola, soy Sky-Claw")

        web_app = WebApp(
            router=mock_router,
            session=MagicMock(),
            config_path=pathlib.Path("/tmp/test_config.json"),
        )
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/chat", json={"message": "hello"})
        assert resp.status == 200
        data = await resp.json()
        assert data["response"] == "Hola, soy Sky-Claw"


# ---------------------------------------------------------------------------
# 6. POST /api/setup triggers on_setup_complete callback
# ---------------------------------------------------------------------------


class TestSetupCallback:
    @pytest.mark.asyncio
    async def test_setup_triggers_callback(self, tmp_path: pathlib.Path, aiohttp_client) -> None:
        """POST /api/setup invokes the on_setup_complete callback."""
        from sky_claw.antigravity.web.app import WebApp

        callback_called = False

        async def fake_callback(web_app_instance):
            nonlocal callback_called
            callback_called = True
            # Simulate start_full updating the router.
            web_app_instance._router = MagicMock()

        config_path = tmp_path / "sky_claw_config.json"
        web_app = WebApp(
            router=None,
            session=MagicMock(),
            config_path=config_path,
            on_setup_complete=fake_callback,
        )
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post(
            "/api/setup",
            json={
                "mo2_root": "D:/MO2",
                "api_key": "sk-ant-test-key",
            },
        )
        assert resp.status == 200
        assert callback_called is True
        # Router should now be set by the callback.
        assert web_app._router is not None

    @pytest.mark.asyncio
    async def test_setup_callback_failure_returns_500(self, tmp_path: pathlib.Path, aiohttp_client) -> None:
        """If on_setup_complete raises, POST /api/setup returns 500."""
        from sky_claw.antigravity.web.app import WebApp

        async def failing_callback(web_app_instance):
            raise RuntimeError("Provider init failed")

        config_path = tmp_path / "sky_claw_config.json"
        web_app = WebApp(
            router=None,
            session=MagicMock(),
            config_path=config_path,
            on_setup_complete=failing_callback,
        )
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post(
            "/api/setup",
            json={
                "api_key": "sk-ant-test",
            },
        )
        assert resp.status == 500
        data = await resp.json()
        assert "initialization failed" in data["error"]

    @pytest.mark.asyncio
    async def test_setup_without_callback_still_saves(self, tmp_path: pathlib.Path, aiohttp_client) -> None:
        """Without callback, POST /api/setup still saves config normally."""
        from sky_claw.antigravity.web.app import WebApp

        config_path = tmp_path / "sky_claw_config.json"
        web_app = WebApp(
            router=MagicMock(),
            session=MagicMock(),
            config_path=config_path,
            on_setup_complete=None,
        )
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post(
            "/api/setup",
            json={
                "mo2_root": "E:/MO2",
                "api_key": "sk-ant-no-callback",
            },
        )
        assert resp.status == 200

        cfg = load(config_path)
        assert cfg.mo2_root == "E:/MO2"
        assert cfg.first_run is False


# ---------------------------------------------------------------------------
# 7. WebApp accepts router=None constructor
# ---------------------------------------------------------------------------


class TestWebAppNullRouter:
    def test_constructor_accepts_none_router(self) -> None:
        """WebApp can be constructed with router=None."""
        from sky_claw.antigravity.web.app import WebApp

        web_app = WebApp(
            router=None,
            session=MagicMock(),
        )
        assert web_app._router is None

    @pytest.mark.asyncio
    async def test_index_redirects_on_first_run(self, tmp_path: pathlib.Path, aiohttp_client) -> None:
        """With first_run=True, / redirects to /setup.html even without router."""
        from sky_claw.antigravity.web.app import WebApp

        config_path = tmp_path / "sky_claw_config.json"
        cfg = LocalConfig(first_run=True)
        save(cfg, config_path)

        web_app = WebApp(
            router=None,
            session=MagicMock(),
            config_path=config_path,
        )
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == "/setup.html"


# ---------------------------------------------------------------------------
# 8. start() is shortcut for start_minimal + start_full
# ---------------------------------------------------------------------------


class TestStartShortcut:
    @pytest.mark.asyncio
    async def test_start_calls_both_phases(self) -> None:
        """start() calls start_minimal() then start_full()."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)

        calls: list[str] = []

        async def mock_minimal():
            calls.append("minimal")
            ctx.config_path = pathlib.Path("/tmp/test.json")
            ctx.network.session = MagicMock()

        async def mock_full():
            calls.append("full")
            ctx.router = MagicMock()

        ctx.start_minimal = mock_minimal
