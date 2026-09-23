"""Proxy LOOT que conserva su contrato pero ejecuta dentro del worker USVFS."""

from __future__ import annotations

import asyncio
import contextvars
import pathlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from sky_claw.local.loot.cli import DEFAULT_LOOT_INTERNAL_GAME_ID, LOOTNotFoundError
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    build_attestation_challenge,
)
from sky_claw.local.mo2.vfs_contracts import VfsJob, VfsJobResult

#: Nombres del CLI interno de MO2 (implementation detail privado, NO backend
#: productivo — decisión PR-0). El binario canónico del internal LOOT de MO2
#: es ``<instancia MO2>\loot\lootcli.exe``.
_MO2_INTERNAL_LOOT_NAMES: frozenset[str] = frozenset({"lootcli.exe", "loot-cli.exe"})


def _is_mo2_internal_loot(loot_exe: pathlib.Path, install_root: pathlib.Path) -> bool:
    """¿*loot_exe* es el LOOT interno de MO2 en vez del standalone oficial?

    Backend productivo (decisión de diseño, no debatible sin evidencia nueva):

    ``Sky-Claw → BrokeredLootRunner → MO2/USVFS → LOOT.exe standalone oficial``

    El internal LOOT de MO2 vive en el subárbol ``<instancia MO2>\\loot\\``
    (``lootcli.exe`` y compañía): su versión sigue al release de MO2 (no al
    binario standalone que opera el operador), MO2 lo reescribe en sus
    updates y su dialecto CLI puede divergir del standalone. Se rechaza
    fail-closed:

    * cualquier exe llamado ``lootcli.exe``/``loot-cli.exe`` (en cualquier
      lugar: es el nombre del wrapper CLI interno de MO2), y
    * cualquier exe dentro del subárbol ``<instancia MO2>\\loot\\`` (aunque
      se llame ``LOOT.exe``: es la copia que MO2 gestiona).

    El standalone del operador queda afuera tanto en ``C:\\Tools\\LOOT``
    como colocado junto a la instalación de MO2 (``<instancia MO2>\\loot.exe``
    — raíz de la instalación, no el subárbol ``loot\\``).

    **Case-insensitive por diseño (review FINDING A):** el target productivo
    es Windows, donde ``loot``, ``Loot`` y ``LOOT`` son el MISMO directorio.
    La comparación es del contrato lógico, no del filesystem host: se
    comparan ``Path.parts`` relativos a ``install_root`` con ``casefold``
    COMPONENTE POR COMPONENTE (tuplas puras). NO se hace ``lower()``/
    ``casefold()`` de la ruta completa para luego crear un ``Path``: eso
    mezclaría la comparación lógica con la resolución real del filesystem.
    La resolución real (``resolve()``) ya la hizo el constructor antes de
    llamar a esta función; aquí no se toca disco.
    """
    if loot_exe.name.casefold() in _MO2_INTERNAL_LOOT_NAMES:
        return True
    exe_parts = loot_exe.parts
    root_parts = install_root.parts
    if len(exe_parts) <= len(root_parts):
        return False
    # strict=False a propósito: se empareja solo el prefijo install_root.
    if any(a.casefold() != b.casefold() for a, b in zip(exe_parts, root_parts, strict=False)):
        return False
    return exe_parts[len(root_parts)].casefold() == "loot"


class VfsBrokerProtocol(Protocol):
    def submit(
        self,
        job: VfsJob,
        *,
        challenge: VfsAttestationChallenge,
        mo2_root: pathlib.Path | None = None,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
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
        mo2_root: pathlib.Path | None = None,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        profile: str,
        game_data_dir: pathlib.Path,
        loot_exe: pathlib.Path,
        timeout: int,
        mutation_targets: Callable[[], tuple[pathlib.Path, ...]],
        overwrite_mod: str | None = None,
    ) -> None:
        self._broker = broker
        self._instance_id = instance_id
        resolved_data = data_root or mo2_root
        if resolved_data is None:
            raise ValueError("se requiere data_root o mo2_root")
        self._data_root = resolved_data.resolve()
        self._mods_dir = mods_dir.resolve() if mods_dir is not None else (self._data_root / "mods")
        self._install_root = (install_root or mo2_root or self._data_root).resolve()
        self._mo2_root = self._install_root
        self._profile = profile
        self._game_data_dir = game_data_dir.resolve()
        self._loot_exe = loot_exe.resolve()
        self._timeout = timeout
        self._mutation_targets = mutation_targets
        self._overwrite_mod = overwrite_mod
        if _is_mo2_internal_loot(self._loot_exe, self._install_root):
            # Fail-closed: el backend productivo es el LOOT.exe standalone
            # oficial, nunca el loot\lootcli.exe interno de MO2 (ver
            # _is_mo2_internal_loot). Construir un runner sobre el internal
            # invalidaría la decisión de backend de forma silenciosa.
            raise ValueError(
                "F8 guard: el LOOT.exe resuelto es el LOOT interno de MO2 "
                f"({self._loot_exe}, en el subárbol {self._install_root / 'loot'} "
                "o llamado lootcli.exe), implementation detail privado. El backend "
                "productivo es el LOOT.exe standalone oficial del operador."
            )
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

    @property
    def install_root(self) -> pathlib.Path:
        return self._install_root

    @property
    def data_root(self) -> pathlib.Path:
        return self._data_root

    @property
    def mods_dir(self) -> pathlib.Path:
        return self._mods_dir

    def for_profile(self, profile: str) -> BrokeredLootRunner:
        """Crea un runner aislado que resuelve targets del perfil solicitado."""
        if profile == self._profile:
            return self
        resolver = LoadOrderFileResolver(mo2_root=self._data_root, profile=profile)
        return BrokeredLootRunner(
            broker=self._broker,
            instance_id=self._instance_id,
            data_root=self._data_root,
            mods_dir=self._mods_dir,
            install_root=self._install_root,
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
            data_root=self._data_root,
            mods_dir=self._mods_dir,
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
            data_root=self._data_root,
            mods_dir=self._mods_dir,
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
                # Id INTERNO de Sky-Claw, no el string de CLI: el worker valida
                # contra la MISMA frontera (LOOT_CLI_GAME_IDENTIFIERS de
                # sky_claw.local.loot.cli) y la traducción al dialecto de
                # `--game` ocurre una sola vez, en el runner (PR-0).
                "game": DEFAULT_LOOT_INTERNAL_GAME_ID,
                "update_masterlist": update_masterlist,
            },
            timeout_seconds=float(self._timeout),
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=targets,
        )
        result = await self._broker.submit(
            job,
            challenge=challenge,
            data_root=self._data_root,
            mods_dir=self._mods_dir,
            install_root=self._install_root,
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
    mo2_root: pathlib.Path | None = None,
    data_root: pathlib.Path | None = None,
    mods_dir: pathlib.Path | None = None,
    install_root: pathlib.Path | None = None,
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
    effective_data = data_root or mo2_root
    if effective_data is None:
        return VfsRequiredLootRunner("F8 guard: falta la ruta de datos de MO2 para ejecutar bajo USVFS.")
    resolver = LoadOrderFileResolver(mo2_root=effective_data, profile=profile)
    try:
        return BrokeredLootRunner(
            broker=broker,
            instance_id=instance_id,
            data_root=effective_data,
            mods_dir=mods_dir or (effective_data / "mods"),
            install_root=install_root or mo2_root or effective_data,
            profile=profile,
            game_data_dir=game_path / "Data",
            loot_exe=loot_exe,
            timeout=timeout,
            mutation_targets=lambda: tuple(resolver.resolve().files),
        )
    except ValueError as exc:
        # F8: mismo patrón fail-closed que los otros guards del factory —
        # un backend inválido (p. ej. el loot\lootcli.exe interno de MO2,
        # anclado por _is_mo2_internal_loot) no se convierte en subprocess.
        # El mensaje del constructor ya lleva el prefijo "F8 guard:".
        return VfsRequiredLootRunner(str(exc))


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
