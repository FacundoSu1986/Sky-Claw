"""Proxy Synthesis que conserva el contrato de ``SynthesisRunner`` dentro del worker USVFS."""

from __future__ import annotations

import asyncio
import contextvars
import pathlib
from collections.abc import Awaitable, Callable
from typing import NoReturn, Protocol

from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    build_attestation_challenge,
)
from sky_claw.local.mo2.vfs_broker import VfsJobTimeoutError
from sky_claw.local.mo2.vfs_contracts import (
    SYNTHESIS_CLI_EXECUTABLE_NAMES,
    JsonValue,
    VfsJob,
    VfsJobResult,
)
from sky_claw.local.tools.patcher_pipeline import SynthesisSettingsProfile
from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisNotFoundError,
    SynthesisResult,
    SynthesisRunner,
    SynthesisTimeoutError,
    _EspProbe,
)

_PLUGIN_SUFFIXES: tuple[str, ...] = (".esp", ".esm", ".esl")


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


def _read_expected_plugins(load_order_path: pathlib.Path | None) -> list[str]:
    """Extrae los plugins activos (``*Plugin.esp``) de ``plugins.txt`` para atestar USVFS."""
    if load_order_path is None or load_order_path.is_symlink() or not load_order_path.is_file():
        raise SynthesisExecutionError("El perfil MO2 activo no tiene un plugins.txt regular legible para atestar USVFS")
    try:
        raw_text = load_order_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise SynthesisExecutionError(f"No se pudo leer {load_order_path}: {exc}") from exc

    plugins: list[str] = []
    seen: set[str] = set()
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("*"):
            continue
        candidate = line[1:].strip()
        if not candidate:
            continue
        try:
            safe_name = assert_safe_component(candidate, field="expected_plugins[]")
        except PathViolationError as exc:
            raise SynthesisExecutionError(f"Nombre de plugin inválido en plugins.txt: {candidate!r}") from exc
        if not safe_name.casefold().endswith(_PLUGIN_SUFFIXES):
            continue
        key = safe_name.casefold()
        if key not in seen:
            seen.add(key)
            plugins.append(safe_name)
    return plugins


def _typed_worker_failure(
    result: VfsJobResult,
    failure_kind: JsonValue,
    *,
    config: SynthesisConfig,
) -> NoReturn:
    """Re-lanza la excepción tipada de ``SynthesisRunner`` emitida por el worker."""
    if not result.success:
        if failure_kind == "timeout":
            raise SynthesisTimeoutError(config.timeout_seconds)
        if failure_kind == "not_found":
            raise SynthesisNotFoundError(config.synthesis_exe)
        if failure_kind == "execution_error":
            raise SynthesisExecutionError(
                result.message or "Synthesis falló dentro de USVFS",
                return_code=result.exit_code,
                stderr=result.stderr or result.message,
            )
    raise SynthesisExecutionError(
        f"Resultado del worker Synthesis fuera de contrato: synthesis_failure_kind={failure_kind!r} "
        f"con success={result.success!r}"
    )


def _string_list_strict(value: object, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SynthesisExecutionError(f"Respuesta del worker Synthesis inválida en {field}: se esperaba list[str]")
    return list(value)


class BrokeredSynthesisRunner:
    """Ejecuta ``Synthesis.Bethesda.CLI run-pipeline`` dentro del worker USVFS."""

    data_visibility_domain: str = "virtual_usvfs"

    def __init__(
        self,
        *,
        config: SynthesisConfig,
        broker: VfsBrokerProtocol,
        instance_id: str,
        mo2_root: pathlib.Path | None = None,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        overwrite_mod: str | None = None,
    ) -> None:
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise SynthesisExecutionError("Brokered Synthesis requiere instance_id no vacío")
        if config.synthesis_exe.name.casefold() not in SYNTHESIS_CLI_EXECUTABLE_NAMES:
            raise SynthesisExecutionError(
                f"Brokered Synthesis requires Synthesis.Bethesda.CLI.exe "
                f"(recibido {config.synthesis_exe.name!r}; no se admite Synthesis.exe GUI WPF)"
            )
        resolved_data = data_root or mo2_root or config.mo2_path
        if resolved_data is None:
            raise SynthesisExecutionError("Brokered Synthesis requiere data_root o mo2_root")
        self._config = config
        self._broker = broker
        self._instance_id = instance_id.strip()
        self._data_root = resolved_data.resolve()
        self._mods_dir = mods_dir.resolve() if mods_dir is not None else (self._data_root / "mods").resolve()
        self._install_root = (install_root or mo2_root or self._data_root).resolve()
        self._profile = config.mo2_profile or config.profile
        game_data = config.data_folder_path if config.data_folder_path is not None else (config.game_path / "Data")
        self._game_data_dir = game_data.resolve()
        self._overwrite_mod = overwrite_mod
        # Se usa únicamente para inspección read-only local y validación TES4, nunca para spawn.
        self._local_inspector = SynthesisRunner(config)
        self._prepared: contextvars.ContextVar[VfsAttestationChallenge | None] = contextvars.ContextVar(
            f"vfs-synthesis-attestation-{id(self)}",
            default=None,
        )
        self._last_result: contextvars.ContextVar[VfsJobResult | None] = contextvars.ContextVar(
            f"vfs-synthesis-result-{id(self)}",
            default=None,
        )

    @property
    def last_vfs_result(self) -> VfsJobResult | None:
        """Resultado VFS ligado a la invocación async actual."""
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

    # --- Delegación read-only local para paridad con SynthesisRunner ---

    def inspect_settings(self, patcher_ids: list[str] | None = None) -> SynthesisSettingsProfile:
        return self._local_inspector.inspect_settings(patcher_ids)

    def settings_fingerprint(self) -> str:
        return self._local_inspector.settings_fingerprint()

    def load_order_fingerprint(self) -> str:
        return self._local_inspector.load_order_fingerprint()

    def output_base_paths(self, patcher_ids: list[str] | None = None) -> tuple[pathlib.Path, ...]:
        return self._local_inspector.output_base_paths(patcher_ids)

    def discover_output_files(self, patcher_ids: list[str] | None = None) -> list[pathlib.Path]:
        return self._local_inspector.discover_output_files(patcher_ids)

    def _discover_output_files_for_groups(self, group_names: tuple[str, ...]) -> list[pathlib.Path]:
        return self._local_inspector._discover_output_files_for_groups(group_names)

    async def _probe_output_files(self, group_names: tuple[str, ...]) -> dict[pathlib.Path, _EspProbe]:
        return await self._local_inspector._probe_output_files(group_names)

    async def validate_synthesis_esp(self, esp_path: pathlib.Path) -> bool:
        return await self._local_inspector.validate_synthesis_esp(esp_path)

    # --- Atestación VFS ---

    async def prepare_attestation(self) -> VfsAttestationChallenge:
        """Captura el desafío pre-run sin arrancar ningún worker."""
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
        self._prepared.set(None)

    def _build_job_payload(
        self,
        *,
        patcher_ids: list[str],
        expected_plugins: list[str],
        settings_fingerprint: str,
        load_order_fingerprint: str,
    ) -> dict[str, JsonValue]:
        settings_path = self._config.pipeline_settings_path
        if settings_path is None:
            raise SynthesisExecutionError("Synthesis PipelineSettingsPath is required")
        payload: dict[str, JsonValue] = {
            "synthesis_exe": str(self._config.synthesis_exe.resolve()),
            "output_path": str(self._config.output_path.resolve()),
            "pipeline_settings_path": str(settings_path.resolve()),
            "synthesis_profile": self._config.profile,
            "patcher_ids": list(patcher_ids),
            "expected_plugins": expected_plugins,
            "persistence_mode": self._config.persistence_mode,
            "block_building_within_mo2": self._config.block_building_within_mo2,
            "shortcircuit": True if self._config.shortcircuit is None else self._config.shortcircuit,
            "require_patcher_receipts": self._config.require_patcher_receipts,
            "expected_settings_fingerprint": settings_fingerprint,
            "expected_load_order_fingerprint": load_order_fingerprint,
        }
        optional_paths: tuple[tuple[str, pathlib.Path | None], ...] = (
            ("extra_data_folder", self._config.extra_data_folder),
            ("working_dir", self._config.working_dir),
            ("temp_dir", self._config.temp_dir),
            ("dotnet_root", self._config.dotnet_root),
            ("persistence_path", self._config.persistence_path),
        )
        for key, path_val in optional_paths:
            if path_val is not None:
                payload[key] = str(path_val.resolve())
        if self._config.target_runtime is not None:
            payload["target_runtime"] = self._config.target_runtime
        return payload

    async def run_pipeline(
        self,
        patcher_ids: list[str],
        extra_args: list[str] | None = None,
        *,
        expected_settings_fingerprint: str | None = None,
        expected_load_order_fingerprint: str | None = None,
    ) -> SynthesisResult:
        """Despacha ``run-pipeline`` dentro del worker USVFS con payload cerrado."""
        if extra_args:
            raise SynthesisExecutionError(
                "Brokered Synthesis no admite extra_args arbitrarios; el contrato USVFS es cerrado"
            )
        if not patcher_ids:
            raise SynthesisExecutionError("At least one Synthesis patcher must be selected")

        settings_fp = (
            expected_settings_fingerprint
            if expected_settings_fingerprint is not None
            else await asyncio.to_thread(self.settings_fingerprint)
        )
        load_order_fp = (
            expected_load_order_fingerprint
            if expected_load_order_fingerprint is not None
            else await asyncio.to_thread(self.load_order_fingerprint)
        )
        await asyncio.to_thread(self.inspect_settings, patcher_ids)
        expected_plugins = await asyncio.to_thread(_read_expected_plugins, self._config.load_order_file_path)

        payload = self._build_job_payload(
            patcher_ids=patcher_ids,
            expected_plugins=expected_plugins,
            settings_fingerprint=settings_fp,
            load_order_fingerprint=load_order_fp,
        )
        challenge = await self._take_or_build_challenge()
        mutation_target = self._config.output_path.resolve()
        job = VfsJob.create(
            instance_id=self._instance_id,
            profile=self._profile,
            tool_id="synthesis",
            payload=payload,
            timeout_seconds=float(self._config.timeout_seconds),
            expected_fingerprint=challenge.profile_fingerprint,
            mutation_targets=(mutation_target,),
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
            err = SynthesisTimeoutError(self._config.timeout_seconds)
            if getattr(exc, "terminality_unknown", False):
                from sky_claw.app.db.rollback_veto import mark_unknown_terminality

                mark_unknown_terminality(err, teardown_error=getattr(exc, "teardown_error", None))
            raise err from exc

        self._last_result.set(result)
        tool = result.tool_result
        failure_kind = tool.get("synthesis_failure_kind")
        if failure_kind is not None:
            _typed_worker_failure(result, failure_kind, config=self._config)

        raw_esps = _string_list_strict(tool.get("output_esps"), field="output_esps")
        output_esps = tuple(pathlib.Path(item) for item in raw_esps)
        raw_primary = tool.get("output_esp")
        output_esp: pathlib.Path | None
        if raw_primary is None:
            output_esp = None
        elif isinstance(raw_primary, str) and raw_primary:
            output_esp = pathlib.Path(raw_primary)
        else:
            raise SynthesisExecutionError("Respuesta del worker Synthesis inválida en output_esp")

        patchers_executed = _string_list_strict(tool.get("patchers_executed"), field="patchers_executed")
        errors = _string_list_strict(tool.get("errors"), field="errors")
        success = result.success
        if not success and result.message and result.message not in errors:
            errors.insert(0, result.message)
        if success and output_esp is None:
            success = False
            errors.append("Worker Synthesis reportó éxito sin output_esp")

        return SynthesisResult(
            success=success,
            output_esp=output_esp if success else None,
            return_code=result.exit_code if result.exit_code is not None else -1,
            stdout=result.stdout,
            stderr=result.stderr,
            patchers_executed=patchers_executed,
            errors=errors,
            output_esps=output_esps,
        )


#: Callback reutilizable por consumidores que inyectan destinos mutables.
MutationTargetsFactory = Callable[[], tuple[pathlib.Path, ...]]
