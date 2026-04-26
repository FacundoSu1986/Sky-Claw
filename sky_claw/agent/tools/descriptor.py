"""Descriptor de metadatos para herramientas registradas.

Este módulo define la clase ToolDescriptor para encapsular
los metadatos de una herramienta en el registro.

Extraído de tools.py como parte de la refactorización M-13.
TASK-012: añadido params_model para validación Pydantic strict pre-execución.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import pydantic


class ToolDescriptor:
    """Metadata for a single registered tool.

    Attributes:
        name: Unique tool name.
        description: Human-readable description for the LLM.
        input_schema: JSON Schema dict describing tool parameters.
        fn: The async callable that implements the tool.
        params_model: Optional Pydantic BaseModel subclass used by
            ``AsyncToolRegistry.execute`` to validate the LLM arguments
            BEFORE invoking ``fn``. When provided, the model must use
            ``ConfigDict(strict=True)``. When ``None``, the registry
            forwards the raw kwargs (for tools without arguments).
    """

    __slots__ = ("description", "fn", "input_schema", "name", "params_model")

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        fn: Callable[..., Awaitable[str]],
        params_model: type[pydantic.BaseModel] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.fn = fn
        self.params_model = params_model


__all__ = ["ToolDescriptor"]
