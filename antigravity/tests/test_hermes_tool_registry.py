"""Tests for AsyncToolRegistry.hermes_system_prompt_block()."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.antigravity.agent.tools import AsyncToolRegistry


@pytest.fixture()
def registry(tmp_path: pathlib.Path) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
    )


def test_hermes_block_wraps_in_tools_tag(registry: AsyncToolRegistry) -> None:
    block = registry.hermes_system_prompt_block()
    assert block.startswith("<tools>")
    assert block.endswith("</tools>")


def test_hermes_block_contains_all_tool_names(registry: AsyncToolRegistry) -> None:
    block = registry.hermes_system_prompt_block()
    inner = block[len("<tools>") : block.rfind("</tools>")].strip()
    schemas = json.loads(inner)
    names = {s["name"] for s in schemas}
    assert "search_mod" in names
    assert "run_loot_sort" in names


def test_hermes_block_uses_parameters_key(registry: AsyncToolRegistry) -> None:
    block = registry.hermes_system_prompt_block()
    inner = block[len("<tools>") : block.rfind("</tools>")].strip()
    schemas = json.loads(inner)
    for schema in schemas:
        assert "parameters" in schema
        assert "input_schema" not in schema
