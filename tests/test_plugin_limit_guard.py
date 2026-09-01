"""Tests del PluginLimitGuard extraído (PR5 del Strangler Fig).

Oracle de preservación de comportamiento: el guard se extrajo de
``SupervisorAgent._run_plugin_limit_guard`` sin cambio funcional. La política
de lectura del guard NO es la de ``scan_record_conflicts``: acá ``plugins.txt``
tiene precedencia y, si existe pero no produce plugins habilitados, NO se cae
a ``loadorder.txt`` (comportamiento histórico congelado, no "mejorado").
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.app.orchestrator.plugin_limit_guard import (
    PluginLimitGuard,
    _read_active_plugins_blocking,
)
from sky_claw.local.xedit.conflict_analyzer import ConflictAnalyzer


def _resolver_para(profile_dir: pathlib.Path) -> MagicMock:
    resolver = MagicMock()
    resolver.resolve_modlist_path = MagicMock(return_value=profile_dir / "modlist.txt")
    return resolver


# ── P1: plugins.txt tiene precedencia sobre loadorder.txt ────────────────────


async def test_plugins_txt_tiene_precedencia_sobre_loadorder(tmp_path: pathlib.Path) -> None:
    (tmp_path / "loadorder.txt").write_text("DelLoadOrder.esp\nOtroDelLoad.esp", encoding="utf-8")
    (tmp_path / "plugins.txt").write_text("*DelPluginsTxt.esp\n", encoding="utf-8")
    guard = PluginLimitGuard(path_resolver=_resolver_para(tmp_path))

    result = await guard.run("MiPerfil")

    assert result["plugin_count"] == 1


# ── P2: fallback a loadorder.txt cuando plugins.txt no existe ────────────────


async def test_fallback_a_loadorder_cuando_no_hay_plugins_txt(tmp_path: pathlib.Path) -> None:
    (tmp_path / "loadorder.txt").write_text("A.esp\nB.esm\nL.esl\n", encoding="utf-8")
    guard = PluginLimitGuard(path_resolver=_resolver_para(tmp_path))

    result = await guard.run("MiPerfil")

    assert result["plugin_count"] == 3


# ── P3: plugins.txt existente pero sin habilitados NO cae a loadorder ────────


async def test_plugins_txt_vacio_no_cae_a_loadorder(tmp_path: pathlib.Path) -> None:
    """Comportamiento histórico: ``plugins.txt`` existente gana la precedencia
    aunque no produzca plugins habilitados; no hay fallback implícito."""
    (tmp_path / "loadorder.txt").write_text("A.esp\nB.esp\nC.esp\n", encoding="utf-8")
    (tmp_path / "plugins.txt").write_text("Desmarcado.esp\n", encoding="utf-8")
    guard = PluginLimitGuard(path_resolver=_resolver_para(tmp_path))

    result = await guard.run("MiPerfil")

    assert result["valid"] is True
    assert result["plugin_count"] == 0


# ── P4: la lectura del load order corre fuera del event loop ─────────────────


async def test_la_lectura_corre_en_thread(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "plugins.txt").write_text("*A.esp\n", encoding="utf-8")
    calls: list = []
    real_to_thread = asyncio.to_thread

    def _unwrap(f):
        while hasattr(f, "__wrapped__"):
            f = f.__wrapped__
        if hasattr(f, "func"):
            f = f.func
        return f

    async def _spy(fn, *a, **k):
        calls.append(_unwrap(fn))
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(asyncio, "to_thread", _spy)
    guard = PluginLimitGuard(path_resolver=_resolver_para(tmp_path))

    result = await guard.run("MiPerfil")

    assert result["valid"] is True
    assert _read_active_plugins_blocking in calls, f"to_thread no usado para el read: {calls}"


# ── P5: el resolver recibe exactamente el perfil pedido ───────────────────────


async def test_resolver_recibe_el_perfil_exacto(tmp_path: pathlib.Path) -> None:
    (tmp_path / "plugins.txt").write_text("*A.esp\n", encoding="utf-8")
    resolver = _resolver_para(tmp_path)
    guard = PluginLimitGuard(path_resolver=resolver)

    await guard.run("PerfilExplicito")

    resolver.resolve_modlist_path.assert_called_once_with("PerfilExplicito")


# ── P6: se analiza con ConflictAnalyzer.validate_load_order_limit real ───────


async def test_usa_conflict_analyzer_validate_load_order_limit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-sustitución (M11): el guard debe seguir usando
    ``ConflictAnalyzer.validate_load_order_limit`` con la lista exacta y
    ordenada — NO PluginLimitsChecker ni ningún otro analyzer."""
    import sky_claw.app.orchestrator.plugin_limit_guard as plg_module

    (tmp_path / "loadorder.txt").write_text("R2.esm\nR1.esp\n", encoding="utf-8")
    llamadas: list[list[str]] = []
    original = ConflictAnalyzer.validate_load_order_limit

    def _espia(self: ConflictAnalyzer, plugins: list[str], **kwargs: object) -> None:
        llamadas.append(list(plugins))
        original(self, plugins, **kwargs)

    monkeypatch.setattr(ConflictAnalyzer, "validate_load_order_limit", _espia)
    guard = PluginLimitGuard(path_resolver=_resolver_para(tmp_path))

    await guard.run("MiPerfil")

    assert llamadas == [["R2.esm", "R1.esp"]]
    assert "PluginLimitsChecker" not in vars(plg_module)


# ── P7: contrato de éxito exacto ──────────────────────────────────────────────


async def test_contrato_exito_exacto(tmp_path: pathlib.Path) -> None:
    (tmp_path / "plugins.txt").write_text("*A.esp\n*B.esm\n", encoding="utf-8")
    guard = PluginLimitGuard(path_resolver=_resolver_para(tmp_path))

    result = await guard.run("MiPerfil")

    assert result == {"valid": True, "profile": "MiPerfil", "plugin_count": 2, "limit": 254}


# ── P8: límite excedido (RuntimeError del analyzer) ───────────────────────────


async def test_contrato_limite_excedido(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "plugins.txt").write_text("*A.esp\n*B.esp\n", encoding="utf-8")

    def _excede(self: ConflictAnalyzer, plugins: list[str], **kwargs: object) -> None:
        raise RuntimeError("límite excedido")

    monkeypatch.setattr(ConflictAnalyzer, "validate_load_order_limit", _excede)
    guard = PluginLimitGuard(path_resolver=_resolver_para(tmp_path))

    result = await guard.run("MiPerfil")

    assert result == {
        "valid": False,
        "profile": "MiPerfil",
        "plugin_count": 2,
        "limit": 254,
        "error": "límite excedido",
    }


# ── P9: ValueError / OSError (contrato corto histórico) ───────────────────────


async def test_contrato_value_error(tmp_path: pathlib.Path) -> None:
    resolver = MagicMock()
    resolver.resolve_modlist_path = MagicMock(side_effect=ValueError("ruta inválida"))
    guard = PluginLimitGuard(path_resolver=resolver)

    result = await guard.run("MiPerfil")

    assert result == {"valid": False, "error": "ruta inválida"}


async def test_contrato_os_error(tmp_path: pathlib.Path) -> None:
    resolver = MagicMock()
    resolver.resolve_modlist_path = MagicMock(side_effect=OSError("disco inaccesible"))
    guard = PluginLimitGuard(path_resolver=resolver)

    result = await guard.run("MiPerfil")

    assert result == {"valid": False, "error": "disco inaccesible"}
