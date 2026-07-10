"""Tests del orquestador mínimo de rituales en sandbox (T-27b·1).

Hallazgo que motiva el módulo: el ``ProfileSandbox`` de T-27 (#245) quedó
huérfano — nadie en producción posee el ciclo de vida clone → ritual → diff.
``run_ritual_in_sandbox`` es ese dueño mínimo: materializa el clon, corre el
ritual inyectado contra él y devuelve el diff explicable. El caller queda
dueño del clon: ``promote()`` tras aprobación HITL o ``discard()``.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
from typing import Any

import pytest

from sky_claw.local.mo2.profile_sandbox import ProfileSandbox, SandboxClone, SandboxSymlinkError
from sky_claw.local.mo2.sandbox_run import SandboxedRunResult, run_ritual_in_sandbox


def _puede_crear_symlinks() -> bool:
    """Guard de privilegios (crear symlinks requiere admin en Windows)."""
    try:
        with tempfile.TemporaryDirectory() as td:
            origen = pathlib.Path(td) / "src"
            origen.mkdir()
            (pathlib.Path(td) / "link").symlink_to(origen, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


_symlink_guard = pytest.mark.skipif(
    sys.platform == "win32" and not _puede_crear_symlinks(),
    reason="Crear symlinks requiere privilegios elevados en Windows",
)


def _mo2(tmp_path: pathlib.Path) -> pathlib.Path:
    """Instancia MO2 mínima: perfil Default + overwrite vacío."""
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n")
    (mo2 / "overwrite").mkdir()
    return mo2


def _sandbox(tmp_path: pathlib.Path) -> ProfileSandbox:
    return ProfileSandbox(mo2_root=_mo2(tmp_path), sandbox_root=tmp_path / "sandbox")


class TestRunRitualInSandbox:
    async def test_la_salida_del_ritual_queda_en_el_clon_no_en_el_real(self, tmp_path: pathlib.Path) -> None:
        """La garantía de T-27: con el sandbox activo, el overwrite real NO se toca."""
        sandbox = _sandbox(tmp_path)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            (clone.overwrite_copy / "Synthesis.esp").write_bytes(b"TES4")
            return {"success": True, "message": ""}

        resultado = await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)

        assert list((tmp_path / "MO2" / "overwrite").iterdir()) == []  # real intacto
        cambios = {(c.area, c.relative_path, c.kind) for c in resultado.diff.changes}
        assert ("overwrite", "Synthesis.esp", "added") in cambios

    async def test_el_result_del_ritual_viaja_intacto(self, tmp_path: pathlib.Path) -> None:
        """El contrato success/message de los tools no se reinterpreta acá."""
        sandbox = _sandbox(tmp_path)
        esperado = {"success": True, "message": "", "patchers_executed": ["a"]}

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            return dict(esperado)

        resultado = await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)

        assert resultado.result == esperado
        assert isinstance(resultado, SandboxedRunResult)

    async def test_el_caller_queda_dueno_del_clon(self, tmp_path: pathlib.Path) -> None:
        """Tras el run el clon sigue vivo: el caller decide promote/discard."""
        sandbox = _sandbox(tmp_path)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            return {"success": True, "message": ""}

        resultado = await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)

        assert resultado.clone.root.is_dir()
        await sandbox.discard(resultado.clone)
        assert not resultado.clone.root.exists()

    async def test_promote_posterior_aplica_la_salida_al_real(self, tmp_path: pathlib.Path) -> None:
        """El flujo completo de la visión: ejecutar → diff → aprobar → promover."""
        sandbox = _sandbox(tmp_path)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            (clone.overwrite_copy / "Synthesis.esp").write_bytes(b"TES4")
            return {"success": True, "message": ""}

        resultado = await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)
        promocion = await sandbox.promote(resultado.clone)

        assert promocion.files_written == 1
        assert (tmp_path / "MO2" / "overwrite" / "Synthesis.esp").read_bytes() == b"TES4"

    async def test_excepcion_del_ritual_descarta_el_clon_y_propaga(self, tmp_path: pathlib.Path) -> None:
        """Un ritual que lanza no deja clones colgados en el sandbox_root."""
        sandbox = _sandbox(tmp_path)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)

        sandbox_root = tmp_path / "sandbox"
        assert not sandbox_root.exists() or list(sandbox_root.iterdir()) == []

    async def test_result_con_success_false_devuelve_diff_igual(self, tmp_path: pathlib.Path) -> None:
        """Un fallo a nivel tool (success=False, sin excepción) NO descarta: el
        diff de las escrituras parciales es evidencia para el operador."""
        sandbox = _sandbox(tmp_path)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            (clone.overwrite_copy / "parcial.log").write_text("x", encoding="utf-8")
            return {"success": False, "message": "patcher falló"}

        resultado = await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)

        assert resultado.result["success"] is False
        assert any(c.relative_path == "parcial.log" for c in resultado.diff.changes)
        assert resultado.clone.root.is_dir()  # el caller decide (forense → discard)

    async def test_cancelacion_propaga_y_limpia_best_effort(self, tmp_path: pathlib.Path) -> None:
        """review Codex #258 (P1): la cancelación NO se traga ni se demora —
        propaga tras un discard best-effort."""
        sandbox = _sandbox(tmp_path)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)

        sandbox_root = tmp_path / "sandbox"
        assert not sandbox_root.exists() or list(sandbox_root.iterdir()) == []

    @_symlink_guard
    async def test_diff_que_lanza_descarta_el_clon_y_propaga(self, tmp_path: pathlib.Path) -> None:
        """review Codex #258 (P2): si el ritual deja un artefacto inseguro y el
        diff() lanza, el clon no debe quedar huérfano (el caller nunca recibió
        el handle para descartarlo)."""
        sandbox = _sandbox(tmp_path)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            (clone.overwrite_copy / "Link").symlink_to(tmp_path, target_is_directory=True)
            return {"success": True, "message": ""}

        with pytest.raises(SandboxSymlinkError):
            await run_ritual_in_sandbox(sandbox=sandbox, ritual=ritual)

        sandbox_root = tmp_path / "sandbox"
        assert not sandbox_root.exists() or list(sandbox_root.iterdir()) == []
