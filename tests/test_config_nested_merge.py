"""Audit #153 (S-3) — Config._load_from_file must not re-inject nested dicts after extraction.

Original code (sky_claw/config.py:158-160) extracts ``[telegram] token`` into
the flat key ``telegram_bot_token`` and then runs ``self._data.update(file_data)``,
which silently re-introduces the raw nested ``{"telegram": {...}}`` dict. The
result is a confusing state where ``_data["telegram"]`` is a dict alongside
the extracted ``_data["telegram_bot_token"]`` — exactly the precedence
confusion the audit flagged.

Contracts verified here:
- Nested-only TOML: extracted flat key wins, no raw nested dict leaks in.
- Top-level-only TOML: behavior unchanged (no nested key appears).
- Mixed TOML (nested + top-level): top-level wins explicitly (it is the
  override path), nested form does not survive as a dict.
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture
def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyring lookups must not hit the real OS during these tests."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *_a, **_k: None)
    monkeypatch.setattr(keyring, "set_password", lambda *_a, **_k: None)


def _write_toml(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(body, encoding="utf-8")
    return path


class TestNestedExtractionLeavesNoRawDict:
    def test_nested_telegram_section_does_not_leak_raw_dict(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        """``[telegram] token = "X"`` produces flat ``telegram_bot_token``,
        not a raw ``_data["telegram"]`` dict.
        """
        from sky_claw.config import Config

        toml = _write_toml(
            tmp_path / "config.toml",
            '[telegram]\ntoken = "X"\nchat_id = "abc"\n',
        )

        cfg = Config(config_path=toml)

        assert cfg._data.get("telegram_bot_token") == "X"
        assert cfg._data.get("telegram_chat_id") == "abc"
        assert "telegram" not in cfg._data, (
            "raw nested 'telegram' dict must not survive in _data — the extraction is the canonical form"
        )

    def test_nested_nexus_section_does_not_leak_raw_dict(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        """``[nexus] api_key`` produces ``nexus_api_key`` flat, no raw nexus dict."""
        from sky_claw.config import Config

        toml = _write_toml(tmp_path / "config.toml", '[nexus]\napi_key = "K"\n')

        cfg = Config(config_path=toml)

        assert cfg._data.get("nexus_api_key") == "K"
        assert "nexus" not in cfg._data

    def test_nested_paths_section_does_not_leak_raw_dict(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        """``[paths]`` produces flat ``mo2_root`` / ``skyrim_path``, no raw paths dict."""
        from sky_claw.config import Config

        toml = _write_toml(
            tmp_path / "config.toml",
            '[paths]\nmo2_path = "/m"\nskyrim_path = "/s"\n',
        )

        cfg = Config(config_path=toml)

        assert cfg._data.get("mo2_root") == "/m"
        assert cfg._data.get("skyrim_path") == "/s"
        assert "paths" not in cfg._data


class TestPrecedenceMixedFormats:
    def test_top_level_override_wins_over_nested(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        """Mixed format: top-level explicit override beats nested section."""
        from sky_claw.config import Config

        # In TOML, top-level keys must appear BEFORE any [section] header,
        # otherwise they get assigned to the previous section. Order matters.
        toml = _write_toml(
            tmp_path / "config.toml",
            'telegram_bot_token = "from-top-level"\n\n[telegram]\ntoken = "from-nested"\n',
        )

        cfg = Config(config_path=toml)

        assert cfg._data["telegram_bot_token"] == "from-top-level", (
            "top-level explicit override must win — this is the documented precedence for the dual-format support"
        )
        assert "telegram" not in cfg._data


class TestTopLevelOnlyUntouched:
    def test_flat_top_level_keys_work_without_nested_sections(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        """Pure top-level config keeps the original simple behavior."""
        from sky_claw.config import Config

        toml = _write_toml(
            tmp_path / "config.toml",
            'telegram_bot_token = "Z"\nllm_provider = "openai"\n',
        )

        cfg = Config(config_path=toml)

        assert cfg._data["telegram_bot_token"] == "Z"
        assert cfg._data["llm_provider"] == "openai"
