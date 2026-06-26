from __future__ import annotations

from unittest.mock import MagicMock

from sky_claw.antigravity.agent.tools import AsyncToolRegistry


def _registry(**kwargs):
    return AsyncToolRegistry(
        registry=MagicMock(), mo2=MagicMock(), sync_engine=MagicMock(), gateway=MagicMock(), **kwargs
    )


def test_search_nexus_is_advertised():
    reg = _registry()
    names = [t["name"] for t in reg.tool_schemas()]
    assert "search_nexus" in names
    schema = next(t for t in reg.tool_schemas() if t["name"] == "search_nexus")
    assert "query" in schema["input_schema"]["properties"]


def test_search_nexus_respects_allowlist():
    reg = _registry(allowed_tools={"search_mod"})
    names = [t["name"] for t in reg.tool_schemas()]
    assert "search_nexus" not in names  # filtered out when not in the allowlist
