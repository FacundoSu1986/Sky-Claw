"""QA-16 — AsyncToolRegistry allowlist por sesion (T2-07).

Verifica que cuando ``allowed_tools`` esta configurado en ``__init__``:
  (a) Tools dentro del set se ejecutan normalmente.
  (b) Tools fuera del set lanzan ``PermissionError`` ANTES de validar params.
  (c) Tools no registrados siguen lanzando ``KeyError`` (precedencia sobre
      la allowlist — el error es por nombre invalido, no por permisos).
  (d) La allowlist es inmutable (frozenset) — mutaciones externas no
      cambian la autorizacion de un registry ya construido.
  (e) Cuando ``allowed_tools=None`` (default), todos los tools registrados
      se permiten (compat con callers existentes).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sky_claw.antigravity.agent.tools import AsyncToolRegistry


def _make_registry(allowed_tools: set[str] | None = None, session_id: str | None = None) -> AsyncToolRegistry:
    """Construye un registry con dependencias mockeadas."""
    registry = AsyncToolRegistry(
        registry=MagicMock(),
        mo2=MagicMock(),
        sync_engine=MagicMock(),
        allowed_tools=allowed_tools,
        session_id=session_id,
    )
    return registry


@pytest.mark.asyncio
async def test_no_allowlist_permits_all_registered_tools() -> None:
    """Default: allowed_tools=None → todos los tools registrados son permitidos."""
    r = _make_registry()
    # search_mod es un built-in. No corremos el handler real (fallaria sin
    # async_registry), solo verificamos que no rechaza por permisos.
    with pytest.raises(Exception) as exc_info:
        await r.execute("search_mod", {"mod_name": "Test"})
    # Cualquier excepcion EXCEPTO PermissionError indica que pasamos el gate.
    assert not isinstance(exc_info.value, PermissionError)


@pytest.mark.asyncio
async def test_allowlist_rejects_tool_outside_set() -> None:
    """Tool registrado pero fuera de la allowlist debe lanzar PermissionError."""
    r = _make_registry(allowed_tools={"search_mod"}, session_id="qa-session")

    with pytest.raises(PermissionError, match="download_mod") as exc_info:
        await r.execute("download_mod", {"nexus_id": 1})

    assert "qa-session" in str(exc_info.value)


@pytest.mark.asyncio
async def test_allowlist_permits_tool_inside_set() -> None:
    """Tool dentro de allowlist debe pasar el gate (luego puede fallar en otro paso)."""
    r = _make_registry(allowed_tools={"search_mod"}, session_id="qa-session")

    # Pasa el gate de allowlist, pero search_mod fallara al ejecutar handler
    # con MagicMock como registry. La PermissionError NO debe lanzarse.
    with pytest.raises(Exception) as exc_info:
        await r.execute("search_mod", {"mod_name": "Test"})

    assert not isinstance(exc_info.value, PermissionError)


@pytest.mark.asyncio
async def test_unknown_tool_raises_key_error_over_permission_error() -> None:
    """KeyError (tool no registrado) tiene precedencia sobre allowlist."""
    r = _make_registry(allowed_tools={"search_mod"})

    with pytest.raises(KeyError, match="bogus_tool"):
        await r.execute("bogus_tool", {})


@pytest.mark.asyncio
async def test_allowlist_is_immutable_after_construction() -> None:
    """Mutar el set original NO debe cambiar la autorizacion del registry."""
    allowed = {"search_mod"}
    r = _make_registry(allowed_tools=allowed)

    # Mutar el set original despues de construir el registry.
    allowed.add("download_mod")

    # El registry debe seguir rechazando download_mod (uso frozenset interno).
    with pytest.raises(PermissionError):
        await r.execute("download_mod", {"nexus_id": 1})


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_everything() -> None:
    """allowed_tools=set() = explicitamente rechaza todos los tools."""
    r = _make_registry(allowed_tools=set(), session_id="empty")

    with pytest.raises(PermissionError, match="search_mod"):
        await r.execute("search_mod", {"mod_name": "x"})


@pytest.mark.asyncio
async def test_allowlist_log_includes_attempted_tool_and_session(caplog) -> None:
    """Tentativas rechazadas se loggean con session_id y allowlist."""
    import logging

    r = _make_registry(allowed_tools={"search_mod"}, session_id="audit-123")

    with caplog.at_level(logging.WARNING), pytest.raises(PermissionError):
        await r.execute("download_mod", {"nexus_id": 1})

    assert any(
        "download_mod" in rec.message and "audit-123" in rec.message and "search_mod" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# PR #143 review fix: tool_schemas() y hermes_system_prompt_block() filtran
# por allowlist tambien, no solo execute(). Evita que el LLM vea capacidades
# que va a recibir PermissionError al invocar.
# ---------------------------------------------------------------------------


def test_tool_schemas_unfiltered_when_no_allowlist() -> None:
    """Sin allowlist, tool_schemas() expone todos los tools registrados."""
    r = _make_registry()
    names = {s["name"] for s in r.tool_schemas()}
    # Sanity: al menos search_mod y download_mod estan registrados.
    assert "search_mod" in names
    assert "download_mod" in names


def test_tool_schemas_filters_by_allowlist() -> None:
    """Con allowlist {search_mod}, tool_schemas NO expone download_mod."""
    r = _make_registry(allowed_tools={"search_mod"})
    names = {s["name"] for s in r.tool_schemas()}
    assert names == {"search_mod"}
    assert "download_mod" not in names


def test_hermes_system_prompt_block_filters_by_allowlist() -> None:
    """Hermes <tools> block excluye tools fuera del allowlist."""
    r = _make_registry(allowed_tools={"search_mod"})
    block = r.hermes_system_prompt_block()
    assert "search_mod" in block
    assert "download_mod" not in block


def test_tools_property_filters_by_allowlist() -> None:
    """``tools`` dict tambien filtra (consumers que iteren ven solo permitidos)."""
    r = _make_registry(allowed_tools={"search_mod"})
    tools = r.tools
    assert "search_mod" in tools
    assert "download_mod" not in tools


def test_empty_allowlist_yields_empty_schemas() -> None:
    """allowed_tools=set() -> tool_schemas vacio. Util para sesiones read-only."""
    r = _make_registry(allowed_tools=set())
    assert r.tool_schemas() == []
    assert r.tools == {}
    block = r.hermes_system_prompt_block()
    # El block sigue siendo valido XML pero sin tools listados.
    assert "<tools>" in block
    # Ningun tool conocido aparece.
    assert "search_mod" not in block
    assert "download_mod" not in block
