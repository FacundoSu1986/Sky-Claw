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

import ast
import json
import os
import pathlib
import struct
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.rollback_veto import exception_forbids_rollback
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.mo2 import brokered_synthesis
from sky_claw.local.mo2.brokered_synthesis import BrokeredSynthesisRunner
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import VfsBridgeLostTeardownError, VfsJobTimeoutError, VfsTeardownError
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
    SynthesisRunner,
    SynthesisTimeoutError,
)
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
from sky_claw.local.validators.preflight import PreflightStatus
from tests._symlink_guard import crear_junction


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
    """Con attestation USVFS válida, un plugin activo ausente en Data virtual bloquea antes de lanzar CLI."""
    rutas = _escenario_mo2(tmp_path)
    rutas["plugins"].write_text("*Skyrim.esm\n*MiModVirtual.esp\n*Huerfano.esp\n", encoding="utf-8")
    manifest = _manifiesto_synthesis(
        rutas,
        payload_overrides={"expected_plugins": ["Skyrim.esm", "MiModVirtual.esp", "Huerfano.esp"]},
    )
    run_capture_mock = AsyncMock()

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", run_capture_mock):
        result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert "Huerfano.esp" in result.message
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
        block_building_within_mo2=True,
        require_patcher_receipts=True,
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
        block_building_within_mo2=True,
        require_patcher_receipts=True,
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
        block_building_within_mo2=True,
        require_patcher_receipts=True,
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
        block_building_within_mo2=True,
        require_patcher_receipts=True,
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

    with pytest.raises(exc_cls) as exc_info:
        await runner.run_pipeline(["PatcherA"])
    assert type(exc_info.value) is exc_cls


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
        synthesis_backend="brokered",
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


def _resolver_para_servicio(rutas: dict[str, pathlib.Path]) -> MagicMock:
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
    return resolver


async def test_rerun_brokered_excluye_outputs_propios_de_expected_plugins_sin_deadlock(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0 #1: *Synthesis.esp y *Synthesis_2.esp en plugins.txt no bloquean reruns brokered tras unlink()."""
    rutas = _escenario_mo2(tmp_path)
    (rutas["plugins"]).write_text(
        "*Skyrim.esm\n*MiModVirtual.esp\n*Synthesis.esp\n*Synthesis_2.esp\n",
        encoding="utf-8",
    )
    overwrite_dir = rutas["mo2"] / "overwrite"
    overwrite_dir.mkdir(exist_ok=True)
    prev_esp = overwrite_dir / "Synthesis.esp"
    prev_esp.write_bytes(_construir_tes4(["Skyrim.esm", "MiModVirtual.esp"]))

    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)

    lock_mgr = DistributedLockManager(tmp_path / "locks.db", default_ttl=5.0, max_retries=1)
    await lock_mgr.initialize()
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    snap_mgr = FileSnapshotManager(snapshot_dir=snap_dir)
    await snap_mgr.initialize()
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=11)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    event_bus = CoreEventBus()
    await event_bus.start()
    captured_jobs: list[VfsJob] = []

    class _WorkerBackedBroker:
        async def submit(self, job: VfsJob, **kwargs: Any) -> VfsJobResult:
            captured_jobs.append(job)
            manifest = VfsWorkerManifest(
                protocol_version=VFS_PROTOCOL_VERSION,
                job=job,
                challenge=kwargs["challenge"],
                mo2_root=rutas["mo2"],
                mods_dir=rutas["mods"],
                virtual_data_dir=rutas["data_virtual"],
                descriptor_path=rutas["mo2"] / "descriptor.json",
            )

            async def _fake_cli(_args: list[str], **_kw: Any) -> tuple[bytes, bytes, int]:
                assert not prev_esp.exists()
                prev_esp.write_bytes(_construir_tes4(["Skyrim.esm", "MiModVirtual.esp"]))
                return (
                    b"[PatcherA] Starting run.\n[PatcherA] Run successful.\nExported patch to final destination: out\n",
                    b"",
                    0,
                )

            with patch("sky_claw.local.tools.synthesis_runner.run_capture", _fake_cli):
                return await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    try:
        svc = SynthesisPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=journal,
            path_resolver=_resolver_para_servicio(rutas),
            event_bus=event_bus,
            synthesis_settings_path=rutas["settings"],
            vfs_broker=_WorkerBackedBroker(),
            instance_id="portable-main",
            synthesis_backend="brokered",
            preparador_fase1=_PreparadorFalso(rutas["mo2"] / "fase1"),
        )
        out = await svc.execute_pipeline(patcher_ids=["PatcherA"])
        assert out["success"] is True, out
        assert captured_jobs[0].payload["expected_plugins"] == ["Skyrim.esm", "MiModVirtual.esp"]
        # PR-3h: el mapa de la Fase 1 viajó y el worker lo aceptó (la corrida terminó bien con el shim).
        assert set(captured_jobs[0].payload["cli_executables"]) == {"id-patcher-a"}
        # PR-3j: corrida terminada bien → se libera exactamente el mapa que viajó.
        liberadas = svc._preparador_fase1.liberadas  # type: ignore[union-attr]
        assert [{k: str(v) for k, v in m.items()} for m in liberadas] == [captured_jobs[0].payload["cli_executables"]]
        journal.commit_transaction.assert_awaited_once_with(11)
        journal.mark_transaction_rolled_back.assert_not_awaited()
    finally:
        await event_bus.stop()
        await lock_mgr.close()


async def test_synthesis_service_respeta_rollback_veto_ante_timeout_y_teardown_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0 #2: terminalidad desconocida (timeout o VfsTeardownError) veta unlink y mark_transaction_rolled_back."""
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)

    async def _correr_con_excepcion(subdir: str, exc_factory: Any, *, pass_output_override: bool) -> None:
        base = tmp_path / subdir
        base.mkdir()
        rutas = _escenario_mo2(base)
        overwrite_dir = rutas["mo2"] / "overwrite"
        overwrite_dir.mkdir(exist_ok=True)
        target_dir = rutas["output"] if pass_output_override else overwrite_dir
        split_parcial = target_dir / "Synthesis_2.esp"

        lock_mgr = DistributedLockManager(base / "locks.db", default_ttl=5.0, max_retries=1)
        await lock_mgr.initialize()
        snap_dir = base / "snapshots"
        snap_dir.mkdir()
        snap_mgr = FileSnapshotManager(snapshot_dir=snap_dir)
        await snap_mgr.initialize()
        journal = AsyncMock()
        journal.begin_transaction = AsyncMock(return_value=42)
        journal.commit_transaction = AsyncMock()
        journal.mark_transaction_rolled_back = AsyncMock()
        event_bus = CoreEventBus()
        await event_bus.start()
        completed_events: list[dict[str, Any]] = []

        async def _on_completed(evt: Any) -> None:
            completed_events.append(dict(evt.payload))

        event_bus.subscribe("synthesis.pipeline.completed", _on_completed)

        class _OrphanedWorkerBroker:
            async def submit(self, _job: VfsJob, **_kwargs: Any) -> VfsJobResult:
                split_parcial.write_bytes(_construir_tes4(["Skyrim.esm"]))
                raise exc_factory()

        try:
            svc = SynthesisPipelineService(
                lock_manager=lock_mgr,
                snapshot_manager=snap_mgr,
                journal=journal,
                path_resolver=_resolver_para_servicio(rutas),
                event_bus=event_bus,
                synthesis_settings_path=rutas["settings"],
                output_path=rutas["output"] if pass_output_override else None,
                vfs_broker=_OrphanedWorkerBroker(),
                instance_id="portable-main",
                synthesis_backend="brokered",
                preparador_fase1=_PreparadorFalso(rutas["mo2"] / "fase1"),
            )
            out = await svc.execute_pipeline(patcher_ids=["PatcherA"])
            await event_bus.stop()

            assert out["success"] is False
            assert split_parcial.exists() is True
            # PR-3j: con terminalidad desconocida el worker podría seguir usando los shims: no se liberan.
            assert svc._preparador_fase1.liberadas == []  # type: ignore[union-attr]
            journal.commit_transaction.assert_not_awaited()
            journal.mark_transaction_rolled_back.assert_not_awaited()
            assert completed_events and completed_events[0]["rolled_back"] is False
        finally:
            await event_bus.stop()
            await lock_mgr.close()

    def _timeout_unknown() -> VfsJobTimeoutError:
        exc = VfsJobTimeoutError("timeout con worker vivo")
        exc.terminality_unknown = True  # type: ignore[attr-defined]
        exc.teardown_error = RuntimeError("worker sigue vivo")  # type: ignore[attr-defined]
        return exc

    await _correr_con_excepcion("timeout_live", _timeout_unknown, pass_output_override=False)
    await _correr_con_excepcion(
        "bridge_lost",
        lambda: VfsBridgeLostTeardownError("bridge desconectado con worker vivo"),
        pass_output_override=True,
    )
    await _correr_con_excepcion(
        "teardown_err",
        lambda: VfsTeardownError("teardown fallido"),
        pass_output_override=False,
    )


async def test_e3r3_backend_explicito_y_wiring_security_hold(tmp_path: pathlib.Path) -> None:
    """P0 #3: E3-R3 exige selector explícito synthesis_backend + WIRING_SECURITY_HOLD activo por defecto."""
    assert brokered_synthesis._CONTENCION_SYNTHESIS_VERIFICADA is False

    rutas = _escenario_mo2(tmp_path)
    resolver = _resolver_para_servicio(rutas)
    broker_mock = MagicMock()
    broker_mock.submit = AsyncMock()

    # 1) broker presente en memoria sin pedir backend="brokered" -> sigue standalone físico (U-01 intacto).
    svc_default = SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=resolver,
        event_bus=MagicMock(),
        synthesis_settings_path=rutas["settings"],
        vfs_broker=broker_mock,
        instance_id="portable-main",
    )
    assert svc_default._data_visibility_domain == "physical"
    runner_default = svc_default._ensure_synthesis_runner()
    assert isinstance(runner_default, SynthesisRunner)
    assert getattr(runner_default, "data_visibility_domain", "physical") == "physical"

    # 2) backend="brokered" con HOLD activo -> guard fail-closed sin tocar broker ni lanzar CLI.
    lock_mgr = DistributedLockManager(tmp_path / "locks_hold.db", default_ttl=5.0, max_retries=1)
    await lock_mgr.initialize()
    snap_dir = tmp_path / "snaps_hold"
    snap_dir.mkdir()
    snap_mgr = FileSnapshotManager(snapshot_dir=snap_dir)
    await snap_mgr.initialize()
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=7)
    journal.mark_transaction_rolled_back = AsyncMock()
    try:
        svc_hold = SynthesisPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=journal,
            path_resolver=resolver,
            event_bus=CoreEventBus(),
            synthesis_settings_path=rutas["settings"],
            vfs_broker=broker_mock,
            instance_id="portable-main",
            synthesis_backend="brokered",
        )
        guard_runner = svc_hold._ensure_synthesis_runner()
        assert guard_runner.data_visibility_domain == "virtual_usvfs"
        assert "WIRING_SECURITY_HOLD" in getattr(guard_runner, "reason", "")
        with patch("sky_claw.local.tools.synthesis_runner.run_capture", AsyncMock()) as run_cap:
            res_hold = await svc_hold.execute_pipeline(patcher_ids=["PatcherA"])
        assert res_hold["success"] is False
        assert "WIRING_SECURITY_HOLD" in res_hold["message"]
        broker_mock.submit.assert_not_awaited()
        run_cap.assert_not_awaited()

        # 3) backend desconocido -> guard fail-closed sin caer a standalone.
        svc_typo = SynthesisPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=journal,
            path_resolver=resolver,
            event_bus=CoreEventBus(),
            synthesis_settings_path=rutas["settings"],
            synthesis_backend="brokerd",
        )
        guard_typo = svc_typo._ensure_synthesis_runner()
        assert guard_typo.data_visibility_domain == "virtual_usvfs"
        with pytest.raises(SynthesisExecutionError, match="no es válido"):
            await guard_typo.run_pipeline(["PatcherA"])
    finally:
        await lock_mgr.close()


async def test_worker_exige_expected_plugins_politica_fase2_y_confina_rutas_sin_enlaces(
    tmp_path: pathlib.Path,
) -> None:
    """P1s: payload exige expected_plugins, policy Fase 2 estricta, PathValidator y links sin pre-resolver."""
    rutas = _escenario_mo2(tmp_path)

    # 1) expected_plugins omitido -> rechazo en worker.
    manifest_sin_expected = _manifiesto_synthesis(rutas)
    payload_sin_expected = dict(manifest_sin_expected.job.payload)
    payload_sin_expected.pop("expected_plugins")
    job_sin_expected = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="synthesis",
        payload=payload_sin_expected,
        timeout_seconds=60.0,
        expected_fingerprint=manifest_sin_expected.challenge.profile_fingerprint,
        mutation_targets=(rutas["output"],),
    )
    res1 = await execute_worker_manifest(
        VfsWorkerManifest(
            protocol_version=VFS_PROTOCOL_VERSION,
            job=job_sin_expected,
            challenge=manifest_sin_expected.challenge,
            mo2_root=rutas["mo2"],
            mods_dir=rutas["mods"],
            virtual_data_dir=rutas["data_virtual"],
            descriptor_path=rutas["mo2"] / "descriptor.json",
        ),
        grandchild_probe=_probe_ok,
    )
    assert res1.success is False
    assert "expected_plugins" in res1.message

    # 2) expected_plugins forjado sin *Huerfano.esp cuando plugins.txt sí lo activa (attestation pasa;
    #    la compuerta de re-cotejo de plugins.txt en _synthesis_handler debe bloquear antes de lanzar CLI).
    rutas["plugins"].write_text("*Skyrim.esm\n*MiModVirtual.esp\n*Huerfano.esp\n", encoding="utf-8")
    manifest_forjado = _manifiesto_synthesis(
        rutas,
        payload_overrides={"expected_plugins": ["Skyrim.esm", "MiModVirtual.esp"]},
    )
    run_capture_forjado = AsyncMock()
    with patch("sky_claw.local.tools.synthesis_runner.run_capture", run_capture_forjado):
        res2 = await execute_worker_manifest(manifest_forjado, grandchild_probe=_probe_ok)
    assert res2.success is False
    assert "Huerfano.esp" in res2.message
    assert "plugins.txt" in res2.message
    run_capture_forjado.assert_not_awaited()

    # Restaurar plugins.txt para pruebas restantes.
    rutas["plugins"].write_text("*Skyrim.esm\n*MiModVirtual.esp\n", encoding="utf-8")

    # 3) Política Fase 2: block_building_within_mo2=False o require_patcher_receipts=False rechazados.
    for flag_name in ("block_building_within_mo2", "require_patcher_receipts"):
        manifest_bad_flag = _manifiesto_synthesis(rutas, payload_overrides={flag_name: False})
        res_flag = await execute_worker_manifest(manifest_bad_flag, grandchild_probe=_probe_ok)
        assert res_flag.success is False
        assert flag_name in res_flag.message

    # 4) PathValidator en _synthesis_handler: ruta fuera de raíces autorizadas rechazada.
    outside_dir = (
        pathlib.Path("Z:/skyclaw_untrusted_outside") if os.name == "nt" else pathlib.Path("/var/skyclaw_outside")
    )
    manifest_outside = _manifiesto_synthesis(rutas, payload_overrides={"working_dir": str(outside_dir)})
    res_outside = await execute_worker_manifest(manifest_outside, grandchild_probe=_probe_ok)
    assert res_outside.success is False
    assert "working_dir" in res_outside.message or "outside" in res_outside.message.casefold()

    # 5) Proxy y worker no pre-resuelven symlinks ni junctions intermedios antes de validar enlaces.
    real_dir = tmp_path / "real_work"
    real_dir.mkdir()
    link_dir = tmp_path / "link_work"
    expected_kind = "symlink"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            motivo = crear_junction(link_dir, real_dir)
            assert motivo is None, f"no se pudo crear junction en Windows: {motivo}"
            expected_kind = "junction"
        else:
            pytest.skip("El entorno no permite crear symlinks de directorio")
    cfg = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
        working_dir=link_dir / "nested",
        block_building_within_mo2=True,
        require_patcher_receipts=True,
    )
    proxy = BrokeredSynthesisRunner(
        config=cfg,
        broker=MagicMock(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )
    with pytest.raises(SynthesisExecutionError, match=expected_kind):
        proxy._build_job_payload(
            patcher_ids=["PatcherA"],
            expected_plugins=["Skyrim.esm"],
            settings_fingerprint="fp1",
            load_order_fingerprint="fp2",
        )
    manifest_nested_link = _manifiesto_synthesis(rutas, payload_overrides={"working_dir": str(link_dir / "nested")})
    res_nested_link = await execute_worker_manifest(manifest_nested_link, grandchild_probe=_probe_ok)
    assert res_nested_link.success is False
    assert expected_kind in res_nested_link.message


async def test_worker_synthesis_atrapa_patcher_pipeline_error_como_fallo_tipado(tmp_path: pathlib.Path) -> None:
    """Un PatcherPipelineError en settings inválidos devuelve fallo tipado y no crashea el worker."""
    rutas = _escenario_mo2(tmp_path)
    rutas["settings"].write_text("{not-valid-json", encoding="utf-8")
    manifest = _manifiesto_synthesis(rutas)

    result = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)

    assert result.success is False
    assert result.tool_result.get("synthesis_failure_kind") == "execution_error"
    assert "Invalid Synthesis pipeline settings" in result.message


def test_barrera_hold_synthesis_esta_aislada_y_enumerada_por_ast() -> None:
    """La barrera _CONTENCION_SYNTHESIS_VERIFICADA no colisiona con DynDOLOD y tiene usos enumerados."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    barrera = "_CONTENCION_SYNTHESIS_VERIFICADA"

    def _scan(root: pathlib.Path) -> list[tuple[str, str]]:
        usos: list[tuple[str, str]] = []
        for ruta in sorted(root.rglob("*.py")):
            tree = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
            for nodo in ast.walk(tree):
                if isinstance(nodo, ast.Name) and nodo.id == barrera:
                    usos.append((ruta.relative_to(repo).as_posix(), type(nodo.ctx).__name__))
                elif isinstance(nodo, ast.Attribute) and nodo.attr == barrera:
                    usos.append((ruta.relative_to(repo).as_posix(), "Attribute"))
        return usos

    usos_prod = sorted(_scan(repo / "sky_claw"))
    assert usos_prod == [
        ("sky_claw/local/mo2/brokered_synthesis.py", "Load"),
        ("sky_claw/local/mo2/brokered_synthesis.py", "Store"),
        ("sky_claw/local/tools/synthesis_service.py", "Attribute"),
    ]
    archivos_tests = sorted({archivo for archivo, _tipo in _scan(repo / "tests")})
    assert archivos_tests == ["tests/test_brokered_synthesis.py"]

    # Ancla E3-R3: la barrera debe ser un literal booleano ``False``, nunca derivada de env/config.
    mod_path = repo / "sky_claw" / "local" / "mo2" / "brokered_synthesis.py"
    mod_tree = ast.parse(mod_path.read_text(encoding="utf-8"), filename=str(mod_path))
    asignaciones = [
        nodo.value
        for nodo in ast.walk(mod_tree)
        if isinstance(nodo, ast.Assign) and any(isinstance(t, ast.Name) and t.id == barrera for t in nodo.targets)
    ]
    assert len(asignaciones) == 1
    assert isinstance(asignaciones[0], ast.Constant) and asignaciones[0].value is False

    # Ancla E3-R3: congelar la cadena de constructores de BrokeredSynthesisRunner y su resolver
    # incluyendo llamadas a nivel de módulo o cuerpo de clase (no solo dentro de funciones).
    def _llamadores(nombre_fn: str) -> list[tuple[str, str]]:
        llamadores: list[tuple[str, str]] = []

        class _Visitor(ast.NodeVisitor):
            def __init__(self, rel_path: str) -> None:
                self._rel = rel_path
                self._scopes: list[str] = ["<module>"]

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._scopes.append(node.name)
                self.generic_visit(node)
                self._scopes.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._scopes.append(node.name)
                self.generic_visit(node)
                self._scopes.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._scopes.append(f"class:{node.name}")
                self.generic_visit(node)
                self._scopes.pop()

            def visit_Call(self, node: ast.Call) -> None:
                if ast.unparse(node.func).split(".")[-1] == nombre_fn:
                    llamadores.append((self._rel, self._scopes[-1]))
                self.generic_visit(node)

        for ruta in sorted((repo / "sky_claw").rglob("*.py")):
            rel = ruta.relative_to(repo).as_posix()
            tree = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
            _Visitor(rel).visit(tree)
        return sorted(llamadores)

    assert _llamadores("BrokeredSynthesisRunner") == [
        ("sky_claw/local/mo2/brokered_synthesis.py", "resolve_synthesis_backend_runner"),
    ]
    assert _llamadores("resolve_synthesis_backend_runner") == [
        ("sky_claw/local/tools/synthesis_service.py", "_ensure_synthesis_runner"),
        ("sky_claw/local/tools/synthesis_service.py", "_ensure_synthesis_runner"),
    ]


async def test_hold_bloquea_incluso_runner_virtual_inyectado_directamente(tmp_path: pathlib.Path) -> None:
    """P1-1(c): inyectar BrokeredSynthesisRunner por parámetro NO saltea WIRING_SECURITY_HOLD."""
    assert brokered_synthesis._CONTENCION_SYNTHESIS_VERIFICADA is False
    rutas = _escenario_mo2(tmp_path)
    broker_mock = MagicMock()
    broker_mock.submit = AsyncMock()
    cfg = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
        block_building_within_mo2=True,
        require_patcher_receipts=True,
    )
    proxy = BrokeredSynthesisRunner(
        config=cfg,
        broker=broker_mock,
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )
    lock_mgr = DistributedLockManager(tmp_path / "locks_inj.db", default_ttl=5.0, max_retries=1)
    await lock_mgr.initialize()
    snap_dir = tmp_path / "snaps_inj"
    snap_dir.mkdir()
    snap_mgr = FileSnapshotManager(snapshot_dir=snap_dir)
    await snap_mgr.initialize()
    try:
        svc = SynthesisPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=AsyncMock(),
            path_resolver=_resolver_para_servicio(rutas),
            event_bus=MagicMock(),
            synthesis_settings_path=rutas["settings"],
            synthesis_runner=proxy,
        )
        res = await svc.execute_pipeline(patcher_ids=["PatcherA"])
        assert res["success"] is False
        assert "WIRING_SECURITY_HOLD" in res["message"]
        broker_mock.submit.assert_not_awaited()
    finally:
        await lock_mgr.close()


async def test_synthesis_service_hace_rollback_normal_ante_timeout_limpio_sin_veto(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-1: un VfsJobTimeoutError limpio (teardown confirmado, sin veto) limpia salidas y marca rolled_back."""
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    overwrite_dir = rutas["mo2"] / "overwrite"
    overwrite_dir.mkdir(exist_ok=True)
    split_parcial = overwrite_dir / "Synthesis_2.esp"

    lock_mgr = DistributedLockManager(tmp_path / "locks_clean_to.db", default_ttl=5.0, max_retries=1)
    await lock_mgr.initialize()
    snap_dir = tmp_path / "snaps_clean_to"
    snap_dir.mkdir()
    snap_mgr = FileSnapshotManager(snapshot_dir=snap_dir)
    await snap_mgr.initialize()
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=42)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    event_bus = CoreEventBus()
    await event_bus.start()
    completed_events: list[dict[str, Any]] = []

    async def _on_completed(evt: Any) -> None:
        completed_events.append(dict(evt.payload))

    event_bus.subscribe("synthesis.pipeline.completed", _on_completed)

    class _CleanTimeoutBroker:
        async def submit(self, _job: VfsJob, **_kwargs: Any) -> VfsJobResult:
            split_parcial.write_bytes(_construir_tes4(["Skyrim.esm"]))
            raise VfsJobTimeoutError("timeout limpio con worker terminado")

    try:
        svc = SynthesisPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=journal,
            path_resolver=_resolver_para_servicio(rutas),
            event_bus=event_bus,
            synthesis_settings_path=rutas["settings"],
            vfs_broker=_CleanTimeoutBroker(),
            instance_id="portable-main",
            synthesis_backend="brokered",
            preparador_fase1=_PreparadorFalso(rutas["mo2"] / "fase1"),
        )
        out = await svc.execute_pipeline(patcher_ids=["PatcherA"])
        await event_bus.stop()

        assert out["success"] is False
        assert split_parcial.exists() is False
        # PR-3j: teardown confirmado (sin veto): los shims de la corrida se liberan.
        assert len(svc._preparador_fase1.liberadas) == 1  # type: ignore[union-attr]
        journal.commit_transaction.assert_not_awaited()
        journal.mark_transaction_rolled_back.assert_awaited_once_with(42)
        assert completed_events and completed_events[0]["rolled_back"] is True
    finally:
        await event_bus.stop()
        await lock_mgr.close()


async def test_brokered_runner_limpia_last_vfs_result_antes_de_nueva_corrida(tmp_path: pathlib.Path) -> None:
    """Una segunda corrida que falla por timeout no conserva el last_vfs_result de la corrida previa."""
    rutas = _escenario_mo2(tmp_path)
    expected_esp = (rutas["output"] / "Synthesis.esp").resolve()
    config = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
        block_building_within_mo2=True,
        require_patcher_receipts=True,
    )
    calls = 0

    class _OkLuegoTimeoutBroker:
        async def submit(self, job: VfsJob, **_kwargs: Any) -> VfsJobResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                return VfsJobResult(
                    protocol_version=VFS_PROTOCOL_VERSION,
                    job_id=job.job_id,
                    success=True,
                    message="",
                    exit_code=0,
                    stdout="ok",
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
            raise VfsJobTimeoutError("timeout en segunda corrida")

    runner = BrokeredSynthesisRunner(
        config=config,
        broker=_OkLuegoTimeoutBroker(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )
    await runner.run_pipeline(["PatcherA"])
    assert runner.last_vfs_result is not None

    with pytest.raises(SynthesisTimeoutError):
        await runner.run_pipeline(["PatcherA"])
    assert runner.last_vfs_result is None


@pytest.mark.parametrize(
    ("block_building", "require_receipts", "expected_fragment"),
    [
        (False, True, "block_building_within_mo2"),
        (True, False, "require_patcher_receipts"),
    ],
)
def test_brokered_runner_rechaza_flags_fase2_desactivados(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    block_building: bool,
    require_receipts: bool,
    expected_fragment: str,
) -> None:
    """BrokeredSynthesisRunner y su resolver rechazan temprano flags de Fase 2 en False."""
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    cfg = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
        block_building_within_mo2=block_building,
        require_patcher_receipts=require_receipts,
    )
    with pytest.raises(SynthesisExecutionError, match=expected_fragment):
        BrokeredSynthesisRunner(
            config=cfg,
            broker=MagicMock(),
            instance_id="portable-main",
            data_root=rutas["mo2"],
            mods_dir=rutas["mods"],
        )
    guard = brokered_synthesis.resolve_synthesis_backend_runner(
        backend="brokered",
        config=cfg,
        broker=MagicMock(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )
    assert isinstance(guard, brokered_synthesis.VfsRequiredSynthesisRunner)
    assert expected_fragment in guard._reason


async def test_worker_synthesis_rechaza_expected_plugins_none_y_persistence_mode_no_hashable(
    tmp_path: pathlib.Path,
) -> None:
    """El handler Synthesis falla cerrado ante expected_plugins=None o persistence_mode=[]."""
    rutas = _escenario_mo2(tmp_path)

    res_none = await execute_worker_manifest(
        _manifiesto_synthesis(rutas, payload_overrides={"expected_plugins": None}),
        grandchild_probe=_probe_ok,
    )
    assert res_none.success is False
    assert "expected_plugins" in res_none.message

    res_list = await execute_worker_manifest(
        _manifiesto_synthesis(rutas, payload_overrides={"persistence_mode": []}),
        grandchild_probe=_probe_ok,
    )
    assert res_list.success is False
    assert "persistence_mode" in res_list.message


async def test_worker_synthesis_usa_lower_ntfs_sin_colision_casefold(tmp_path: pathlib.Path) -> None:
    """La visibilidad usa .lower() (NTFS) y no confunde Straße.esp con Strasse.esp."""
    rutas = _escenario_mo2(tmp_path)
    rutas["plugins"].write_text("*Skyrim.esm\n*Straße.esp\n", encoding="utf-8")
    (rutas["data_virtual"] / "Strasse.esp").write_bytes(b"TES4")

    manifest = _manifiesto_synthesis(
        rutas,
        payload_overrides={"expected_plugins": ["Skyrim.esm", "Straße.esp"]},
    )
    res = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)
    assert res.success is False
    assert "Straße.esp" in res.message


async def test_synthesis_service_rechaza_runner_inyectado_con_output_path_distinto(
    tmp_path: pathlib.Path,
) -> None:
    """Inyectar un runner cuyo output_path difiere del override sandbox del servicio falla cerrado."""
    rutas = _escenario_mo2(tmp_path)
    sandbox_out = tmp_path / "sandbox_overwrite"
    sandbox_out.mkdir()
    real_cfg = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
    )
    injected_runner = SynthesisRunner(real_cfg)

    lock_mgr = DistributedLockManager(tmp_path / "locks_mismatch.db", default_ttl=5.0, max_retries=1)
    await lock_mgr.initialize()
    snap_dir = tmp_path / "snaps_mismatch"
    snap_dir.mkdir()
    snap_mgr = FileSnapshotManager(snapshot_dir=snap_dir)
    await snap_mgr.initialize()
    try:
        svc = SynthesisPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=AsyncMock(),
            path_resolver=_resolver_para_servicio(rutas),
            event_bus=MagicMock(),
            output_path=sandbox_out,
            synthesis_settings_path=rutas["settings"],
            synthesis_runner=injected_runner,
        )
        res = await svc.execute_pipeline(patcher_ids=["PatcherA"])
        assert res["success"] is False
        assert "output_path" in res["message"]
    finally:
        await lock_mgr.close()


# --- PR-3g (ADR 0019): el mapa de shims de la Fase 1 viaja al worker y el worker lo valida -----------


def _shim_falso(raiz: pathlib.Path, nombre_dir: str = "00-patchera") -> pathlib.Path:
    """Arma la forma de un shim materializado (exe + shim.json) sin compilar nada."""
    destino = raiz / nombre_dir
    destino.mkdir(parents=True)
    exe = destino / "SkyClawSynthesisShim.exe"
    exe.write_bytes(b"MZ")
    (destino / "shim.json").write_text("{}", encoding="utf-8")
    return exe


def _proxy_con_mapa(rutas: dict[str, pathlib.Path], mapa: dict[str, pathlib.Path]) -> BrokeredSynthesisRunner:
    cfg = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
        block_building_within_mo2=True,
        require_patcher_receipts=True,
        cli_executables=mapa,
    )
    return BrokeredSynthesisRunner(
        config=cfg,
        broker=MagicMock(),
        instance_id="portable-main",
        data_root=rutas["mo2"],
        mods_dir=rutas["mods"],
    )


def test_runner_brokered_transporta_el_mapa_de_shims(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    shim = _shim_falso(rutas["mo2"] / "shims")
    payload = _proxy_con_mapa(rutas, {"id-patcher-a": shim})._build_job_payload(
        patcher_ids=["PatcherA"], expected_plugins=["Skyrim.esm"], settings_fingerprint="a", load_order_fingerprint="b"
    )
    assert payload["cli_executables"] == {"id-patcher-a": str(shim)}


def test_runner_brokered_sin_mapa_no_manda_la_clave(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    cfg = SynthesisConfig(
        game_path=rutas["game"],
        mo2_path=rutas["mo2"],
        output_path=rutas["output"],
        synthesis_exe=rutas["exe"],
        profile="Default",
        pipeline_settings_path=rutas["settings"],
        data_folder_path=rutas["data_fisico"],
        load_order_file_path=rutas["plugins"],
        block_building_within_mo2=True,
        require_patcher_receipts=True,
    )
    proxy = BrokeredSynthesisRunner(
        config=cfg, broker=MagicMock(), instance_id="portable-main", data_root=rutas["mo2"], mods_dir=rutas["mods"]
    )
    payload = proxy._build_job_payload(
        patcher_ids=["PatcherA"], expected_plugins=["Skyrim.esm"], settings_fingerprint="a", load_order_fingerprint="b"
    )
    assert "cli_executables" not in payload


def test_runner_brokered_rechaza_mapa_vacio_o_shim_detras_de_un_enlace(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    with pytest.raises(SynthesisExecutionError, match="cli_executables"):
        _proxy_con_mapa(rutas, {})._build_job_payload(
            patcher_ids=["PatcherA"], expected_plugins=[], settings_fingerprint="a", load_order_fingerprint="b"
        )
    real = rutas["mo2"] / "shims_reales"
    shim_real = _shim_falso(real)
    enlace = rutas["mo2"] / "shims_enlace"
    try:
        enlace.symlink_to(real, target_is_directory=True)
        tipo = "symlink"
    except OSError:
        if os.name != "nt":
            pytest.skip("El entorno no permite crear symlinks de directorio")
        motivo = crear_junction(enlace, real)
        assert motivo is None, f"no se pudo crear junction en Windows: {motivo}"
        tipo = "junction"
    with pytest.raises(SynthesisExecutionError, match=tipo):
        _proxy_con_mapa(rutas, {"id-patcher-a": enlace / shim_real.parent.name / shim_real.name})._build_job_payload(
            patcher_ids=["PatcherA"], expected_plugins=[], settings_fingerprint="a", load_order_fingerprint="b"
        )


async def test_worker_synthesis_pasa_el_mapa_de_shims_al_traductor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.tools import synthesis_runner as sr

    rutas = _escenario_mo2(tmp_path)
    shim = _shim_falso(rutas["mo2"] / "shims")
    capturado: dict[str, Any] = {}

    def _espia(*_args: Any, **kwargs: Any) -> Any:
        capturado.update(kwargs)
        raise sr.PatcherPipelineError("corte del espía")

    monkeypatch.setattr(sr.PatcherPipeline, "prepare_synthesis_cli_settings", _espia)
    manifest = _manifiesto_synthesis(rutas, payload_overrides={"cli_executables": {"id-patcher-a": str(shim)}})
    run_capture_mock = AsyncMock()
    with patch("sky_claw.local.tools.synthesis_runner.run_capture", run_capture_mock):
        res = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)
    assert res.success is False
    assert "corte del espía" in res.message
    assert capturado["cli_executables"] == {"id-patcher-a": shim.resolve()}
    run_capture_mock.assert_not_awaited()


def _caso_mapa_invalido(rutas: dict[str, pathlib.Path], caso: str) -> object:
    shims = rutas["mo2"] / "shims"
    if caso == "no_es_dict":
        return [str(_shim_falso(shims))]
    if caso == "vacio":
        return {}
    if caso == "valor_no_str":
        return {"id-patcher-a": 7}
    if caso == "clave_vacia":
        return {"  ": str(_shim_falso(shims))}
    if caso == "ruta_relativa":
        return {"id-patcher-a": "shims/00-patchera/SkyClawSynthesisShim.exe"}
    if caso == "nombre_distinto":
        otro = shims / "00-otro"
        otro.mkdir(parents=True)
        (otro / "patcher.exe").write_bytes(b"MZ")
        (otro / "shim.json").write_text("{}", encoding="utf-8")
        return {"id-patcher-a": str(otro / "patcher.exe")}
    if caso == "inexistente":
        return {"id-patcher-a": str(shims / "00-nada" / "SkyClawSynthesisShim.exe")}
    if caso == "sin_shim_json":
        exe = _shim_falso(shims)
        (exe.parent / "shim.json").unlink()
        return {"id-patcher-a": str(exe)}
    if caso == "fuera_de_raices":
        afuera = (
            pathlib.Path("Z:/skyclaw_untrusted_outside") if os.name == "nt" else pathlib.Path("/var/skyclaw_outside")
        )
        return {"id-patcher-a": str(afuera / "SkyClawSynthesisShim.exe")}
    raise AssertionError(caso)


@pytest.mark.parametrize(
    "caso",
    [
        "no_es_dict",
        "vacio",
        "valor_no_str",
        "clave_vacia",
        "ruta_relativa",
        "nombre_distinto",
        "inexistente",
        "sin_shim_json",
        "fuera_de_raices",
    ],
)
async def test_worker_synthesis_rechaza_mapa_de_shims_invalido_antes_de_lanzar(
    tmp_path: pathlib.Path, caso: str
) -> None:
    rutas = _escenario_mo2(tmp_path)
    manifest = _manifiesto_synthesis(rutas, payload_overrides={"cli_executables": _caso_mapa_invalido(rutas, caso)})
    run_capture_mock = AsyncMock()
    with patch("sky_claw.local.tools.synthesis_runner.run_capture", run_capture_mock):
        res = await execute_worker_manifest(manifest, grandchild_probe=_probe_ok)
    assert res.success is False
    assert "cli_executables" in res.message
    run_capture_mock.assert_not_awaited()


# --- PR-3h (ADR 0019): el servicio prepara la Fase 1 antes de cada corrida brokered -----------------


class _PreparadorFalso:
    """Deja un shim con forma válida bajo *raiz* (una raíz autorizada del worker) sin compilar nada."""

    def __init__(self, raiz: pathlib.Path, *, error: Exception | None = None) -> None:
        self._raiz = raiz
        self._error = error
        self.llamadas: list[tuple[pathlib.Path, str, list[str]]] = []
        self.liberadas: list[dict[str, pathlib.Path]] = []

    async def liberar(self, ejecutables: Any) -> None:
        self.liberadas.append(dict(ejecutables))

    async def preparar(
        self, settings_path: pathlib.Path, profile_identifier: str, patcher_ids: list[str]
    ) -> dict[str, pathlib.Path]:
        self.llamadas.append((settings_path, profile_identifier, list(patcher_ids)))
        if self._error is not None:
            raise self._error
        return {"id-patcher-a": _shim_falso(self._raiz, f"corrida-{len(self.llamadas)}")}


def _servicio_brokered(
    rutas: dict[str, pathlib.Path], broker: Any, preparador: Any, **kw: Any
) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=kw.pop("lock_manager", MagicMock()),
        snapshot_manager=kw.pop("snapshot_manager", MagicMock()),
        journal=kw.pop("journal", AsyncMock()),
        path_resolver=_resolver_para_servicio(rutas),
        event_bus=kw.pop("event_bus", MagicMock(publish=AsyncMock())),
        synthesis_settings_path=rutas["settings"],
        vfs_broker=broker,
        instance_id="portable-main",
        synthesis_backend="brokered",
        preparador_fase1=preparador,
        **kw,
    )


async def test_servicio_brokered_sin_preparador_de_fase1_falla_cerrado_sin_despachar(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    broker = MagicMock(submit=AsyncMock())
    journal = AsyncMock()
    out = await _servicio_brokered(rutas, broker, None, journal=journal).execute_pipeline(patcher_ids=["PatcherA"])
    assert out["success"] is False
    assert "Fase 1" in out["message"]
    broker.submit.assert_not_awaited()
    journal.begin_transaction.assert_not_awaited()


async def test_servicio_brokered_corta_si_la_fase1_falla_sin_abrir_transaccion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.tools.synthesis_fase1 import Fase1Error

    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    broker = MagicMock(submit=AsyncMock())
    journal = AsyncMock()
    preparador = _PreparadorFalso(rutas["mo2"] / "fase1", error=Fase1Error("Patcher 'x' (git): HITL denegado"))
    out = await _servicio_brokered(rutas, broker, preparador, journal=journal).execute_pipeline(
        patcher_ids=["PatcherA"]
    )
    assert out["success"] is False
    assert "HITL denegado" in out["message"]
    assert preparador.llamadas == [(rutas["settings"], "Default", ["PatcherA"])]
    broker.submit.assert_not_awaited()
    journal.begin_transaction.assert_not_awaited()


async def test_servicio_standalone_no_llama_a_la_fase1_ni_pasa_el_mapa(tmp_path: pathlib.Path) -> None:
    rutas = _escenario_mo2(tmp_path)
    preparador = _PreparadorFalso(rutas["mo2"] / "fase1")
    runner = MagicMock(spec=SynthesisRunner)
    runner._config = MagicMock(output_path=rutas["output"])
    runner.data_visibility_domain = "physical"
    runner.settings_fingerprint.return_value = "fp"
    runner.load_order_fingerprint.return_value = "lo"
    runner.inspect_settings.return_value = MagicMock(group_names=(), patcher_ids=("PatcherA",))
    svc = SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=AsyncMock(),
        path_resolver=_resolver_para_servicio(rutas),
        event_bus=MagicMock(),
        synthesis_runner=runner,
        preflight=MagicMock(run=AsyncMock(return_value=MagicMock(checks=(), blocks_mutations=False))),
        preparador_fase1=preparador,
    )
    with (
        patch.object(SynthesisPipelineService, "_build_master_overflow_check", return_value=MagicMock()),
        patch.object(SynthesisPipelineService, "_build_synthesis_visibility_check", return_value=MagicMock()),
        patch.object(SynthesisPipelineService, "_append_preflight_check", side_effect=lambda r, c: r),
    ):
        out = await svc.execute_pipeline(patcher_ids=["PatcherA"])
    # Sin grupos de salida corta antes del run; lo que importa es que la Fase 1 no se tocó.
    assert out["success"] is False
    assert preparador.llamadas == []
    runner.run_pipeline.assert_not_called()


def test_todas_las_run_pipeline_aceptan_el_mapa_de_la_corrida() -> None:
    """Ancla enumerante: toda clase con ``run_pipeline`` en los runners de Synthesis acepta el mapa."""
    import inspect

    from sky_claw.local.tools import synthesis_runner as sr

    clases = sorted(
        (
            obj
            for modulo in (sr, brokered_synthesis)
            for _, obj in inspect.getmembers(modulo, inspect.isclass)
            if obj.__module__ == modulo.__name__ and "run_pipeline" in vars(obj)
        ),
        key=lambda c: c.__qualname__,
    )
    assert [c.__qualname__ for c in clases] == [
        "BrokeredSynthesisRunner",
        "SynthesisRunner",
        "VfsRequiredSynthesisRunner",
    ]
    for clase in clases:
        parametro = inspect.signature(clase.run_pipeline).parameters.get("cli_executables")
        assert parametro is not None, clase.__qualname__
        assert parametro.kind is inspect.Parameter.KEYWORD_ONLY and parametro.default is None


async def test_preparador_fase1_estrena_carpeta_de_shims_bajo_su_raiz(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.tools import synthesis_fase1 as f1

    capturado: dict[str, Any] = {}

    async def _falso(*args: Any, **kwargs: Any) -> f1.ResultadoFase1:
        capturado["args"] = args
        capturado.update(kwargs)
        return f1.ResultadoFase1(ejecutables={"a": kwargs["directorio_shims"] / "00-a" / "SkyClawSynthesisShim.exe"})

    monkeypatch.setattr(f1, "preparar_fase1", _falso)
    hitl = MagicMock()
    prep = f1.PreparadorFase1(
        dotnet=tmp_path / "dotnet.exe",
        git=tmp_path / "git.exe",
        cache_raiz=tmp_path / "cache",
        hitl=hitl,
        raiz_shims=tmp_path / "raiz",
    )
    (tmp_path / "raiz").mkdir()
    mapa1 = await prep.preparar(tmp_path / "s.json", "Default", ["a"])
    dir1 = capturado["directorio_shims"]
    await prep.preparar(tmp_path / "s.json", "Default", ["a"])
    dir2 = capturado["directorio_shims"]
    assert dir1 != dir2
    for d in (dir1, dir2):
        assert d.parent.parent == tmp_path / "raiz"
        assert d.parent.name.startswith("skyclaw-synthesis-shims-")
        assert not d.exists()  # preparar_fase1 exige un directorio nuevo
    assert capturado["args"] == (tmp_path / "s.json", "Default", ["a"])
    assert capturado["hitl"] is hitl and capturado["cache_raiz"] == tmp_path / "cache"
    assert capturado["timeout_s"] == f1.TIMEOUT_FASE1_S
    assert mapa1 == {"a": dir1 / "00-a" / "SkyClawSynthesisShim.exe"}


# --- PR-3j: limpieza de las carpetas de shims por corrida -----------------------------------------


async def test_la_fase1_es_el_ultimo_paso_y_una_salida_temprana_no_la_corre(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Salidas previas sin snapshot ni sandbox cortan ANTES de preparar shims: no queda carpeta huérfana."""
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)
    (rutas["mo2"] / "overwrite").mkdir(exist_ok=True)
    (rutas["mo2"] / "overwrite" / "Synthesis.esp").write_bytes(_construir_tes4(["Skyrim.esm"]))
    preparador = _PreparadorFalso(rutas["mo2"] / "fase1")
    broker = MagicMock(submit=AsyncMock())
    out = await _servicio_brokered(rutas, broker, preparador).execute_pipeline(
        patcher_ids=["PatcherA"], create_snapshot=False
    )
    assert out["success"] is False and "snapshot" in out["message"]
    assert preparador.llamadas == [] and preparador.liberadas == []
    broker.submit.assert_not_awaited()


def _preparador_real(tmp_path: pathlib.Path) -> Any:
    from sky_claw.local.tools.synthesis_fase1 import PreparadorFase1

    raiz = tmp_path / "raiz"
    raiz.mkdir()
    return PreparadorFase1(
        dotnet=tmp_path / "d.exe",
        git=tmp_path / "g.exe",
        cache_raiz=tmp_path / "cache",
        hitl=MagicMock(),
        raiz_shims=raiz,
    )


async def test_preparador_borra_la_corrida_si_la_fase1_falla(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.tools import synthesis_fase1 as f1

    async def _falla(*_a: Any, directorio_shims: pathlib.Path, **_k: Any) -> Any:
        (directorio_shims / "00-a").mkdir(parents=True)
        (directorio_shims / "00-a" / "SkyClawSynthesisShim.exe").write_bytes(b"MZ")
        raise f1.Fase1Error("build roto")

    monkeypatch.setattr(f1, "preparar_fase1", _falla)
    prep = _preparador_real(tmp_path)
    with pytest.raises(f1.Fase1Error, match="build roto"):
        await prep.preparar(tmp_path / "s.json", "Default", ["a"])
    assert list((tmp_path / "raiz").iterdir()) == []


async def test_preparador_libera_solo_corridas_propias(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sky_claw.local.tools import synthesis_fase1 as f1

    async def _ok(*_a: Any, directorio_shims: pathlib.Path, **_k: Any) -> f1.ResultadoFase1:
        exe = directorio_shims / "00-a" / "SkyClawSynthesisShim.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        return f1.ResultadoFase1(ejecutables={"a": exe})

    monkeypatch.setattr(f1, "preparar_fase1", _ok)
    prep = _preparador_real(tmp_path)
    mapa = await prep.preparar(tmp_path / "s.json", "Default", ["a"])
    ajena = tmp_path / "ajena" / "shims" / "00-b"
    ajena.mkdir(parents=True)
    (ajena / "SkyClawSynthesisShim.exe").write_bytes(b"MZ")
    otra_raiz = tmp_path / "raiz2" / "skyclaw-synthesis-shims-x" / "shims" / "00-c"
    otra_raiz.mkdir(parents=True)
    (otra_raiz / "SkyClawSynthesisShim.exe").write_bytes(b"MZ")

    await prep.liberar({**mapa, "b": ajena / "SkyClawSynthesisShim.exe", "c": otra_raiz / "SkyClawSynthesisShim.exe"})
    assert list((tmp_path / "raiz").iterdir()) == []  # la propia se borró
    assert (ajena / "SkyClawSynthesisShim.exe").is_file()  # nombre ajeno: intacta
    assert (otra_raiz / "SkyClawSynthesisShim.exe").is_file()  # fuera de raiz_shims: intacta


async def test_un_fallo_al_liberar_no_cambia_el_resultado_del_run(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La limpieza es best-effort: si liberar() falla, el servicio devuelve lo que decidió el run."""
    monkeypatch.setattr(brokered_synthesis, "_CONTENCION_SYNTHESIS_VERIFICADA", True)
    rutas = _escenario_mo2(tmp_path)

    class _LiberaMal(_PreparadorFalso):
        async def liberar(self, ejecutables: Any) -> None:
            raise OSError("archivo en uso")

    svc = _servicio_brokered(rutas, MagicMock(), _LiberaMal(rutas["mo2"] / "fase1"))
    with patch.object(SynthesisPipelineService, "_liberar_shims", wraps=svc._liberar_shims) as espia:
        out = await svc.execute_pipeline(patcher_ids=["PatcherA"])
    # El broker MagicMock no es awaitable: el run falla por su cuenta; lo que importa es que la
    # limpieza corrió, falló y no tapó ese resultado.
    assert out["success"] is False and "archivo en uso" not in out["message"]
    espia.assert_awaited_once()


async def test_preparador_con_resultado_vacio_borra_la_corrida_y_falla(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hallazgo de PR-Agent en #1015: sin shims, nadie podría liberar la carpeta después."""
    from sky_claw.local.tools import synthesis_fase1 as f1

    async def _vacio(*_a: Any, **_k: Any) -> f1.ResultadoFase1:
        return f1.ResultadoFase1(ejecutables={})

    monkeypatch.setattr(f1, "preparar_fase1", _vacio)
    prep = _preparador_real(tmp_path)
    with pytest.raises(f1.Fase1Error, match="ningún shim"):
        await prep.preparar(tmp_path / "s.json", "Default", ["a"])
    assert list((tmp_path / "raiz").iterdir()) == []


async def test_los_logs_de_los_shims_llevan_stage_y_tx_id(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Hallazgo de Codex en #1015: conservar o no poder liberar se correlaciona con la transacción."""
    import logging

    rutas = _escenario_mo2(tmp_path)

    class _LiberaMal(_PreparadorFalso):
        async def liberar(self, ejecutables: Any) -> None:
            raise OSError("archivo en uso")

    svc = _servicio_brokered(rutas, MagicMock(), _LiberaMal(rutas["mo2"] / "fase1"))
    with caplog.at_level(logging.WARNING, logger="sky_claw.local.tools.synthesis_service"):
        await svc._liberar_shims({"a": tmp_path / "s.exe"}, conservar=True, tx_id=42)
        await svc._liberar_shims({"a": tmp_path / "s.exe"}, conservar=False, tx_id=None)
    registros = [r for r in caplog.records if "shims" in r.getMessage().casefold()]
    assert [(r.pipeline_stage, r.tx_id) for r in registros] == [(7, 42), (7, None)]  # type: ignore[attr-defined]
