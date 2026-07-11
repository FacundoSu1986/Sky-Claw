"""Tests del validador post-run (T-21 de TECHNICAL_REVIEW_TASKS.md).

El preflight frena ANTES de un Ritual mutante; el post-run verifica DESPUÉS
qué dejó: re-corre los mismos sensores (masters, límites, overwrite, permisos
— la freshness por closures de #252 garantiza que ven el estado post-mutación)
y suma el check de header 43 (plugin LE sin portear a SE, riesgo real). Cierra
el lazo `validate` del pipeline §4.6; su resultado viaja en el slot
`post_run_validation` del FlightReport (T-28 lo dejó esperando a T-21).
"""

from __future__ import annotations

import json
import pathlib
import struct
from unittest.mock import AsyncMock, MagicMock

from sky_claw.local.mo2.plugin_sources import PluginSources
from sky_claw.local.validators.missing_masters import MasterIssue
from sky_claw.local.validators.post_run import HeaderVersionIssue, PostRunValidator
from sky_claw.local.validators.preflight import PreflightService


def _tes4(path: pathlib.Path, *, form_version: int = 44) -> pathlib.Path:
    """Plugin sintético mínimo con ese formVersion en el header TES4."""
    hedr = struct.pack("<fiI", 1.7, 0, 0x800)
    subrecords = b"HEDR" + struct.pack("<H", len(hedr)) + hedr
    path.write_bytes(b"TES4" + struct.pack("<IIIIHH", len(subrecords), 0, 0, 0, form_version, 0) + subrecords)
    return path


def _preflight_verde() -> PreflightService:
    checker = MagicMock()
    checker.check.return_value = []
    return PreflightService(vfs_checker=checker, loot_version_detector=AsyncMock(return_value=(0, 29, 0)))


class TestPostRunValidator:
    async def test_master_faltante_post_run_sale_rojo(self) -> None:
        """El caso del test rojo del backlog: un run que dejó un master
        faltante → el reporte post-run lo trae en rojo."""
        issue = MasterIssue(
            plugin="Bashed Patch, 0.esp",
            master="Borrado.esm",
            kind="missing",
            severity="critical",
            remediation="reinstalá el mod que lo provee",
        )
        preflight = PreflightService(masters_check=lambda: [issue])

        reporte = await PostRunValidator(preflight=preflight).run()

        assert reporte.preflight.status.value == "red"
        assert reporte.has_findings is True

    async def test_overwrite_sucio_post_run_sale_amarillo(self) -> None:
        from sky_claw.local.validators.overwrite_health import OverwriteScan

        preflight = PreflightService(overwrite_check=lambda: OverwriteScan(files=("residuo.log",), plugins=()))

        reporte = await PostRunValidator(preflight=preflight).run()

        assert reporte.preflight.status.value == "yellow"
        assert reporte.has_findings is True

    async def test_header_43_se_reporta_con_remediacion(self, tmp_path: pathlib.Path) -> None:
        plugins_dir = tmp_path / "Data"
        plugins_dir.mkdir()
        _tes4(plugins_dir / "Porteado.esp", form_version=44)
        _tes4(plugins_dir / "Legacy.esp", form_version=43)

        def _sources() -> PluginSources:
            return PluginSources(plugin_dirs=(plugins_dir,), enabled_plugins=("Porteado.esp", "Legacy.esp"))

        reporte = await PostRunValidator(preflight=_preflight_verde(), plugin_sources=_sources).run()

        assert reporte.headers_checked is True
        assert len(reporte.header_issues) == 1
        issue = reporte.header_issues[0]
        assert isinstance(issue, HeaderVersionIssue)
        assert issue.plugin == "Legacy.esp"
        assert issue.form_version == 43
        assert "43" in issue.remediation
        assert reporte.has_findings is True  # el preflight verde no tapa el header

    async def test_plugin_ilegible_se_saltea_best_effort(self, tmp_path: pathlib.Path) -> None:
        """Un plugin truncado/no-TES4 no tumba el validador (skip con debug)."""
        plugins_dir = tmp_path / "Data"
        plugins_dir.mkdir()
        (plugins_dir / "Roto.esp").write_bytes(b"NO-TES4")

        def _sources() -> PluginSources:
            return PluginSources(plugin_dirs=(plugins_dir,), enabled_plugins=("Roto.esp",))

        reporte = await PostRunValidator(preflight=_preflight_verde(), plugin_sources=_sources).run()

        assert reporte.header_issues == ()

    async def test_sin_plugin_sources_no_miente(self) -> None:
        """Sin fuentes resolubles el check de headers se declara no corrido."""
        reporte = await PostRunValidator(preflight=_preflight_verde()).run()

        assert reporte.headers_checked is False
        assert reporte.header_issues == ()
        assert reporte.has_findings is False  # preflight verde + sin issues

    async def test_to_dict_es_serializable_con_kind(self, tmp_path: pathlib.Path) -> None:
        plugins_dir = tmp_path / "Data"
        plugins_dir.mkdir()
        _tes4(plugins_dir / "Legacy.esp", form_version=43)

        def _sources() -> PluginSources:
            return PluginSources(plugin_dirs=(plugins_dir,), enabled_plugins=("Legacy.esp",))

        reporte = await PostRunValidator(preflight=_preflight_verde(), plugin_sources=_sources).run()
        data = reporte.to_dict()

        assert data["kind"] == "post_run_validation"
        assert data["has_findings"] is True
        assert data["headers_checked"] is True
        assert data["header_issues"][0]["plugin"] == "Legacy.esp"
        assert data["preflight"]["status"] == "green"
        json.dumps(data)
