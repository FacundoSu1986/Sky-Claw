"""Pure-helper tests for the Forge dashboard's real-data rendering.

Phase 1 replaces hardcoded vitals ("CPU 3%", "GPU 18%", "Memoria 41%") and
hardcoded ritual states ("No instalado") with values derived from live
telemetry and a real :class:`EnvironmentScanner` snapshot. These helpers are
the pure seam between that data and the inline-styled HTML.
"""

from __future__ import annotations

from pathlib import Path

from sky_claw.antigravity.gui.views.forge_dashboard import (
    _RITUALS,
    _fmt_pct,
    _ritual_status,
    _vital_bar_width,
)
from sky_claw.local.discovery.environment import (
    EnvironmentSnapshot,
    ToolInfo,
)


# ── Metric formatting ──────────────────────────────────────────────────────────
def test_fmt_pct_none_is_nd() -> None:
    assert _fmt_pct(None) == "N/D"


def test_fmt_pct_rounds_to_int_percent() -> None:
    assert _fmt_pct(41.4) == "41%"
    assert _fmt_pct(2.6) == "3%"
    assert _fmt_pct(0) == "0%"


def test_vital_bar_width_clamped_and_zero_for_unknown() -> None:
    assert _vital_bar_width(None) == 0
    assert _vital_bar_width(150) == 100
    assert _vital_bar_width(-5) == 0
    assert _vital_bar_width(41.6) == 42


# ── Ritual availability from the environment snapshot ───────────────────────────
def _snapshot_with(tool_key: str) -> EnvironmentSnapshot:
    snap = EnvironmentSnapshot()
    snap.tools[tool_key] = ToolInfo(name=tool_key.upper(), exe_path=Path("/x") / f"{tool_key}.exe")
    return snap


def test_ritual_status_unknown_when_no_snapshot() -> None:
    assert _ritual_status(None, "loot") == "unknown"


def test_ritual_status_available_when_tool_detected() -> None:
    assert _ritual_status(_snapshot_with("loot"), "loot") == "available"


def test_ritual_status_missing_when_tool_absent() -> None:
    assert _ritual_status(EnvironmentSnapshot(), "loot") == "missing"


def test_every_ritual_maps_to_a_scanner_tool_key() -> None:
    # The scanner keys (scanner.py tool_defs): loot, xedit, pandora, wrye_bash, dyndolod.
    valid = {"loot", "xedit", "pandora", "wrye_bash", "dyndolod"}
    for ritual in _RITUALS:
        assert ritual["tool"] in valid
