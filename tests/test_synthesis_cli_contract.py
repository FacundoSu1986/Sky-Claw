"""Contrato real de argumentos y settings para Synthesis CLI 0.36.6 / V2."""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools.patcher_pipeline import PatcherPipeline, PatcherPipelineError
from sky_claw.local.tools.synthesis_runner import SynthesisConfig, SynthesisExecutionError, SynthesisRunner


def _write_settings(path: pathlib.Path) -> None:
    patcher_type = "Synthesis.Bethesda.Execution.Settings.GithubPatcherSettings, Synthesis.Bethesda.Execution"
    settings = {
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
                                "$type": patcher_type,
                                "ID": "id-light",
                                "Nickname": "patcher-light",
                                "On": True,
                                "RemoteRepoPath": "https://example.invalid/light",
                            },
                            {
                                "$type": patcher_type,
                                "ID": "id-heavy",
                                "Nickname": "patcher-heavy",
                                "On": True,
                                "RemoteRepoPath": "https://example.invalid/heavy",
                            },
                        ],
                    }
                ],
                "SplitIfMaxMastersExceeded": True,
                "UpdateLoadOrderAfterRun": True,
            }
        ],
        "WorkingDirectory": str(path.parent),
    }
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _runner(tmp_path: pathlib.Path) -> tuple[SynthesisRunner, pathlib.Path, pathlib.Path, pathlib.Path]:
    game = tmp_path / "Skyrim"
    data = game / "Data"
    data.mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    profile_dir = mo2 / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    # LoadOrderFilePath in Synthesis 0.36.6 overrides Mutagen's plugin-listings
    # path provider, so the MO2 input must retain the '*' activation markers.
    load_order = profile_dir / "plugins.txt"
    load_order.write_text("*Skyrim.esm\n", encoding="utf-8")
    settings_path = tmp_path / "PipelineSettings.json"
    _write_settings(settings_path)
    synthesis_exe = tmp_path / "Synthesis.CLI.exe"
    synthesis_exe.touch()
    output = tmp_path / "output"
    output.mkdir()
    runner = SynthesisRunner(
        SynthesisConfig(
            game_path=game,
            mo2_path=mo2,
            output_path=output,
            synthesis_exe=synthesis_exe,
            timeout_seconds=10,
            profile="Default",
            pipeline_settings_path=settings_path,
            data_folder_path=data,
            load_order_file_path=load_order,
        )
    )
    return runner, settings_path, data, load_order


async def test_runner_emite_argv_oficial_y_filtra_settings_sin_mutar_mo2(tmp_path: pathlib.Path) -> None:
    runner, source_settings, data_folder, load_order = _runner(tmp_path)
    output = tmp_path / "output"
    captured: dict[str, object] = {}

    async def _fake_cli(args: list[str], **kwargs: object) -> tuple[bytes, bytes, int]:
        captured["args"] = list(args)
        captured["timeout"] = kwargs["timeout"]
        settings_argument = pathlib.Path(args[args.index("--PipelineSettingsPath") + 1])
        captured["temporary_settings"] = settings_argument
        assert settings_argument != source_settings
        plugins_argument = pathlib.Path(args[args.index("--LoadOrderFilePath") + 1])
        captured["temporary_plugins"] = plugins_argument
        assert plugins_argument != load_order
        assert plugins_argument.read_bytes() == load_order.read_bytes()
        temporary_settings = json.loads(settings_argument.read_text(encoding="utf-8"))
        captured["temporary_payload"] = temporary_settings
        profile = temporary_settings["Profiles"][0]
        assert profile["Nickname"] == "Default"
        assert profile["UpdateLoadOrderAfterRun"] is False
        assert [p["Nickname"] for p in profile["Groups"][0]["Patchers"]] == ["patcher-heavy"]
        (output / "Synthesis.esp").write_bytes(b"TES4" + b"\x00" * 128)
        (output / "Synthesis_2.esp").write_bytes(b"TES4" + b"\x01" * 128)
        # No texto de sucesso inventado por los tests antiguos: el exit code y
        # la atribución de cada ESP son la señal de éxito del contrato.
        return b"Pipeline run completed", b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _fake_cli):
        result = await runner.run_pipeline(["patcher-heavy"])

    args = captured["args"]
    assert isinstance(args, list)
    assert args[1] == "run-pipeline"
    assert args[args.index("--OutputDirectory") + 1] == str(output)
    assert args[args.index("--DataFolderPath") + 1] == str(data_folder)
    temporary_plugins = captured["temporary_plugins"]
    assert isinstance(temporary_plugins, pathlib.Path)
    assert args[args.index("--LoadOrderFilePath") + 1] == str(temporary_plugins)
    assert args[args.index("--ProfileIdentifier") + 1] == "Default"
    assert "--patcher" not in args
    assert "--game-path" not in args
    assert "--output-path" not in args
    assert "--no-prompt" not in args

    temporary_settings = captured["temporary_settings"]
    assert isinstance(temporary_settings, pathlib.Path)
    assert not temporary_settings.exists()
    temporary_plugins = captured["temporary_plugins"]
    assert isinstance(temporary_plugins, pathlib.Path)
    assert not temporary_plugins.exists()
    assert result.success is True
    assert result.output_esp == output / "Synthesis.esp"
    assert result.output_esps == (output / "Synthesis.esp", output / "Synthesis_2.esp")
    assert result.errors == []


async def test_settings_profile_y_patcher_desconocidos_fallan_antes_del_proceso(tmp_path: pathlib.Path) -> None:
    runner, _, _, _ = _runner(tmp_path)

    with pytest.raises(SynthesisExecutionError, match="not enabled"):
        runner.inspect_settings(["no-configurado"])


async def test_runner_rechaza_settings_cambiados_despues_del_preflight(tmp_path: pathlib.Path) -> None:
    runner, settings_path, _, _ = _runner(tmp_path)
    expected_fingerprint = runner.settings_fingerprint()
    settings_path.write_text(settings_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    run_capture = AsyncMock()

    with (
        patch("sky_claw.local.tools.synthesis_runner.run_capture", run_capture),
        pytest.raises(SynthesisExecutionError, match="changed after preflight"),
    ):
        await runner.run_pipeline(
            ["patcher-heavy"],
            expected_settings_fingerprint=expected_fingerprint,
        )

    run_capture.assert_not_awaited()


async def test_runner_rechaza_plugins_txt_cambiado_despues_del_preflight(tmp_path: pathlib.Path) -> None:
    runner, _, _, plugins_file = _runner(tmp_path)
    expected_fingerprint = runner.load_order_fingerprint()
    plugins_file.write_text("*Skyrim.esm\n*NewPlugin.esp\n", encoding="utf-8")
    run_capture = AsyncMock()

    with (
        patch("sky_claw.local.tools.synthesis_runner.run_capture", run_capture),
        pytest.raises(SynthesisExecutionError, match="plugins.txt changed after preflight"),
    ):
        await runner.run_pipeline(
            ["patcher-heavy"],
            expected_load_order_fingerprint=expected_fingerprint,
        )

    run_capture.assert_not_awaited()


async def test_runner_detecta_settings_modificados_durante_la_cli(tmp_path: pathlib.Path) -> None:
    runner, settings_path, _, _ = _runner(tmp_path)
    expected_settings = runner.settings_fingerprint()
    expected_plugins = runner.load_order_fingerprint()

    async def _mutating_cli(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        settings_path.write_text(settings_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return b"Pipeline run completed", b"", 0

    with (
        patch("sky_claw.local.tools.synthesis_runner.run_capture", _mutating_cli),
        pytest.raises(SynthesisExecutionError, match="changed while Synthesis was running"),
    ):
        await runner.run_pipeline(
            ["patcher-heavy"],
            expected_settings_fingerprint=expected_settings,
            expected_load_order_fingerprint=expected_plugins,
        )


async def test_runner_usa_snapshot_de_plugins_y_detecta_cambios_durante_la_cli(tmp_path: pathlib.Path) -> None:
    runner, _, _, plugins_file = _runner(tmp_path)
    expected_settings = runner.settings_fingerprint()
    expected_plugins = runner.load_order_fingerprint()
    captured_snapshot: pathlib.Path | None = None

    async def _mutating_cli(args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        nonlocal captured_snapshot
        captured_snapshot = pathlib.Path(args[args.index("--LoadOrderFilePath") + 1])
        assert captured_snapshot != plugins_file
        assert captured_snapshot.read_bytes() == b"*Skyrim.esm\n"
        plugins_file.write_text("*Skyrim.esm\n*NewPlugin.esp\n", encoding="utf-8")
        return b"Pipeline run completed", b"", 0

    with (
        patch("sky_claw.local.tools.synthesis_runner.run_capture", _mutating_cli),
        pytest.raises(SynthesisExecutionError, match="plugins.txt changed while Synthesis was running"),
    ):
        await runner.run_pipeline(
            ["patcher-heavy"],
            expected_settings_fingerprint=expected_settings,
            expected_load_order_fingerprint=expected_plugins,
        )

    assert captured_snapshot is not None
    assert not captured_snapshot.exists()


async def test_split_sin_output_base_no_se_reporta_como_exito(tmp_path: pathlib.Path) -> None:
    runner, _, _, _ = _runner(tmp_path)
    output = tmp_path / "output"

    async def _solo_split(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        (output / "Synthesis_2.esp").write_bytes(b"TES4" + b"\x02" * 128)
        return b"Pipeline run completed", b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _solo_split):
        result = await runner.run_pipeline(["patcher-heavy"])

    assert result.success is False
    assert result.output_esp is None
    assert result.output_esps == (output / "Synthesis_2.esp",)
    assert any("base output Synthesis.esp is missing" in error for error in result.errors)


def test_group_base_name_that_collides_with_another_groups_split_is_rejected(tmp_path: pathlib.Path) -> None:
    runner, settings_path, _, _ = _runner(tmp_path)
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    extra_group = json.loads(json.dumps(payload["Profiles"][0]["Groups"][0]))
    extra_group["Name"] = "Synthesis_2"
    extra_group["Patchers"][0]["ID"] = "id-other"
    extra_group["Patchers"][0]["Nickname"] = "other"
    payload["Profiles"][0]["Groups"].append(extra_group)
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SynthesisExecutionError, match="overlap split output"):
        runner.inspect_settings()


@pytest.mark.parametrize(
    ("selector", "ambiguous"),
    [("requested", False), ("id-selected", True)],
)
def test_selector_no_incluye_otro_patcher_cuyo_alias_coincide_con_id_canonico(
    tmp_path: pathlib.Path,
    selector: str,
    ambiguous: bool,
) -> None:
    _, settings_path, _, _ = _runner(tmp_path)
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    patchers = payload["Profiles"][0]["Groups"][0]["Patchers"]
    patchers[0]["ID"] = "id-selected"
    patchers[0]["Nickname"] = "requested"
    patchers[1]["ID"] = "id-other"
    patchers[1]["Nickname"] = "id-selected"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    if ambiguous:
        with pytest.raises(PatcherPipelineError, match="ambiguous"):
            PatcherPipeline.prepare_synthesis_cli_settings(settings_path, "Default", [selector])
        return

    temporary_settings, profile = PatcherPipeline.prepare_synthesis_cli_settings(
        settings_path,
        "Default",
        [selector],
    )
    try:
        temporary_payload = json.loads(temporary_settings.read_text(encoding="utf-8"))
        chosen = temporary_payload["Profiles"][0]["Groups"][0]["Patchers"]
        assert profile.patcher_ids == ("id-selected",)
        assert [patcher["Nickname"] for patcher in chosen] == ["requested"]
    finally:
        temporary_settings.unlink(missing_ok=True)


def test_split_collision_con_sufijo_decimal_enorme_falla_como_error_de_dominio(tmp_path: pathlib.Path) -> None:
    runner, settings_path, _, _ = _runner(tmp_path)
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    extra_group = json.loads(json.dumps(payload["Profiles"][0]["Groups"][0]))
    extra_group["Name"] = "Synthesis_" + "9" * 5000
    extra_group["Patchers"][0]["ID"] = "id-other"
    extra_group["Patchers"][0]["Nickname"] = "other"
    payload["Profiles"][0]["Groups"].append(extra_group)
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SynthesisExecutionError, match="overlap split output"):
        runner.inspect_settings()


def test_mo2_profile_and_synthesis_profile_identifier_are_independent(tmp_path: pathlib.Path) -> None:
    from dataclasses import replace

    runner, _, _, load_order = _runner(tmp_path)
    config = replace(
        runner._config,
        profile="profile-default",
        mo2_profile="Default",
        load_order_file_path=None,
    )
    mapped_runner = SynthesisRunner(config)

    assert config.profile == "profile-default"
    assert config.mo2_profile == "Default"
    assert config.load_order_file_path == load_order
    args = mapped_runner._build_cli_args(["id-heavy"])
    assert args[args.index("--ProfileIdentifier") + 1] == "profile-default"
    assert args[args.index("--LoadOrderFilePath") + 1] == str(load_order)
    with pytest.raises(SynthesisExecutionError, match="cannot override managed"):
        mapped_runner._build_cli_args(["id-heavy"], ["--PipelineSettingsPath=/tampered.json"])


def test_default_pipeline_settings_path_is_current_working_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sky_claw.local.tools.synthesis_runner import SynthesisConfig

    monkeypatch.chdir(tmp_path)
    exe_dir = tmp_path / "tools"
    exe_dir.mkdir()
    exe = exe_dir / "Synthesis.CLI.exe"
    exe.touch()
    config = SynthesisConfig(
        game_path=tmp_path / "Skyrim",
        mo2_path=tmp_path / "MO2",
        output_path=tmp_path / "output",
        synthesis_exe=exe,
    )

    assert config.pipeline_settings_path == tmp_path / "PipelineSettings.json"
