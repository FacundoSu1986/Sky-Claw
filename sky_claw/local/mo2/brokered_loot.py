"""Proxy LOOT que conserva su contrato pero ejecuta dentro del worker USVFS."""

from __future__ import annotations

import asyncio
import contextvars
import pathlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from sky_claw.local.loot.cli import LOOTNotFoundError
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    build_attestation_challenge,
)
from sky_claw.local.mo2.vfs_contracts import VfsJob, VfsJobResult


class VfsBrokerProtocol(Protocol):
    def submit(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        mo2_root: pathlib.Path,
        virtual_data_dir: pathlib.Path,
        overwrite_mod: str | None = None,
    ) -> Awaitable[VfsJobResult]: ...


class BrokeredLootRunner:
    """Implementa ``LOOTRunner.sort`` sin crear el subprocess en el daemon."""

    def __init__(
        self,
        *,
        broker: VfsBrokerProtocol,
        instance_id: str,
        mo2_root: pathlib.Path,
        profile: str,
        game_data_dir: pathlib.Path,
        loot_exe: pathlib.Path,
        timeout: int,
        mutation_targets: Callable[[], tuple[pathlib.Path, ...]],
        overwrite_mod: str | None = None,
    ) -> None:
        self._broker = broker
        self._instance_id = instance_id
        self._mo2_root = mo2_root.resolve()
        self._profile = profile
        self._game_data_dir = game_data_dir.resolve()
        self._loot_exe = loot_exe.resolve()
        self._timeout = timeout
        self._mutation_targets = mutation_targets
        self._overwrite_mod = overwrite_mod
        self._prepared: contextvars.ContextVar[VfsAttestationChallenge | None] = contextvars.ContextVar(
            f"vfs-attestation-{id(self)}",
            default=None,
        )
        self._last_result: contextvars.ContextVar[VfsJobResult | None] = contextvars.ContextVar(
            f"vfs-result-{id(self)}",
            default=None,
        )

    @property
    def last_vfs_result(self) -> VfsJobResult | None:
        """Resultado ligado a la invocación async actual."""
        return self._last_result.get()

    def for_profile(self, profile: str) -> BrokeredLootRunner:
        """Crea un runner aislado que resuelve targets del perfil solicitado."""
        if profile == self._profile:
            return self
        resolver = LoadOrderFileResolver(mo2_root=self._mo2_root, profile=profile)
        return BrokeredLootRunner(
            broker=self._broker,
            instance_id=self._instance_id,
            mo2_root=self._mo2_root,
            profile=profile,
            game_data_dir=self._game_data_dir,
            loot_exe=self._loot_exe,
            timeout=self._timeout,
            mutation_targets=lambda: tuple(resolver.resolve().files),
            overwrite_mod=self._overwrite_mod,
        )

    def mutation_targets(self) -> tuple[pathlib.Path, ...]:
        """Devuelve los targets fisicos que el daemon debe snapshotear."""
        return tuple(path.resolve() for path in self._mutation_targets())

    async def prepare_attestation(self) -> VfsAttestationChallenge:
        """Captura el fingerprint pre-HITL sin arrancar ningún worker."""
        challenge = await asyncio.to_thread(
            build_attestation_challenge,
            mo2_root=self._mo2_root,
            profile=self._profile,
            physical_data_dir=self._game_data_dir,
        )
        self._prepared.set(challenge)
        return challenge

    async def _take_or_build_challenge(self) -> VfsAttestationChallenge:
        challenge = self._prepared.get()
        self._prepared.set(None)
        if challenge is not None:
            return challenge
        return await asyncio.to_thread(
            build_attestation_challenge,
            mo2_root=self._mo2_root,
            profile=self._profile,
            physical_data_dir=self._game_data_dir,
        )

    def clear_prepared_attestation(self) -> None:
        """Descarta el preview ligado a la invocación async actual."""
        self._prepared.set(None)

    async def sort(self, *, update_masterlist: bool = False) -> LOOTResult:
        challenge = await self._take_or_build_challenge()
        targets = await asyncio.to_thread(self.mutation_targets)
        job = VfsJob.create(
            instance_id=self._instance_id,
            profile=self._profile,
            tool_id="loot_sort",
            payload={
                "loot_exe": str(self._loot_exe),
                "game": "SkyrimSE",
                "update_masterlist": update_masterlist,
            },
            timeout_seconds=float(self._timeout),
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=targets,
        )
        result = await self._broker.submit(
            job,
            challenge=challenge,
            mo2_root=self._mo2_root,
            virtual_data_dir=self._game_data_dir,
            overwrite_mod=self._overwrite_mod,
        )
        self._last_result.set(result)
        tool = result.tool_result
        sorted_plugins = _string_list(tool.get("sorted_plugins"))
        warnings = _string_list(tool.get("warnings"))
        errors = _string_list(tool.get("errors"))
        if not result.success and result.message and result.message not in errors:
            errors.insert(0, result.message)
        return LOOTResult(
            return_code=result.exit_code if result.exit_code is not None else -1,
            sorted_plugins=sorted_plugins,
            warnings=warnings,
            errors=errors,
            missing_patches=_missing_patches(tool.get("missing_patches")),
            raw_stdout=result.stdout,
            raw_stderr=result.stderr,
        )


class VfsRequiredLootRunner:
    """Guard explicito que impide reconstruir un subprocess standalone."""

    def __init__(self, message: str) -> None:
        self._message = message

    def for_profile(self, _profile: str) -> VfsRequiredLootRunner:
        return self

    async def sort(self, *, update_masterlist: bool = False) -> LOOTResult:
        del update_masterlist
        raise LOOTNotFoundError(self._message)


def build_vfs_loot_runner(
    *,
    broker: VfsBrokerProtocol | None,
    instance_id: str | None,
    mo2_root: pathlib.Path,
    game_path: pathlib.Path | None,
    loot_exe: pathlib.Path | None,
    profile: str,
    timeout: int = 120,
) -> BrokeredLootRunner | VfsRequiredLootRunner:
    """Construye el runner productivo o un guard F8 sin fallback directo."""
    if broker is None or instance_id is None:
        return VfsRequiredLootRunner(
            "F8 guard: LOOT requiere el VfsExecutionBroker de MO2/USVFS; no se creara un subprocess standalone."
        )
    if game_path is None or loot_exe is None or not loot_exe.is_file():
        return VfsRequiredLootRunner("F8 guard: faltan rutas verificadas de Skyrim o LOOT para ejecutar bajo USVFS.")
    resolver = LoadOrderFileResolver(mo2_root=mo2_root, profile=profile)
    return BrokeredLootRunner(
        broker=broker,
        instance_id=instance_id,
        mo2_root=mo2_root,
        profile=profile,
        game_data_dir=game_path / "Data",
        loot_exe=loot_exe,
        timeout=timeout,
        mutation_targets=lambda: tuple(resolver.resolve().files),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _missing_patches(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    parsed: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if all(isinstance(key, str) and isinstance(field, str) for key, field in item.items()):
            parsed.append(dict(item))
    return parsed
