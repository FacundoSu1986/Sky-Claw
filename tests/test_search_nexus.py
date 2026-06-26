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


# ── Task 2: SearchNexusParams ──────────────────────────────────────────────


def test_search_params_defaults_and_validation():
    from sky_claw.antigravity.agent.tools.schemas import SearchNexusParams

    p = SearchNexusParams.model_validate({"query": "armor"}, strict=True)
    assert p.query == "armor"
    assert p.min_downloads is None
    assert p.limit == 5

    p2 = SearchNexusParams.model_validate({"query": "armor", "min_downloads": 500, "limit": 3}, strict=True)
    assert p2.min_downloads == 500
    assert p2.limit == 3


def test_search_params_limit_capped_and_query_required():
    import pydantic

    from sky_claw.antigravity.agent.tools.schemas import SearchNexusParams

    with pytest.raises(pydantic.ValidationError):
        SearchNexusParams.model_validate({"query": "armor", "limit": 99}, strict=True)  # > 10
    with pytest.raises(pydantic.ValidationError):
        SearchNexusParams.model_validate({"query": ""}, strict=True)  # empty
