from __future__ import annotations

import pytest

from sky_claw.config import ALLOWED_HOSTS, ALLOWED_METHODS, Config


def test_brave_host_is_allowlisted():
    assert "api.search.brave.com" in ALLOWED_HOSTS
    assert ALLOWED_METHODS["api.search.brave.com"] == frozenset(["GET"])


def test_search_api_key_is_a_known_secret(tmp_path):
    cfg = Config(tmp_path / "config.toml")
    # Default present (so the wizard/keyring path recognises it) and empty.
    assert cfg._data.get("search_api_key") == ""
