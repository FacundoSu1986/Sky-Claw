"""Tests for sky_claw.antigravity.agent.hermes_parser — zero-trust XML tool-call extraction."""

from __future__ import annotations

import pytest

from sky_claw.antigravity.agent.hermes_parser import extract_tool_calls, has_tool_calls


def test_has_tool_calls_true() -> None:
    text = 'Sure!\n<tool_call>\n{"name": "search_mod", "arguments": {"mod_name": "SKSE"}}\n</tool_call>'
    assert has_tool_calls(text) is True


def test_has_tool_calls_false() -> None:
    assert has_tool_calls("Just a normal reply.") is False


def test_extract_single_tool_call() -> None:
    text = '<tool_call>{"name": "run_loot_sort", "arguments": {"profile": "Default"}}</tool_call>'
    calls = extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "run_loot_sort"
    assert calls[0]["arguments"] == {"profile": "Default"}


def test_extract_multiple_tool_calls() -> None:
    text = (
        '<tool_call>{"name": "search_mod", "arguments": {"mod_name": "SKSE"}}</tool_call>'
        "\n"
        '<tool_call>{"name": "run_loot_sort", "arguments": {"profile": "Default"}}</tool_call>'
    )
    calls = extract_tool_calls(text)
    assert len(calls) == 2
    assert calls[0]["name"] == "search_mod"
    assert calls[1]["name"] == "run_loot_sort"


def test_extract_no_arguments_key() -> None:
    text = '<tool_call>{"name": "close_game"}</tool_call>'
    calls = extract_tool_calls(text)
    assert calls[0]["arguments"] == {}


def test_extract_malformed_json_raises() -> None:
    text = "<tool_call>NOT JSON</tool_call>"
    with pytest.raises(ValueError, match="Malformed JSON"):
        extract_tool_calls(text)


def test_extract_missing_name_raises() -> None:
    text = '<tool_call>{"arguments": {}}</tool_call>'
    with pytest.raises(ValueError, match="Missing 'name'"):
        extract_tool_calls(text)


def test_extract_ignores_surrounding_text() -> None:
    text = 'I\'ll call the tool now.\n<tool_call>{"name": "close_game"}</tool_call>\nDone.'
    calls = extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "close_game"


def test_extract_multiline_json() -> None:
    text = '<tool_call>\n{"name": "search_mod",\n "arguments": {"mod_name": "SkyUI"}}\n</tool_call>'
    calls = extract_tool_calls(text)
    assert calls[0]["arguments"]["mod_name"] == "SkyUI"


def test_extract_non_dict_payload_raises() -> None:
    text = "<tool_call>[1, 2, 3]</tool_call>"
    with pytest.raises(ValueError, match="must be a JSON object"):
        extract_tool_calls(text)


def test_extract_non_dict_arguments_raises() -> None:
    text = '<tool_call>{"name": "search_mod", "arguments": "bad"}</tool_call>'
    with pytest.raises(ValueError, match="'arguments' must be a JSON object"):
        extract_tool_calls(text)
