"""X-1 (auditoría del lote #335–#344) — los scans de rutas de tools del arranque
corren en un hilo *daemon*, no en el executor non-daemon de ``asyncio.to_thread``.

#338 sacó los scans LOOT/xEdit del event loop con ``asyncio.to_thread``, pero ese
executor usa hilos NON-daemon que ``asyncio.run`` joinea sin timeout al cerrar —
re-introduciendo el hazard de shutdown que #337 había cerrado para
``AutoDetector`` con su offload a hilo daemon (``run_off_loop``). ``_scan_tool_paths``
debe usar ese mismo offload compartido.
"""

from __future__ import annotations

import argparse
import pathlib
import threading

import pytest

from sky_claw.app_context import AppContext


def _ctx(tmp_path: pathlib.Path) -> AppContext:
    return AppContext(argparse.Namespace(db_path=str(tmp_path / "registry.db")))


@pytest.mark.asyncio
async def test_scan_tool_paths_corre_en_hilo_daemon(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El scan síncrono debe correr fuera del loop en un hilo *daemon*: un hilo
    non-daemon colgado (``asyncio.to_thread``) bloquearía el shutdown de
    ``asyncio.run`` pese a la cancelación del arranque."""
    ctx = _ctx(tmp_path)
    hilos: list[threading.Thread] = []
    esperado = tmp_path / "loot.exe"

    def _fake_scan(common_paths: tuple[pathlib.Path, ...], exe_name: str) -> pathlib.Path:
        hilos.append(threading.current_thread())
        return esperado

    monkeypatch.setattr("sky_claw.app_context.scan_common_paths", _fake_scan)

    result = await ctx._scan_tool_paths((tmp_path,), "loot.exe")

    assert result == esperado
    assert hilos, "el scan no corrió fuera del event loop"
    assert hilos[0].daemon is True


@pytest.mark.asyncio
async def test_scan_tool_paths_propaga_none(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin match, el scan devuelve None (contrato de scan_common_paths)."""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr("sky_claw.app_context.scan_common_paths", lambda _paths, _exe: None)
    assert await ctx._scan_tool_paths((tmp_path,), "SSEEdit.exe") is None
