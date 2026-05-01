"""Characterization tests for SupervisorAgent.dispatch_tool.

These tests lock in the EXACT behavior of the public dispatch_tool contract
(signature, error shapes, tool routing) independently of implementation.
They serve as the regression net for the Strangler Fig extraction to
OrchestrationToolDispatcher + tool_strategies/.

Construction strategy: SupervisorAgent.__new__ skips __init__ (which is heavy:
DB, journal, lock manager, services). Tests only inject the attributes
dispatch_tool actually reads.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.models import HitlApprovalRequest, LootExecutionParams
from sky_claw.antigravity.core.schemas import ScrapingQuery
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.orchestrator.tool_dispatcher import build_orchestration_dispatcher
from sky_claw.local.xedit.conflict_analyzer import ConflictReport


@pytest.fixture
def supervisor() -> SupervisorAgent:
    """Construction-free SupervisorAgent with only dispatch_tool's collaborators."""
    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup.scraper = MagicMock()
    sup.scraper.query_nexus = AsyncMock()
    sup.tools = MagicMock()
    sup.tools.run_loot = AsyncMock()
    sup.interface = MagicMock()
    sup.interface.request_hitl = AsyncMock()
    sup._synthesis_service = MagicMock()
    sup._synthesis_service.execute_pipeline = AsyncMock()
    sup._xedit_service = MagicMock()
    sup._xedit_service.execute_patch = AsyncMock()
    sup._dyndolod_service = MagicMock()
    sup._dyndolod_service.execute = AsyncMock()
    sup.profile_name = "TestProfile"
    sup._tool_dispatcher = build_orchestration_dispatcher(sup)
    return sup


# ---------------------------------------------------------------------------
# query_mod_metadata
# ---------------------------------------------------------------------------


async def test_query_mod_metadata_validates_with_pydantic(supervisor):
    """Invalid payload raises pydantic.ValidationError; valid payload calls scraper.query_nexus
    with a ScrapingQuery instance and returns model_dump() of the result."""
    fake_metadata = MagicMock()
    fake_metadata.model_dump.return_value = {"mod_id": 42, "name": "TestMod"}
    supervisor.scraper.query_nexus.return_value = fake_metadata

    result = await supervisor.dispatch_tool("query_mod_metadata", {"query": "skyrim cool mod"})

    supervisor.scraper.query_nexus.assert_awaited_once()
    call_args = supervisor.scraper.query_nexus.await_args.args
    assert isinstance(call_args[0], ScrapingQuery)
    assert call_args[0].query == "skyrim cool mod"
    assert result == {"mod_id": 42, "name": "TestMod"}


async def test_query_mod_metadata_invalid_payload_raises(supervisor):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await supervisor.dispatch_tool("query_mod_metadata", {"query": ""})  # min_length=1


# ---------------------------------------------------------------------------
# execute_loot_sorting (HITL-gated)
# ---------------------------------------------------------------------------


async def test_execute_loot_sorting_hitl_approved(supervisor):
    """When interface.request_hitl returns 'approved', tools.run_loot is called with LootExecutionParams."""
    supervisor.interface.request_hitl.return_value = "approved"
    supervisor.tools.run_loot.return_value = {"status": "ok"}

    result = await supervisor.dispatch_tool(
        "execute_loot_sorting",
        {"profile_name": "MyProfile", "update_masterlist": False},
    )

    supervisor.interface.request_hitl.assert_awaited_once()
    hitl_req = supervisor.interface.request_hitl.await_args.args[0]
    assert isinstance(hitl_req, HitlApprovalRequest)
    assert hitl_req.context_data == {"profile": "MyProfile"}

    supervisor.tools.run_loot.assert_awaited_once()
    loot_params = supervisor.tools.run_loot.await_args.args[0]
    assert isinstance(loot_params, LootExecutionParams)
    assert loot_params.profile_name == "MyProfile"
    assert loot_params.update_masterlist is False
    assert result == {"status": "ok"}


async def test_execute_loot_sorting_hitl_denied(supervisor):
    """When HITL is denied, returns the exact aborted dict (Spanish string preserved)."""
    supervisor.interface.request_hitl.return_value = "denied"

    result = await supervisor.dispatch_tool(
        "execute_loot_sorting",
        {"profile_name": "Default", "update_masterlist": True},
    )

    assert result == {"status": "aborted", "reason": "Usuario denegó la operación."}
    supervisor.tools.run_loot.assert_not_awaited()


# ---------------------------------------------------------------------------
# execute_synthesis_pipeline (try/except + dict guard)
# ---------------------------------------------------------------------------


async def test_execute_synthesis_pipeline_success(supervisor):
    supervisor._synthesis_service.execute_pipeline.return_value = {"status": "ok", "patches": 3}

    result = await supervisor.dispatch_tool("execute_synthesis_pipeline", {"patcher_ids": ["a"]})

    supervisor._synthesis_service.execute_pipeline.assert_awaited_once_with(patcher_ids=["a"])
    assert result == {"status": "ok", "patches": 3}


async def test_execute_synthesis_pipeline_exception_wrapped(supervisor):
    supervisor._synthesis_service.execute_pipeline.side_effect = RuntimeError("boom")

    result = await supervisor.dispatch_tool("execute_synthesis_pipeline", {"patcher_ids": ["a"]})

    assert result["status"] == "error"
    assert result["reason"] == "SynthesisPipelineExecutionFailed"
    assert "boom" in result["details"]


async def test_execute_synthesis_pipeline_non_dict_result(supervisor):
    supervisor._synthesis_service.execute_pipeline.return_value = "oops not a dict"

    result = await supervisor.dispatch_tool("execute_synthesis_pipeline", {"patcher_ids": ["a"]})

    assert result == {"status": "error", "reason": "InvalidSynthesisPipelineResult"}


# ---------------------------------------------------------------------------
# resolve_conflict_with_patch (try/except + dict guard + ConflictReport ctor)
# ---------------------------------------------------------------------------


async def test_resolve_conflict_with_patch_success(supervisor):
    import pathlib

    supervisor._xedit_service.execute_patch.return_value = {"status": "ok"}
    payload = {
        "target_plugin": "Skyrim.esm",
        "report": {"total_conflicts": 5, "critical_conflicts": 2},
    }

    result = await supervisor.dispatch_tool("resolve_conflict_with_patch", payload)

    supervisor._xedit_service.execute_patch.assert_awaited_once()
    kwargs = supervisor._xedit_service.execute_patch.await_args.kwargs
    assert isinstance(kwargs["target_plugin"], pathlib.Path)
    assert str(kwargs["target_plugin"]) == "Skyrim.esm"
    assert isinstance(kwargs["report"], ConflictReport)
    assert kwargs["report"].total_conflicts == 5
    assert kwargs["report"].critical_conflicts == 2
    assert result == {"status": "ok"}


async def test_resolve_conflict_with_patch_exception_wrapped(supervisor):
    supervisor._xedit_service.execute_patch.side_effect = RuntimeError("xedit crashed")
    payload = {
        "target_plugin": "Skyrim.esm",
        "report": {"total_conflicts": 0, "critical_conflicts": 0},
    }

    result = await supervisor.dispatch_tool("resolve_conflict_with_patch", payload)

    assert result["status"] == "error"
    assert result["reason"] == "XEditPatchExecutionFailed"
    assert "xedit crashed" in result["details"]


async def test_resolve_conflict_with_patch_non_dict_result(supervisor):
    supervisor._xedit_service.execute_patch.return_value = ["not", "a", "dict"]
    payload = {
        "target_plugin": "Skyrim.esm",
        "report": {"total_conflicts": 0, "critical_conflicts": 0},
    }

    result = await supervisor.dispatch_tool("resolve_conflict_with_patch", payload)

    assert result == {"status": "error", "reason": "InvalidXEditPatchResult"}


# ---------------------------------------------------------------------------
# generate_lods
# ---------------------------------------------------------------------------


async def test_generate_lods_delegates(supervisor):
    supervisor._dyndolod_service.execute.return_value = {"status": "ok", "lods": 1234}

    result = await supervisor.dispatch_tool("generate_lods", {"preset": "High", "run_texgen": False})

    supervisor._dyndolod_service.execute.assert_awaited_once_with(preset="High", run_texgen=False)
    assert result == {"status": "ok", "lods": 1234}


async def test_generate_lods_filters_extra_llm_keys(supervisor):
    """VULN-2 fix: Extra keys injected by the LLM (e.g. 'tool_name') are filtered out."""
    supervisor._dyndolod_service.execute.return_value = {"status": "ok"}

    result = await supervisor.dispatch_tool(
        "generate_lods",
        {"preset": "Medium", "tool_name": "generate_lods", "spurious": 42},
    )

    # Only valid keys should be forwarded
    supervisor._dyndolod_service.execute.assert_awaited_once_with(preset="Medium")
    assert result == {"status": "ok"}


async def test_execute_synthesis_pipeline_filters_extra_llm_keys(supervisor):
    """VULN-2 fix: Extra keys injected by the LLM are filtered out."""
    supervisor._synthesis_service.execute_pipeline.return_value = {"status": "ok"}

    result = await supervisor.dispatch_tool(
        "execute_synthesis_pipeline",
        {"patcher_ids": ["a"], "tool_name": "execute_synthesis_pipeline", "extra": True},
    )

    supervisor._synthesis_service.execute_pipeline.assert_awaited_once_with(patcher_ids=["a"])
    assert result == {"status": "ok"}


# ---------------------------------------------------------------------------
# scan_asset_conflicts (raw + JSON variants)
# ---------------------------------------------------------------------------


async def test_scan_asset_conflicts_returns_dataclass_dicts(supervisor):
    @dataclasses.dataclass
    class FakeConflict:
        path: str
        severity: str

    fake = [FakeConflict(path="a.dds", severity="warn"), FakeConflict(path="b.nif", severity="info")]
    supervisor.scan_asset_conflicts = MagicMock(return_value=fake)

    result = await supervisor.dispatch_tool("scan_asset_conflicts", {})

    assert result["status"] == "success"
    assert result["conflicts"] == [
        {"path": "a.dds", "severity": "warn"},
        {"path": "b.nif", "severity": "info"},
    ]


async def test_scan_asset_conflicts_json(supervisor):
    supervisor.scan_asset_conflicts_json = MagicMock(return_value='{"foo": "bar"}')

    result = await supervisor.dispatch_tool("scan_asset_conflicts_json", {})

    assert result == {"status": "success", "json_report": '{"foo": "bar"}'}


# ---------------------------------------------------------------------------
# generate_bashed_patch (delegates to supervisor method)
# ---------------------------------------------------------------------------


async def test_generate_bashed_patch_delegates(supervisor):
    supervisor.execute_wrye_bash_pipeline = AsyncMock(return_value={"success": True, "return_code": 0})

    result = await supervisor.dispatch_tool(
        "generate_bashed_patch",
        {"profile": "MyProfile", "validate_limit": False},
    )

    supervisor.execute_wrye_bash_pipeline.assert_awaited_once_with(profile="MyProfile", validate_limit=False)
    assert result == {"success": True, "return_code": 0}


async def test_generate_bashed_patch_filters_extra_llm_keys(supervisor):
    """VULN-2 fix: Extra keys injected by the LLM are filtered out."""
    supervisor.execute_wrye_bash_pipeline = AsyncMock(return_value={"success": True})

    result = await supervisor.dispatch_tool(
        "generate_bashed_patch",
        {"profile": "P", "validate_limit": True, "tool_name": "generate_bashed_patch", "extra": 42},
    )

    supervisor.execute_wrye_bash_pipeline.assert_awaited_once_with(profile="P", validate_limit=True)
    assert result == {"success": True}


# ---------------------------------------------------------------------------
# validate_plugin_limit (default + explicit profile)
# ---------------------------------------------------------------------------


async def test_validate_plugin_limit_default_profile(supervisor):
    supervisor._run_plugin_limit_guard = AsyncMock(return_value={"valid": True})

    result = await supervisor.dispatch_tool("validate_plugin_limit", {})

    supervisor._run_plugin_limit_guard.assert_awaited_once_with("TestProfile")
    assert result == {"valid": True}


async def test_validate_plugin_limit_explicit_profile(supervisor):
    supervisor._run_plugin_limit_guard = AsyncMock(return_value={"valid": False, "error": "too many"})

    result = await supervisor.dispatch_tool("validate_plugin_limit", {"profile": "Other"})

    supervisor._run_plugin_limit_guard.assert_awaited_once_with("Other")
    assert result == {"valid": False, "error": "too many"}


# ---------------------------------------------------------------------------
# Unknown tool fallback
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_tool_not_found(supervisor):
    """LLM hallucinated tool name → exact legacy error dict."""
    result = await supervisor.dispatch_tool("nonexistent_tool", {"anything": 1})

    assert result == {"status": "error", "reason": "ToolNotFound"}


# ---------------------------------------------------------------------------
# _create_hitl_request (VULN-1 fix: ghost method now exists)
# ---------------------------------------------------------------------------


def test_create_hitl_request_circuit_breaker_halt(supervisor):
    """Converts a circuit_breaker_halt dict from the graph to HitlApprovalRequest."""
    hitl_dict = {
        "action_type": "circuit_breaker_halt",
        "reason": "Loop detected: patch_plugin called 3 times",
    }
    result = supervisor._create_hitl_request(hitl_dict)

    assert isinstance(result, HitlApprovalRequest)
    assert result.action_type == "circuit_breaker_halt"
    assert "Loop detected" in result.reason


def test_create_hitl_request_destructive_xedit(supervisor):
    """Converts a destructive_xedit dict to HitlApprovalRequest."""
    hitl_dict = {
        "action_type": "destructive_xedit",
        "reason": "xEdit patch requires approval",
    }
    result = supervisor._create_hitl_request(hitl_dict)

    assert isinstance(result, HitlApprovalRequest)
    assert result.action_type == "destructive_xedit"


def test_create_hitl_request_defaults_to_circuit_breaker(supervisor):
    """When action_type is missing, defaults to circuit_breaker_halt."""
    hitl_dict = {"reason": "some reason"}
    result = supervisor._create_hitl_request(hitl_dict)

    assert isinstance(result, HitlApprovalRequest)
    assert result.action_type == "circuit_breaker_halt"
    assert result.reason == "some reason"


def test_create_hitl_request_empty_reason(supervisor):
    """When reason is missing, defaults to empty string."""
    hitl_dict = {"action_type": "download_external"}
    result = supervisor._create_hitl_request(hitl_dict)

    assert isinstance(result, HitlApprovalRequest)
    assert result.action_type == "download_external"
    assert result.reason == ""


def test_create_hitl_request_preserves_context_data(supervisor):
    """context_data from the graph callback is forwarded to HitlApprovalRequest."""
    hitl_dict = {
        "action_type": "circuit_breaker_halt",
        "reason": "guardrail tripped",
        "context_data": {"loop_count": 3, "tool_name": "patch_plugin"},
    }
    result = supervisor._create_hitl_request(hitl_dict)

    assert isinstance(result, HitlApprovalRequest)
    assert result.context_data == {"loop_count": 3, "tool_name": "patch_plugin"}


def test_create_hitl_request_defaults_context_data(supervisor):
    """When context_data is missing, defaults to empty dict."""
    hitl_dict = {"action_type": "circuit_breaker_halt", "reason": "test"}
    result = supervisor._create_hitl_request(hitl_dict)

    assert isinstance(result, HitlApprovalRequest)
    assert result.context_data == {}
