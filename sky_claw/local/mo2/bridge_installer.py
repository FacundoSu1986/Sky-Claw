"""Instalación transaccional del bundle Python del bridge en una instancia MO2."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import uuid
from collections.abc import Callable, Sequence

from sky_claw.antigravity.security.file_permissions import restrict_to_owner
from sky_claw.antigravity.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION


class MO2BridgeInstallError(RuntimeError):
    """La instancia, configuración o actualización del bridge es inválida."""


class MO2BridgeInstaller:
    """Copia el plugin con staging + swap y rollback ante fallo."""

    def __init__(
        self,
        *,
        hardener: Callable[[pathlib.Path], None] = restrict_to_owner,
    ) -> None:
        self._hardener = hardener
        self._bundle = pathlib.Path(__file__).parent / "plugin_bundle" / "skyclaw_bridge"

    def install(
        self,
        *,
        mo2_root: pathlib.Path,
        worker_executable: pathlib.Path,
        worker_prefix: Sequence[str],
        descriptor_path: pathlib.Path,
        instance_id: str,
    ) -> pathlib.Path:
        root = mo2_root.resolve()
        if not (root / "ModOrganizer.exe").is_file():
            raise MO2BridgeInstallError(f"ModOrganizer.exe no existe bajo {root}")
        worker = worker_executable.resolve()
        if not worker.is_file() or worker_executable.is_symlink():
            raise MO2BridgeInstallError("worker_executable fijo no existe o es symlink")
        try:
            instance = assert_safe_component(instance_id, field="instance_id")
        except PathViolationError as exc:
            raise MO2BridgeInstallError(str(exc)) from exc
        prefix = tuple(worker_prefix)
        if not prefix or any(not isinstance(arg, str) or not arg for arg in prefix):
            raise MO2BridgeInstallError("worker_prefix debe contener argumentos no vacíos")
        descriptor = descriptor_path.resolve()
        if not descriptor.is_absolute() or descriptor_path.is_symlink():
            raise MO2BridgeInstallError("descriptor_path debe ser absoluto y no symlink")
        if not self._bundle.is_dir():
            raise MO2BridgeInstallError(f"bundle del bridge ausente en {self._bundle}")

        plugins = root / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        destination = plugins / "skyclaw_bridge"
        if destination.is_symlink():
            raise MO2BridgeInstallError("el destino skyclaw_bridge no puede ser un symlink")
        suffix = uuid.uuid4().hex
        staging = plugins / f".skyclaw_bridge.{suffix}.tmp"
        backup = plugins / f".skyclaw_bridge.{suffix}.backup"
        moved_old = False
        try:
            shutil.copytree(
                self._bundle,
                staging,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            config_path = staging / "bridge_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "protocol_version": VFS_PROTOCOL_VERSION,
                        "instance_id": instance,
                        "descriptor_path": str(descriptor),
                        "worker_executable": str(worker),
                        "worker_prefix": list(prefix),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            self._harden_tree(staging)
            if destination.exists():
                os.replace(destination, backup)
                moved_old = True
            os.replace(staging, destination)
        except Exception as exc:
            if moved_old and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            if staging.exists():
                shutil.rmtree(staging)
            raise MO2BridgeInstallError(f"no se pudo instalar el bridge: {exc}") from exc
        if backup.exists():
            shutil.rmtree(backup)
        return destination.resolve()

    def _harden_tree(self, root: pathlib.Path) -> None:
        # Archivos primero; endurecer el directorio padre al final preserva el
        # acceso del owner durante toda la preparación.
        paths = sorted(root.rglob("*"), key=lambda path: (path.is_dir(), len(path.parts)))
        for path in paths:
            self._hardener(path)
        self._hardener(root)
