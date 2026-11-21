"""Tests TDD (PR-2 / ADR 0019) para ejecución brokered de Synthesis bajo MO2 USVFS.

Cubre:
- ``tool_id="synthesis"`` en ``ALLOWED_VFS_TOOL_IDS`` y ``"synthesis.bethesda.cli.exe"`` en
  ``SYNTHESIS_CLI_EXECUTABLE_NAMES`` (rechazando ``Synthesis.exe`` GUI WPF).
- Validación estricta del payload en ``execute_worker_manifest`` (esquema cerrado, sin shell ni
  claves desconocidas, ``output_path`` ligado a ``mutation_targets``, rechazo de symlinks,
  visibilidad obligatoria de ``expected_plugins`` en el Data virtual antes del spawn, y
  validación binaria TES4 de cada ESP generado).
- ``BrokeredSynthesisRunner``: construcción fail-closed, ``data_visibility_domain ==
  "virtual_usvfs"``, rechazo de ``extra_args``, armado de ``VfsJob(tool_id="synthesis")``,
  propagación de ``terminality_unknown`` en timeouts y traducción de fallos tipados del worker.
- ``SynthesisPipelineService``: en dominio ``virtual_usvfs`` omite el sensor físico
  ``vfs_visibility`` (evitando falso rojo bajo MO2 USVFS), mientras el dominio ``physical``
  conserva U-01 activo; sin fallback silencioso a standalone.
"""

from __future__ import annotations

import json
import pathlib
import struct
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.db.rollback_veto import exception_forbids_rollback
from sky_claw.local.mo2.brokered_synthesis import BrokeredSynthesisRunner
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import VfsJobTimeoutError
from sky_claw.local.mo2.vfs_contracts import (
    ALLOWED_VFS_TOOL_IDS,
    SYNTHESIS_CLI_EXECUTABLE_NAMES,
    VFS_PROTOCOL_VERSION,
    VfsJob,
    VfsJobResult,
)
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import execute_worker_manifest
from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisNotFoundError,
    SynthesisTimeoutError,
)
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from sky_claw.local.validators.preflight import PreflightStatus


def _construir_tes4(masters: list[str]) -> bytes:
    """Construye un plugin TES4 binario válido con subrecords HEDR + MAST."""
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
        + struct.pack("<I", 0)
        + struct.pack("<I", 0)
        + struct.pack("<I", 0)
        + struct.pack("<H", 44)
        + struct.pack("<H", 0)
    )
    return header + payload


def _escenario_mo2(
    tmp_path: pathlib.Path,
    *,
    exe_name: str = "Synthesis.Bethesda.CLI.exe",
) -> dict[str, pathlib.Path]:
    mo2 = tmp_path / "MO2"
    perfil = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    data_fisico = tmp_path / "Skyrim" / "Data"
    data_virtual = tmp_path / "virtual" / "Data"
    output_dir = mo2 / "mods" / "Sky-Claw Synthesis Output"
    for carpeta in (perfil, mod, data_fisico, data_virtual, output_dir):
        carpeta.mkdir(parents=True)
    (perfil / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (perfil / "plugins.txt").write_text("*Skyrim.esm\n*MiModVirtual.esp\n", encoding="utf-8")
    (data_fisico / "Skyrim.esm").write_bytes(_construir_tes4([]))
    (mod / "canary.txt").write_bytes(b"canary-synthesis")
    (mod / "MiModVirtual.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
    # En Data virtual existen el canario y MiModVirtual.esp (NO en Data físico).
    (data_virtual / "canary.txt").write_bytes(b"canary-synthesis")
    (data_virtual / "Skyrim.esm").write_bytes(_construir_tes4([]))
    (data_virtual / "MiModVirtual.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))

    settings_path = tmp_path / "PipelineSettings.json"
    settings_path.write_text(
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
                                        "$type": (
                                            "Synthesis.Bethesda.Execution.Settings.CliPatcherSettings, "
                                            "Synthesis.Bethesda.Execution"
                                        ),
                                        "ID": "id-patcher-a",
                                        "Nickname": "PatcherA",
                                        "On": True,
                                        "PathToExecutable": str(tmp_path / "PatcherA.exe"),
                                    }
                                ],
                            }
                        ],
                        "SplitIfMaxMastersExceeded": True,
                        "UpdateLoadOrderAfterRun": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    exe = tmp_path / exe_name
    exe.touch()
    return {
        "game": tmp_path / "Skyrim",
        "mo2": mo2,
        "mods": mo2 / "mods",
        "data_fisico": data_fisico,
        "data_virtual": data_virtual,
        "output": output_dir,
        "settings": settings_path,
        "plugins": perfil / "plugins.txt",
        "exe": exe,
    }


async def _probe_ok(_path: pathlib.Path, sha: str, _timeout: float) -> str:
    return sha


def _manifiesto_synthesis(
    rutas: dict[str, pathlib.Path],
    *,
    payload_overrides: dict[str, Any] | None = None,
    mutation_targets: tuple[pathlib.Path, ...] | None = None,
) -> VfsWorkerManifest:
    challenge = build_attestation_challenge(
        mo2_root=rutas["mo2"],
        profile="Default",
        physical_data_dir=rutas["data_fisico"],
    )
    payload: dict[str, Any] = {
        "synthesis_exe": str(rutas["exe"]),
        "output_path": str(rutas["output"]),
        "pipeline_settings_path": str(rutas["settings"]),
        "synthesis_profile": "Default",
        "patcher_ids": ["PatcherA"],
        "expected_plugins": ["Skyrim.esm", "MiModVirtual.esp"],
        "block_building_within_mo2": True,
        "shortcircuit": True,
        "require_patcher_receipts": True,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    targets = mutation_targets if mutation_targets is not None else (rutas["output"],)
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="synthesis",
        payload=payload,
        timeout_seconds=60.0,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=targets,
    )
    return VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        mo2_root=rutas["mo2"],
        virtual_data_dir=rutas["data_virtual"],
        descriptor_path=rutas["mo2"] / "descriptor.json",
    )


def test_synthesis_esta_registrado_en_contratos_vfs() -> None:
    assert "synthesis" in ALLOWED_VFS_TOOL_IDS
    assert "synthesis.bethesda.cli.exe" in SYNTHESIS_CLI_EXECUTABLE_NAMES
    assert "synthesis.exe" not in SYNTHESIS_CLI_EXECUTABLE_NAMES


async def test_worker_synthesis_ejecuta_y_valida_tes4_bajo_usvfs(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    manifest = _manifiesto_synthesis(rutas)
    captured_args: list[str] = []

    async def _fake_cli(args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        captured_args.extend(args)
        (rutas["output"] / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm", "MiModVirtual.esp"]))
        return (
            b"[PatcherA] Starting run.\n[PatcherA] Run successful.\nExported patch to final destination: out\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _fake_cli):
        result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is True
    assert result.exit_code == 0
    assert result.tool_result["output_esp"] == str((rutas["output"] / "Synthesis.esp").resolve())
    assert result.tool_result["patchers_executed"] == ["PatcherA"]
    assert captured_args[captured_args.index("--DataFolderPath") + 1] == str(rutas["data_virtual"].resolve())
    assert "--BlockBuildingWithinMo2" in captured_args
    assert "--Shortcircuit" in captured_args


async def test_worker_synthesis_rechaza_gui_wpf_synthesis_exe(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path, exe_name="Synthesis.exe")
    manifest = _manifiesto_synthesis(rutas)

    result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert "synthesis.bethesda.cli.exe" in result.message.casefold()


async def test_worker_synthesis_rechaza_output_fuera_de_mutation_targets(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    otro_destino = tmp_path / "otro_output"
    otro_destino.mkdir()
    manifest = _manifiesto_synthesis(rutas, payload_overrides={"output_path": str(otro_destino)})

    result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert "mutation_targets" in result.message


async def test_worker_synthesis_rechaza_symlink_en_rutas(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    target = tmp_path / "real_settings.json"
    target.write_text(rutas["settings"].read_text(encoding="utf-8"), encoding="utf-8")
    link = tmp_path / "symlink_settings.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("El entorno Windows no permite crear symlinks sin privilegios")

    manifest = _manifiesto_synthesis(rutas, payload_overrides={"pipeline_settings_path": str(link)})

    result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert "symlink" in result.message.casefold()


async def test_worker_synthesis_bloquea_si_falta_plugin_en_data_virtual(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    (rutas["data_virtual"] / "MiModVirtual.esp").unlink()
    manifest = _manifiesto_synthesis(rutas)
    run_capture_mock = AsyncMock()

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", run_capture_mock):
        result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert "MiModVirtual.esp" in result.message
    run_capture_mock.assert_not_awaited()


async def test_worker_synthesis_rechaza_clave_desconocida_en_payload(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    manifest = _manifiesto_synthesis(rutas, payload_overrides={"shell_command": "calc.exe"})

    result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert "payload" in result.message.casefold()


@pytest.mark.parametrize(
    "bytes_esp",
    [
        pytest.param(b"TES4" + struct.pack("<I", 500) + b"\x00" * 120, id="header_truncado"),
        pytest.param(
            _construir_tes4([f"Mod{i:03d}.esp" for i in range(255)]),
            id="mas_de_254_masters",
        ),
    ],
)
async def test_worker_synthesis_falla_si_esp_generado_es_invalido(
    tmp_path: pathlib.Path,
    bytes_esp: bytes,
) -> None:
    rutas = _escenario_mo2(tmp_path)
    manifest = _manifiesto_synthesis(rutas)

    async def _esp_invalido(_args: list[str], **_kwargs: Any) -> tuple[bytes, bytes, int]:
        (rutas["output"] / "Synthesis.esp").write_bytes(bytes_esp)
        return (
            b"[PatcherA] Starting run.\n[PatcherA] Run successful.\nExported patch to final destination: out\n",
            b"",
            0,
        )

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _esp_invalido):
        result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert "tes4" in result.message.casefold() or "invalid" in result.message.casefold()


def test_brokered_runner_rechaza_synthesis_exe_gui_al_construirse(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path, exe_name="Synthesis.exe")
    config = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
    )
    with pytest.raises(SynthesisExecutionError, match=r"Synthesis\.Bethesda\.CLI\.exe"):
        BrokeredSynthesisRunner(
            config=config,
            broker=MagicMock(),
            instance_id="portable-main",
            data_root=rutas["mo2"],
            mods_dir=rutas["mods"],
        )


async def test_brokered_runner_rechaza_extra_args(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    config = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
    )
    runner = BrokeredSynthesisRunner(
        config=config,
        broker=MagicMock(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )
    with pytest.raises(SynthesisExecutionError, match="extra_args"):
        await runner.run_pipeline(["PatcherA"], extra_args=["--CustomFlag"])


async def test_brokered_runner_envia_job_y_devuelve_synthesis_result(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    config = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        timeout_seconds=120,
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
        block_building_within_mo2=True,
        shortcircuit=True,
        require_patcher_receipts=True,
    )
    captured_job: dict[str, VfsJob] = {}
    expected_esp = (rutas["output"] / "Synthesis.esp").resolve()

    class _FakeBroker:
        async def submit(self, job: VfsJob, **_kwargs: Any) -> VfsJobResult:
            captured_job["job"] = job
            return VfsJobResult(
                protocol_version=VFS_PROTOCOL_VERSION,
                job_id=job.job_id,
                success=True,
                message="",
                exit_code=0,
                stdout="[PatcherA] Run successful.",
                stderr="",
                outputs=(rutas["output"].resolve(),),
                rollback_state="not_required",
                attestation={"canary": "ok"},
                tool_result={
                    "output_esp": str(expected_esp),
                    "output_esps": [str(expected_esp)],
                    "patchers_executed": ["PatcherA"],
                    "errors": [],
                },
            )

    runner = BrokeredSynthesisRunner(
        config=config,
        broker=_FakeBroker(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )
    assert runner.data_visibility_domain == "virtual_usvfs"

    result = await runner.run_pipeline(["PatcherA"])

    assert result.success is True
    assert result.output_esp == expected_esp
    assert result.output_esps == (expected_esp,)
    assert result.patchers_executed == ["PatcherA"]
    assert runner.last_vfs_result is not None
    job = captured_job["job"]
    assert job.tool_id == "synthesis"
    assert job.payload["expected_plugins"] == ["Skyrim.esm", "MiModVirtual.esp"]
    assert job.mutation_targets == (rutas["output"].resolve(),)


async def test_brokered_runner_propaga_terminality_unknown_en_timeout(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    config = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        timeout_seconds=15,
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
    )

    class _TimeoutBroker:
        async def submit(self, _job: VfsJob, **_kwargs: Any) -> VfsJobResult:
            exc = VfsJobTimeoutError("timeout bajo USVFS")
            exc.terminality_unknown = True  # type: ignore[attr-defined]
            exc.teardown_error = RuntimeError("worker sigue vivo")  # type: ignore[attr-defined]
            raise exc

    runner = BrokeredSynthesisRunner(
        config=config,
        broker=_TimeoutBroker(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )

    with pytest.raises(SynthesisTimeoutError) as exc_info:
        await runner.run_pipeline(["PatcherA"])

    assert exception_forbids_rollback(exc_info.value) is True


@pytest.mark.parametrize(
    ("failure_kind", "exc_cls"),
    [
        ("timeout", SynthesisTimeoutError),
        ("not_found", SynthesisNotFoundError),
        ("execution_error", SynthesisExecutionError),
    ],
)
async def test_brokered_runner_traduce_fallos_tipados_del_worker(
    tmp_path: pathlib.Path,
    failure_kind: str,
    exc_cls: type[Exception],
) -> None:
    rutas = _escenario_mo2(tmp_path)
    config = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
    )

    class _FailingBroker:
        async def submit(self, job: VfsJob, **_kwargs: Any) -> VfsJobResult:
            return VfsJobResult(
                protocol_version=VFS_PROTOCOL_VERSION,
                job_id=job.job_id,
                success=False,
                message="fallo tipado en worker",
                exit_code=-1,
                stdout="",
                stderr="",
                outputs=(),
                rollback_state="not_required",
                attestation={},
                tool_result={"synthesis_failure_kind": failure_kind},
            )

    runner = BrokeredSynthesisRunner(
        config=config,
        broker=_FailingBroker(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )

    with pytest.raises(exc_cls):
        await runner.run_pipeline(["PatcherA"])


async def test_synthesis_service_en_dominio_virtual_no_bloquea_por_data_fisico(tmp_path: pathlib.Path) -> None:
    """En dominio virtual_usvfs, el sensor físico NO aplica; standalone conserva U-01 rojo."""
    rutas = _escenario_mo2(tmp_path)
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = rutas["game"]
    resolver.get_skyrim_path_raw.return_value = rutas["game"]
    resolver.get_mo2_path.return_value = rutas["mo2"]
    resolver.get_mo2_instance_data_root.return_value = rutas["mo2"]
    resolver.get_mo2_mods_path.return_value = rutas["mods"]
    resolver.get_mo2_mods_path_best_effort.return_value = rutas["mods"]
    resolver.get_mo2_mods_path_para_destino.return_value = rutas["mods"]
    resolver.get_active_profile.return_value = "Default"
    resolver.get_synthesis_exe.return_value = rutas["exe"]

    svc_virtual = SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
        vfs_broker=MagicMock(),
        instance_id="portable-main",
    )
    reporte = await svc_virtual._ensure_preflight().run()
    visibilidad = next((c for c in reporte.checks if c.name == "vfs_visibility"), None)
    assert visibilidad is None
    assert reporte.blocks_mutations is False

    # Contracara standalone: con el mismo Data físico pelado DEBE bloquear en ROJO.
    svc_fisico = SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
    )
    reporte_fisico = await svc_fisico._ensure_preflight().run()
    visibilidad_fisica = next((c for c in reporte_fisico.checks if c.name == "vfs_visibility"), None)
    assert visibilidad_fisica is not None
    assert visibilidad_fisica.status is PreflightStatus.RED
