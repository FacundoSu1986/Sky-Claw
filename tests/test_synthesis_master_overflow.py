"""Fail-closed del fan-in de masters y cobertura del split de Synthesis."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools.synthesis_runner import SynthesisConfig, SynthesisExecutionError, SynthesisRunner
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from sky_claw.local.validators.preflight import PreflightReport, PreflightStatus


def _write_settings(path: pathlib.Path, *, split: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "Version": 2,
                "Profiles": [
                    {
                        "Nickname": "Default",
                        "ID": "profile-default",
                        "TargetRelease": "SkyrimSE",
                        "Groups": [
                            {
                                "On": True,
                                "Name": "Synthesis",
                                "Patchers": [
                                    {
                                        "$type": "Synthesis.Bethesda.Execution.Settings.GithubPatcherSettings, Synthesis.Bethesda.Execution",
                                        "ID": "patcher-id",
                                        "Nickname": "patcher",
                                        "On": True,
                                        "RemoteRepoPath": "https://example.invalid/patcher",
                                    }
                                ],
                            }
                        ],
                        "SplitIfMaxMastersExceeded": split,
                        "UpdateLoadOrderAfterRun": True,
                    }
                ],
                "WorkingDirectory": str(path.parent),
            }
        ),
        encoding="utf-8",
    )


def _service_case(
    tmp_path: pathlib.Path, *, plugin_count: int, split: bool
) -> tuple[SynthesisPipelineService, SynthesisRunner]:
    game = tmp_path / "Skyrim"
    data = game / "Data"
    data.mkdir(parents=True)
    (data / "Skyrim.esm").write_bytes(b"TES4" + b"\x00" * 128)
    mo2 = tmp_path / "MO2"
    (mo2 / "overwrite").mkdir(parents=True)
    (mo2 / "mods").mkdir()
    profile_dir = mo2 / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    active = [f"Plugin{index:03}.esp" for index in range(plugin_count)]
    (profile_dir / "plugins.txt").write_text("".join(f"*{name}\n" for name in active), encoding="utf-8")
    (profile_dir / "loadorder.txt").write_text("".join(f"{name}\n" for name in active), encoding="utf-8")
    exe = tmp_path / "Synthesis.CLI.exe"
    exe.touch()
    settings_path = tmp_path / "PipelineSettings.json"
    _write_settings(settings_path, split=split)
    runner = SynthesisRunner(
        SynthesisConfig(
            game_path=game,
            mo2_path=mo2,
            output_path=mo2 / "overwrite",
            synthesis_exe=exe,
            profile="Default",
            pipeline_settings_path=settings_path,
            data_folder_path=data,
            load_order_file_path=profile_dir / "plugins.txt",
        )
    )

    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_path.return_value = mo2
    resolver.get_mo2_instance_data_root.return_value = mo2
    resolver.get_synthesis_exe.return_value = exe
    resolver.get_active_profile.return_value = "Default"
    resolver.get_mo2_mods_path_best_effort.return_value = mo2 / "mods"
    resolver.get_mo2_mods_path_para_destino.return_value = mo2 / "mods"
    preflight = _GreenPreflight()
    service = SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=AsyncMock(),
        path_resolver=resolver,
        event_bus=AsyncMock(),
        pipeline_config_path=tmp_path / "no-internal-pipeline.json",
        preflight=preflight,  # type: ignore[arg-type]
        synthesis_settings_path=settings_path,
    )
    return service, runner


class _GreenPreflight:
    async def run(self) -> PreflightReport:
        return PreflightReport(status=PreflightStatus.GREEN, checks=())


def test_overflow_potencial_sin_auto_split_es_rojo(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=255, split=False)

    check = service._build_master_overflow_check(runner, ["patcher"])

    assert check.name == "synthesis_master_overflow"
    assert check.status is PreflightStatus.RED
    assert "deshabilitado" in check.summary
    assert any("256 plugin(s)" in detail for detail in check.details)


def test_overflow_potencial_con_auto_split_es_warning_no_bloqueante(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=256, split=True)

    check = service._build_master_overflow_check(runner, ["patcher"])

    assert check.status is PreflightStatus.YELLOW
    assert "auto-split" in check.summary


def test_fan_in_bajo_el_limite_no_requiere_split(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=20, split=False)

    check = service._build_master_overflow_check(runner, ["patcher"])

    assert check.status is PreflightStatus.GREEN
    assert "21/254" in check.summary


def test_synthesis_falla_cerrado_con_visibilidad_parcial_del_overlay(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=2, split=True)

    check = service._build_synthesis_visibility_check(runner)

    assert check.status is PreflightStatus.RED
    assert "no están visibles en el Data físico" in check.summary


def test_synthesis_acepta_plugins_de_mod_si_todos_estan_visibles_en_data(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=2, split=True)
    data = runner._config.data_folder_path
    assert data is not None
    for index in range(2):
        (data / f"Plugin{index:03}.esp").write_bytes(b"TES4" + b"\\x00" * 128)

    check = service._build_synthesis_visibility_check(runner)

    assert check.status is PreflightStatus.GREEN
    assert "2 plugin(s) activos de mods" in check.summary


@pytest.mark.asyncio
async def test_preflight_master_overflow_bloquea_antes_de_journal_o_cli(tmp_path: pathlib.Path) -> None:
    service, _ = _service_case(tmp_path, plugin_count=255, split=False)
    journal = service._journal

    with patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock) as run_mock:
        result = await service.execute_pipeline(patcher_ids=["patcher"])

    assert result["success"] is False
    assert result["reason"] == "PreflightBlocked"
    assert any(check["name"] == "synthesis_master_overflow" for check in result["preflight"]["checks"])
    journal.begin_transaction.assert_not_awaited()
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_settings_v2_cambiados_durante_preflight_fallan_antes_de_transaccion(
    tmp_path: pathlib.Path,
) -> None:
    service, runner = _service_case(tmp_path, plugin_count=20, split=True)
    settings_path = runner._config.pipeline_settings_path
    assert settings_path is not None

    class _ChangingPreflight:
        async def run(self) -> PreflightReport:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
            payload["Profiles"][0]["UpdateLoadOrderAfterRun"] = False
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
            return PreflightReport(status=PreflightStatus.GREEN, checks=())

    service._preflight = _ChangingPreflight()  # type: ignore[assignment]
    journal = service._journal

    with patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock) as run_mock:
        result = await service.execute_pipeline(patcher_ids=["patcher"])

    assert result["success"] is False
    assert "changed during preflight" in result["message"]
    journal.begin_transaction.assert_not_awaited()
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugins_txt_cambiado_durante_preflight_bloquea_la_transaccion(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=20, split=True)
    plugins_file = runner._config.load_order_file_path
    assert plugins_file is not None

    class _ChangingPluginsPreflight:
        async def run(self) -> PreflightReport:
            plugins_file.write_text(plugins_file.read_text(encoding="utf-8") + "*NewPlugin.esp\n", encoding="utf-8")
            return PreflightReport(status=PreflightStatus.GREEN, checks=())

    service._preflight = _ChangingPluginsPreflight()  # type: ignore[assignment]
    journal = service._journal

    with patch.object(SynthesisRunner, "run_pipeline", new_callable=AsyncMock) as run_mock:
        result = await service.execute_pipeline(patcher_ids=["patcher"])

    assert result["success"] is False
    assert "plugins.txt changed during preflight" in result["message"]
    journal.begin_transaction.assert_not_awaited()
    run_mock.assert_not_awaited()


def test_split_default_upstream_aplica_si_el_campo_esta_ausente(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=255, split=True)
    settings_path = runner._config.pipeline_settings_path
    assert settings_path is not None
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    del payload["Profiles"][0]["SplitIfMaxMastersExceeded"]
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    check = service._build_master_overflow_check(runner, ["patcher"])

    assert check.status is PreflightStatus.YELLOW
    assert "auto-split" in check.summary


def test_split_malformado_no_se_trata_como_default_habilitado(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=255, split=True)
    settings_path = runner._config.pipeline_settings_path
    assert settings_path is not None
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["Profiles"][0]["SplitIfMaxMastersExceeded"] = "true"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    check = service._build_master_overflow_check(runner, ["patcher"])

    assert check.status is PreflightStatus.RED
    assert "no está confirmado" in check.summary


def test_fan_in_exactamente_254_esta_en_el_limite_seguro(tmp_path: pathlib.Path) -> None:
    service, runner = _service_case(tmp_path, plugin_count=253, split=False)

    check = service._build_master_overflow_check(runner, ["patcher"])

    assert check.status is PreflightStatus.GREEN
    assert "254/254" in check.summary


def test_perfil_synthesis_se_puede_mapear_sin_cambiar_el_load_order_mo2(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner = _service_case(tmp_path, plugin_count=20, split=True)
    settings_path = runner._config.pipeline_settings_path
    assert settings_path is not None
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["Profiles"][0]["Nickname"] = "Synthesis Production"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")
    service._synthesis_settings_path = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SYNTHESIS_PIPELINE_SETTINGS", raising=False)
    monkeypatch.setenv("SYNTHESIS_PROFILE_IDENTIFIER", "profile-default")

    resolved = service._ensure_synthesis_runner()

    assert resolved._config.profile == "profile-default"
    assert resolved._config.mo2_profile == "Default"
    assert resolved._config.load_order_file_path == tmp_path / "MO2" / "profiles" / "Default" / "plugins.txt"
    assert resolved._config.pipeline_settings_path == settings_path
    assert resolved.inspect_settings(["patcher"]).profile_identifier == "profile-default"


def test_profile_mo2_sin_plugins_txt_no_usa_loadorder_txt_como_reemplazo(tmp_path: pathlib.Path) -> None:
    service, _ = _service_case(tmp_path, plugin_count=20, split=True)
    plugins_file = tmp_path / "MO2" / "profiles" / "Default" / "plugins.txt"
    plugins_file.unlink()

    with pytest.raises(SynthesisExecutionError, match="no regular plugins.txt"):
        service._ensure_synthesis_runner()


def test_profile_mo2_inseguro_falla_antes_de_resolver_archivos(tmp_path: pathlib.Path) -> None:
    service, _ = _service_case(tmp_path, plugin_count=20, split=True)
    service._path_resolver.get_active_profile.return_value = "../escape"

    with pytest.raises(SynthesisExecutionError, match="active MO2 profile safely"):
        service._ensure_synthesis_runner()
