"""Contrato de resultado compartido de los tools (cierra la deuda #5 de CLAUDE.md).

Histórico: cada servicio reportaba el detalle del error bajo una clave distinta
(LOOT/Pandora/QuickAutoClean → ``logs``, DynDOLOD/LOOT → ``errors`` lista,
xEdit-patch → ``error``/``details``, runners → ``stderr``), y el consumidor
(`summarize_ritual_result`) tenía que adivinar encadenándolas — cada shape nueva
reintroducía el toast opaco "error desconocido" (parcheado en #214 y #216).

Contrato:
- Los servicios emiten ``success: bool`` y ``message: str`` (canónico) además de
  sus campos estructurados existentes (aditivo, sin breaking changes).
- :func:`normalize_tool_result` es la ÚNICA pieza que conoce las claves legacy;
  cualquier consumidor la usa en vez de inspeccionar el dict crudo.

Módulo puro: sin imports del proyecto, importable desde cualquier capa.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ToolResult(TypedDict):
    """Vista normalizada del resultado de un tool."""

    success: bool
    message: str
    return_code: int | None
    warnings: list[str]


#: Claves legacy consultadas en orden de especificidad cuando falta ``message``.
_LEGACY_DETAIL_KEYS: tuple[str, ...] = ("details", "error", "logs", "stderr")


def normalize_tool_result(raw: dict[str, Any]) -> ToolResult:
    """Normaliza el dict crudo de un servicio/tool al contrato :class:`ToolResult`.

    Reglas:
    - ``success``: prioriza el booleano explícito ``raw["success"]``; si falta,
      ``raw["status"] == "success"``.
    - ``message``: prioriza el canónico ``raw["message"]``; si falta, cae en
      orden determinista a ``details → error → logs → stderr → errors (lista
      unida) → reason``. En fallo sin dato alguno: ``"error desconocido"``
      (único sitio del sistema donde puede originarse ese texto). En éxito sin
      message: cadena vacía — el consumidor arma su propio copy.
    """
    success = bool(raw["success"]) if "success" in raw else str(raw.get("status", "")).lower() == "success"

    message = _extract_message(raw)
    if not message and not success:
        message = "error desconocido"

    return_code = raw.get("return_code")
    warnings_raw = raw.get("warnings")
    warnings = [str(w) for w in warnings_raw] if isinstance(warnings_raw, (list, tuple)) else []

    return ToolResult(
        success=success,
        message=message,
        return_code=int(return_code) if isinstance(return_code, int) else None,
        warnings=warnings,
    )


def _extract_message(raw: dict[str, Any]) -> str:
    """Extrae el detalle humano: ``message`` canónico o las claves legacy."""
    canonical = raw.get("message")
    if canonical:
        return str(canonical)
    for key in _LEGACY_DETAIL_KEYS:
        value = raw.get(key)
        if value:
            return str(value)
    errors = raw.get("errors")
    if isinstance(errors, (list, tuple)) and errors:
        return "; ".join(str(e) for e in errors)
    reason = raw.get("reason")
    if reason:
        return str(reason)
    return ""
