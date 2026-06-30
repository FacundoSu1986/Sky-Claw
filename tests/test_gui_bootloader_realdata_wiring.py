"""Tests for the bootloader seams that feed real data into the GUI store.

Phase 1 wires two producers into the reactive store:
- a telemetry bridge: ``system.telemetry.*`` Events → ``sys_cpu/sys_gpu/sys_ram``.
- an environment scan: ``EnvironmentScanner.scan()`` → ``environment_snapshot``.

Both are exercised here against a real :class:`ReactiveStore` with fakes for
the event/scanner so no live daemon or disk scan is required.
"""

from __future__ import annotations

from pathlib import Path

from sky_claw.antigravity.core.event_bus import Event
from sky_claw.antigravity.gui._bootloader import (
    _hydrate_tool_env_from_snapshot,
    _make_telemetry_store_bridge,
    _run_environment_scan,
)
from sky_claw.antigravity.gui.state.reactive_store import ReactiveStore
from sky_claw.antigravity.gui.views.forge_dashboard import (
    STORE_KEY_CPU,
    STORE_KEY_ENV,
    STORE_KEY_GPU,
    STORE_KEY_RAM,
)
from sky_claw.local.discovery.environment import EnvironmentSnapshot, MO2Info, SkyrimInfo, ToolInfo


async def test_telemetry_bridge_writes_cpu_gpu_ram_to_store() -> None:
    store = ReactiveStore()
    bridge = _make_telemetry_store_bridge(store)
    await bridge(
        Event(
            topic="system.telemetry.metrics",
            payload={"cpu": 12.5, "ram_mb": 900.0, "ram_percent": 47.0, "gpu": 33.0},
            source="telemetry-daemon",
        )
    )
    assert store.get(STORE_KEY_CPU) == 12.5
    assert store.get(STORE_KEY_RAM) == 47.0
    assert store.get(STORE_KEY_GPU) == 33.0


async def test_telemetry_bridge_preserves_none_gpu() -> None:
    store = ReactiveStore()
    bridge = _make_telemetry_store_bridge(store)
    await bridge(
        Event(
            topic="system.telemetry.metrics",
            payload={"cpu": 1.0, "ram_mb": 1.0, "ram_percent": 1.0, "gpu": None},
            source="telemetry-daemon",
        )
    )
    assert store.get(STORE_KEY_GPU) is None


class _FakeScanner:
    def __init__(self, snapshot: EnvironmentSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    async def scan(self) -> EnvironmentSnapshot:
        self.calls += 1
        return self._snapshot


async def test_environment_scan_publishes_snapshot_to_store() -> None:
    store = ReactiveStore()
    snap = EnvironmentSnapshot()
    scanner = _FakeScanner(snap)
    await _run_environment_scan(scanner, store)
    assert scanner.calls == 1
    assert store.get(STORE_KEY_ENV) is snap


class _BoomScanner:
    async def scan(self) -> EnvironmentSnapshot:
        raise RuntimeError("disk on fire")


async def test_environment_scan_swallows_errors() -> None:
    store = ReactiveStore()
    # A failed scan must not crash startup; the store key simply stays unset.
    await _run_environment_scan(_BoomScanner(), store)
    assert store.get(STORE_KEY_ENV) is None


def test_hydrate_tool_env_from_snapshot_seeds_resolver_env(monkeypatch) -> None:
    # The dispatcher's PathResolutionService reads tool paths only from os.environ,
    # so the scan's resolved exes must be bridged there or "available" rituals fail.
    for var in ("SKYRIM_PATH", "MO2_PATH", "LOOT_EXE", "WRYE_BASH_PATH", "DYNDLOD_EXE", "PANDORA_EXE"):
        monkeypatch.delenv(var, raising=False)
    snap = EnvironmentSnapshot()
    snap.skyrim = SkyrimInfo(path=Path("/games/Skyrim"), exe_name="SkyrimSE.exe")
    snap.mo2 = MO2Info(path=Path("/modding/MO2"))
    snap.tools["loot"] = ToolInfo(name="LOOT", exe_path=Path("/tools/LOOT/loot.exe"))
    snap.tools["wrye_bash"] = ToolInfo(name="WRYE BASH", exe_path=Path("/tools/WB/Wrye Bash.exe"))
    snap.tools["dyndolod"] = ToolInfo(name="DYNDOLOD", exe_path=Path("/tools/DynDOLOD/DynDOLOD64.exe"))
    snap.tools["pandora"] = ToolInfo(name="PANDORA", exe_path=Path("/tools/Pandora/Pandora.exe"))

    _hydrate_tool_env_from_snapshot(snap)

    import os

    assert os.environ["SKYRIM_PATH"] == str(Path("/games/Skyrim"))
    assert os.environ["MO2_PATH"] == str(Path("/modding/MO2"))
    assert os.environ["LOOT_EXE"] == str(Path("/tools/LOOT/loot.exe"))
    assert os.environ["WRYE_BASH_PATH"] == str(Path("/tools/WB/Wrye Bash.exe"))
    assert os.environ["DYNDLOD_EXE"] == str(Path("/tools/DynDOLOD/DynDOLOD64.exe"))
    assert os.environ["PANDORA_EXE"] == str(Path("/tools/Pandora/Pandora.exe"))


def test_hydrate_tool_env_does_not_clobber_explicit_env(monkeypatch) -> None:
    # An operator-set env var must win over the scan (setdefault semantics).
    monkeypatch.setenv("LOOT_EXE", "/custom/loot.exe")
    snap = EnvironmentSnapshot()
    snap.tools["loot"] = ToolInfo(name="LOOT", exe_path=Path("/tools/LOOT/loot.exe"))

    _hydrate_tool_env_from_snapshot(snap)

    import os

    assert os.environ["LOOT_EXE"] == "/custom/loot.exe"


def test_hydrate_tool_env_noop_on_empty_snapshot(monkeypatch) -> None:
    monkeypatch.delenv("SKYRIM_PATH", raising=False)
    _hydrate_tool_env_from_snapshot(EnvironmentSnapshot())
    import os

    assert "SKYRIM_PATH" not in os.environ
