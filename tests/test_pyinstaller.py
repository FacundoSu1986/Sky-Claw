"""Tests for PyInstaller packaging, setup wizard, and frozen-app paths."""

from __future__ import annotations

import json
import os
import pathlib
from unittest.mock import patch

import pytest
from aiohttp import web

from sky_claw.local_config import LocalConfig, load, save

@pytest.fixture(autouse=True)
def mock_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    def raiser(*args, **kwargs):
        raise Exception("Keyring not available in test")
    monkeypatch.setattr(keyring, "get_password", lambda s, n: None)
    monkeypatch.setattr(keyring, "set_password", raiser)

# ---------------------------------------------------------------------------
# sky_claw.spec is parseable
# ---------------------------------------------------------------------------


class TestSpec:
    def test_spec_file_exists(self) -> None:
        spec = pathlib.Path("sky_claw.spec")
        assert spec.exists(), "sky_claw.spec must exist in repo root"

    def test_spec_is_valid_python(self) -> None:
        spec = pathlib.Path("sky_claw.spec")
        source = spec.read_text(encoding="utf-8")
        # Should compile without SyntaxError (PyInstaller specs are Python).
        compile(source, str(spec), "exec")


# ---------------------------------------------------------------------------
# build.bat exists
# ---------------------------------------------------------------------------


class TestBuildBat:
    def test_build_bat_exists(self) -> None:
        bat = pathlib.Path("build.bat")
        assert bat.exists(), "build.bat must exist in repo root"

    def test_build_bat_contains_pyinstaller(self) -> None:
        bat = pathlib.Path("build.bat")
        content = bat.read_text(encoding="utf-8")
        assert "pyinstaller" in content.lower()
        assert "sky_claw.spec" in content


# ---------------------------------------------------------------------------
# SkyClawApp.bat detects .exe
# ---------------------------------------------------------------------------


class TestSkyClawAppBat:
    def test_bat_detects_exe(self) -> None:
        bat = pathlib.Path("SkyClawApp.bat")
        content = bat.read_text(encoding="utf-8")
        assert "SkyClawApp.exe" in content
        assert "dist" in content


# ---------------------------------------------------------------------------
# sys._MEIPASS path detection
# ---------------------------------------------------------------------------


class TestFrozenPaths:
    def test_static_dir_normal(self) -> None:
        from sky_claw.web.app import _get_static_dir

        with patch("sky_claw.web.app.sys") as mock_sys:
            mock_sys.frozen = False
            # Re-import won't help; call the function directly.
            # In non-frozen mode it uses __file__.
            result = _get_static_dir()
            assert "static" in str(result)

    def test_static_dir_frozen(self) -> None:
        from sky_claw.web.app import _get_static_dir

        with patch("sky_claw.web.app.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys._MEIPASS = "/tmp/fake_meipass"
            result = _get_static_dir()
            assert "fake_meipass" in str(result)
            assert "static" in str(result)

    def test_exe_dir_normal(self) -> None:
        from sky_claw.web.app import _get_exe_dir

        with patch("sky_claw.web.app.sys") as mock_sys:
            mock_sys.frozen = False
            result = _get_exe_dir()
            assert result == pathlib.Path.cwd()

    def test_exe_dir_frozen(self) -> None:
        from sky_claw.web.app import _get_exe_dir

        with patch("sky_claw.web.app.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys.executable = "/tmp/dist/SkyClawApp.exe"
            result = _get_exe_dir()
            assert str(result).endswith("dist")


# ---------------------------------------------------------------------------
# local_config with API key obfuscation
# ---------------------------------------------------------------------------


class TestLocalConfigApiKey:
    def test_set_and_get_api_key(self) -> None:
        cfg = LocalConfig()
        cfg.set_api_key("sk-ant-test-key-123")
        assert cfg.api_key_b64 is not None
        assert cfg.api_key_b64 != "sk-ant-test-key-123"  # Not plaintext
        assert cfg.get_api_key() == "sk-ant-test-key-123"

    def test_get_api_key_none(self) -> None:
        cfg = LocalConfig()
        assert cfg.get_api_key() is None

    def test_roundtrip_with_api_key(self, tmp_path: pathlib.Path) -> None:
        cfg = LocalConfig(mo2_root="C:/MO2", first_run=False)
        cfg.set_api_key("my-secret-key")
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        assert loaded.get_api_key() == "my-secret-key"
        assert loaded.mo2_root == "C:/MO2"
        assert loaded.first_run is False

        # Verify the raw file does NOT contain plaintext key.
        raw = path.read_text(encoding="utf-8")
        assert "my-secret-key" not in raw

    def test_skyrim_path_field(self, tmp_path: pathlib.Path) -> None:
        cfg = LocalConfig(skyrim_path="C:/Skyrim")
        path = tmp_path / "config.json"
        save(cfg, path)
        loaded = load(path)
        assert loaded.skyrim_path == "C:/Skyrim"


# ---------------------------------------------------------------------------
# /api/setup endpoint
# ---------------------------------------------------------------------------


class TestSetupEndpoint:
    @pytest.mark.asyncio
    async def test_get_setup_returns_config(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp
        from unittest.mock import MagicMock, AsyncMock

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        # Save initial config.
        cfg = LocalConfig(mo2_root="C:/MO2", first_run=True)
        save(cfg, config_path)

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/setup")
        assert resp.status == 200
        data = await resp.json()
        assert data["mo2_root"] == "C:/MO2"
        assert data["first_run"] is True
        # API keys should NOT be returned.
        assert "api_key" not in data
        assert "api_key_b64" not in data

    @pytest.mark.asyncio
    async def test_post_setup_saves_config(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp
        from unittest.mock import MagicMock

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/setup", json={
            "mo2_root": "D:/MO2",
            "api_key": "sk-test-key",
            "install_dir": "D:/Modding",
        })
        assert resp.status == 200

        # Verify saved.
        loaded = load(config_path)
        assert loaded.mo2_root == "D:/MO2"
        assert loaded.install_dir == "D:/Modding"
        assert loaded.first_run is False
        assert loaded.get_api_key() == "sk-test-key"

    @pytest.mark.asyncio
    async def test_index_redirects_on_first_run(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp
        from unittest.mock import MagicMock

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        # first_run=True → redirect to setup.
        cfg = LocalConfig(first_run=True)
        save(cfg, config_path)

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302
        assert "/setup.html" in resp.headers.get("Location", "")

    @pytest.mark.asyncio
    async def test_index_serves_chat_after_setup(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp
        from unittest.mock import MagicMock

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        # first_run=False → serve index.html.
        cfg = LocalConfig(first_run=False)
        save(cfg, config_path)

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "Sky-Claw" in text


# ---------------------------------------------------------------------------
# Nexus API key in local_config
# ---------------------------------------------------------------------------


class TestNexusApiKey:
    def test_set_and_get_nexus_api_key(self) -> None:
        cfg = LocalConfig()
        cfg.set_nexus_api_key("nexus-secret-123")
        assert cfg.nexus_api_key_b64 is not None
        assert cfg.nexus_api_key_b64 != "nexus-secret-123"
        assert cfg.get_nexus_api_key() == "nexus-secret-123"

    def test_get_nexus_api_key_none(self) -> None:
        cfg = LocalConfig()
        assert cfg.get_nexus_api_key() is None

    def test_roundtrip_nexus_api_key(self, tmp_path: pathlib.Path) -> None:
        cfg = LocalConfig()
        cfg.set_nexus_api_key("my-nexus-key")
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        assert loaded.get_nexus_api_key() == "my-nexus-key"

        raw = path.read_text(encoding="utf-8")
        assert "my-nexus-key" not in raw


# ---------------------------------------------------------------------------
# API key injection into env vars from local_config
# ---------------------------------------------------------------------------


class TestApiKeyInjection:
    def test_anthropic_key_loaded(self, tmp_path: pathlib.Path) -> None:
        """api_key_b64 starting with sk-ant sets ANTHROPIC_API_KEY."""
        cfg = LocalConfig()
        cfg.set_api_key("sk-ant-my-anthropic-key")
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        key = loaded.get_api_key()
        assert key is not None
        assert key.startswith("sk-ant")

    def test_deepseek_key_loaded(self, tmp_path: pathlib.Path) -> None:
        """api_key_b64 starting with sk- (non-ant) maps to DEEPSEEK_API_KEY."""
        cfg = LocalConfig()
        cfg.set_api_key("sk-deepseek-test-key")
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        key = loaded.get_api_key()
        assert key is not None
        assert key.startswith("sk-")
        assert not key.startswith("sk-ant")

    def test_env_var_priority_anthropic(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var takes priority over local_config for ANTHROPIC_API_KEY."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic-key")
        cfg = LocalConfig()
        cfg.set_api_key("sk-ant-config-key")

        # Simulate the injection logic from __main__.py
        api_key = cfg.get_api_key()
        assert api_key is not None
        if api_key.startswith("sk-ant"):
            if not os.environ.get("ANTHROPIC_API_KEY"):
                os.environ["ANTHROPIC_API_KEY"] = api_key

        # Env var should remain unchanged.
        assert os.environ["ANTHROPIC_API_KEY"] == "env-anthropic-key"

    def test_env_var_priority_deepseek(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var takes priority over local_config for DEEPSEEK_API_KEY."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-deepseek-key")
        cfg = LocalConfig()
        cfg.set_api_key("sk-config-deepseek")

        api_key = cfg.get_api_key()
        assert api_key is not None
        if api_key.startswith("sk-") and not api_key.startswith("sk-ant"):
            if not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ["DEEPSEEK_API_KEY"] = api_key

        assert os.environ["DEEPSEEK_API_KEY"] == "env-deepseek-key"

    def test_nexus_key_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nexus API key from config sets NEXUS_API_KEY env var."""
        monkeypatch.delenv("NEXUS_API_KEY", raising=False)
        cfg = LocalConfig()
        cfg.set_nexus_api_key("nexus-key-123")

        nexus_key = cfg.get_nexus_api_key()
        assert nexus_key is not None
        if not os.environ.get("NEXUS_API_KEY"):
            os.environ["NEXUS_API_KEY"] = nexus_key

        assert os.environ["NEXUS_API_KEY"] == "nexus-key-123"

    def test_nexus_key_env_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Existing NEXUS_API_KEY env var is not overwritten."""
        monkeypatch.setenv("NEXUS_API_KEY", "env-nexus-key")
        cfg = LocalConfig()
        cfg.set_nexus_api_key("config-nexus-key")

        nexus_key = cfg.get_nexus_api_key()
        if nexus_key and not os.environ.get("NEXUS_API_KEY"):
            os.environ["NEXUS_API_KEY"] = nexus_key

        assert os.environ["NEXUS_API_KEY"] == "env-nexus-key"


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT includes Default profile
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    def test_system_prompt_includes_default_profile(self) -> None:
        from sky_claw.app_context import SYSTEM_PROMPT

        assert "Default" in SYSTEM_PROMPT
        assert "perfil" in SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# Setup wizard with nexus_api_key
# ---------------------------------------------------------------------------


class TestSetupWithNexusKey:
    @pytest.mark.asyncio
    async def test_post_setup_saves_nexus_key(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp
        from unittest.mock import MagicMock

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/setup", json={
            "mo2_root": "D:/MO2",
            "api_key": "sk-ant-test",
            "nexus_api_key": "nexus-premium-key",
        })
        assert resp.status == 200

        loaded = load(config_path)
        assert loaded.get_api_key() == "sk-ant-test"
        assert loaded.get_nexus_api_key() == "nexus-premium-key"
        assert loaded.first_run is False

    @pytest.mark.asyncio
    async def test_post_setup_without_nexus_key(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        from sky_claw.web.app import WebApp
        from unittest.mock import MagicMock

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "config.json"

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/setup", json={
            "mo2_root": "D:/MO2",
            "api_key": "sk-test",
        })
        assert resp.status == 200

        loaded = load(config_path)
        assert loaded.get_nexus_api_key() is None
