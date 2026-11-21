"""Tests TDD (PR-1 / ADR 0019) para el endurecimiento del contrato CLI de Synthesis 0.36.6.

Cubre:
- Settings V2: ``SplitIfMaxMastersExceeded`` explícito, ``DotNetPathOverride``,
  exclusión de patchers/grupos deshabilitados (``PrepForRun.cs:24``), lista blanca
  cerrada de ``$type`` (``TypeNameHandling.Auto``) y exposición de nicknames.
- Contrato CLI 0.36.6: ``--PersistenceMode Text`` fail-closed sin directorio,
  ``--ExtraDataFolder``, ``--TargetRuntime``, ``--BlockBuildingWithinMo2``,
  ``--Shortcircuit`` y bloqueo de alias largos/cortos (incluidos pegados).
- Contención y entorno mínimo: ``require_job=True``, ``max_output_bytes``, ``cwd``,
  lista blanca OS sin fuga de secretos, variables anti-servidor .NET 10
  (``DOTNET_CLI_USE_MSBUILD_SERVER=0``, ``MSBUILDDISABLENODEREUSE=1``,
  ``UseSharedCompilation=false``) y saneo de ``PATH`` frente a x86.
- Veredicto fail-closed: marcadores reales de ``ConsoleReporter``/``ErrorClassifier``,
  trampa de ``RunSomePatchers`` con ``exit 0`` y validación TES4 delegada al parser
  canónico ``read_plugin_header()`` (<= 254 masters, sin truncado ni symlinks).
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
import sys
from typing import Any
from unittest.mock import patch

import pytest

from sky_claw.local.tools.patcher_pipeline import PatcherPipeline, PatcherPipelineError
from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisRunner,
)


def _construir_tes4(masters: list[str], *, cuerpo_extra: bytes = b"") -> bytes:
    """Construye un archivo TES4 binario real con subrecords HEDR + MAST."""
    hedr_payload = struct.pack("<fII", 1.70, 1, 0x800)
    subrecords = [b"HEDR" + struct.pack("<H", len(hedr_payload)) + hedr_payload]
    for master in masters:
        encoded = master.encode("cp1252") + b"\x00"
        subrecords.append(b"MAST" + struct.pack("<H", len(encoded)) + encoded)
        subrecords.append(b"DATA" + struct.pack("<H", 8) + b"\x00" * 8)
    payload = b"".join(subrecords)
    header = (
        b"TES4"
        + struct.pack("<I", len(payload))
        + struct.pack("<I", 0)  # flags
        + struct.pack("<I", 0)  # formID
        + struct.pack("<I", 0)  # vc
        + struct.pack("<H", 44)  # formVersion SSE
        + struct.pack("<H", 0)  # vc2
    )
    return header + payload + cuerpo_extra


def _escribir_settings_v2(
    path: pathlib.Path,
    *,
    include_split_field: bool = True,
    split_value: bool = True,
    include_disabled_patcher: bool = False,
    patcher_type: str = "Synthesis.Bethesda.Execution.Settings.GithubPatcherSettings, Synthesis.Bethesda.Execution",
) -> None:
    patchers: list[dict[str, Any]] = [
        {
            "$type": patcher_type,
            "ID": "id-light",
            "Nickname": "PatcherLight",
            "On": True,
            "RemoteRepoPath": "https://example.invalid/light",
        },
        {
            "$type": patcher_type,
            "ID": "id-heavy",
            "Nickname": "PatcherHeavy",
            "On": True,
            "RemoteRepoPath": "https://example.invalid/heavy",
        },
    ]
    if include_disabled_patcher:
        patchers.append(
            {
                "$type": patcher_type,
                "ID": "id-disabled",
                "Nickname": "PatcherDisabled",
                "On": False,
                "RemoteRepoPath": "https://example.invalid/disabled",
            }
        )
    profile: dict[str, Any] = {
        "Nickname": "Default",
        "ID": "profile-default",
        "TargetRelease": "SkyrimSE",
        "Groups": [
            {
                "On": True,
                "Name": "Synthesis",
                "Patchers": patchers,
            }
        ],
        "UpdateLoadOrderAfterRun": True,
    }
    if include_split_field:
        profile["SplitIfMaxMastersExceeded"] = split_value
    if include_disabled_patcher:
        profile["Groups"].append(
            {
                "On": False,
                "Name": "DisabledGroup",
                "Patchers": [
                    {
                        "$type": "Synthesis.Bethesda.Execution.Settings.SolutionPatcherSettings, Synthesis.Bethesda.Execution",
                        "ID": "id-disabled-group-patcher",
                        "Nickname": "DisabledSolution",
                        "On": True,
                        "SolutionPath": "C:/disabled/patcher.sln",
                    }
                ],
            }
        )
    payload = {
        "Version": 2,
        "Profiles": [profile],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _crear_entorno_base(tmp_path: pathlib.Path, **config_kwargs: Any) -> tuple[SynthesisRunner, pathlib.Path]:
    game = tmp_path / "Skyrim"
    data = game / "Data"
    data.mkdir(parents=True)
    mo2 = tmp_path / "MO2"
    profile_dir = mo2 / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    load_order = profile_dir / "plugins.txt"
    load_order.write_bytes(b"*Skyrim.esm\n")
    settings_path = tmp_path / "PipelineSettings.json"
    _escribir_settings_v2(settings_path, include_split_field=False, include_disabled_patcher=True)
    exe = tmp_path / "Synthesis.Bethesda.CLI.exe"
    exe.touch()
    output = tmp_path / "output"
    output.mkdir()
    config = SynthesisConfig(
        game_path=game,
        mo2_path=mo2,
        output_path=output,
        synthesis_exe=exe,
        timeout_seconds=10,
        profile="Default",
        pipeline_settings_path=settings_path,
        data_folder_path=data,
        load_order_file_path=load_order,
        **config_kwargs,
    )
    return SynthesisRunner(config), output


# =============================================================================
# 1. Settings V2 (patcher_pipeline.py)
# =============================================================================


def test_prepare_settings_serializa_split_explicitamente_cuando_se_omite(tmp_path: pathlib.Path) -> None:
    settings_path = tmp_path / "PipelineSettings.json"
    _escribir_settings_v2(settings_path, include_split_field=False)

    temp_default, profile_default = PatcherPipeline.prepare_synthesis_cli_settings(settings_path, "Default")
    temp_false, profile_false = PatcherPipeline.prepare_synthesis_cli_settings(
        settings_path,
        "Default",
        split_if_max_masters_exceeded=False,
    )
    try:
        payload_default = json.loads(temp_default.read_text(encoding="utf-8"))
        payload_false = json.loads(temp_false.read_text(encoding="utf-8"))
        assert payload_default["Profiles"][0]["SplitIfMaxMastersExceeded"] is True
        assert profile_default.split_if_max_masters_exceeded is True
        assert payload_false["Profiles"][0]["SplitIfMaxMastersExceeded"] is False
        assert profile_false.split_if_max_masters_exceeded is False
    finally:
        temp_default.unlink(missing_ok=True)
        temp_false.unlink(missing_ok=True)


def test_prepare_settings_fija_dotnet_path_override_sin_mutar_original(tmp_path: pathlib.Path) -> None:
    settings_path = tmp_path / "PipelineSettings.json"
    _escribir_settings_v2(settings_path)
    original_bytes = settings_path.read_bytes()
    dotnet_x64 = pathlib.Path("C:/Program Files/dotnet/dotnet.exe")

    temp_path, _ = PatcherPipeline.prepare_synthesis_cli_settings(
        settings_path,
        "Default",
        dotnet_path_override=dotnet_x64,
    )
    try:
        payload = json.loads(temp_path.read_text(encoding="utf-8"))
        assert payload["DotNetPathOverride"] == str(dotnet_x64)
        assert settings_path.read_bytes() == original_bytes
    finally:
        temp_path.unlink(missing_ok=True)


def test_prepare_settings_elimina_patchers_y_grupos_deshabilitados(tmp_path: pathlib.Path) -> None:
    """PrepForRun.cs:24 recorre incluso patchers On=false: no deben sobrevivir en la copia."""
    settings_path = tmp_path / "PipelineSettings.json"
    _escribir_settings_v2(settings_path, include_disabled_patcher=True)

    temp_path, profile = PatcherPipeline.prepare_synthesis_cli_settings(settings_path, "Default")
    try:
        payload = json.loads(temp_path.read_text(encoding="utf-8"))
        groups = payload["Profiles"][0]["Groups"]
        assert [g["Name"] for g in groups] == ["Synthesis"]
        patcher_nicknames = [p["Nickname"] for p in groups[0]["Patchers"]]
        assert patcher_nicknames == ["PatcherLight", "PatcherHeavy"]
        assert profile.patcher_nicknames == ("PatcherLight", "PatcherHeavy")
    finally:
        temp_path.unlink(missing_ok=True)


def test_read_settings_rechaza_dollar_type_desconocido(tmp_path: pathlib.Path) -> None:
    """TypeNameHandling.Auto en Constants.cs exige lista blanca cerrada de $type."""
    settings_path = tmp_path / "PipelineSettings.json"
    _escribir_settings_v2(
        settings_path,
        patcher_type="System.Windows.Data.ObjectDataProvider, PresentationFramework",
    )

    with pytest.raises(PatcherPipelineError, match=r"Unsupported Synthesis patcher \$type"):
        PatcherPipeline.inspect_synthesis_settings(settings_path, "Default")


# =============================================================================
# 2. Contrato CLI 0.36.6 y flags protegidos
# =============================================================================


def test_persistence_mode_text_sin_ruta_falla_cerrado_antes_del_spawn(tmp_path: pathlib.Path) -> None:
    """RunPersistencePreparer.cs:28 lanza ArgumentNullException si Text no trae ruta."""
    with pytest.raises(SynthesisExecutionError, match="persistence_path"):
        _crear_entorno_base(tmp_path, persistence_mode="Text", persistence_path=None)


def test_build_cli_args_emite_opciones_oficiales_0366(tmp_path: pathlib.Path) -> None:
    extra_data = tmp_path / "extra_data"
    persistence_dir = tmp_path / "persistence_state"
    runner, _ = _crear_entorno_base(
        tmp_path,
        extra_data_folder=extra_data,
        persistence_mode="Text",
        persistence_path=persistence_dir,
        target_runtime="net10.0",
        block_building_within_mo2=True,
        shortcircuit=True,
    )

    args = runner._build_cli_args(["PatcherLight"])
    assert args[1] == "run-pipeline"
    assert args[args.index("--ExtraDataFolder") + 1] == str(extra_data)
    assert args[args.index("--PersistenceMode") + 1] == "Text"
    assert args[args.index("--PersistencePath") + 1] == str(persistence_dir)
    assert args[args.index("--TargetRuntime") + 1] == "net10.0"
    assert "--BlockBuildingWithinMo2" in args
    assert "--Shortcircuit" in args


@pytest.mark.parametrize(
    "tampered_arg",
    [
        "--ExtraDataFolder=/evil",
        "-e",
        "-e=/evil",
        "-e/evil",
        "--PersistencePath=/evil",
        "-r",
        "-r=/evil",
        "-r/evil",
        "--PersistenceMode=None",
        "-m",
        "-m=None",
        "-mNone",
        "--TargetRuntime=net8.0",
        "-t",
        "-t=net8.0",
        "-tnet8.0",
        "--BlockBuildingWithinMo2",
        "--BlockBuildingWithinMo2=false",
        "--Shortcircuit",
        "--Shortcircuit=false",
    ],
)
def test_extra_args_bloquea_opciones_0366_y_alias_cortos_pegados(
    tmp_path: pathlib.Path,
    tampered_arg: str,
) -> None:
    runner, _ = _crear_entorno_base(tmp_path)
    with pytest.raises(SynthesisExecutionError, match="cannot override managed"):
        runner._build_cli_args(["PatcherLight"], [tampered_arg])


# =============================================================================
# 3. Contención de proceso y entorno mínimo sin fuga de secretos
# =============================================================================


async def test_execute_process_pasa_require_job_limite_bytes_cwd_y_entorno_minimo(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "run_cwd"
    work_dir.mkdir()
    temp_dir = tmp_path / "run_tmp"
    temp_dir.mkdir()
    dotnet_x64 = tmp_path / "Program Files" / "dotnet"
    dotnet_x64.mkdir(parents=True)
    dotnet_x86 = tmp_path / "Program Files (x86)" / "dotnet"
    dotnet_x86.mkdir(parents=True)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-must-not-leak")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret-must-not-leak")
    monkeypatch.setenv("PATH", os.pathsep.join([str(dotnet_x86), str(tmp_path / "bin")]))

    runner, output = _crear_entorno_base(
        tmp_path,
        working_dir=work_dir,
        temp_dir=temp_dir,
        dotnet_root=dotnet_x64,
        max_output_bytes=4 * 1024 * 1024,
    )
    captured_kwargs: dict[str, Any] = {}

    async def _fake_capture(args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        captured_kwargs.update(kwargs)
        (output / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        return (
            b"[PatcherLight] Starting run.\n[PatcherLight] Run successful.\nExported patch to final destination: out\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _fake_capture):
        result = await runner.run_pipeline(["PatcherLight"])

    assert result.success is True
    assert captured_kwargs["require_job"] is True
    assert captured_kwargs["max_output_bytes"] == 4 * 1024 * 1024
    assert captured_kwargs["cwd"] == str(work_dir)
    env = captured_kwargs["env"]
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert env["DOTNET_CLI_USE_MSBUILD_SERVER"] == "0"
    assert env["MSBUILDDISABLENODEREUSE"] == "1"
    assert env["UseSharedCompilation"] == "false"
    assert env["DOTNET_NOLOGO"] == "1"
    assert env["DOTNET_CLI_TELEMETRY_OPTOUT"] == "1"
    assert env["TEMP"] == str(temp_dir)
    assert env["TMP"] == str(temp_dir)
    assert env["DOTNET_ROOT"] == str(dotnet_x64)
    path_entries = env["PATH"].split(os.pathsep)
    assert path_entries[0] == str(dotnet_x64)
    assert str(dotnet_x86) not in path_entries


# =============================================================================
# 4. Logs reales de ConsoleReporter / ErrorClassifier y trampa RunSomePatchers
# =============================================================================


def test_parse_output_reconoce_marcadores_reales_de_console_reporter(tmp_path: pathlib.Path) -> None:
    runner, _ = _crear_entorno_base(tmp_path)
    stdout = (
        "[PatcherLight] Starting run.\n"
        "[PatcherLight] Run successful.\n"
        "[PatcherHeavy] Starting run.\n"
        "[PatcherHeavy] Run successful.\n"
        "Exported patch to final destination: C:\\out\\Synthesis.esp\n"
    )
    success, executed, errors = runner.parse_output(stdout, "")
    assert success is True
    assert executed == ["PatcherLight", "PatcherHeavy"]
    assert errors == []


@pytest.mark.parametrize(
    "linea_fatal",
    [
        "Overall error:\nSystem.Exception: boom",
        "Error detected: Too Many Masters",
        "[PatcherLight] Preparation error:\nNuGet restore failed",
        "Process exited in failure: exit code 1",
        "Synthesis.Bethesda.Execution.Exceptions.Mo2BuildBlockedException: blocked",
        "Synthesis.Bethesda.Execution.Exceptions.Mo2BuildException: access denied",
        "NonAdjacentSplitModsException: splits not adjacent",
        "TooManyMastersException: exceeded 254",
        "MissingModException: missing Skyrim.esm",
        "error MSB4018: The build task failed unexpectedly.",
        "Program.cs(10,5): error CS0246: The type or namespace name could not be found",
    ],
)
def test_parse_output_detecta_errores_fatales_reales_upstream(
    tmp_path: pathlib.Path,
    linea_fatal: str,
) -> None:
    runner, _ = _crear_entorno_base(tmp_path)
    success, _, errors = runner.parse_output(
        "[PatcherLight] Run successful.\nExported patch to final destination: out\n",
        linea_fatal,
    )
    assert success is False
    assert errors


async def test_require_patcher_receipts_rechaza_patcher_omitido_con_exit_0(tmp_path: pathlib.Path) -> None:
    """Reproduce RunSomePatchers.cs:31-48: PatcherHeavy sale 0 sin archivo y grupo igual exporta."""
    runner, output = _crear_entorno_base(tmp_path, require_patcher_receipts=True)

    async def _omite_segundo_patcher(_args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        (output / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        return (
            b"[PatcherLight] Starting run.\n"
            b"[PatcherLight] Run successful.\n"
            b"[PatcherHeavy] Starting run.\n"
            b"Exported patch to final destination: out\\Synthesis.esp\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _omite_segundo_patcher):
        result = await runner.run_pipeline(["PatcherLight", "PatcherHeavy"])

    assert result.success is False
    assert result.output_esp is None
    assert any("PatcherHeavy" in err for err in result.errors)


async def test_require_patcher_receipts_pasa_cuando_todos_tienen_recibo_y_export(tmp_path: pathlib.Path) -> None:
    runner, output = _crear_entorno_base(tmp_path, require_patcher_receipts=True)

    async def _corrida_completa(_args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        (output / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        return (
            b"[PatcherLight] Starting run.\n"
            b"[PatcherLight] Run successful.\n"
            b"[PatcherHeavy] Starting run.\n"
            b"[PatcherHeavy] Run successful.\n"
            b"Exported patch to final destination: out\\Synthesis.esp\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _corrida_completa):
        result = await runner.run_pipeline(["PatcherLight", "PatcherHeavy"])

    assert result.success is True
    assert result.output_esp == output / "Synthesis.esp"
    assert result.patchers_executed == ["PatcherLight", "PatcherHeavy"]


# =============================================================================
# 5. Validación TES4 canónica (delegada en read_plugin_header)
# =============================================================================


async def test_validate_synthesis_esp_rechaza_header_tes4_truncado(tmp_path: pathlib.Path) -> None:
    """Header dice data_size=500 pero solo hay 120 bytes: el chequeo viejo dejaba pasar esto."""
    runner, output = _crear_entorno_base(tmp_path)
    esp = output / "Synthesis.esp"
    esp.write_bytes(b"TES4" + struct.pack("<I", 500) + b"\x00" * 120)

    assert await runner.validate_synthesis_esp(esp) is False


async def test_validate_synthesis_esp_rechaza_mas_de_254_masters_y_acepta_254(tmp_path: pathlib.Path) -> None:
    runner, output = _crear_entorno_base(tmp_path)
    esp_254 = output / "Synthesis_ok.esp"
    esp_255 = output / "Synthesis_overflow.esp"
    esp_254.write_bytes(_construir_tes4([f"Mod{i:03d}.esp" for i in range(254)]))
    esp_255.write_bytes(_construir_tes4([f"Mod{i:03d}.esp" for i in range(255)]))

    assert await runner.validate_synthesis_esp(esp_254) is True
    assert await runner.validate_synthesis_esp(esp_255) is False


async def test_validate_synthesis_esp_rechaza_symlink(tmp_path: pathlib.Path) -> None:
    runner, output = _crear_entorno_base(tmp_path)
    target = tmp_path / "real.esp"
    target.write_bytes(_construir_tes4(["Skyrim.esm"]))
    link = output / "Synthesis.esp"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("El entorno Windows no permite crear symlinks sin privilegios")

    assert await runner.validate_synthesis_esp(link) is False


# =============================================================================
# 6. Hardening post-revisión (Codex P1 + Qodo #1/#2/#3)
# =============================================================================


async def test_recibos_exigen_multiplicidad_cuando_dos_patchers_comparten_nickname(tmp_path: pathlib.Path) -> None:
    """Codex P1: dos IDs distintos con el mismo Nickname no pueden colapsar en un set."""
    runner, output = _crear_entorno_base(tmp_path, require_patcher_receipts=True)
    settings_path = runner._config.pipeline_settings_path
    assert settings_path is not None
    patcher_type = "Synthesis.Bethesda.Execution.Settings.GithubPatcherSettings, Synthesis.Bethesda.Execution"
    payload = {
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
                            {"$type": patcher_type, "ID": "id-one", "Nickname": "SameName", "On": True},
                            {"$type": patcher_type, "ID": "id-two", "Nickname": "SameName", "On": True},
                        ],
                    }
                ],
            }
        ],
    }
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    async def _solo_un_exito(_args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        (output / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        return (
            b"[SameName] Starting run.\n"
            b"[SameName] Run successful.\n"
            b"[SameName] Starting run.\n"
            b"Exported patch to final destination: out\\Synthesis.esp\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _solo_un_exito):
        incomplete = await runner.run_pipeline(["id-one", "id-two"])

    assert incomplete.success is False
    assert incomplete.output_esp is None
    assert any("SameName" in err for err in incomplete.errors)

    async def _ambos_exitos(_args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        (output / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]) + b"v2")
        return (
            b"[SameName] Starting run.\n"
            b"[SameName] Run successful.\n"
            b"[SameName] Starting run.\n"
            b"[SameName] Run successful.\n"
            b"Exported patch to final destination: out\\Synthesis.esp\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _ambos_exitos):
        complete = await runner.run_pipeline(["id-one", "id-two"])

    assert complete.success is True
    assert complete.output_esp == output / "Synthesis.esp"


async def test_recibos_exigen_confirmacion_export_por_cada_grupo_habilitado(tmp_path: pathlib.Path) -> None:
    """Hermano de Codex P1: N grupos habilitados requieren N confirmaciones Exported patch."""
    runner, output = _crear_entorno_base(tmp_path, require_patcher_receipts=True)
    settings_path = runner._config.pipeline_settings_path
    assert settings_path is not None
    patcher_type = "Synthesis.Bethesda.Execution.Settings.GithubPatcherSettings, Synthesis.Bethesda.Execution"
    payload = {
        "Version": 2,
        "Profiles": [
            {
                "Nickname": "Default",
                "ID": "profile-default",
                "TargetRelease": "SkyrimSE",
                "Groups": [
                    {
                        "On": True,
                        "Name": "GroupA",
                        "Patchers": [{"$type": patcher_type, "ID": "id-a", "Nickname": "PatcherA", "On": True}],
                    },
                    {
                        "On": True,
                        "Name": "GroupB",
                        "Patchers": [{"$type": patcher_type, "ID": "id-b", "Nickname": "PatcherB", "On": True}],
                    },
                ],
            }
        ],
    }
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    async def _un_solo_export(_args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        (output / "GroupA.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        (output / "GroupB.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        return (
            b"[PatcherA] Starting run.\n"
            b"[PatcherA] Run successful.\n"
            b"[PatcherB] Starting run.\n"
            b"[PatcherB] Run successful.\n"
            b"Exported patch to final destination: out\\GroupA.esp\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _un_solo_export):
        incomplete = await runner.run_pipeline(["id-a", "id-b"])

    assert incomplete.success is False
    assert any("Exported patch to final destination:" in err for err in incomplete.errors)

    async def _dos_exports(_args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        (output / "GroupA.esp").write_bytes(_construir_tes4(["Skyrim.esm"]) + b"2")
        (output / "GroupB.esp").write_bytes(_construir_tes4(["Skyrim.esm"]) + b"2")
        return (
            b"[PatcherA] Starting run.\n"
            b"[PatcherA] Run successful.\n"
            b"[PatcherB] Starting run.\n"
            b"[PatcherB] Run successful.\n"
            b"Exported patch to final destination: out\\GroupA.esp\n"
            b"Exported patch to final destination: out\\GroupB.esp\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _dos_exports):
        complete = await runner.run_pipeline(["id-a", "id-b"])

    assert complete.success is True


async def test_dotnet_root_normaliza_ruta_a_ejecutable_y_rechaza_archivo_arbitrario(tmp_path: pathlib.Path) -> None:
    """Qodo #1: si dotnet_root apunta a dotnet.exe, normalizar al padre; archivo arbitrario falla cerrado."""
    dotnet_dir = tmp_path / "dotnet64"
    dotnet_dir.mkdir()
    dotnet_exe = dotnet_dir / ("dotnet.exe" if sys.platform == "win32" else "dotnet")
    dotnet_exe.touch()

    runner, output = _crear_entorno_base(tmp_path, dotnet_root=dotnet_exe)
    assert runner._config.dotnet_root == dotnet_dir

    captured_override: list[str] = []
    captured_env: dict[str, str] = {}

    async def _inspect_temp(args: list[str], **kwargs: Any) -> tuple[bytes, bytes, int]:
        temp_settings = pathlib.Path(args[args.index("--PipelineSettingsPath") + 1])
        data = json.loads(temp_settings.read_text(encoding="utf-8"))
        captured_override.append(data["DotNetPathOverride"])
        captured_env.update(kwargs["env"])
        (output / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
        return b"Successfully generated patch output", b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _inspect_temp):
        result = await runner.run_pipeline(["PatcherLight"])

    assert result.success is True
    assert captured_override == [str(dotnet_exe)]
    assert captured_env["DOTNET_ROOT"] == str(dotnet_dir)

    arbitrary = tmp_path / "not_dotnet.txt"
    arbitrary.write_text("x", encoding="utf-8")
    with pytest.raises(SynthesisExecutionError, match="directory"):
        _crear_entorno_base(tmp_path / "sub", dotnet_root=arbitrary)


def test_entorno_filtra_subdirectorios_dotnet_x86_sin_afectar_x64(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qodo #2: filtrar también subdirectorios bajo Program Files (x86)\\dotnet\\... en PATH."""
    dotnet_x64 = tmp_path / "Program Files" / "dotnet"
    dotnet_x64.mkdir(parents=True)
    x86_root = r"C:\Program Files (x86)\dotnet"
    x86_sdk = r"C:\Program Files (x86)\dotnet\sdk\8.0.406"
    x86_tools = "C:/Program Files (x86)/dotnet/tools"
    unrelated_x86 = r"C:\Program Files (x86)\Steam"
    unrelated_dotnet_like = r"C:\Program Files (x86)\dotnet-custom"
    x64_sdk = r"C:\Program Files\dotnet\sdk\10.0.100"

    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([x86_root, x86_sdk, x86_tools, unrelated_x86, unrelated_dotnet_like, x64_sdk]),
    )
    runner, _ = _crear_entorno_base(tmp_path, dotnet_root=dotnet_x64)
    entries = runner._build_subprocess_env()["PATH"].split(os.pathsep)

    assert entries[0] == str(dotnet_x64)
    assert x86_root not in entries
    assert x86_sdk not in entries
    assert x86_tools not in entries
    assert unrelated_x86 in entries
    assert unrelated_dotnet_like in entries
    assert x64_sdk in entries


def test_settings_v2_acepta_type_valido_de_perfil_y_rechaza_type_anidado_o_en_grupo(
    tmp_path: pathlib.Path,
) -> None:
    """Qodo #3: valida $type polimórfico permitido en Profile y rechaza $type en raíz, grupo o subobjetos."""
    settings_path = tmp_path / "PipelineSettings.json"
    _escribir_settings_v2(settings_path)
    base = json.loads(settings_path.read_text(encoding="utf-8"))

    # Caso válido: el perfil declara el tipo polimórfico real ISynthesisProfileSettings -> V2.SynthesisProfile.
    valid = json.loads(json.dumps(base))
    valid["Profiles"][0]["$type"] = (
        "Synthesis.Bethesda.Execution.Settings.V2.SynthesisProfile, Synthesis.Bethesda.Execution"
    )
    settings_path.write_text(json.dumps(valid), encoding="utf-8")
    profile = PatcherPipeline.inspect_synthesis_settings(settings_path, "Default")
    assert profile.group_names == ("Synthesis",)

    # Caso inválido 1: $type en objeto raíz.
    bad_root = json.loads(json.dumps(base))
    bad_root["$type"] = "System.Windows.Data.ObjectDataProvider, PresentationFramework"
    settings_path.write_text(json.dumps(bad_root), encoding="utf-8")
    with pytest.raises(PatcherPipelineError, match=r"\$type"):
        PatcherPipeline.inspect_synthesis_settings(settings_path, "Default")

    # Caso inválido 2: $type no permitido en Profile.
    bad_profile = json.loads(json.dumps(base))
    bad_profile["Profiles"][0]["$type"] = "System.Diagnostics.Process, System"
    settings_path.write_text(json.dumps(bad_profile), encoding="utf-8")
    with pytest.raises(PatcherPipelineError, match=r"\$type"):
        PatcherPipeline.inspect_synthesis_settings(settings_path, "Default")

    # Caso inválido 3: $type dentro de un grupo habilitado.
    bad_group = json.loads(json.dumps(base))
    bad_group["Profiles"][0]["Groups"][0]["$type"] = "System.Object, mscorlib"
    settings_path.write_text(json.dumps(bad_group), encoding="utf-8")
    with pytest.raises(PatcherPipelineError, match=r"\$type"):
        PatcherPipeline.inspect_synthesis_settings(settings_path, "Default")

    # Caso inválido 4: $type anidado en un subobjeto del patcher (p. ej. LastSuccessfulRun).
    bad_nested = json.loads(json.dumps(base))
    bad_nested["Profiles"][0]["Groups"][0]["Patchers"][0]["LastSuccessfulRun"] = {
        "$type": "System.Windows.Data.ObjectDataProvider, PresentationFramework",
        "TargetRepo": "https://example.invalid/x",
    }
    settings_path.write_text(json.dumps(bad_nested), encoding="utf-8")
    with pytest.raises(PatcherPipelineError, match=r"\$type"):
        PatcherPipeline.inspect_synthesis_settings(settings_path, "Default")
