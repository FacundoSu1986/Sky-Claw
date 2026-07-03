"""Tests del análisis profundo de conflictos del supervisor (F6).

``SupervisorAgent.scan_record_conflicts`` corre el análisis de records de xEdit
(read-only) y devuelve un ``ConflictReport`` que el bridge persiste. Cubre el
seam puro de parseo de plugins y las guardas del método (sin plugins / sin
rutas / delegación al analyzer) sin construir un supervisor completo.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from sky_claw.antigravity.orchestrator import supervisor as sup_mod
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent, parse_active_plugins
from sky_claw.local.xedit.conflict_analyzer import ConflictReport


# ── parse_active_plugins (seam puro) ────────────────────────────────────────────
def test_toma_esp_esm_esl_habilitados_en_orden() -> None:
    modlist = "+A.esp\n+Base.esm\n+Light.esl\n"
    assert parse_active_plugins(modlist) == ["A.esp", "Base.esm", "Light.esl"]


def test_ignora_deshabilitados_y_no_plugins() -> None:
    modlist = "+Activo.esp\n-Desactivado.esp\n# comentario\n+Textures Mod\n+Otro.esp\n"
    assert parse_active_plugins(modlist) == ["Activo.esp", "Otro.esp"]


def test_modlist_vacio_devuelve_vacio() -> None:
    assert parse_active_plugins("") == []


# ── scan_record_conflicts (guardas + delegación) ────────────────────────────────
def _bare_supervisor(path_resolver: Any) -> SupervisorAgent:
    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup._path_resolver = path_resolver  # type: ignore[attr-defined]
    return sup


async def test_sin_plugins_devuelve_reporte_vacio_sin_correr_xedit(monkeypatch: pytest.MonkeyPatch) -> None:
    llamado = {"analyze": False}

    async def _stub_analyze(self: Any, plugins: Any, runner: Any) -> ConflictReport:
        llamado["analyze"] = True
        return ConflictReport(total_conflicts=99, critical_conflicts=9)

    monkeypatch.setattr(sup_mod.ConflictAnalyzer, "analyze", _stub_analyze)
    sup = _bare_supervisor(SimpleNamespace(get_active_profile=lambda: "Default"))

    report = await sup.scan_record_conflicts(plugins=[])
    assert report.total_conflicts == 0
    assert llamado["analyze"] is False


async def test_sin_rutas_configuradas_lanza(monkeypatch: pytest.MonkeyPatch) -> None:
    sup = _bare_supervisor(
        SimpleNamespace(
            get_active_profile=lambda: "Default",
            get_skyrim_path=lambda: None,
            get_xedit_path=lambda: None,
        )
    )
    with pytest.raises(RuntimeError, match="SKYRIM_PATH y XEDIT_PATH"):
        await sup.scan_record_conflicts(plugins=["A.esp", "B.esp"])


async def test_delega_en_el_analyzer_con_los_plugins(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.chdir(tmp_path)  # XEditRunner.__init__ crea output_dir bajo cwd
    capturado: dict[str, Any] = {}
    esperado = ConflictReport(total_conflicts=3, critical_conflicts=1)

    async def _stub_analyze(self: Any, plugins: Any, runner: Any) -> ConflictReport:
        capturado["plugins"] = plugins
        return esperado

    monkeypatch.setattr(sup_mod.ConflictAnalyzer, "analyze", _stub_analyze)
    sup = _bare_supervisor(
        SimpleNamespace(
            get_active_profile=lambda: "Default",
            get_skyrim_path=lambda: tmp_path / "skyrim",
            get_xedit_path=lambda: tmp_path / "xedit.exe",
        )
    )

    report = await sup.scan_record_conflicts(plugins=["A.esp", "B.esp"])
    assert report is esperado
    assert capturado["plugins"] == ["A.esp", "B.esp"]


async def test_lee_plugins_del_modlist_cuando_no_se_pasan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.chdir(tmp_path)
    modlist = tmp_path / "modlist.txt"
    modlist.write_text("+A.esp\n-Off.esp\n+Base.esm\n", encoding="utf-8")
    capturado: dict[str, Any] = {}

    async def _stub_analyze(self: Any, plugins: Any, runner: Any) -> ConflictReport:
        capturado["plugins"] = plugins
        return ConflictReport(total_conflicts=0, critical_conflicts=0)

    monkeypatch.setattr(sup_mod.ConflictAnalyzer, "analyze", _stub_analyze)
    sup = _bare_supervisor(
        SimpleNamespace(
            get_active_profile=lambda: "Default",
            resolve_modlist_path=lambda profile: modlist,
            get_skyrim_path=lambda: tmp_path / "skyrim",
            get_xedit_path=lambda: tmp_path / "xedit.exe",
        )
    )

    await sup.scan_record_conflicts()
    assert capturado["plugins"] == ["A.esp", "Base.esm"]
