"""Spawn strategy de TexGen/DynDOLOD dentro de una sesión MO2/USVFS.

Este módulo es el único lugar que conoce cómo traducir la identidad de una
herramienta DynDOLOD a un ``VfsJob``. El runner recibe solamente una strategy y
conserva su lifecycle habitual: PID, readiness UIA/HITL, deadline y resultado.

El worker no recibe un command string ni reconstruye switches. La lista argv que
llega acá es exactamente la que ``DynDOLODRunner._build_xedit_args`` ya produjo.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    build_attestation_challenge,
)
from sky_claw.local.mo2.vfs_contracts import (
    VFS_TOOL_EXECUTABLE_NAMES,
    VfsJob,
)
from sky_claw.local.mo2.vfs_session import VfsProcessSession

if TYPE_CHECKING:
    from sky_claw.local.tools.dyndolod_runner import DynDOLODProcess


class BrokeredDynDOLODProtocol(Protocol):
    async def open_session(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> VfsProcessSession: ...


class BrokeredDynDOLODProcess:
    """Adaptador mínimo de ``VfsProcessSession`` al proceso que consume el runner."""

    def __init__(self, session: VfsProcessSession) -> None:
        self._session = session
        self._cancel_task: asyncio.Task[None] | None = None
        self._output: tuple[str, str] | None = None

    @property
    def backend_managed(self) -> bool:
        return True

    @property
    def pid(self) -> int:
        return self._session.pid

    @property
    def returncode(self) -> int | None:
        return self._session.returncode

    @property
    def stdout(self) -> None:
        # La captura bounded vive en el worker. No se crea un stream IPC nuevo.
        return None

    @property
    def stderr(self) -> None:
        return None

    def assign_job(self) -> None:
        """El Job Object pertenece al worker/bridge, nunca al daemon."""
        return None

    def kill(self) -> None:
        """Compatibilidad con ``kill_and_reap`` sin matar el PID desde el daemon.

        El helper histórico llama a ``kill`` seguido de ``wait``. Para una
        sesión brokered ambos métodos se traducen a ``session.cancel()``; no hay
        ``os.kill``, ``taskkill`` ni ``proc.kill`` sobre el tool remoto.
        """
        if self._cancel_task is None:
            self._cancel_task = asyncio.create_task(self._session.cancel())

    async def terminate(self) -> None:
        """Teardown del backend brokered: la sesión es la autoridad."""
        await self._session.cancel()

    async def wait(self) -> int:
        if self._cancel_task is not None:
            await self._cancel_task
            return self._session.returncode if self._session.returncode is not None else -1
        return await self._session.wait()

    async def captured_output(self) -> tuple[str, str] | None:
        if self._output is None:
            result = await self._session.result()
            self._output = (result.stdout, result.stderr)
        return self._output


class BrokeredDynDOLODSpawnStrategy:
    """Construye un challenge/job nuevo y abre una sesión por herramienta."""

    def __init__(
        self,
        *,
        broker: BrokeredDynDOLODProtocol,
        instance_id: str,
        profile: str,
        data_root: pathlib.Path,
        mods_dir: pathlib.Path,
        install_root: pathlib.Path,
        physical_data_dir: pathlib.Path,
        virtual_data_dir: pathlib.Path,
        output_roots: Mapping[str, pathlib.Path],
    ) -> None:
        self._broker = broker
        self._instance_id = instance_id
        self._profile = profile
        self._data_root = data_root.resolve()
        self._mods_dir = mods_dir.resolve()
        self._install_root = install_root.resolve()
        self._physical_data_dir = physical_data_dir.resolve()
        self._virtual_data_dir = virtual_data_dir.resolve()
        self._output_roots = {key: value.resolve() for key, value in output_roots.items()}

    async def spawn(
        self,
        *,
        executable: pathlib.Path,
        args: list[str],
        tool_name: str,
        cwd: pathlib.Path,
        timeout: float,
    ) -> DynDOLODProcess:
        tool_id = {"TexGen": "texgen", "DynDOLOD": "dyndolod"}.get(tool_name)
        if tool_id is None:
            raise ValueError(f"herramienta no permitida para VFS: {tool_name!r}")
        validated_executable = self._validate_executable(tool_id, executable)
        resolved_cwd = self._validate_cwd(cwd)
        output_root = self._output_roots.get(tool_id)
        if output_root is None:
            raise ValueError(f"falta output root brokered para {tool_id}")

        # Se construye SIEMPRE por spawn: TexGen y DynDOLOD nunca comparten un
        # challenge, incluso si el perfil no cambió entre ambas etapas.
        challenge = await asyncio.to_thread(
            build_attestation_challenge,
            data_root=self._data_root,
            mods_dir=self._mods_dir,
            profile=self._profile,
            physical_data_dir=self._physical_data_dir,
        )
        job = VfsJob.create(
            instance_id=self._instance_id,
            profile=self._profile,
            tool_id=tool_id,
            payload={
                "executable": str(validated_executable),
                "argv": list(args),
                "cwd": str(resolved_cwd),
            },
            timeout_seconds=float(timeout),
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(output_root,),
        )
        session = await self._broker.open_session(
            job,
            challenge=challenge,
            data_root=self._data_root,
            mods_dir=self._mods_dir,
            install_root=self._install_root,
            virtual_data_dir=self._virtual_data_dir,
        )
        return BrokeredDynDOLODProcess(session)

    @staticmethod
    def _validate_executable(tool_id: str, executable: pathlib.Path) -> pathlib.Path:
        if not executable.is_absolute():
            raise ValueError("el executable brokered debe ser absoluto")
        if executable.is_symlink() or not executable.is_file():
            raise ValueError("el executable brokered debe ser un archivo real y no un symlink")
        resolved = executable.resolve()
        if resolved.name.casefold() != VFS_TOOL_EXECUTABLE_NAMES[tool_id]:
            raise ValueError(f"executable {resolved.name!r} no corresponde a tool_id {tool_id!r}")
        return resolved

    @staticmethod
    def _validate_cwd(cwd: pathlib.Path) -> pathlib.Path:
        if not cwd.is_absolute():
            raise ValueError("cwd brokered debe ser absoluto")
        if cwd.is_symlink() or not cwd.is_dir():
            raise ValueError("cwd brokered debe ser una carpeta real")
        return cwd.resolve()


def build_brokered_dyndolod_spawn_strategy(
    *,
    broker: BrokeredDynDOLODProtocol | None,
    instance_id: str | None,
    profile: str | None,
    data_root: pathlib.Path | None,
    mods_dir: pathlib.Path | None,
    install_root: pathlib.Path | None,
    physical_data_dir: pathlib.Path | None,
    virtual_data_dir: pathlib.Path | None,
    output_roots: Mapping[str, pathlib.Path] | None,
) -> BrokeredDynDOLODSpawnStrategy | None:
    """Seam explícito: faltan dependencias => no se activa brokered en silencio."""
    values = (
        broker,
        instance_id,
        profile,
        data_root,
        mods_dir,
        install_root,
        physical_data_dir,
        virtual_data_dir,
        output_roots,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("la strategy brokered requiere broker, instancia, perfil y todas las raíces")
    assert broker is not None
    assert instance_id is not None
    assert profile is not None
    assert data_root is not None
    assert mods_dir is not None
    assert install_root is not None
    assert physical_data_dir is not None
    assert virtual_data_dir is not None
    assert output_roots is not None
    return BrokeredDynDOLODSpawnStrategy(
        broker=broker,
        instance_id=instance_id,
        profile=profile,
        data_root=data_root,
        mods_dir=mods_dir,
        install_root=install_root,
        physical_data_dir=physical_data_dir,
        virtual_data_dir=virtual_data_dir,
        output_roots=output_roots,
    )
