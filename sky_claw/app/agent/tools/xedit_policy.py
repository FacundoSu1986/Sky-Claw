"""Política de capacidades xEdit expuestas al agente LLM."""

from __future__ import annotations

from typing import Literal, cast, get_args

# Fuente única de los nombres que el schema puede anunciar al LLM. El alias
# histórico sigue siendo una capacidad válida, pero siempre se canonicaliza
# antes del staging para que no pueda seleccionar un archivo local distinto.
XEditAgentScriptName = Literal[
    "dump_record_detail.pas",
    "list_all_conflicts.pas",
    "list_grass_worldspaces.pas",
    "list_zero_bound_grass.pas",
    "list_conflicts.pas",
]

XEDIT_AGENT_SCRIPT_ALIASES: dict[str, str] = {
    "list_conflicts.pas": "list_all_conflicts.pas",
}

XEDIT_AGENT_ALLOWED_SCRIPT_NAMES: frozenset[str] = frozenset(
    cast(tuple[str, ...], get_args(XEditAgentScriptName))
)
XEDIT_AGENT_CANONICAL_SCRIPTS: frozenset[str] = XEDIT_AGENT_ALLOWED_SCRIPT_NAMES - frozenset(
    XEDIT_AGENT_SCRIPT_ALIASES
)


def canonicalizar_script_xedit_del_agente(script_name: str) -> str:
    """Devuelve el nombre bundleado canónico de una capacidad permitida."""
    return XEDIT_AGENT_SCRIPT_ALIASES.get(script_name, script_name)


__all__ = [
    "XEDIT_AGENT_ALLOWED_SCRIPT_NAMES",
    "XEDIT_AGENT_CANONICAL_SCRIPTS",
    "XEDIT_AGENT_SCRIPT_ALIASES",
    "XEditAgentScriptName",
    "canonicalizar_script_xedit_del_agente",
]
