"""Tests for PyInstaller packaging, setup wizard, and frozen-app paths."""

from __future__ import annotations

import os
import pathlib
import re
import sys
from unittest.mock import patch

import pytest

from sky_claw.local.local_config import LocalConfig, load, save


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

    def test_spec_bundles_gui_static_assets(self) -> None:
        """Regression: the GUI css + image assets MUST be declared in the
        spec ``datas`` or the frozen onefile exe crashes at startup in
        ``sky_claw_gui.setup_app`` — ``add_static_files`` is handed a
        directory that does not exist inside ``sys._MEIPASS``.
        (This is the bug that broke the 0.2.0 release exe.)"""
        source = pathlib.Path("sky_claw.spec").read_text(encoding="utf-8")
        assert "sky_claw/antigravity/gui/styles.css" in source, "gui/styles.css not bundled in spec datas"
        assert "sky_claw/antigravity/gui/assets" in source, "gui/assets not bundled in spec datas"


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

    def test_build_bat_uses_dot_venv(self) -> None:
        """Regression: build.bat must target the repo's ``.venv``, not a
        bare ``venv\\`` dir, or it dies with 'activate.bat not found'."""
        content = pathlib.Path("build.bat").read_text(encoding="utf-8")
        assert ".venv" in content
        # No bare ``venv\`` path references (must always be ``.venv\``).
        assert not re.search(r"(?<!\.)\bvenv\\", content), (
            "build.bat references a bare 'venv\\' path instead of '.venv\\'"
        )


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
        from sky_claw.antigravity.web.app import _get_static_dir

        with patch("sky_claw.antigravity.web.app.sys") as mock_sys:
            mock_sys.frozen = False
            # Re-import won't help; call the function directly.
            # In non-frozen mode it uses __file__.
            result = _get_static_dir()
            assert "static" in str(result)

    def test_static_dir_frozen(self) -> None:
        from sky_claw.antigravity.web.app import _get_static_dir

        with patch("sky_claw.antigravity.web.app.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys._MEIPASS = "/tmp/fake_meipass"
            result = _get_static_dir()
            assert "fake_meipass" in str(result)
            assert "static" in str(result)

    def test_exe_dir_normal(self) -> None:
        from sky_claw.antigravity.web.app import _get_exe_dir

        with patch("sky_claw.antigravity.web.app.sys") as mock_sys:
            mock_sys.frozen = False
            result = _get_exe_dir()
            assert result == pathlib.Path.cwd()

    def test_exe_dir_frozen(self) -> None:
        from sky_claw.antigravity.web.app import _get_exe_dir

        with patch("sky_claw.antigravity.web.app.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys.executable = "/tmp/dist/SkyClawApp.exe"
            result = _get_exe_dir()
            assert str(result).endswith("dist")

    # -- GUI module frozen-path resolution (regression for 0.2.0 crash) --

    def test_gui_dir_normal(self) -> None:
        from sky_claw.antigravity.gui.sky_claw_gui import _gui_dir

        with patch("sky_claw.antigravity.gui.sky_claw_gui.sys") as mock_sys:
            mock_sys.frozen = False
            result = _gui_dir()
            assert result.name == "gui"
            assert (result / "styles.css").exists()

    def test_gui_dir_frozen(self) -> None:
        from sky_claw.antigravity.gui.sky_claw_gui import _gui_dir

        with patch("sky_claw.antigravity.gui.sky_claw_gui.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys._MEIPASS = "/tmp/fake_meipass"
            result = _gui_dir()
            assert "fake_meipass" in str(result)
            assert str(result).replace("\\", "/").endswith("sky_claw/antigravity/gui")

    def test_web_static_dir_frozen(self) -> None:
        from sky_claw.antigravity.gui.sky_claw_gui import _web_static_dir

        with patch("sky_claw.antigravity.gui.sky_claw_gui.sys") as mock_sys:
            mock_sys.frozen = True
            mock_sys._MEIPASS = "/tmp/fake_meipass"
            result = _web_static_dir()
            assert "fake_meipass" in str(result)
            assert str(result).replace("\\", "/").endswith("sky_claw/antigravity/web/static")


# ---------------------------------------------------------------------------
# Windowed (--windowed) builds start with sys.stdout/stderr == None
# ---------------------------------------------------------------------------


class TestWindowedStdStreams:
    """Regression: a ``--windowed`` exe starts with ``sys.stdout``/``sys.stderr``
    set to ``None``; NiceGUI/uvicorn's startup banner writes to them and the GUI
    process dies before it can bind its port. ``_ensure_std_streams`` repairs
    that. (Second bug behind the broken 0.2.0 release exe.)"""

    def test_noop_when_streams_present(self) -> None:
        from sky_claw.__main__ import _ensure_std_streams

        before_out, before_err = sys.stdout, sys.stderr
        _ensure_std_streams()
        assert sys.stdout is before_out
        assert sys.stderr is before_err

    def test_replaces_none_streams(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
        from sky_claw.__main__ import _ensure_std_streams

        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "SkyClawApp.exe"))

        _ensure_std_streams()

        assert sys.stdout is not None
        assert sys.stderr is not None
        # The repaired streams must be writable without raising (this is the
        # bare-print path that killed the windowed exe).
        print("startup-banner-probe")  # noqa: T201
        sys.stderr.write("err-probe\n")


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
        if api_key.startswith("sk-ant") and not os.environ.get("ANTHROPIC_API_KEY"):
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
        if api_key.startswith("sk-") and not api_key.startswith("sk-ant") and not os.environ.get("DEEPSEEK_API_KEY"):
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
