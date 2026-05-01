"""Descriptor de metadatos para herramientas registradas.

Este módulo define la clase ToolDescriptor para encapsular
los metadatos de una herramienta en el registro.

Extraído de tools.py como parte de la refactorización M-13.

TASK-011 Single Source of Truth: Added params_model field to link
each tool to its Pydantic validation model, enabling centralized
validation in AsyncToolRegistry.execute() and dynamic JSON schema
generation via model_json_schema().
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import BaseModel


class ToolDescriptor:
    """Metadata for a single registered tool.

    Attributes:
        name: Unique tool name.
        description: Human-readable description for the LLM.
        input_schema: JSON Schema dict describing tool parameters.
            Auto-generated from ``params_model`` when provided and
            ``input_schema`` is not explicitly given.
        fn: The async callable that implements the tool.
        params_model: Optional Pydantic BaseModel class used for
            centralized parameter validation in ``execute()``.
            When provided, ``input_schema`` is derived dynamically
            via ``model_json_schema()`` + ``_clean_schema()``.
    """

    __slots__ = ("description", "fn", "input_schema", "name", "params_model")

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None = None,
        fn: Callable[..., Awaitable[str]] | None = None,
        params_model: type[BaseModel] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.params_model = params_model

        # Single Source of Truth: generate schema from Pydantic model
        # when input_schema is not explicitly provided.
        if input_schema is not None:
            self.input_schema = input_schema
        elif params_model is not None:
            from sky_claw.antigravity.agent.tools.schemas import _clean_schema

            self.input_schema = _clean_schema(params_model.model_json_schema())
        else:
            self.input_schema = {"type": "object", "properties": {}}

        self.fn = fn  # type: ignore[assignment]


__all__ = ["ToolDescriptor"]
