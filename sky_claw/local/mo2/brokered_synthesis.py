"""Proxy Synthesis que conserva el contrato de ``SynthesisRunner`` dentro del worker USVFS."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import pathlib
from collections.abc import Awaitable, Callable, Mapping
from typing import NoReturn, Protocol

from sky_claw.app.db.rollback_veto import exception_forbids_rollback, mark_unknown_terminality
from sky_claw.app.security.links import link_kind
from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.mo2.vfs_attestation import (
    VfsAttestationChallenge,
    build_attestation_challenge,
)
from sky_claw.local.mo2.vfs_broker import VfsJobTimeoutError, VfsTeardownError
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

logger = logging.getLogger(__name__)

_PLUGIN_SUFFIXES: tuple[str, ...] = (".esp", ".esm", ".esl")

SYNTHESIS_BACKEND_STANDALONE = "standalone"
SYNTHESIS_BACKEND_BROKERED = "brokered"
_BACKENDS_VALIDOS: frozenset[str] = frozenset({SYNTHESIS_BACKEND_STANDALONE, SYNTHESIS_BACKEND_BROKERED})

# Hold E3-R3 equivalente a brokered_dyndolod / brokered_loot: no habilitar
# hasta verificar contención real del worker en Windows + gates S1-S5 (ADR 0019 §3.2).
_CONTENCION_SYNTHESIS_VERIFICADA = False

_MOTIVO_DEL_HOLD = (
    "el backend 'brokered' está en WIRING_SECURITY_HOLD: la contención del worker en el Job Object "
    "y los gates empíricos S1-S5 sobre Windows+MO2 no están verificados de punta a punta. "
    "No se construye BrokeredSynthesisRunner ni se lanza ninguna herramienta, y no hay fallback "
    "a standalone. Usá synthesis_backend='standalone' mientras dure el hold. "
    "Ver docs/adr/0019-synthesis-two-phase-usvfs.md."
)


def _normalizar_backend(valor: object) -> str | None:
    """Normaliza el selector explícito ``synthesis_backend``; otros valores devuelven ``None``."""
    if isinstance(valor, str):
        normalizado = valor.strip().casefold()
        if normalizado in _BACKENDS_VALIDOS:
            return normalizado
    return None


def dominio_de_visibilidad_del_backend(valor: object) -> str:
    """Dominio de visibilidad de datos que implica el selector ``synthesis_backend`` (PR-2b3).

    Fuente ÚNICA de la decisión: el servicio la usa para elegir entre la salida física y la raíz
    administrada, y la strategy del dispatcher para decidir si clona el overwrite. Solo
    ``standalone`` (normalizado) es ``physical``; cualquier otro valor, válido o no, es
    ``virtual_usvfs``, así que un selector inválido nunca degrada a la ruta física y lo frena el guard
    fail-closed del servicio.
    """
    return "physical" if _normalizar_backend(valor) == SYNTHESIS_BACKEND_STANDALONE else "virtual_usvfs"


def _validar_ruta_sin_enlaces(path: pathlib.Path, *, field: str) -> pathlib.Path:
    """Rechaza traversal y symlinks/junctions en ruta y ancestros SIN pre-resolver."""
    if ".." in path.parts:
        raise SynthesisExecutionError(f"La ruta {field} contiene traversal ('..') prohibido: {path}")
    abs_path = path if path.is_absolute() else pathlib.Path(os.path.abspath(path))
    for candidate in (abs_path, *abs_path.parents):
        if candidate.parent == candidate:
            continue
        kind = link_kind(candidate)
        if kind is not None:
            raise SynthesisExecutionError(f"La ruta {field} atraviesa un {kind} prohibido ({candidate}): {path}")
    return abs_path


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


def _read_expected_plugins(
    load_order_path: pathlib.Path | None,
    *,
    excluded_groups: tuple[str, ...] = (),
) -> list[str]:
    """Extrae plugins activos de ``plugins.txt``, excluyendo outputs propios del grupo."""
    if load_order_path is None or link_kind(load_order_path) is not None or not load_order_path.is_file():
        raise SynthesisExecutionError("El perfil MO2 activo no tiene un plugins.txt regular legible para atestar USVFS")
    _validar_ruta_sin_enlaces(load_order_path, field="load_order_file_path")
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
        if not safe_name.lower().endswith(_PLUGIN_SUFFIXES):
            continue
        # Excluir outputs propios del grupo para evitar deadlock en reruns cuando
        # ya figuran activos en plugins.txt y se hace unlink() previo al spawn.
        if any(SynthesisRunner._output_belongs_to_group(safe_name, group_name) for group_name in excluded_groups):
            continue
        key = safe_name.lower()
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
        if not config.block_building_within_mo2:
            raise SynthesisExecutionError(
                "Brokered Synthesis requiere block_building_within_mo2=True (contrato Fase 2)"
            )
        if not config.require_patcher_receipts:
            raise SynthesisExecutionError("Brokered Synthesis requiere require_patcher_receipts=True (contrato Fase 2)")
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
        cli_executables: Mapping[str, pathlib.Path] | None = None,
    ) -> dict[str, JsonValue]:
        # PR-3h: el mapa de la corrida (preparado por el servicio) manda; sin él, el de la config.
        mapa_cli = cli_executables if cli_executables is not None else self._config.cli_executables
        cli_executables_json: dict[str, JsonValue] | None = None
        if mapa_cli is not None:
            # PR-3g: el mapa de shims de la Fase 1 viaja al worker. Perderlo en el camino dejaría correr
            # bajo USVFS los patchers Git/Solution sin shim (compilando dentro del VFS). Vacío = error
            # del llamador, no "sin shims": se rechaza en vez de mandar un mapa que no cubre a nadie.
            if not mapa_cli:
                raise SynthesisExecutionError("cli_executables (shims de la Fase 1) no puede ser un mapa vacío")
            cli_executables_json = {}
            for clave, ruta in mapa_cli.items():
                if not isinstance(clave, str) or not clave.strip():
                    raise SynthesisExecutionError("cli_executables solo admite IDs de patcher no vacíos")
                cli_executables_json[clave] = str(
                    _validar_ruta_sin_enlaces(pathlib.Path(ruta), field=f"cli_executables[{clave!r}]")
                )
        settings_path = self._config.pipeline_settings_path
        if settings_path is None:
            raise SynthesisExecutionError("Synthesis PipelineSettingsPath is required")
        synthesis_exe_safe = _validar_ruta_sin_enlaces(self._config.synthesis_exe, field="synthesis_exe")
        output_path_safe = _validar_ruta_sin_enlaces(self._config.output_path, field="output_path")
        settings_path_safe = _validar_ruta_sin_enlaces(settings_path, field="pipeline_settings_path")
        patcher_ids_json: list[JsonValue] = list(patcher_ids)
        expected_plugins_json: list[JsonValue] = list(expected_plugins)
        payload: dict[str, JsonValue] = {
            "synthesis_exe": str(synthesis_exe_safe),
            "output_path": str(output_path_safe),
            "pipeline_settings_path": str(settings_path_safe),
            "synthesis_profile": self._config.profile,
            "patcher_ids": patcher_ids_json,
            "expected_plugins": expected_plugins_json,
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
                payload[key] = str(_validar_ruta_sin_enlaces(path_val, field=key))
        if self._config.target_runtime is not None:
            payload["target_runtime"] = self._config.target_runtime
        if cli_executables_json is not None:
            payload["cli_executables"] = cli_executables_json
        return payload

    async def run_pipeline(
        self,
        patcher_ids: list[str],
        extra_args: list[str] | None = None,
        *,
        expected_settings_fingerprint: str | None = None,
        expected_load_order_fingerprint: str | None = None,
        cli_executables: Mapping[str, pathlib.Path] | None = None,
    ) -> SynthesisResult:
        """Despacha ``run-pipeline`` dentro del worker USVFS con payload cerrado."""
        self._last_result.set(None)
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
        profile_info = await asyncio.to_thread(self.inspect_settings, patcher_ids)
        expected_plugins = await asyncio.to_thread(
            _read_expected_plugins,
            self._config.load_order_file_path,
            excluded_groups=profile_info.group_names,
        )

        payload = self._build_job_payload(
            patcher_ids=patcher_ids,
            expected_plugins=expected_plugins,
            settings_fingerprint=settings_fp,
            load_order_fingerprint=load_order_fp,
            cli_executables=cli_executables,
        )
        challenge = await self._take_or_build_challenge()
        mutation_target = _validar_ruta_sin_enlaces(self._config.output_path, field="output_path")
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
            if exception_forbids_rollback(exc):
                mark_unknown_terminality(err, teardown_error=getattr(exc, "teardown_error", None))
            raise err from exc
        except VfsTeardownError as exc:
            err_exec = SynthesisExecutionError(str(exc), stderr=str(exc))
            mark_unknown_terminality(err_exec, teardown_error=exc)
            raise err_exec from exc

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


class VfsRequiredSynthesisRunner:
    """Guard fail-closed: se pidió backend brokered/inválido y jamás cae a standalone."""

    data_visibility_domain: str = "virtual_usvfs"

    def __init__(self, reason: str, *, config: SynthesisConfig | None = None) -> None:
        self._reason = reason
        self._config_opt = config

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def _config(self) -> SynthesisConfig:
        if self._config_opt is None:
            raise SynthesisExecutionError(self._reason)
        return self._config_opt

    def inspect_settings(self, patcher_ids: list[str] | None = None) -> SynthesisSettingsProfile:
        del patcher_ids
        raise SynthesisExecutionError(self._reason)

    def settings_fingerprint(self) -> str:
        raise SynthesisExecutionError(self._reason)

    def load_order_fingerprint(self) -> str:
        raise SynthesisExecutionError(self._reason)

    def output_base_paths(self, patcher_ids: list[str] | None = None) -> tuple[pathlib.Path, ...]:
        del patcher_ids
        raise SynthesisExecutionError(self._reason)

    def discover_output_files(self, patcher_ids: list[str] | None = None) -> list[pathlib.Path]:
        del patcher_ids
        raise SynthesisExecutionError(self._reason)

    def _discover_output_files_for_groups(self, group_names: tuple[str, ...]) -> list[pathlib.Path]:
        del group_names
        raise SynthesisExecutionError(self._reason)

    async def _probe_output_files(self, group_names: tuple[str, ...]) -> dict[pathlib.Path, _EspProbe]:
        del group_names
        raise SynthesisExecutionError(self._reason)

    async def validate_synthesis_esp(self, esp_path: pathlib.Path) -> bool:
        del esp_path
        raise SynthesisExecutionError(self._reason)

    async def run_pipeline(
        self,
        patcher_ids: list[str] | None = None,
        extra_args: list[str] | None = None,
        *,
        expected_settings_fingerprint: str | None = None,
        expected_load_order_fingerprint: str | None = None,
        cli_executables: Mapping[str, pathlib.Path] | None = None,
    ) -> SynthesisResult:
        del patcher_ids, extra_args, expected_settings_fingerprint, expected_load_order_fingerprint, cli_executables
        raise SynthesisExecutionError(self._reason)


def _guard(reason: str, *, config: SynthesisConfig | None = None) -> VfsRequiredSynthesisRunner:
    mensaje = f"F8 guard: {reason}"
    logger.error("Synthesis brokered no disponible; ejecución falla cerrada: %s", mensaje)
    return VfsRequiredSynthesisRunner(mensaje, config=config)


def resolve_synthesis_backend_runner(
    *,
    backend: object,
    config: SynthesisConfig | None,
    broker: VfsBrokerProtocol | None,
    instance_id: str | None,
    mo2_root: pathlib.Path | None = None,
    data_root: pathlib.Path | None = None,
    mods_dir: pathlib.Path | None = None,
    install_root: pathlib.Path | None = None,
    overwrite_mod: str | None = None,
) -> BrokeredSynthesisRunner | VfsRequiredSynthesisRunner | None:
    """Resuelve el runner Synthesis según el selector explícito ``synthesis_backend``.

    * ``standalone`` -> ``None``: ``SynthesisPipelineService`` usa ``SynthesisRunner``.
    * ``brokered`` -> :class:`BrokeredSynthesisRunner` si el hold está levantado y
      todos los requisitos son válidos; si no, :class:`VfsRequiredSynthesisRunner`.
    * cualquier otro valor -> :class:`VfsRequiredSynthesisRunner` (jamás fallback).
    """
    solicitado = _normalizar_backend(backend)
    if solicitado == SYNTHESIS_BACKEND_STANDALONE:
        return None
    if solicitado != SYNTHESIS_BACKEND_BROKERED:
        return _guard(
            f"el backend Synthesis {backend!r} no es válido (valores admitidos: "
            f"'{SYNTHESIS_BACKEND_STANDALONE}' | '{SYNTHESIS_BACKEND_BROKERED}'). "
            "No se hace fallback a standalone.",
            config=config,
        )
    if not _CONTENCION_SYNTHESIS_VERIFICADA:
        return _guard(_MOTIVO_DEL_HOLD, config=config)
    if broker is None:
        return _guard(
            "el backend 'brokered' requiere VfsExecutionBroker cableado y no puede degradar a standalone.",
            config=config,
        )
    if not isinstance(instance_id, str) or not instance_id.strip():
        return _guard(
            "Brokered Synthesis requiere instance_id válido; prohibido fallback silencioso a standalone.",
            config=config,
        )
    if config is None:
        return _guard(
            "Brokered Synthesis requiere SynthesisConfig completo antes de construir el runner.",
            config=None,
        )
    try:
        return BrokeredSynthesisRunner(
            config=config,
            broker=broker,
            instance_id=instance_id,
            mo2_root=mo2_root,
            data_root=data_root,
            mods_dir=mods_dir,
            install_root=install_root,
            overwrite_mod=overwrite_mod,
        )
    except SynthesisExecutionError as exc:
        return _guard(str(exc), config=config)


#: Callback reutilizable por consumidores que inyectan destinos mutables.
MutationTargetsFactory = Callable[[], tuple[pathlib.Path, ...]]
