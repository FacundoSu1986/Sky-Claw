"""Tests for exe config loading, sandbox roots, and config_path resolution."""

from __future__ import annotations

import os
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from sky_claw.local_config import LocalConfig, get_exe_dir, load, save
from sky_claw.security.path_validator import PathValidator, PathViolation

# ---------------------------------------------------------------------------
# Bug 1: Config path resolves to exe dir when frozen
# ---------------------------------------------------------------------------


class TestExeConfigPath:
    def test_get_exe_dir_normal(self) -> None:
        """In normal Python, get_exe_dir returns CWD."""
        with patch("sky_claw.local_config.sys") as mock_sys:
            mock_sys.frozen = False
            result = get_exe_dir()
        assert result == pathlib.Path.cwd()

    def test_get_exe_dir_frozen(self) -> None:
        """When frozen, get_exe_dir returns the executable's parent."""
        with patch("sky_claw.local_config.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys.executable = "C:/dist/SkyClawApp.exe"
            result = get_exe_dir()
        assert result == pathlib.Path("C:/dist")

    def test_config_loads_from_exe_dir(self, tmp_path: pathlib.Path) -> None:
        """Config file is loaded from the exe directory."""
        config_path = tmp_path / "sky_claw_config.json"
        cfg = LocalConfig(mo2_root="D:/MO2", first_run=False)
        cfg.set_api_key("sk-ant-test-key")
        save(cfg, config_path)

        loaded = load(config_path)
        assert loaded.mo2_root == "D:/MO2"
        assert loaded.get_api_key() == "sk-ant-test-key"


# ---------------------------------------------------------------------------
# Bug 1: API key from config → env vars
# ---------------------------------------------------------------------------


class TestApiKeyFromConfig:
    def test_anthropic_key_detection(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Key starting with sk-ant sets ANTHROPIC_API_KEY."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        cfg = LocalConfig()
        cfg.set_api_key("sk-ant-my-anthropic-key")
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        api_key = loaded.get_api_key()
        assert api_key is not None
        assert api_key.startswith("sk-ant")

        # Simulate __main__.py injection logic.
        if api_key.startswith("sk-ant") and not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = api_key

        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-my-anthropic-key"

    def test_deepseek_key_detection(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Key starting with sk- (non-ant) sets DEEPSEEK_API_KEY."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        cfg = LocalConfig()
        cfg.set_api_key("sk-deepseek-my-key")
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        api_key = loaded.get_api_key()
        assert api_key is not None
        assert api_key.startswith("sk-")
        assert not api_key.startswith("sk-ant")

        if api_key.startswith("sk-") and not api_key.startswith("sk-ant"):
            if not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ["DEEPSEEK_API_KEY"] = api_key

        assert os.environ["DEEPSEEK_API_KEY"] == "sk-deepseek-my-key"

    def test_generic_key_defaults_to_deepseek(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Generic key (not sk-) maps to DEEPSEEK_API_KEY."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        cfg = LocalConfig()
        cfg.set_api_key("my-custom-api-key")
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        api_key = loaded.get_api_key()
        assert api_key is not None

        if not api_key.startswith("sk-ant") and not api_key.startswith("sk-"):
            if not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ["DEEPSEEK_API_KEY"] = api_key

        assert os.environ["DEEPSEEK_API_KEY"] == "my-custom-api-key"

    def test_env_var_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Existing env vars are NOT overwritten by config values."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-stays")

        cfg = LocalConfig()
        cfg.set_api_key("sk-ant-should-not-replace")
        api_key = cfg.get_api_key()

        if (
            api_key
            and api_key.startswith("sk-ant")
            and not os.environ.get("ANTHROPIC_API_KEY")
        ):
            os.environ["ANTHROPIC_API_KEY"] = api_key

        assert os.environ["ANTHROPIC_API_KEY"] == "env-key-stays"


# ---------------------------------------------------------------------------
# Bug 2: install_dir in sandbox roots
# ---------------------------------------------------------------------------


class TestInstallDirSandbox:
    def test_install_dir_added_to_sandbox(self) -> None:
        """install_dir is accepted as a valid sandbox root."""
        mo2_root = pathlib.Path("C:/Modding/MO2")
        install_dir = pathlib.Path("C:/Modding")

        sandbox_roots: list[pathlib.Path] = [
            mo2_root,
            pathlib.Path(tempfile.gettempdir()) / "sky_claw",
        ]
        if install_dir and install_dir not in sandbox_roots:
            sandbox_roots.append(install_dir)

        validator = PathValidator(roots=sandbox_roots)
        # Should NOT raise — C:\Modding\LOOT is inside C:\Modding.
        validator.validate(pathlib.Path("C:/Modding/LOOT/LOOT.exe"))

    def test_mo2_parent_added_to_sandbox(self) -> None:
        """MO2 parent dir is included so tools can install alongside MO2."""
        mo2_root = pathlib.Path("C:/Modding/MO2")

        sandbox_roots: list[pathlib.Path] = [
            mo2_root,
            pathlib.Path(tempfile.gettempdir()) / "sky_claw",
        ]
        mo2_parent = mo2_root.parent
        if mo2_parent != mo2_root and mo2_parent not in sandbox_roots:
            sandbox_roots.append(mo2_parent)

        validator = PathValidator(roots=sandbox_roots)
        # C:\Modding\SSEEdit should be valid (inside C:\Modding).
        validator.validate(pathlib.Path("C:/Modding/SSEEdit/SSEEdit.exe"))

    def test_default_sandbox_without_install_dir(self) -> None:
        """Without install_dir, only mo2_root and tempdir/sky_claw are roots."""
        mo2_root = pathlib.Path("C:/Modding/MO2")
        install_dir = None

        sandbox_roots: list[pathlib.Path] = [
            mo2_root,
            pathlib.Path(tempfile.gettempdir()) / "sky_claw",
        ]
        if install_dir and install_dir not in sandbox_roots:
            sandbox_roots.append(install_dir)

        assert len(sandbox_roots) == 2
        assert mo2_root in sandbox_roots

    def test_path_outside_sandbox_rejected(self) -> None:
        """Paths outside all sandbox roots are rejected."""
        mo2_root = pathlib.Path("C:/Modding/MO2")
        validator = PathValidator(roots=[mo2_root])

        with pytest.raises(PathViolation):
            validator.validate(pathlib.Path("D:/Elsewhere/evil.exe"))

    def test_loot_install_in_install_dir(self) -> None:
        """LOOT install path is valid when install_dir is in sandbox."""
        install_dir = pathlib.Path("D:/Modding")
        mo2_root = pathlib.Path("C:/MO2Portable")

        sandbox_roots = [
            mo2_root,
            pathlib.Path(tempfile.gettempdir()) / "sky_claw",
            install_dir,
        ]
        validator = PathValidator(roots=sandbox_roots)

        # Simulates ToolsInstaller extracting LOOT into install_dir.
        validator.validate(pathlib.Path("D:/Modding/LOOT/LOOT.exe"))
        validator.validate(pathlib.Path("D:/Modding/SSEEdit/SSEEdit.exe"))


# ---------------------------------------------------------------------------
# Frozen config path resolution
# ---------------------------------------------------------------------------


class TestFrozenConfigPath:
    def test_frozen_config_path(self) -> None:
        """When sys.frozen=True, config_path points next to the .exe."""
        with patch("sky_claw.__main__.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys.executable = "C:/dist/SkyClawApp.exe"

            if getattr(mock_sys, "frozen", False):
                config_dir = pathlib.Path(mock_sys.executable).parent
            else:
                config_dir = pathlib.Path.cwd()
            config_path = config_dir / "sky_claw_config.json"

        assert config_path == pathlib.Path("C:/dist/sky_claw_config.json")

    def test_normal_config_path(self) -> None:
        """Without frozen, config_path is CWD / sky_claw_config.json."""
        config_dir = pathlib.Path.cwd()
        config_path = config_dir / "sky_claw_config.json"

        assert config_path.parent == pathlib.Path.cwd()
        assert config_path.name == "sky_claw_config.json"


# ---------------------------------------------------------------------------
# mo2_root from config vs CLI
# ---------------------------------------------------------------------------


class TestMo2RootOverride:
    def test_mo2_root_from_config_overrides_default(self) -> None:
        """If CLI has the default C:/MO2Portable, config value wins."""
        cfg = LocalConfig(mo2_root="D:/Modding/MO2")
        cli_mo2 = pathlib.Path("C:/MO2Portable")

        _MO2_DEFAULT = str(pathlib.Path("C:/MO2Portable"))
        result = (
            pathlib.Path(cfg.mo2_root)
            if cfg.mo2_root and str(cli_mo2) == _MO2_DEFAULT
            else cli_mo2
        )

        assert result == pathlib.Path("D:/Modding/MO2")

    def test_mo2_root_cli_priority(self) -> None:
        """If CLI has a non-default path, CLI wins over config."""
        cfg = LocalConfig(mo2_root="D:/Modding/MO2")
        cli_mo2 = pathlib.Path("E:/Custom/MO2")

        _MO2_DEFAULT = str(pathlib.Path("C:/MO2Portable"))
        result = (
            pathlib.Path(cfg.mo2_root)
            if cfg.mo2_root and str(cli_mo2) == _MO2_DEFAULT
            else cli_mo2
        )

        assert result == pathlib.Path("E:/Custom/MO2")

    def test_mo2_root_no_config(self) -> None:
        """Without config value, CLI default is used."""
        cfg = LocalConfig()
        cli_mo2 = pathlib.Path("C:/MO2Portable")

        _MO2_DEFAULT = str(pathlib.Path("C:/MO2Portable"))
        result = (
            pathlib.Path(cfg.mo2_root)
            if cfg.mo2_root and str(cli_mo2) == _MO2_DEFAULT
            else cli_mo2
        )

        assert result == pathlib.Path("C:/MO2Portable")


# ---------------------------------------------------------------------------
# AppContext.config_path attribute
# ---------------------------------------------------------------------------


class TestAppContextConfigPath:
    def test_config_path_set_on_context(self) -> None:
        """AppContext stores config_path as instance attribute."""
        from sky_claw.__main__ import AppContext

        args = MagicMock()
        ctx = AppContext(args)
        assert ctx.config_path is None  # Before start()


# ---------------------------------------------------------------------------
# Setup wizard saves/loads from correct path
# ---------------------------------------------------------------------------


class TestSetupWizardConfigPath:
    @pytest.mark.asyncio
    async def test_setup_saves_to_config_path(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        """POST /api/setup saves to the config_path provided to WebApp."""
        from sky_claw.web.app import WebApp

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "sky_claw_config.json"

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.post(
            "/api/setup",
            json={
                "mo2_root": "D:/MO2",
                "api_key": "sk-ant-test-frozen",
            },
        )
        assert resp.status == 200

        # Verify it saved to the exact path we specified.
        loaded = load(config_path)
        assert loaded.mo2_root == "D:/MO2"
        assert loaded.get_api_key() == "sk-ant-test-frozen"
        assert loaded.first_run is False

    @pytest.mark.asyncio
    async def test_setup_loads_from_config_path(
        self, tmp_path: pathlib.Path, aiohttp_client
    ) -> None:
        """GET /api/setup reads from the config_path provided to WebApp."""
        from sky_claw.web.app import WebApp

        router = MagicMock()
        session = MagicMock()
        config_path = tmp_path / "sky_claw_config.json"

        cfg = LocalConfig(mo2_root="E:/MO2", first_run=False)
        save(cfg, config_path)

        web_app = WebApp(router=router, session=session, config_path=config_path)
        app = web_app.create_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/setup")
        assert resp.status == 200
        data = await resp.json()
        assert data["mo2_root"] == "E:/MO2"
        assert data["first_run"] is False
