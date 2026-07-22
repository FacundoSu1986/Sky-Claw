"""Distribución atómica del plugin mínimo dentro de una instancia MO2."""

from __future__ import annotations

import json
import pathlib

import pytest

from sky_claw.local.mo2.bridge_installer import MO2BridgeInstaller, MO2BridgeInstallError


def _fixture(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    mo2 = tmp_path / "MO2"
    (mo2 / "plugins").mkdir(parents=True)
    (mo2 / "ModOrganizer.exe").write_bytes(b"mo2")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"worker")
    return mo2, worker


def test_instala_bundle_y_config_fija_sin_secretos(tmp_path: pathlib.Path) -> None:
    mo2, worker = _fixture(tmp_path)
    hardened: list[pathlib.Path] = []
    installer = MO2BridgeInstaller(hardener=hardened.append)
    descriptor = tmp_path / "state" / "portable-main.json"

    installed = installer.install(
        mo2_root=mo2,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        instance_id="portable-main",
    )

    assert installed == (mo2 / "plugins" / "skyclaw_bridge").resolve()
    assert (installed / "__init__.py").is_file()
    assert (installed / "protocol.py").is_file()
    config = json.loads((installed / "bridge_config.json").read_text(encoding="utf-8"))
    assert config["worker_executable"] == str(worker.resolve())
    assert config["descriptor_path"] == str(descriptor.resolve())
    assert "token" not in config
    assert any(path.name == "bridge_config.json" for path in hardened)
    assert not (installed / "__pycache__").exists()


def test_reinstalacion_reemplaza_bundle_sin_dejar_backup(tmp_path: pathlib.Path) -> None:
    mo2, worker = _fixture(tmp_path)
    installer = MO2BridgeInstaller(hardener=lambda _path: None)
    descriptor = tmp_path / "state" / "portable-main.json"
    first = installer.install(
        mo2_root=mo2,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        instance_id="portable-main",
    )
    (first / "obsoleto.txt").write_text("old", encoding="utf-8")

    second = installer.install(
        mo2_root=mo2,
        worker_executable=worker,
        worker_prefix=("--vfs-worker", "--verbose"),
        descriptor_path=descriptor,
        instance_id="portable-main",
    )

    assert not (second / "obsoleto.txt").exists()
    assert not list((mo2 / "plugins").glob(".skyclaw_bridge.*.backup"))
    config = json.loads((second / "bridge_config.json").read_text(encoding="utf-8"))
    assert config["worker_prefix"] == ["--vfs-worker", "--verbose"]


def test_rechaza_instancia_o_worker_inexistente(tmp_path: pathlib.Path) -> None:
    installer = MO2BridgeInstaller(hardener=lambda _path: None)

    with pytest.raises(MO2BridgeInstallError, match="ModOrganizer.exe"):
        installer.install(
            mo2_root=tmp_path / "no-mo2",
            worker_executable=tmp_path / "no-worker.exe",
            worker_prefix=("--vfs-worker",),
            descriptor_path=tmp_path / "descriptor.json",
            instance_id="portable-main",
        )
