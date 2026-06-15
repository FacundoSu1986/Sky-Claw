"""Provider-scoped LLM model config: per-provider {provider}_model fields plus
one-time migration of the legacy global llm_model.

Eliminates the cross-provider fragility where a single global llm_model was
sent to every provider (switching providers carried a stale, incompatible
model). Each provider now has its own model; an unset one falls back to the
provider's DEFAULT_MODEL (resolved downstream), never to another provider's.
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture
def _no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *_a, **_k: None)
    monkeypatch.setattr(keyring, "set_password", lambda *_a, **_k: None)


def _cfg(tmp_path: pathlib.Path, body: str = ""):
    from sky_claw.config import Config

    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return Config(config_path=path)


class TestProviderModelFields:
    def test_each_provider_model_defaults_empty(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        cfg = _cfg(tmp_path)
        assert cfg.anthropic_model == ""
        assert cfg.deepseek_model == ""
        assert cfg.openai_model == ""
        assert cfg.ollama_model == ""

    def test_explicit_provider_model_is_loaded(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        cfg = _cfg(tmp_path, 'openai_model = "gpt-4o"\n')
        assert cfg.openai_model == "gpt-4o"


class TestLegacyModelMigration:
    def test_global_model_migrates_to_active_provider(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        """A legacy global llm_model is copied to the active provider's slot."""
        cfg = _cfg(tmp_path, 'llm_provider = "anthropic"\nllm_model = "claude-3-opus"\n')
        assert cfg.anthropic_model == "claude-3-opus"
        # Other providers must NOT inherit it (no cross-provider contamination).
        assert cfg.openai_model == ""
        assert cfg.deepseek_model == ""

    def test_migration_does_not_clobber_existing_provider_model(
        self, tmp_path: pathlib.Path, _no_keyring: None
    ) -> None:
        cfg = _cfg(
            tmp_path,
            'llm_provider = "openai"\nllm_model = "stale"\nopenai_model = "gpt-4o"\n',
        )
        assert cfg.openai_model == "gpt-4o"

    def test_no_migration_when_global_model_empty(self, tmp_path: pathlib.Path, _no_keyring: None) -> None:
        cfg = _cfg(tmp_path, 'llm_provider = "deepseek"\n')
        assert cfg.deepseek_model == ""
