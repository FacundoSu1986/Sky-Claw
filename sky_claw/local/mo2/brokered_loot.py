"""Proxy LOOT que conserva su contrato pero ejecuta dentro del worker USVFS."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import pathlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from sky_claw.local.loot.cli import (
    DEFAULT_LOOT_INTERNAL_GAME_ID,
    LOOT_FAILURE_KIND_PRECONDITION,
    LOOT_FAILURE_KIND_TIMEOUT,
    LOOTNotFoundError,
    LOOTPreconditionError,
    LOOTTimeoutError,
)
from sky_claw.local.loot.data_root import (
    DEFAULT_LOOT_DATA_BASE,
    ensure_loot_data_path_exists,
    resolve_loot_data_path,
)
from sky_claw.local.loot.execution_witness import LootExecutionWitness, LootExecutionWitnessPayloadError
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    build_attestation_challenge,
)
from sky_claw.local.mo2.vfs_broker import VfsJobTimeoutError
from sky_claw.local.mo2.vfs_contracts import JsonValue, VfsJob, VfsJobResult

logger = logging.getLogger(__name__)

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
    """Implementa ``LOOTRunner.sort`` sin crear el subprocess en el daemon.

    PR-1: ``loot_data_path`` es el LOOT DATA ROOT propiedad de Sky-Claw
    (``--loot-data-path``). Ver ``sky_claw.local.loot.data_root`` para
    contrato upstream 0.29.1 y decisión de lifetime (persistente por
    instancia+perfil, no por operación). El daemon decide la ruta una sola
    vez y el worker no la reinventa.
    """

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
        loot_data_path: pathlib.Path | None = None,
        loot_data_base: pathlib.Path | None = None,
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
        # PR-1: data root de LOOT propiedad de Sky-Claw
        self._loot_data_base = (
            pathlib.Path(loot_data_base).resolve(strict=False)
            if loot_data_base is not None
            else DEFAULT_LOOT_DATA_BASE.resolve(strict=False)
        )
        self._loot_data_path: pathlib.Path | None
        if loot_data_path is not None:
            p = pathlib.Path(loot_data_path)
            if not p.is_absolute():
                raise ValueError(f"loot_data_path must be absolute, got {loot_data_path}")
            # No symlink escape si el repo tiene defensa (PathValidator)
            if p.is_symlink():
                raise ValueError("loot_data_path must not be a symlink")
            self._loot_data_path = p.resolve(strict=False)
        else:
            self._loot_data_path = None
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

    @property
    def loot_data_path(self) -> pathlib.Path | None:
        return self._loot_data_path

    @property
    def loot_data_base(self) -> pathlib.Path:
        return self._loot_data_base

    def for_profile(self, profile: str) -> BrokeredLootRunner:
        """Crea un runner aislado que resuelve targets del perfil solicitado.

        PR-1: recalcula el LOOT data root para el nuevo perfil (aislamiento
        por perfil), usando la misma base e instance_id. Si el runner
        original no tenía loot_data_path (legacy/test), el nuevo tampoco
        (fail-closed en sort seguirá aplicando).
        """
        if profile == self._profile:
            return self
        resolver = LoadOrderFileResolver(mo2_root=self._data_root, profile=profile)
        new_loot_data_path: pathlib.Path | None = None
        if self._loot_data_path is not None:
            try:
                new_loot_data_path = resolve_loot_data_path(
                    instance_id=self._instance_id,
                    profile=profile,
                    base_dir=self._loot_data_base,
                    game_path=self._game_data_dir.parent,
                    loot_exe=self._loot_exe,
                    mods_dir=self._mods_dir,
                    data_root=self._data_root,
                )
            except Exception:
                # Si la resolución falla (perfil hostil), fail-closed en sort
                # igualmente — no crear un path inventado
                new_loot_data_path = None
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
            loot_data_path=new_loot_data_path,
            loot_data_base=self._loot_data_base,
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
        # PR-1: fail-closed si falta loot_data_path en productivo
        if self._loot_data_path is None:
            raise ValueError(
                "F8 guard / PR-1: loot_data_path ausente — el backend productivo "
                "de Sky-Claw NUNCA ejecuta LOOT.exe sin --loot-data-path explícito."
            )
        # Asegurar que el root exista (padres). LOOT crea el root con
        # create_directory, pero requiere padre existente.
        try:
            ensure_loot_data_path_exists(self._loot_data_path)
        except Exception as exc:
            raise ValueError(f"loot_data_path no se pudo asegurar en disco: {exc}") from exc
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
                # PR-1: data root propiedad de Sky-Claw, explícito, estable
                "loot_data_path": str(self._loot_data_path),
            },
            timeout_seconds=float(self._timeout),
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=targets,
        )
        try:
            result = await self._broker.submit(
                job,
                challenge=challenge,
                data_root=self._data_root,
                mods_dir=self._mods_dir,
                install_root=self._install_root,
                virtual_data_dir=self._game_data_dir,
                overwrite_mod=self._overwrite_mod,
            )
        except VfsJobTimeoutError as exc:
            # PR-2: este proxy conserva el contrato de LOOTRunner.sort ("Raises
            # LOOTTimeoutError"). El timeout del broker usa el MISMO valor que el
            # del runner del worker pero arranca antes (incluye lanzamiento y
            # attestation), así que en un cuelgue real es el que dispara primero;
            # sin esta traducción el camino productivo reportaría un error
            # genérico donde el directo reporta TIMEOUT.
            raise LOOTTimeoutError(self._timeout) from exc
        self._last_result.set(result)
        tool = result.tool_result
        failure_kind = tool.get("loot_failure_kind")
        if failure_kind is not None:
            return _typed_worker_failure(result, failure_kind, timeout=self._timeout)
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
            execution_witness=_execution_witness(tool.get("execution_witness")),
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
    loot_data_path: pathlib.Path | None = None,
    loot_data_base: pathlib.Path | None = None,
) -> BrokeredLootRunner | VfsRequiredLootRunner:
    """Construye el runner productivo o un guard F8 sin fallback directo.

    PR-1: resuelve el LOOT data root propiedad de Sky-Claw si no se pasa
    explícito. El root es persistente por instancia+perfil (no por operación)
    para no romper bootstrap de masterlist/settings.
    """
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
        # PR-1: resolver loot_data_path si no viene explícito
        resolved_loot_data_path = loot_data_path
        if resolved_loot_data_path is None:
            try:
                resolved_loot_data_path = resolve_loot_data_path(
                    instance_id=instance_id,
                    profile=profile,
                    base_dir=loot_data_base,
                    game_path=game_path,
                    loot_exe=loot_exe,
                    mods_dir=mods_dir or (effective_data / "mods"),
                    data_root=effective_data,
                )
            except Exception as exc:
                return VfsRequiredLootRunner(f"F8 guard / PR-1: no se pudo resolver loot_data_path: {exc}")
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
            loot_data_path=resolved_loot_data_path,
            loot_data_base=loot_data_base,
        )
    except ValueError as exc:
        # F8: mismo patrón fail-closed que los otros guards del factory —
        # un backend inválido (p. ej. el loot\lootcli.exe interno de MO2,
        # anclado por _is_mo2_internal_loot) no se convierte en subprocess.
        # El mensaje del constructor ya lleva el prefijo "F8 guard:".
        return VfsRequiredLootRunner(str(exc))


def _typed_worker_failure(result: VfsJobResult, failure_kind: JsonValue, *, timeout: int) -> LOOTResult:
    """PR-2: re-lanza la excepción de ``LOOTRunner`` que el worker tipó.

    Un ``loot_failure_kind`` desconocido o acompañado de ``success=True`` es un
    resultado inconsistente: se devuelve como fallo de proceso, nunca se ignora.
    """
    if not result.success:
        if failure_kind == LOOT_FAILURE_KIND_TIMEOUT:
            raise LOOTTimeoutError(timeout)
        if failure_kind == LOOT_FAILURE_KIND_PRECONDITION:
            raise LOOTPreconditionError(result.message or "Precondición headless de LOOT fallida en el worker.")
    detalle = f"Resultado del worker inconsistente: loot_failure_kind={failure_kind!r} con success={result.success!r}."
    return LOOTResult(
        return_code=-1,
        errors=[detalle],
        raw_stdout=result.stdout,
        raw_stderr=result.stderr,
    )


def _execution_witness(value: JsonValue | None) -> LootExecutionWitness | None:
    """PR-2: valida el testigo con schema CERRADO; ausente o inválido → ``None``.

    ``None`` nunca es atribuible (el servicio falla con
    EXECUTION_NOT_ATTRIBUTABLE), así que un payload arbitrario jamás se
    convierte en evidencia: no hay "testigo parcial".
    """
    if value is None:
        return None
    try:
        return LootExecutionWitness.from_payload(value)
    except LootExecutionWitnessPayloadError as exc:
        logger.warning("execution_witness inválido en el resultado del worker LOOT: %s", exc)
        return None


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
