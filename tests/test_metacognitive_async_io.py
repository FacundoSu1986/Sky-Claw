"""PT-1 (PS-3): SecurityMetacognition._phase_resolve debe leer los archivos a
escanear vía asyncio.to_thread para no bloquear el event loop durante escaneos
de repositorios grandes.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch


async def test_phase_resolve_lee_archivos_en_thread(tmp_path: pathlib.Path, monkeypatch) -> None:
    from sky_claw.antigravity.security.metacognitive_logic import SecurityMetacognition

    archivo = tmp_path / "modulo.py"
    archivo.write_text("import os\nprint('hola')\n", encoding="utf-8")

    engine = SecurityMetacognition(str(tmp_path))
    engine.session_data["files_to_scan"] = [str(archivo)]

    gov = MagicMock()
    gov.is_scanned_and_clean = AsyncMock(return_value=False)
    gov.update_scan_result = AsyncMock()

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

    with patch(
        "sky_claw.antigravity.security.metacognitive_logic.GovernanceManager.get_instance",
        return_value=gov,
    ):
        await engine._phase_resolve()

    # La lectura del archivo pasó por un thread.
    from sky_claw.antigravity.security.metacognitive_logic import _read_text_file_blocking

    assert _read_text_file_blocking in calls, f"to_thread no usado para el read: {calls}"
    # Regresión funcional: el escaneo corrió y persistió su resultado.
    gov.update_scan_result.assert_awaited_once()
    assert engine.session_data["status"] == "RESOLVING"
