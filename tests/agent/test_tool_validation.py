"""TASK-012 — Regression tests for centralized Pydantic validation.

These tests pin down the contract that ``AsyncToolRegistry.execute``
validates every LLM-supplied argument dict against the descriptor's
``params_model`` BEFORE invoking the handler, and that
``LLMRouter`` surfaces the resulting ``pydantic.ValidationError`` to the
model as structured, self-correcting feedback rather than crashing.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pydantic
import pytest

from sky_claw.antigravity.agent.router import _format_validation_feedback
from sky_claw.antigravity.agent.tools import (
    AsyncToolRegistry,
    ProfileParams,
    SearchModParams,
)
from sky_claw.antigravity.agent.tools.descriptor import ToolDescriptor


@pytest.fixture()
def registry(tmp_path: pathlib.Path) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
    )


# ---------------------------------------------------------------------------
# AsyncToolRegistry.execute — strict validation on entry
# ---------------------------------------------------------------------------


class TestExecuteValidation:
    """The centralized ``execute`` MUST raise ValidationError before the handler runs."""

    @pytest.mark.asyncio
    async def test_search_mod_rejects_wrong_type(self, registry: AsyncToolRegistry) -> None:
        with pytest.raises(pydantic.ValidationError):
            await registry.execute("search_mod", {"mod_name": 123})

    @pytest.mark.asyncio
    async def test_search_mod_rejects_empty_string(self, registry: AsyncToolRegistry) -> None:
        with pytest.raises(pydantic.ValidationError):
            await registry.execute("search_mod", {"mod_name": ""})

    @pytest.mark.asyncio
    async def test_check_load_order_rejects_invalid_pattern(self, registry: AsyncToolRegistry) -> None:
        # ProfileParams pattern excludes ';' to defeat CLI argument injection.
        with pytest.raises(pydantic.ValidationError):
            await registry.execute("check_load_order", {"profile": "Default; rm -rf /"})

    @pytest.mark.asyncio
    async def test_install_mod_rejects_negative_id(self, registry: AsyncToolRegistry) -> None:
        with pytest.raises(pydantic.ValidationError):
            await registry.execute("install_mod", {"nexus_id": -1, "version": "1.0"})

    @pytest.mark.asyncio
    async def test_install_mod_rejects_string_for_int(self, registry: AsyncToolRegistry) -> None:
        # strict=True forbids "42" -> 42 coercion.
        with pytest.raises(pydantic.ValidationError):
            await registry.execute("install_mod", {"nexus_id": "42", "version": "1.0"})

    @pytest.mark.asyncio
    async def test_unknown_tool_raises_keyerror(self, registry: AsyncToolRegistry) -> None:
        with pytest.raises(KeyError, match="Unknown tool"):
            await registry.execute("does_not_exist", {})

    @pytest.mark.asyncio
    async def test_handler_receives_validated_kwargs(self, tmp_path: pathlib.Path) -> None:
        """Custom tool with ``params_model`` to prove kwargs are passed through after validation."""
        seen: dict[str, object] = {}

        async def handler(mod_name: str) -> str:
            seen["mod_name"] = mod_name
            return json.dumps({"ok": True})

        mo2 = MagicMock()
        mo2.root = tmp_path
        reg = AsyncToolRegistry(
            registry=MagicMock(),
            mo2=mo2,
            sync_engine=MagicMock(),
        )
        reg._tools["custom_tool"] = ToolDescriptor(
            name="custom_tool",
            description="Test tool with strict validation",
            input_schema={"type": "object", "properties": {"mod_name": {"type": "string"}}},
            fn=handler,
            params_model=SearchModParams,
        )

        result = await reg.execute("custom_tool", {"mod_name": "SkyUI"})
        assert json.loads(result) == {"ok": True}
        assert seen["mod_name"] == "SkyUI"

    @pytest.mark.asyncio
    async def test_descriptor_without_params_model_passes_raw_kwargs(self, tmp_path: pathlib.Path) -> None:
        """Tools without ``params_model`` (e.g. zero-arg tools) bypass validation."""
        called: dict[str, bool] = {"yes": False}

        async def handler() -> str:
            called["yes"] = True
            return json.dumps({"ok": True})

        mo2 = MagicMock()
        mo2.root = tmp_path
        reg = AsyncToolRegistry(
            registry=MagicMock(),
            mo2=mo2,
            sync_engine=MagicMock(),
        )
        reg._tools["noargs"] = ToolDescriptor(
            name="noargs",
            description="No-arg tool",
            input_schema={"type": "object", "properties": {}},
            fn=handler,
            params_model=None,
        )

        result = await reg.execute("noargs", {})
        assert json.loads(result) == {"ok": True}
        assert called["yes"] is True


# ---------------------------------------------------------------------------
# tool_schemas — derived from Pydantic
# ---------------------------------------------------------------------------


class TestSchemaDerivation:
    """``tool_schemas`` must derive from ``model_json_schema`` for tools with ``params_model``."""

    def test_schema_is_clean_for_anthropic(self, registry: AsyncToolRegistry) -> None:
        schemas = registry.tool_schemas()
        by_name = {s["name"]: s["input_schema"] for s in schemas}

        # Root-level 'title' (which Pydantic injects automatically) MUST be stripped.
        for name, schema in by_name.items():
            assert "title" not in schema, f"tool {name!r} schema still has root 'title'"

        # Per-property 'title' MUST be stripped too.
        for name, schema in by_name.items():
            for prop_name, prop_schema in schema.get("properties", {}).items():
                if isinstance(prop_schema, dict):
                    assert "title" not in prop_schema, f"tool {name!r} property {prop_name!r} still has 'title'"

    def test_search_mod_schema_matches_pydantic(self, registry: AsyncToolRegistry) -> None:
        schemas = {s["name"]: s["input_schema"] for s in registry.tool_schemas()}
        # The shape must reflect SearchModParams: required mod_name str
        assert schemas["search_mod"]["type"] == "object"
        assert "mod_name" in schemas["search_mod"]["properties"]
        assert schemas["search_mod"]["properties"]["mod_name"]["type"] == "string"
        assert "mod_name" in schemas["search_mod"]["required"]

    def test_install_mod_schema_has_constraints(self, registry: AsyncToolRegistry) -> None:
        schemas = {s["name"]: s["input_schema"] for s in registry.tool_schemas()}
        nexus = schemas["install_mod"]["properties"]["nexus_id"]
        # Pydantic exposes the gt=0 constraint as exclusiveMinimum=0
        assert nexus.get("exclusiveMinimum") == 0


# ---------------------------------------------------------------------------
# _format_validation_feedback — structured constructive error for the LLM
# ---------------------------------------------------------------------------


class TestValidationFeedback:
    def test_feedback_has_required_keys(self) -> None:
        try:
            ProfileParams(profile="")
        except pydantic.ValidationError as exc:
            feedback = _format_validation_feedback("check_load_order", exc)

        assert feedback["error"].startswith("Invalid arguments")
        assert feedback["tool"] == "check_load_order"
        assert isinstance(feedback["validation_errors"], list)
        assert feedback["validation_errors"]
        first = feedback["validation_errors"][0]
        assert "field" in first
        assert "issue" in first
        assert "input" in first
        assert "instruction" in feedback

    def test_feedback_is_json_serializable(self) -> None:
        try:
            ProfileParams(profile=42)
        except pydantic.ValidationError as exc:
            feedback = _format_validation_feedback("check_load_order", exc)

        # default=str needed because Pydantic may include non-serializable types in 'input'.
        encoded = json.dumps(feedback, ensure_ascii=False, default=str)
        assert "Invalid arguments" in encoded
        assert "check_load_order" in encoded

    def test_feedback_lists_field_path(self) -> None:
        try:
            SearchModParams(mod_name="")  # min_length=1 violation
        except pydantic.ValidationError as exc:
            feedback = _format_validation_feedback("search_mod", exc)

        fields = [err["field"] for err in feedback["validation_errors"]]
        assert "mod_name" in fields


# ---------------------------------------------------------------------------
# Hermes mode counter independence
# ---------------------------------------------------------------------------


class TestHermesCounterIndependence:
    """Parse errors must not consume the execution-retry budget and vice versa.

    These tests target the LLMRouter chat loop with Hermes mode enabled.
    """

    @pytest.mark.asyncio
    async def test_parse_failures_do_not_block_subsequent_exec(self, tmp_path: pathlib.Path) -> None:
        """After a malformed XML reply, the loop must keep going.

        Pre-TASK-012 the single ``hermes_error_count`` counted parse and
        execution failures together, so a model that fumbled XML twice in
        a row could exhaust the budget reserved for legitimate execution
        retries. With separate counters, a single parse failure consumes
        only the parse budget and a subsequent valid tool_use must still
        be processed.
        """
        from sky_claw.antigravity.agent.router import LLMRouter

        mo2 = MagicMock()
        mo2.root = tmp_path
        # Stub the registry so search_mod returns immediately without DB I/O.
        reg = AsyncToolRegistry(
            registry=MagicMock(),
            mo2=mo2,
            sync_engine=MagicMock(),
        )

        async def stub_search(**_kwargs: object) -> str:
            return json.dumps({"matches": []})

        reg._tools["search_mod"].fn = stub_search  # type: ignore[assignment]

        # Stub provider yields: malformed xml -> valid tool_call -> done.
        valid_call = '<tool_call>{"name": "search_mod", "arguments": {"mod_name": "SKSE"}}</tool_call>'
        scripted = [
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "<tool_call>not-json</tool_call>"}],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": valid_call}],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done."}],
            },
        ]
        provider = MagicMock()
        provider.chat = AsyncMock(side_effect=scripted)

        router = LLMRouter(
            provider=provider,
            tool_registry=reg,
            db_path=str(tmp_path / "history.db"),
            hermes_mode=True,
        )
        await router.open()
        try:
            # See tests/test_router.py — SemanticRouter exposes classify(),
            # but LLMRouter.chat() calls .route(); mock it on the instance.
            router._semantic_router.route = MagicMock(  # type: ignore[method-assign]
                return_value={
                    "intent": "CHAT_GENERAL",
                    "confidence": 0.7,
                    "target_agent": None,
                    "tool_name": None,
                    "parameters": {},
                    "original_text": "",
                }
            )
            session = MagicMock()
            await router.chat("hi", session=session, chat_id="t1")
        finally:
            await router.close()

        # The provider must have been called more than once — a parse failure on
        # the first response should NOT have aborted the loop.
        assert provider.chat.await_count >= 2
