"""Tests de AsyncToolRegistry.xml_tool_prompt_block()."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry


@pytest.fixture()
def registry(tmp_path: pathlib.Path) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
    )


def test_xml_tool_block_wraps_in_tools_tag(registry: AsyncToolRegistry) -> None:
    block = registry.xml_tool_prompt_block()
    assert block.startswith("<tools>")
    assert block.endswith("</tools>")


def test_xml_tool_block_contains_all_tool_names(registry: AsyncToolRegistry) -> None:
    block = registry.xml_tool_prompt_block()
    inner = block[len("<tools>") : block.rfind("</tools>")].strip()
    schemas = json.loads(inner)
    names = {s["name"] for s in schemas}
    assert names == set(registry._tools)


def test_xml_tool_block_uses_parameters_key(registry: AsyncToolRegistry) -> None:
    block = registry.xml_tool_prompt_block()
    inner = block[len("<tools>") : block.rfind("</tools>")].strip()
    schemas = json.loads(inner)
    for schema in schemas:
        assert "parameters" in schema
        assert "input_schema" not in schema


def test_legacy_prompt_method_matches_canonical_method(registry: AsyncToolRegistry) -> None:
    assert registry.hermes_system_prompt_block() == registry.xml_tool_prompt_block()
