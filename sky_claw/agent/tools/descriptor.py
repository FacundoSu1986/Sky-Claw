"""Descriptor de metadatos para herramientas registradas.

Este módulo define la clase ToolDescriptor para encapsular
los metadatos de una herramienta en el registro.

Extraído de tools.py como parte de la refactorización M-13.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class ToolDescriptor:
    """Metadata for a single registered tool.

    Attributes:
        name: Unique tool name.
        description: Human-readable description for the LLM.
        input_schema: JSON Schema dict describing tool parameters
        fn: The async callable that implements the tool
    """

    __slots__ = ("description", "fn", "input_schema", "name")

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        fn: Callable[..., Awaitable[str]],
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.fn = fn


__all__ = ["ToolDescriptor"]
