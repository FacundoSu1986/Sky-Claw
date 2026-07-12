"""Tests del sensor de límites de plugins full/light (T-30·2, Oleada 7).

Segundo sensor del preflight. Skyrim SE tiene DOS pools independientes: full
(.esp/.esm no ligeros) máx 254 y light (FE: .esl + .esp/.esm con flag ligero)
máx 4096. La clave frente al chequeo por extensión existente
(``conflict_analyzer.validate_load_order_limit``): un ``.esp`` con flag ESL
NO consume slot full — se cuenta con los **flags reales** del header, no con
la extensión.
"""

from __future__ import annotations

import pathlib
import struct

from sky_claw.local.validators.plugin_limits import (
    FULL_PLUGIN_LIMIT,
    PluginLimitsChecker,
    limits_preflight_check,
)
from sky_claw.local.validators.preflight import PreflightStatus

_FLAG_LIGHT = 0x00000200


def _plugin(path: pathlib.Path, *, flags: int = 0) -> pathlib.Path:
    hedr = struct.pack("<fiI", 1.7, 0, 0x800)
    subrecords = b"HEDR" + struct.pack("<H", len(hedr)) + hedr
    header = b"TES4" + struct.pack("<IIIIHH", len(subrecords), flags, 0, 0, 44, 0)
    path.write_bytes(header + subrecords)
    return path


def _mk(data: pathlib.Path, name: str, *, flags: int = 0) -> str:
    _plugin(data / name, flags=flags)
    return name


# ---------------------------------------------------------------------------
# Clasificación full vs light por flag real
# ---------------------------------------------------------------------------


def test_esp_normal_cuenta_full(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, "A.esp"), _mk(data, "B.esm")]

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    assert limits.full_count == 2
    assert limits.light_count == 0


def test_esl_por_extension_cuenta_light(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, "Light.esl")]

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    assert limits.full_count == 0
    assert limits.light_count == 1


def test_esp_con_flag_light_cuenta_light_no_full(tmp_path: pathlib.Path) -> None:
    """El caso que la heurística por extensión pierde: ESPFE."""
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, "Espfe.esp", flags=_FLAG_LIGHT)]

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    assert limits.full_count == 0
    assert limits.light_count == 1


# ---------------------------------------------------------------------------
# Límites
# ---------------------------------------------------------------------------


def test_full_dentro_del_limite_sin_issues(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, f"M{i:04d}.esp") for i in range(10)]

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    assert limits.issues == ()


def test_full_excedido_es_critical(tmp_path: pathlib.Path) -> None:
    """255 plugins full reales (distintos) exceden el límite de 254 → critical."""
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, f"M{i:04d}.esp") for i in range(FULL_PLUGIN_LIMIT + 1)]

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    assert limits.full_count == FULL_PLUGIN_LIMIT + 1
    criticos = [i for i in limits.issues if i.severity == "critical"]
    assert len(criticos) == 1
    assert criticos[0].kind == "full_exceeded"
    assert "esl" in criticos[0].detail.lower()  # remediación: convertir a ESL


def test_plugin_repetido_no_se_cuenta_doble(tmp_path: pathlib.Path) -> None:
    """Un load order stale con nombres repetidos no infla el conteo: un plugin
    no puede ocupar dos slots."""
    data = tmp_path / "Data"
    data.mkdir()
    _mk(data, "Dup.esp")

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(["Dup.esp", "dup.esp", "DUP.ESP"])

    assert limits.full_count == 1


def test_full_cerca_del_limite_es_warning(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, f"M{i:04d}.esp") for i in range(FULL_PLUGIN_LIMIT - 2)]

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    assert limits.full_count == FULL_PLUGIN_LIMIT - 2
    assert any(i.kind == "full_near" and i.severity == "warning" for i in limits.issues)


def test_plugin_no_encontrado_no_cuenta(tmp_path: pathlib.Path) -> None:
    """Un plugin del load order ausente en disco no consume slot (el sensor de
    masters ya reporta el not-found)."""
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, "Real.esp"), "Fantasma.esp"]

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    assert limits.full_count == 1


def test_header_ilegible_cae_a_extension_y_avisa(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    (data / "Corrupto.esp").write_bytes(b"garbage")

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(["Corrupto.esp"])

    assert limits.full_count == 1  # cae a extensión (.esp → full)
    assert limits.unreadable == 1
    assert any(i.kind == "unreadable" and i.severity == "warning" for i in limits.issues)


def test_esl_ilegible_tambien_avisa(tmp_path: pathlib.Path) -> None:
    """Un .esl corrupto sigue contando light por extensión, pero NO se saltea
    la validación del header: se registra como ilegible igual que otras
    extensiones (review Codex PR #250)."""
    data = tmp_path / "Data"
    data.mkdir()
    (data / "Roto.esl").write_bytes(b"garbage")

    limits = PluginLimitsChecker(plugin_dirs=[data]).check(["Roto.esl"])

    assert limits.light_count == 1  # .esl es ligero por extensión
    assert limits.unreadable == 1
    assert any(i.kind == "unreadable" and i.severity == "warning" for i in limits.issues)


# ---------------------------------------------------------------------------
# Puente al semáforo
# ---------------------------------------------------------------------------


def test_check_verde_reporta_conteos(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, "A.esp"), _mk(data, "L.esl")]
    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    check = limits_preflight_check(limits)

    assert check.name == "plugin_limits"
    assert check.status is PreflightStatus.GREEN
    assert "1" in check.summary  # menciona los conteos


def test_check_rojo_si_excede(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    enabled = [_mk(data, f"M{i:04d}.esp") for i in range(FULL_PLUGIN_LIMIT + 1)]
    limits = PluginLimitsChecker(plugin_dirs=[data]).check(enabled)

    check = limits_preflight_check(limits)

    assert check.status is PreflightStatus.RED


# ---------------------------------------------------------------------------
# L-1: _run_plugin_limit_guard lee el load order real
# ---------------------------------------------------------------------------


async def test_guard_usa_plugins_txt_y_descarta_modlist(tmp_path: pathlib.Path) -> None:
    """El guard cuenta plugins habilitados, no carpetas de mods activas."""
    from unittest.mock import MagicMock

    from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent

    modlist = tmp_path / "modlist.txt"
    modlist.write_text(
        "+CarpetaConNombre.esp\n+OtraCarpeta.esm\n",
        encoding="utf-8",
    )
    (tmp_path / "plugins.txt").write_text(
        "*A.esp\n*Light.esl\nB.esm\n# comentario\n",
        encoding="utf-8",
    )

    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup._path_resolver = MagicMock()  # type: ignore[attr-defined]
    sup._path_resolver.resolve_modlist_path = MagicMock(return_value=modlist)

    result = await sup._run_plugin_limit_guard("Default")

    assert result["valid"] is True
    # Solo A.esp y Light.esl están habilitados; B.esm no lleva '*'.
    assert result["plugin_count"] == 2


async def test_guard_lee_load_order_en_thread(tmp_path: pathlib.Path, monkeypatch) -> None:
    """PT-1 (S-6): la lectura del load order debe hacerse en un
    thread (asyncio.to_thread) para no bloquear el event loop — el guard corre
    en la ruta async de orquestación antes de DynDOLOD/Synthesis/Wrye Bash."""
    import asyncio
    from unittest.mock import MagicMock

    from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent

    modlist = tmp_path / "modlist.txt"
    modlist.write_text("+Carpeta.esp\n", encoding="utf-8")
    (tmp_path / "plugins.txt").write_text("*A.esp\n*Light.esl\n*B.esm\n", encoding="utf-8")

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

    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup._path_resolver = MagicMock()  # type: ignore[attr-defined]
    sup._path_resolver.resolve_modlist_path = MagicMock(return_value=modlist)

    result = await sup._run_plugin_limit_guard("Default")

    # Regresión funcional: se cuentan las tres entradas habilitadas reales.
    assert result["valid"] is True
    assert result["plugin_count"] == 3
    # La lectura del load order pasó por un thread.
    from sky_claw.antigravity.orchestrator.supervisor import _read_active_plugins_blocking

    assert _read_active_plugins_blocking in calls, f"to_thread no usado para el read: {calls}"
