"""Compatibility shim for the legacy XML tool-call parser module name."""

from sky_claw.app.agent.xml_tool_call_parser import TOOL_CALL_RE, extract_tool_calls, has_tool_calls

__all__ = ["TOOL_CALL_RE", "extract_tool_calls", "has_tool_calls"]
