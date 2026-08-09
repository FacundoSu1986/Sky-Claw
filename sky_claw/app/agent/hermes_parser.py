"""Shim de compatibilidad para el nombre legado del parser XML de tools."""

from sky_claw.app.agent.xml_tool_call_parser import TOOL_CALL_RE, extract_tool_calls, has_tool_calls

__all__ = ["TOOL_CALL_RE", "extract_tool_calls", "has_tool_calls"]
