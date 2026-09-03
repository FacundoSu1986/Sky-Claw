"""Regression tests for the LLM-facing read-only xEdit script capability."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from sky_claw.app.agent.tools.system_tools import run_xedit_script


_READ_ONLY_SCRIPTS = (
    "dump_record_detail.pas",
    "list_all_conflicts.pas",
    "list_grass_worldspaces.pas",
    "list_zero_bound_grass.pas",
)


class _RunnerWithCanonicalStaging:
    """Small production-shaped double: methods live on the concrete class."""

    def __init__(self) -> None:
        self.staged: list[list[str]] = []
        self.runs: list[tuple[str, list[str]]] = []

    async def ensure_scripts_staged(self, script_names: list[str]) -> list[Any]:
        self.staged.append(list(script_names))
        return []

    async def run_script(self, script_name: str, plugins: list[str]) -> Any:
        self.runs.append((script_name, list(plugins)))
        return SimpleNamespace(
            success=True,
            return_code=0,
            processed_plugins=list(plugins),
            conflicts=[],
            errors=[],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("script_name", _READ_ONLY_SCRIPTS)
async def test_read_only_bundle_is_staged_then_executed(script_name: str) -> None:
    # Arrange
    runner = _RunnerWithCanonicalStaging()
    plugins = ["Skyrim.esm"]

    # Act
    result = json.loads(await run_xedit_script(runner, script_name, plugins))

    # Assert
    assert result["success"] is True
    assert runner.staged == [[script_name]]
    assert runner.runs == [(script_name, plugins)]


@pytest.mark.asyncio
async def test_legacy_alias_resolves_to_canonical_bundle_before_staging() -> None:
    # Arrange
    runner = _RunnerWithCanonicalStaging()

    # Act
    result = json.loads(await run_xedit_script(runner, "list_conflicts.pas", ["Skyrim.esm"]))

    # Assert
    assert result["success"] is True
    assert runner.staged == [["list_all_conflicts.pas"]]
    assert runner.runs == [("list_all_conflicts.pas", ["Skyrim.esm"])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script_name",
    (
        "apply_leveled_list_merge.pas",
        "evil_local.pas",
    ),
)
async def test_non_read_only_or_unknown_script_is_rejected_before_runner(script_name: str) -> None:
    # Arrange
    runner = _RunnerWithCanonicalStaging()

    # Act
    result = json.loads(await run_xedit_script(runner, script_name, ["Skyrim.esm"]))

    # Assert
    assert result["success"] is False
    assert "not allowed" in result["error"]
    assert runner.staged == []
    assert runner.runs == []


@pytest.mark.asyncio
async def test_no_runner_contract_precedes_script_policy() -> None:
    # Arrange / Act
    result = json.loads(await run_xedit_script(None, "test.pas", ["test.esp"]))

    # Assert
    assert result == {"error": "xEdit runner is not configured"}


@pytest.mark.asyncio
async def test_cancelled_run_propagates_cancelled_error() -> None:
    class _CancelledRunner(_RunnerWithCanonicalStaging):
        async def run_script(self, script_name: str, plugins: list[str]) -> Any:
            self.runs.append((script_name, list(plugins)))
            raise asyncio.CancelledError

    # Arrange
    runner = _CancelledRunner()

    # Act / Assert
    with pytest.raises(asyncio.CancelledError):
        await run_xedit_script(runner, "list_all_conflicts.pas", ["Skyrim.esm"])

    assert runner.staged == [["list_all_conflicts.pas"]]
    assert runner.runs == [("list_all_conflicts.pas", ["Skyrim.esm"])]
