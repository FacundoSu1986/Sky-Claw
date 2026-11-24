"""Worker de vida corta que MO2 lanza bajo el mapping USVFS solicitado."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import logging
import os
import pathlib
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias

from sky_claw.app.security.links import link_kind
from sky_claw.app.security.path_validator import PathValidator, PathViolationError, assert_safe_component
from sky_claw.local.loot.cli import (
    DEFAULT_LOOT_INTERNAL_GAME_ID,
    LOOT_CLI_GAME_IDENTIFIERS,
    LOOT_FAILURE_KIND_PRECONDITION,
    LOOT_FAILURE_KIND_TIMEOUT,
    LOOT_FAILURE_KINDS,
    LOOTConfig,
    LOOTNotFoundError,
    LOOTPreconditionError,
    LOOTRunner,
    LOOTTimeoutError,
)
from sky_claw.local.mo2.vfs_arranque import abortar_por_identidad, nombre_de_log_del_worker
from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, verify_vfs_attestation
from sky_claw.local.mo2.vfs_contracts import (
    ALLOWED_VFS_SESSION_TOOL_IDS,
    SYNTHESIS_CLI_EXECUTABLE_NAMES,
    VFS_PROTOCOL_VERSION,
    VFS_TOOL_EXECUTABLE_NAMES,
    JsonValue,
    VfsJobResult,
    VfsSessionEvent,
    VfsSessionEventError,
    VfsToolExitEvent,
    VfsToolStartedEvent,
)
from sky_claw.local.mo2.vfs_ipc import VfsFrameError, read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_job_identity import (
    JobIdentityError,
    PruebaDeJob,
    verificar_identidad_del_job,
)
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest, read_worker_manifest
from sky_claw.local.tools._process import kill_and_reap
from sky_claw.local.tools.patcher_pipeline import PatcherPipeline, PatcherPipelineError
from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisNotFoundError,
    SynthesisRunner,
    SynthesisTimeoutError,
)
from sky_claw.logging_config import (
    default_log_dir,
    setup_logging,
    shutdown_logging,
    subprocess_error_extra,
)
from sky_claw.sin_consola import creationflags_sin_consola

logger = logging.getLogger(__name__)

_GRANDCHILD_TIMEOUT_SECONDS = 10.0
_MAX_DESCRIPTOR_BYTES = 64 * 1024
_RESULT_ACK_TIMEOUT_SECONDS = 5.0
SYNTHESIS_FAILURE_KINDS: frozenset[str] = frozenset({"timeout", "not_found", "execution_error"})
_SYNTHESIS_PLUGIN_SUFFIXES: tuple[str, ...] = (".esp", ".esm", ".esl")
_SYNTHESIS_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "synthesis_exe",
        "output_path",
        "pipeline_settings_path",
        "synthesis_profile",
        "patcher_ids",
        "expected_plugins",
    }
)
_SYNTHESIS_OPTIONAL_KEYS: frozenset[str] = frozenset(
    {
        "extra_data_folder",
        "working_dir",
        "temp_dir",
        "dotnet_root",
        "persistence_mode",
        "persistence_path",
        "target_runtime",
        "block_building_within_mo2",
        "shortcircuit",
        "require_patcher_receipts",
        "expected_settings_fingerprint",
        "expected_load_order_fingerprint",
        # PR-3g (ADR 0019): ID de patcher → shim de la Fase 1; ver _synthesis_cli_executables.
        "cli_executables",
    }
)
#: Plazo MÁXIMO para que el bridge confirme la supervisión. Es una cota de FALLO CERRADO,
#: no un sustituto de la garantía: vencido el plazo el worker ABORTA sin haber creado el nieto de
#: la attestation ni despachado la herramienta; jamás prosigue "por las dudas". La confirmación real llega en milisegundos.
_SUPERVISION_TIMEOUT_SECONDS = 30.0
#: Captura por stream de un proceso de sesión: COLA acotada, no buffer completo.
#: Un tool charlatán no puede inflar el ``VfsJobResult`` (frame IPC de 1 MiB) ni
#: la memoria del worker; el diagnóstico conserva los últimos bytes.
_MAX_CAPTURA_EN_BYTES = 64 * 1024
#: Tras la salida del hijo directo, un nieto puede retener el pipe heredado: el
#: drenaje espera EOF sólo esta gracia y después se cancela.
_DRENAJE_GRACIA_SEGUNDOS = 2.0
#: Sondeo del ``returncode`` para detectar la salida del hijo directo (ver
#: :func:`_esperar_salida_del_proceso_directo`). Corto: la gracia de drenaje
#: domina la latencia y el costo es un wakeup cada 100 ms.
_SONDEO_DE_SALIDA_SEGUNDOS = 0.1
#: Windows ``CREATE_NO_WINDOW`` — sin consola parpadeante para tools de consola.
_CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True, slots=True)
class VfsBrokerDescriptor:
    host: str
    port: int
    secret: bytes
    instance_id: str
    session_id: str
    expires_at: float


class VfsWorkerBootstrapError(RuntimeError):
    """Descriptor/manifiesto inválido o conexión imposible al broker."""


def load_broker_descriptor(path: pathlib.Path) -> VfsBrokerDescriptor:
    """Lee el endpoint owner-only y rechaza hosts remotos o sesiones vencidas."""
    try:
        if path.is_symlink():
            raise VfsWorkerBootstrapError("el descriptor no puede ser un symlink")
        if path.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise VfsWorkerBootstrapError("el descriptor excede el tamaño permitido")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except VfsWorkerBootstrapError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VfsWorkerBootstrapError(f"no se pudo leer el descriptor: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("protocol_version") != VFS_PROTOCOL_VERSION:
        raise VfsWorkerBootstrapError("descriptor incompatible")
    host = raw.get("host")
    port = raw.get("port")
    token = raw.get("token")
    instance_id = raw.get("instance_id")
    session_id = raw.get("session_id")
    expires_at = raw.get("expires_at")
    if host != "127.0.0.1":
        raise VfsWorkerBootstrapError("el broker debe escuchar solo en loopback IPv4")
    if type(port) is not int or not 1 <= port <= 65_535:
        raise VfsWorkerBootstrapError("puerto inválido en descriptor")
    if not all(isinstance(value, str) and value for value in (token, instance_id, session_id)):
        raise VfsWorkerBootstrapError("descriptor incompleto")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise VfsWorkerBootstrapError("expires_at inválido en descriptor")
    if float(expires_at) < time.time():
        raise VfsWorkerBootstrapError("descriptor de broker vencido")
    assert isinstance(token, str)
    try:
        secret = base64.b64decode(token, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise VfsWorkerBootstrapError("token inválido en descriptor") from exc
    if len(secret) < 32:
        raise VfsWorkerBootstrapError("token demasiado corto en descriptor")
    assert isinstance(instance_id, str)
    assert isinstance(session_id, str)
    return VfsBrokerDescriptor(
        host=host,
        port=port,
        secret=secret,
        instance_id=instance_id,
        session_id=session_id,
        expires_at=float(expires_at),
    )


class GrandchildProbe(Protocol):
    def __call__(
        self,
        path: pathlib.Path,
        sha256: str,
        timeout: float,
    ) -> Awaitable[str]: ...


@dataclass(frozen=True, slots=True)
class VfsToolExecution:
    """Salida interna de un handler, luego envuelta en ``VfsJobResult``."""

    success: bool
    message: str
    exit_code: int | None
    stdout: str
    stderr: str
    outputs: tuple[pathlib.Path, ...]
    tool_result: dict[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def ok(cls) -> VfsToolExecution:
        return cls(
            success=True,
            message="",
            exit_code=0,
            stdout="",
            stderr="",
            outputs=(),
        )


VfsToolHandler: TypeAlias = Callable[[VfsWorkerManifest], Awaitable[VfsToolExecution]]


class VfsWorkerEventSink(Protocol):
    """Canal de eventos mid-job de una sesión, atado al job del worker."""

    @property
    def job_id(self) -> str: ...

    async def emit(self, event: VfsSessionEvent) -> None: ...


VfsSessionToolHandler: TypeAlias = Callable[[VfsWorkerManifest, VfsWorkerEventSink], Awaitable[VfsToolExecution]]


@dataclass(slots=True)
class _SinkDeEventos:
    """Sink real: serializa eventos por el socket autenticado bajo write lock."""

    job_id: str
    writer: asyncio.StreamWriter
    secret: bytes
    write_lock: asyncio.Lock

    async def emit(self, event: VfsSessionEvent) -> None:
        if event.job_id != self.job_id:
            raise VfsSessionEventError(f"el evento apunta a otro job: {event.job_id!r}")
        async with self.write_lock:
            await write_authenticated_message(self.writer, event.to_dict(), self.secret)


def _failure(
    manifest: VfsWorkerManifest,
    message: str,
    *,
    attestation: dict[str, JsonValue] | None = None,
    tool_result: dict[str, JsonValue] | None = None,
) -> VfsJobResult:
    return VfsJobResult(
        protocol_version=VFS_PROTOCOL_VERSION,
        job_id=manifest.job.job_id,
        success=False,
        message=message,
        exit_code=None,
        stdout="",
        stderr=message,
        outputs=(),
        rollback_state="not_started",
        attestation=attestation,
        tool_result=tool_result or {},
    )


def _expected_plugins_visibility(manifest: VfsWorkerManifest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Comprueba el conjunto completo esperado dentro del Data virtual.

    La lista viene validada y firmada dentro del VfsJob. Se vuelve a tratar
    fail-closed en el worker: cualquier forma inesperada o error de enumeración
    impide el dispatch de la herramienta.
    """
    raw = manifest.job.payload.get("expected_plugins")
    if raw is None:
        return (), ()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise ValueError("payload.expected_plugins inválido en worker")
    try:
        visible_keys = {
            entry.name.lower()
            for entry in manifest.virtual_data_dir.iterdir()
            if link_kind(entry) is None and entry.is_file()
        }
    except OSError as exc:
        raise OSError(f"no se pudo enumerar el Data virtual para verificar plugins: {exc}") from exc
    expected = tuple(item for item in raw if isinstance(item, str))
    visible = tuple(name for name in expected if name.lower() in visible_keys)
    missing = tuple(name for name in expected if name.lower() not in visible_keys)
    return visible, missing


async def execute_worker_manifest(
    manifest: VfsWorkerManifest,
    *,
    handlers: Mapping[str, VfsToolHandler] | None = None,
    session_handlers: Mapping[str, VfsSessionToolHandler] | None = None,
    grandchild_probe: GrandchildProbe | None = None,
    event_sink: VfsWorkerEventSink | None = None,
) -> VfsJobResult:
    """Attesta worker+nieto y solo entonces despacha la herramienta allowlisted.

    ``session_handlers`` recibe el sink de eventos (procesos vivos de PR-586A);
    ``handlers`` conserva la firma histórica de un argumento. Un tool_id presente
    en ambos mapas es un dispatch ambiguo y falla cerrado; un handler de sesión
    sin ``event_sink`` cableado también (perdería PID y exit code en silencio).
    """
    try:
        proof = await asyncio.to_thread(
            verify_vfs_attestation,
            challenge=manifest.challenge,
            data_root=manifest.data_root,
            mods_dir=manifest.mods_dir,
            profile=manifest.job.profile,
            virtual_data_dir=manifest.virtual_data_dir,
        )
    except VfsAttestationError as exc:
        return _failure(manifest, f"attestation VFS falló: {exc}")

    attestation: dict[str, JsonValue] = dict(proof.to_dict())
    canary_path = manifest.virtual_data_dir / pathlib.Path(*manifest.challenge.relative_path.parts)
    probe = grandchild_probe or run_grandchild_probe
    try:
        child_sha = await probe(
            canary_path,
            manifest.challenge.sha256,
            min(_GRANDCHILD_TIMEOUT_SECONDS, manifest.job.timeout_seconds),
        )
    except (OSError, RuntimeError, TimeoutError) as exc:
        return _failure(
            manifest,
            f"attestation del proceso nieto falló: {exc}",
            attestation=attestation,
        )
    if child_sha != manifest.challenge.sha256:
        return _failure(
            manifest,
            "attestation del proceso nieto devolvió un hash diferente",
            attestation=attestation,
        )
    attestation["grandchild_sha256"] = child_sha

    visibility_evidence: dict[str, JsonValue] = {}
    try:
        visible_plugins, missing_plugins = _expected_plugins_visibility(manifest)
    except (OSError, ValueError) as exc:
        return _failure(
            manifest,
            f"no se pudo demostrar la visibilidad completa del load order bajo USVFS: {exc}",
            attestation=attestation,
        )
    if "expected_plugins" in manifest.job.payload:
        visibility_evidence = {
            "visible_plugins": list(visible_plugins),
            "missing_plugins": list(missing_plugins),
        }
        if missing_plugins:
            return _failure(
                manifest,
                "load order incompleto bajo USVFS: faltan plugin(s) activos en el Data virtual: "
                + ", ".join(missing_plugins),
                attestation=attestation,
                tool_result=visibility_evidence,
            )

    tool_id = manifest.job.tool_id
    seleccionados_sesion = dict(session_handlers) if session_handlers is not None else _default_session_handlers()
    seleccionados_planos = dict(handlers) if handlers is not None else _default_handlers()
    if handlers is not None and session_handlers is not None:
        ambiguos = sorted(set(handlers) & set(session_handlers))
        if ambiguos:
            # Ambigüedad = el CALLER declaró el mismo tool_id en los dos mapas;
            # un override silencioso escondería cuál de los dos corre. Los
            # defaults no cuentan: el registro de sesión productivo nace vacío y
            # un handler de sesión explícito sí puede tomar un id allowlisted
            # (es la única forma de ejercitar la primitive con health/loot_sort).
            return _failure(
                manifest,
                f"dispatch ambiguo para {ambiguos}: está en handlers y en session_handlers",
                attestation=attestation,
            )
    handler_sesion = seleccionados_sesion.get(tool_id)
    if handler_sesion is not None:
        if event_sink is None:
            # Un sink no-op aparentaría éxito con PID y exit_code perdidos.
            return _failure(
                manifest,
                f"handler de sesión {tool_id!r} sin sink de eventos cableado",
                attestation=attestation,
            )
        invocacion = functools.partial(handler_sesion, manifest, event_sink)
    else:
        handler = seleccionados_planos.get(tool_id)
        if handler is None:
            return _failure(
                manifest,
                f"handler no disponible para tool_id allowlisted {tool_id!r}",
                attestation=attestation,
            )
        invocacion = functools.partial(handler, manifest)
    try:
        execution = await invocacion()
    except (LOOTNotFoundError, LOOTTimeoutError, OSError, ValueError, RuntimeError) as exc:
        return _failure(
            manifest,
            f"la herramienta falló antes de producir resultado: {exc}",
            attestation=attestation,
        )
    return VfsJobResult(
        protocol_version=VFS_PROTOCOL_VERSION,
        job_id=manifest.job.job_id,
        success=execution.success,
        message=execution.message,
        exit_code=execution.exit_code,
        stdout=execution.stdout,
        stderr=execution.stderr,
        outputs=tuple(path.resolve() for path in execution.outputs),
        rollback_state="not_required" if execution.success else "pending",
        attestation=attestation,
        tool_result={**execution.tool_result, **visibility_evidence},
    )


async def _health_handler(_manifest: VfsWorkerManifest) -> VfsToolExecution:
    return VfsToolExecution(
        success=True,
        message="",
        exit_code=0,
        stdout="VFS health attestation succeeded",
        stderr="",
        outputs=(),
    )


def _payload_string(payload: Mapping[str, JsonValue], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"payload.{field_name} debe ser un string no vacío")
    return value


def _loot_failure_execution(
    *,
    kind: str,
    message: str,
    outputs: tuple[pathlib.Path, ...],
) -> VfsToolExecution:
    """PR-2: fallo tipado de LOOT sin ``LOOTResult`` (ver ``LOOT_FAILURE_KINDS``)."""
    if kind not in LOOT_FAILURE_KINDS:
        raise ValueError(f"loot_failure_kind no permitido: {kind!r}")
    return VfsToolExecution(
        success=False,
        message=message,
        exit_code=None,
        stdout="",
        stderr=message,
        outputs=outputs,
        tool_result={"loot_failure_kind": kind},
    )


def _synthesis_failure_execution(
    *,
    kind: str,
    message: str,
    outputs: tuple[pathlib.Path, ...],
) -> VfsToolExecution:
    """Fallo tipado de Synthesis sin ``SynthesisResult`` (ver ``SYNTHESIS_FAILURE_KINDS``)."""
    if kind not in SYNTHESIS_FAILURE_KINDS:
        raise ValueError(f"synthesis_failure_kind no permitido: {kind!r}")
    return VfsToolExecution(
        success=False,
        message=message,
        exit_code=None,
        stdout="",
        stderr=message,
        outputs=outputs,
        tool_result={"synthesis_failure_kind": kind},
    )


def _optional_payload_path(payload: Mapping[str, JsonValue], field: str) -> pathlib.Path | None:
    if field not in payload or payload[field] is None:
        return None
    return _session_payload_path(payload, field)


def _optional_payload_str(payload: Mapping[str, JsonValue], field: str) -> str | None:
    if field not in payload or payload[field] is None:
        return None
    return _payload_string(payload, field)


def _optional_payload_bool(payload: Mapping[str, JsonValue], field: str, *, default: bool) -> bool:
    if field not in payload:
        return default
    value = payload[field]
    if type(value) is not bool:
        raise ValueError(f"payload.{field} debe ser bool")
    return value


def _synthesis_patcher_ids(payload: Mapping[str, JsonValue]) -> list[str]:
    raw = payload.get("patcher_ids")
    if not isinstance(raw, list) or not raw:
        raise ValueError("payload.patcher_ids debe ser una lista no vacía de strings")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("payload.patcher_ids solo admite strings no vacíos")
        result.append(item)
    return result


def _validar_ruta_worker_sin_enlaces(path: pathlib.Path, *, field: str) -> pathlib.Path:
    """Rechaza rutas relativas, traversal y symlinks/junctions antes de ``resolve()``."""
    if not path.is_absolute():
        raise ValueError(f"{field} debe ser una ruta absoluta")
    if ".." in path.parts:
        raise ValueError(f"{field} contiene traversal ('..') prohibido")
    for candidate in (path, *path.parents):
        if candidate.parent == candidate:
            continue
        kind = link_kind(candidate)
        if kind is not None:
            raise ValueError(f"{field} atraviesa un {kind} prohibido ({candidate})")
    return path.resolve()


def _synthesis_payload_path(payload: Mapping[str, JsonValue], field: str) -> pathlib.Path:
    raw = _payload_string(payload, field)
    return _validar_ruta_worker_sin_enlaces(pathlib.Path(raw), field=f"payload.{field}")


def _optional_synthesis_payload_path(payload: Mapping[str, JsonValue], field: str) -> pathlib.Path | None:
    if field not in payload or payload[field] is None:
        return None
    return _synthesis_payload_path(payload, field)


def _synthesis_cli_executables(
    payload: Mapping[str, JsonValue],
    validator: PathValidator,
) -> dict[str, pathlib.Path] | None:
    """Valida el mapa ``ID de patcher → shim`` de la Fase 1 (PR-3g, ADR 0019).

    El shim es lo que Synthesis va a ejecutar bajo USVFS en lugar del patcher, así que cada ruta pasa
    las mismas compuertas que el resto del payload (absoluta, sin traversal ni enlaces, confinada a las
    raíces autorizadas) y además tiene que tener la FORMA de un shim materializado: el ejecutable con
    su nombre fijo y su ``shim.json`` regular al lado. Que el mapa cubra exactamente los patchers
    seleccionados lo exige el traductor (``prepare_synthesis_cli_settings``) dentro del runner.
    """
    # Import perezoso: el shim arrastra pefile y el worker no lo necesita salvo con este mapa.
    from sky_claw.local.tools.synthesis_shim import NOMBRE_CONFIG_SHIM, NOMBRE_EJECUTABLE_SHIM

    if "cli_executables" not in payload or payload["cli_executables"] is None:
        return None
    raw = payload["cli_executables"]
    if not isinstance(raw, dict) or not raw:
        raise ValueError("payload.cli_executables debe ser un objeto no vacío {ID de patcher: ruta del shim}")
    result: dict[str, pathlib.Path] = {}
    for clave, valor in raw.items():
        campo = f"payload.cli_executables[{clave!r}]"
        if not isinstance(clave, str) or not clave.strip():
            raise ValueError("payload.cli_executables solo admite IDs de patcher no vacíos")
        if not isinstance(valor, str) or not valor:
            raise ValueError(f"{campo} debe ser una ruta (string no vacío)")
        ruta = _validar_ruta_worker_sin_enlaces(pathlib.Path(valor), field=campo)
        try:
            validator.validate(ruta, strict_symlink=True)
        except PathViolationError as exc:
            raise ValueError(f"{campo} fuera de raíces autorizadas: {exc}") from exc
        if ruta.name.casefold() != NOMBRE_EJECUTABLE_SHIM.casefold():
            raise ValueError(f"{campo} debe apuntar a {NOMBRE_EJECUTABLE_SHIM}, no a {ruta.name!r}")
        if link_kind(ruta) is not None or not ruta.is_file():
            raise ValueError(f"{campo} debe ser un archivo regular real y no un enlace")
        config_shim = ruta.parent / NOMBRE_CONFIG_SHIM
        if link_kind(config_shim) is not None or not config_shim.is_file():
            raise ValueError(f"{campo} no tiene su {NOMBRE_CONFIG_SHIM} regular al lado")
        result[clave] = ruta
    return result


def _active_input_plugins_from_load_order(
    load_order_path: pathlib.Path,
    *,
    excluded_groups: tuple[str, ...],
) -> tuple[str, ...]:
    """Lee plugins activos en ``plugins.txt``, excluyendo outputs propios del grupo."""
    try:
        raw_text = load_order_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ValueError(f"no se pudo leer {load_order_path}: {exc}") from exc
    plugins: list[str] = []
    seen: set[str] = set()
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith("*"):
            continue
        candidate = line[1:].strip()
        if not candidate:
            continue
        try:
            safe_name = assert_safe_component(candidate, field="plugins.txt[]")
        except PathViolationError as exc:
            raise ValueError(f"nombre de plugin inválido en plugins.txt: {candidate!r}") from exc
        if not safe_name.lower().endswith(_SYNTHESIS_PLUGIN_SUFFIXES):
            continue
        if any(SynthesisRunner._output_belongs_to_group(safe_name, grp) for grp in excluded_groups):
            continue
        key = safe_name.lower()
        if key not in seen:
            seen.add(key)
            plugins.append(safe_name)
    return tuple(plugins)


async def _synthesis_handler(manifest: VfsWorkerManifest) -> VfsToolExecution:
    """Valida el contrato cerrado de ``tool_id='synthesis'`` y ejecuta bajo USVFS."""
    if manifest.job.tool_id != "synthesis":
        raise ValueError("handler Synthesis recibió otro tool_id")

    payload = manifest.job.payload
    missing = _SYNTHESIS_REQUIRED_KEYS - set(payload)
    if missing:
        raise ValueError(f"payload de synthesis omite campo(s) requerido(s): {sorted(missing)}")
    unexpected = set(payload) - (_SYNTHESIS_REQUIRED_KEYS | _SYNTHESIS_OPTIONAL_KEYS)
    if unexpected:
        raise ValueError(f"payload de synthesis contiene campos no permitidos: {sorted(unexpected)}")
    raw_expected_plugins = payload.get("expected_plugins")
    if not isinstance(raw_expected_plugins, list) or any(
        not isinstance(item, str) or not item for item in raw_expected_plugins
    ):
        raise ValueError("payload.expected_plugins debe ser una lista de strings no vacíos")

    synthesis_exe = _synthesis_payload_path(payload, "synthesis_exe")
    if synthesis_exe.name.casefold() not in SYNTHESIS_CLI_EXECUTABLE_NAMES:
        raise ValueError(
            f"executable incompatible con synthesis: {synthesis_exe.name}; "
            f"se requiere {sorted(SYNTHESIS_CLI_EXECUTABLE_NAMES)}"
        )
    if link_kind(synthesis_exe) is not None or not synthesis_exe.is_file():
        raise ValueError("payload.synthesis_exe debe ser un archivo regular real y no un symlink")

    output_path = _synthesis_payload_path(payload, "output_path")
    declared_outputs = tuple(path.resolve() for path in manifest.job.mutation_targets)
    if len(declared_outputs) != 1 or output_path != declared_outputs[0]:
        raise ValueError("payload.output_path debe coincidir con un único mutation_targets declarado")

    pipeline_settings_path = _synthesis_payload_path(payload, "pipeline_settings_path")
    if link_kind(pipeline_settings_path) is not None or not pipeline_settings_path.is_file():
        raise ValueError("payload.pipeline_settings_path debe ser un archivo regular real y no un symlink")

    raw_load_order = manifest.data_root / "profiles" / manifest.job.profile / "plugins.txt"
    load_order_file_path = _validar_ruta_worker_sin_enlaces(raw_load_order, field="plugins.txt")
    if not load_order_file_path.is_file():
        raise ValueError("el perfil MO2 del job no tiene plugins.txt regular legible")

    synthesis_profile = _payload_string(payload, "synthesis_profile")
    patcher_ids = _synthesis_patcher_ids(payload)

    extra_data_folder = _optional_synthesis_payload_path(payload, "extra_data_folder")
    working_dir = _optional_synthesis_payload_path(payload, "working_dir")
    temp_dir = _optional_synthesis_payload_path(payload, "temp_dir")
    dotnet_root = _optional_synthesis_payload_path(payload, "dotnet_root")
    persistence_path = _optional_synthesis_payload_path(payload, "persistence_path")

    raw_persistence_mode = payload.get("persistence_mode", "None")
    if not isinstance(raw_persistence_mode, str) or raw_persistence_mode not in {"None", "Text"}:
        raise ValueError(f"payload.persistence_mode no permitido: {raw_persistence_mode!r}")
    persistence_mode: Literal["None", "Text"] = "Text" if raw_persistence_mode == "Text" else "None"
    if persistence_mode == "Text" and persistence_path is None:
        raise ValueError("payload.persistence_path es obligatorio cuando persistence_mode='Text'")

    target_runtime = _optional_payload_str(payload, "target_runtime")
    expected_settings_fp = _optional_payload_str(payload, "expected_settings_fingerprint")
    expected_lo_fp = _optional_payload_str(payload, "expected_load_order_fingerprint")
    block_building_within_mo2 = _optional_payload_bool(payload, "block_building_within_mo2", default=True)
    shortcircuit = _optional_payload_bool(payload, "shortcircuit", default=True)
    require_patcher_receipts = _optional_payload_bool(payload, "require_patcher_receipts", default=True)

    if not block_building_within_mo2:
        raise ValueError("payload.block_building_within_mo2 debe ser True en ejecución brokered Fase 2")
    if not require_patcher_receipts:
        raise ValueError("payload.require_patcher_receipts debe ser True en ejecución brokered Fase 2")

    virtual_data_dir = manifest.virtual_data_dir.resolve()
    game_path = virtual_data_dir.parent.resolve()
    sandbox_roots: list[pathlib.Path] = [
        synthesis_exe.parent,
        game_path,
        manifest.data_root,
        manifest.mods_dir,
        manifest.install_root,
        output_path.parent,
        output_path,
        manifest.descriptor_path.parent.resolve(),
        pathlib.Path(tempfile.gettempdir()).resolve(),
    ]
    validator = PathValidator(roots=sandbox_roots)
    confined_paths: tuple[tuple[str, pathlib.Path | None], ...] = (
        ("output_path", output_path),
        ("pipeline_settings_path", pipeline_settings_path),
        ("extra_data_folder", extra_data_folder),
        ("working_dir", working_dir),
        ("temp_dir", temp_dir),
        ("persistence_path", persistence_path),
    )
    for field_name, path_val in confined_paths:
        if path_val is not None:
            try:
                validator.validate(path_val, strict_symlink=True)
            except PathViolationError as exc:
                raise ValueError(f"payload.{field_name} fuera de raíces autorizadas: {exc}") from exc
    cli_executables = _synthesis_cli_executables(payload, validator)

    if dotnet_root is not None:
        dotnet_roots: list[pathlib.Path] = [*sandbox_roots]
        if os.name == "nt":
            for env_key in ("ProgramFiles", "ProgramW6432"):
                pf = os.environ.get(env_key)
                if pf:
                    dotnet_roots.append(pathlib.Path(pf) / "dotnet")
        else:
            dotnet_roots.extend((pathlib.Path("/usr/share/dotnet"), pathlib.Path("/usr/lib/dotnet")))
        dotnet_validator = PathValidator(roots=dotnet_roots)
        try:
            dotnet_validator.validate(dotnet_root, strict_symlink=True)
        except PathViolationError as exc:
            raise ValueError(f"payload.dotnet_root fuera de raíces autorizadas: {exc}") from exc

    try:
        settings_profile = PatcherPipeline.inspect_synthesis_settings(
            pipeline_settings_path,
            synthesis_profile,
            patcher_ids=patcher_ids,
        )
        required_inputs = _active_input_plugins_from_load_order(
            load_order_file_path,
            excluded_groups=settings_profile.group_names,
        )
        visible_keys = {
            entry.name.lower() for entry in virtual_data_dir.iterdir() if link_kind(entry) is None and entry.is_file()
        }
        missing_inputs = [name for name in required_inputs if name.lower() not in visible_keys]
        if missing_inputs:
            raise ValueError(
                "load order incompleto bajo USVFS: faltan plugin(s) activos de plugins.txt en el Data virtual: "
                + ", ".join(missing_inputs)
            )
        config = SynthesisConfig(
            game_path=game_path,
            mo2_path=manifest.data_root,
            output_path=output_path,
            synthesis_exe=synthesis_exe,
            timeout_seconds=max(1, int(manifest.job.timeout_seconds)),
            profile=synthesis_profile,
            pipeline_settings_path=pipeline_settings_path,
            data_folder_path=virtual_data_dir,
            load_order_file_path=load_order_file_path,
            mo2_profile=manifest.job.profile,
            extra_data_folder=extra_data_folder,
            working_dir=working_dir,
            temp_dir=temp_dir,
            dotnet_root=dotnet_root,
            persistence_mode=persistence_mode,
            persistence_path=persistence_path,
            target_runtime=target_runtime,
            block_building_within_mo2=block_building_within_mo2,
            shortcircuit=shortcircuit,
            require_patcher_receipts=require_patcher_receipts,
            cli_executables=cli_executables,
        )
        runner = SynthesisRunner(config)
        result = await runner.run_pipeline(
            patcher_ids,
            expected_settings_fingerprint=expected_settings_fp,
            expected_load_order_fingerprint=expected_lo_fp,
        )
    except PatcherPipelineError as exc:
        return _synthesis_failure_execution(
            kind="execution_error",
            message=f"Invalid Synthesis pipeline settings: {exc}",
            outputs=(),
        )
    except SynthesisTimeoutError as exc:
        return _synthesis_failure_execution(
            kind="timeout",
            message=str(exc),
            outputs=manifest.job.mutation_targets,
        )
    except SynthesisNotFoundError as exc:
        return _synthesis_failure_execution(
            kind="not_found",
            message=str(exc),
            outputs=(),
        )
    except SynthesisExecutionError as exc:
        return _synthesis_failure_execution(
            kind="execution_error",
            message=str(exc),
            outputs=manifest.job.mutation_targets,
        )

    errors = list(result.errors)
    output_esps = tuple(result.output_esps or ((result.output_esp,) if result.output_esp is not None else ()))
    success = result.success
    output_esp = result.output_esp

    if success:
        if not output_esps:
            success = False
            errors.append("Synthesis devolvió éxito sin ningún archivo ESP generado")
        else:
            for esp_file in output_esps:
                if not await runner.validate_synthesis_esp(esp_file):
                    success = False
                    errors.append(
                        f"Validación TES4 fallida para {esp_file.name}: plugin inválido, truncado o >254 masters"
                    )
        if not success:
            output_esp = None

    message = (
        "" if success else ("; ".join(errors) or result.stderr or result.stdout or "Synthesis falló dentro de USVFS")
    )
    errors_json: list[JsonValue] = list(errors)
    tool_result: dict[str, JsonValue] = {
        "output_esp": str(output_esp.resolve()) if output_esp is not None else None,
        "output_esps": [str(esp.resolve()) for esp in output_esps],
        "patchers_executed": list(result.patchers_executed),
        "errors": errors_json,
    }
    return VfsToolExecution(
        success=success,
        message=message,
        exit_code=result.return_code,
        stdout=result.stdout,
        stderr=result.stderr,
        outputs=manifest.job.mutation_targets,
        tool_result=tool_result,
    )


async def _loot_handler(manifest: VfsWorkerManifest) -> VfsToolExecution:
    payload = manifest.job.payload
    # ``expected_plugins`` (C2) lo consume el gate genérico de visibilidad ANTES
    # de este handler (``_expected_plugins_visibility``); acá sólo se admite.
    allowed = {"loot_exe", "game", "update_masterlist", "loot_data_path", "expected_plugins"}
    unexpected = set(payload) - allowed
    if unexpected:
        raise ValueError(f"payload de loot_sort contiene campos no permitidos: {sorted(unexpected)}")
    loot_exe = pathlib.Path(_payload_string(payload, "loot_exe"))
    if not loot_exe.is_absolute():
        raise ValueError("payload.loot_exe debe ser una ruta absoluta")
    # Allowlist con la MISMA fuente que el broker y el runner (PR-0): los ids
    # INTERNOS del dominio. El string de CLI ("Skyrim Special Edition") no viaja
    # por IPC y no se hardcodea en tres lugares: la traducción al dialecto de
    # `--game` la hace una sola vez el runner (to_loot_cli_game_id). Si broker y
    # worker divergieran de esta frontera, el test T3 (
    # tests/test_loot_game_identifier_contract.py) la caza.
    game = payload.get("game", DEFAULT_LOOT_INTERNAL_GAME_ID)
    if not isinstance(game, str) or game not in LOOT_CLI_GAME_IDENTIFIERS:
        raise ValueError("payload.game no está permitido")
    update_masterlist = payload.get("update_masterlist", False)
    if type(update_masterlist) is not bool:
        raise ValueError("payload.update_masterlist debe ser bool")
    # PR-1: loot_data_path es obligatorio en productivo, absoluto, no symlink
    # La decisión del PATH se toma en el daemon/control plane, NO dentro del
    # worker mediante descubrimiento ambiental implícito. El worker recibe ruta
    # explícita ya resuelta, la valida de nuevo y la pasa al runner.
    loot_data_path_raw = payload.get("loot_data_path")
    if loot_data_path_raw is None:
        raise ValueError(
            "payload.loot_data_path ausente — PR-1 fail-closed: el backend "
            "productivo NUNCA ejecuta LOOT.exe sin --loot-data-path explícito"
        )
    if not isinstance(loot_data_path_raw, str) or not loot_data_path_raw:
        raise ValueError("payload.loot_data_path debe ser un string no vacío")
    loot_data_path = pathlib.Path(loot_data_path_raw)
    if not loot_data_path.is_absolute():
        raise ValueError("payload.loot_data_path debe ser una ruta absoluta")
    if loot_data_path.is_symlink():
        raise ValueError("payload.loot_data_path no puede ser un symlink")
    # Validar que no sea el default GUI LOOT (%LOCALAPPDATA%\LOOT)
    from sky_claw.local.loot.data_root import get_default_loot_gui_data_path

    resolved_loot_data = loot_data_path.resolve(strict=False)
    default_gui = get_default_loot_gui_data_path()
    if default_gui is not None:
        try:
            if resolved_loot_data == default_gui.resolve(strict=False):
                raise ValueError("payload.loot_data_path no puede ser el default GUI LOOT")
        except ValueError:
            raise
        except Exception:
            pass
    game_path = manifest.virtual_data_dir.parent.resolve()
    # El validator incluye también el loot_data_path base para permitirlo
    validator = PathValidator(
        roots=[
            loot_exe.parent.resolve(),
            game_path,
            manifest.data_root,
            manifest.mods_dir,
            manifest.install_root,
            resolved_loot_data.parent.resolve(strict=False),
            resolved_loot_data.resolve(strict=False),
        ]
    )
    runner = LOOTRunner(
        LOOTConfig(
            loot_exe=loot_exe.resolve(),
            game_path=game_path,
            game=game,
            timeout=max(1, int(manifest.job.timeout_seconds)),
            loot_data_path=resolved_loot_data,
        ),
        path_validator=validator,
    )
    try:
        result = await runner.sort(update_masterlist=update_masterlist)
    except LOOTTimeoutError as exc:
        # PR-2: tipado explícito en vez de caer al `_failure` genérico: el daemon
        # re-lanza LOOTTimeoutError y el servicio reporta TIMEOUT igual que en
        # el camino directo. LOOT ya fue matado y reapeado por el runner, pero
        # pudo haber mutado antes de colgarse → los targets siguen declarados.
        return _loot_failure_execution(
            kind=LOOT_FAILURE_KIND_TIMEOUT, message=str(exc), outputs=manifest.job.mutation_targets
        )
    except LOOTPreconditionError as exc:
        # PR-2: versión no atestiguable, settings o testigo no preparables → LOOT NO se lanzó.
        return _loot_failure_execution(kind=LOOT_FAILURE_KIND_PRECONDITION, message=str(exc), outputs=())
    message = (
        "" if result.success else "; ".join(result.errors) or result.raw_stderr or result.raw_stdout or "LOOT falló"
    )
    tool_result: dict[str, JsonValue] = {
        "sorted_plugins": list(result.sorted_plugins),
        "warnings": list(result.warnings),
        "errors": list(result.errors),
        "missing_patches": [dict(item) for item in result.missing_patches],
    }
    if result.execution_witness is not None:
        # PR-2: el testigo cruza la frontera como DATO con schema cerrado v1
        # (LootExecutionWitness.to_payload); el daemon lo valida con
        # from_payload y NO lo vuelve a inferir. Una sola fuente de verdad.
        tool_result["execution_witness"] = dict(result.execution_witness.to_payload())
    return VfsToolExecution(
        success=result.success,
        message=message,
        exit_code=result.return_code,
        stdout=result.raw_stdout,
        stderr=result.raw_stderr,
        outputs=manifest.job.mutation_targets,
        tool_result=tool_result,
    )


def _session_payload_path(payload: Mapping[str, JsonValue], field: str) -> pathlib.Path:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"payload.{field} debe ser un string no vacío")
    path = pathlib.Path(value)
    if not path.is_absolute():
        raise ValueError(f"payload.{field} debe ser una ruta absoluta")
    # Revisar el objeto textual antes de resolver evita que un symlink quede
    # convertido en un archivo aparentemente normal y eluda la policy.
    if path.is_symlink():
        raise ValueError(f"payload.{field} no puede ser un symlink")
    return path.resolve()


def _session_argv(payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
    raw = payload.get("argv")
    if not isinstance(raw, list):
        raise ValueError("payload.argv debe ser una lista de strings")
    argv: list[str] = []
    for item in raw:
        if type(item) is not str:
            raise ValueError("payload.argv debe ser una lista de strings")
        argv.append(item)
    # No se filtran metacaracteres: create_subprocess_exec recibe cada elemento
    # separado y, por diseño, un carácter especial sigue siendo un argumento.
    return tuple(argv)


def _switch_path(argv: tuple[str, ...], prefix: str) -> pathlib.Path:
    values = [item[len(prefix) :] for item in argv if item.casefold().startswith(prefix.casefold())]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"argv debe contener exactamente un {prefix}...")
    return pathlib.Path(values[0].rstrip("\\/")).resolve()


def _validate_session_launch(manifest: VfsWorkerManifest) -> tuple[pathlib.Path, tuple[str, ...], pathlib.Path]:
    """Revalida el contrato del GUI tool dentro del worker bajo USVFS."""
    job = manifest.job
    if job.tool_id not in ALLOWED_VFS_SESSION_TOOL_IDS:
        raise ValueError(f"tool_id de sesión no permitido: {job.tool_id!r}")
    payload = job.payload
    executable = _session_payload_path(payload, "executable")
    expected_name = VFS_TOOL_EXECUTABLE_NAMES[job.tool_id]
    if executable.name.casefold() != expected_name:
        raise ValueError(f"executable incompatible con {job.tool_id}: {executable.name}")
    if executable.is_symlink() or not executable.is_file():
        raise ValueError("el executable brokered debe ser un archivo real y no un symlink")
    argv = _session_argv(payload)
    game_modes = [item.casefold() for item in argv if item.casefold() in {"-sse", "-tes5vr"}]
    if len(game_modes) != 1:
        raise ValueError("argv debe declarar exactamente un game mode (-sse o -tes5vr)")
    for prefix in ("-d:", "-m:", "-p:", "-t:", "-o:"):
        _switch_path(argv, prefix)
    data_arg = _switch_path(argv, "-d:")
    if data_arg != manifest.virtual_data_dir.resolve():
        raise ValueError("argv.-d no coincide con virtual_data_dir del job")
    profile_plugins = (manifest.data_root / "profiles" / job.profile / "plugins.txt").resolve()
    if _switch_path(argv, "-p:") != profile_plugins:
        raise ValueError("argv.-p no corresponde al perfil del job")
    ini_dir = _switch_path(argv, "-m:")
    if not ini_dir.is_dir() or not (ini_dir / "Skyrim.ini").is_file():
        raise ValueError("argv.-m debe apuntar a una carpeta con Skyrim.ini")
    output_arg = _switch_path(argv, "-o:")
    declared_outputs = tuple(path.resolve() for path in job.mutation_targets)
    if len(declared_outputs) != 1 or output_arg != declared_outputs[0]:
        raise ValueError("argv.-o no coincide con el output root declarado por el job")
    cwd = _session_payload_path(payload, "cwd")
    if cwd.is_symlink() or not cwd.is_dir():
        raise ValueError("payload.cwd debe ser una carpeta real")
    return executable, argv, cwd


async def _session_tool_handler(
    manifest: VfsWorkerManifest,
    event_sink: VfsWorkerEventSink,
) -> VfsToolExecution:
    executable, argv, cwd = _validate_session_launch(manifest)
    outcome = await run_brokered_process(
        VfsProcessSpec(executable=executable, arguments=argv, cwd=cwd),
        event_sink=event_sink,
    )
    # Este ``success`` no es el veredicto final del ritual: expresa únicamente
    # que el proceso GUI devolvió 0. La captura truncada es diagnóstico y viaja
    # en ``tool_result``; el daemon sigue cruzando exit code con log y artefacto.
    proceso_ok = outcome.exit_code == 0
    return VfsToolExecution(
        success=proceso_ok,
        message="" if outcome.exit_code == 0 else f"{manifest.job.tool_id} terminó con código {outcome.exit_code}",
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        outputs=manifest.job.mutation_targets,
        tool_result={
            "stdout_truncated": outcome.stdout_truncated,
            "stderr_truncated": outcome.stderr_truncated,
        },
    )


async def _texgen_session_handler(
    manifest: VfsWorkerManifest,
    event_sink: VfsWorkerEventSink,
) -> VfsToolExecution:
    if manifest.job.tool_id != "texgen":
        raise ValueError("handler TexGen recibió otro tool_id")
    return await _session_tool_handler(manifest, event_sink)


async def _dyndolod_session_handler(
    manifest: VfsWorkerManifest,
    event_sink: VfsWorkerEventSink,
) -> VfsToolExecution:
    if manifest.job.tool_id != "dyndolod":
        raise ValueError("handler DynDOLOD recibió otro tool_id")
    return await _session_tool_handler(manifest, event_sink)


# ---------------------------------------------------------------------------
# Primitive de proceso vivo de una sesión (PR-586A)
# ---------------------------------------------------------------------------
#
# El worker posee el spawn bajo USVFS; el daemon posee la decisión. Esta
# primitive es lo único que hace falta del lado del worker para sostener esa
# asimetría: publica el PID con el proceso vivo, acota la captura, garantiza el
# reap y NO agrega un reloj propio (el deadline es el timeout del job y la
# cancelación llega por el canal autenticado del worker).


@dataclass(frozen=True, slots=True)
class VfsProcessSpec:
    """Proceso a lanzar dentro del worker hookeado por USVFS."""

    executable: pathlib.Path
    arguments: tuple[str, ...] = ()
    cwd: pathlib.Path | None = None


@dataclass(frozen=True, slots=True)
class VfsProcessOutcome:
    """Desenlace de :func:`run_brokered_process` (captura de cola acotada).

    ``*_truncated`` significa **captura incompleta**, por cualquiera de las dos
    causas: se superó el límite de bytes del buffer, o el drenaje se canceló
    antes del EOF (gracia agotada por un descendiente que retiene el pipe). Un
    stream que llegó a EOF dentro del límite queda en ``False``.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _CapturaAcotada:
    """Buffer de cola con tope: conserva los ÚLTIMOS bytes (diagnóstico)."""

    __slots__ = ("_buffer", "_limite", "truncada")

    def __init__(self, limite: int) -> None:
        self._buffer = bytearray()
        self._limite = limite
        self.truncada = False

    def agregar(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        if len(self._buffer) > self._limite:
            self.truncada = True
            del self._buffer[: len(self._buffer) - self._limite]

    def texto(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


async def _drenar(stream: asyncio.StreamReader | None, captura: _CapturaAcotada) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            captura.agregar(chunk)
    except asyncio.CancelledError:
        # Cancelar el drenaje (gracia agotada o teardown) deja la captura
        # INCOMPLETA aunque no se haya superado el límite de bytes: el
        # consumidor tiene que poder distinguirlo de un EOF real.
        captura.truncada = True
        raise


async def _drenar_con_gracia(drenajes: list[asyncio.Task[None]]) -> None:
    """Espera EOF de los drenajes; si un descendiente retiene el pipe, los cancela.

    Se invoca recién cuando el hijo directo terminó. Al cancelar, ``_drenar``
    marca la captura del stream como incompleta: la gracia acota la espera, no
    la disimula.
    """
    try:
        await asyncio.wait_for(asyncio.gather(*drenajes), timeout=_DRENAJE_GRACIA_SEGUNDOS)
    except TimeoutError:
        for drenaje in drenajes:
            drenaje.cancel()
        await asyncio.gather(*drenajes, return_exceptions=True)


async def _esperar_salida_del_proceso_directo(proc: asyncio.subprocess.Process) -> int:
    """Espera SÓLO la terminación del hijo directo, sin depender del EOF de pipes.

    ``await proc.wait()`` NO sirve como señal de vida del proceso: en
    ``asyncio.base_subprocess`` los waiters de ``wait()`` se resuelven en
    ``_call_connection_lost``, y ``_try_finish`` sólo lo invoca cuando TODOS los
    pipes quedaron desconectados. Un descendiente que herede stdout estira ese
    EOF —y con él la espera—, así que un nieto podría mantener "vivo" al job
    artificialmente. ``proc.returncode``, en cambio, lo setea ``_process_exited``
    al detectar la terminación real del hijo (child watcher en POSIX, handle del
    proceso en Windows): se observa ese valor con un sondeo corto sobre API
    pública, sin tocar internals de asyncio y sin relojes sobre la vida del tool.
    """
    while proc.returncode is None:
        await asyncio.sleep(_SONDEO_DE_SALIDA_SEGUNDOS)
    return proc.returncode


def _liberar_pipes_del_hijo(proc: asyncio.subprocess.Process) -> None:
    """Cierra el transporte del subproceso para liberar pipes retenidos.

    ``asyncio.subprocess.Process`` no expone API pública para cerrar sus pipes:
    el dueño de los handles es el transporte, y si un descendiente retiene la
    punta de escritura, ``_try_finish`` nunca lo cierra solo (es el mismo motivo
    por el que ``wait()`` esperaba el EOF). ``BaseSubprocessTransport.close()`` es
    idempotente, no bloquea y sólo mata al hijo si TODAVÍA vive — acá ya terminó.
    El acceso es defensivo porque ``_transport`` no es parte del contrato público.
    """
    transporte = getattr(proc, "_transport", None)
    cerrar = getattr(transporte, "close", None)
    if callable(cerrar):
        cerrar()


async def _reap_sin_cancelables(proc: asyncio.subprocess.Process) -> None:
    """``kill_and_reap`` blindado: completa el teardown aunque al caller lo cancelen."""
    limpieza = asyncio.ensure_future(kill_and_reap(proc))
    cancelada = False
    while not limpieza.done():
        try:
            await asyncio.shield(limpieza)
        except asyncio.CancelledError:
            cancelada = True
    if cancelada:
        raise asyncio.CancelledError


async def run_brokered_process(
    spec: VfsProcessSpec,
    *,
    event_sink: VfsWorkerEventSink,
    limite_captura_bytes: int = _MAX_CAPTURA_EN_BYTES,
) -> VfsProcessOutcome:
    """Lanza y espera un proceso bajo la USVFS del worker, publicando su PID.

    ``tool_started`` se emite con el proceso YA vivo y ``tool_exit`` con su
    código real. En cancelación del handler (el broker manda ``cancel``), el
    proceso se mata y se reapea antes de propagar; el Job Object del bridge
    sigue siendo el backstop duro del árbol completo.
    """
    if not spec.executable.is_absolute():
        raise ValueError("el ejecutable de la sesión debe ser una ruta absoluta")
    if limite_captura_bytes <= 0:
        raise ValueError("limite_captura_bytes debe ser positivo")

    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if spec.cwd is not None:
        kwargs["cwd"] = str(spec.cwd)
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW

    inicio = time.monotonic()
    proc = await asyncio.create_subprocess_exec(str(spec.executable), *spec.arguments, **kwargs)
    captura_out = _CapturaAcotada(limite_captura_bytes)
    captura_err = _CapturaAcotada(limite_captura_bytes)
    drenajes = [
        asyncio.create_task(_drenar(proc.stdout, captura_out)),
        asyncio.create_task(_drenar(proc.stderr, captura_err)),
    ]
    try:
        await event_sink.emit(VfsToolStartedEvent(job_id=event_sink.job_id, tool_pid=proc.pid))
        try:
            # Secuencia explícita: drenajes concurrentes → terminación REAL del
            # hijo directo → recién ahí la gracia del drenaje. El deadline del
            # job (backstop del broker) sigue siendo el único reloj de vida.
            await _esperar_salida_del_proceso_directo(proc)
        except asyncio.CancelledError:
            with contextlib.suppress(asyncio.CancelledError):
                await _reap_sin_cancelables(proc)
            raise
        await _drenar_con_gracia(drenajes)
        exit_code = proc.returncode if proc.returncode is not None else -1
        await event_sink.emit(VfsToolExitEvent(job_id=event_sink.job_id, exit_code=exit_code))
        return VfsProcessOutcome(
            exit_code=exit_code,
            stdout=captura_out.texto(),
            stderr=captura_err.texto(),
            duration_seconds=time.monotonic() - inicio,
            stdout_truncated=captura_out.truncada,
            stderr_truncated=captura_err.truncada,
        )
    finally:
        for drenaje in drenajes:
            if not drenaje.done():
                drenaje.cancel()
        await asyncio.gather(*drenajes, return_exceptions=True)
        if proc.returncode is None:
            with contextlib.suppress(asyncio.CancelledError):
                await _reap_sin_cancelables(proc)
        # Si el drenaje se canceló por gracia (o por teardown), el transporte
        # puede quedar abierto con la punta retenida por un descendiente: se
        # libera explícitamente en vez de esperar al GC con un ResourceWarning.
        _liberar_pipes_del_hijo(proc)


def _default_handlers() -> dict[str, VfsToolHandler]:
    return {
        "health": _health_handler,
        "loot_sort": _loot_handler,
        "synthesis": _synthesis_handler,
    }


def _default_session_handlers() -> dict[str, VfsSessionToolHandler]:
    """Handlers cerrados de las dos herramientas GUI migradas en PR-586B."""
    return {
        "texgen": _texgen_session_handler,
        "dyndolod": _dyndolod_session_handler,
    }


def _grandchild_command(path: pathlib.Path, expected_sha256: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--vfs-probe-child", str(path), expected_sha256]
    return [
        sys.executable,
        "-m",
        "sky_claw.local.mo2.vfs_worker",
        "--probe-child",
        str(path),
        expected_sha256,
    ]


async def run_grandchild_probe(
    path: pathlib.Path,
    expected_sha256: str,
    timeout: float,
) -> str:
    """Crea un nieto real; USVFS debe inyectarlo mediante el worker hookeado."""
    proc = await asyncio.create_subprocess_exec(
        *_grandchild_command(path, expected_sha256),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags_sin_consola(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):  # noqa: BLE001 - reap best-effort en cleanup
            await proc.wait()
        raise
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"probe nieto terminó con código {proc.returncode}: {detail}")
    observed = stdout.decode("ascii", errors="strict").strip()
    if observed != expected_sha256:
        raise RuntimeError("probe nieto devolvió un hash inesperado")
    return observed


def run_probe_child(path: pathlib.Path, expected_sha256: str) -> int:
    """Entry point mínimo del proceso nieto usado por la attestation."""
    try:
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return 2
    if observed != expected_sha256:
        return 3
    sys.stdout.write(observed)
    sys.stdout.flush()
    return 0


async def _esperar_supervision(
    reader: asyncio.StreamReader,
    *,
    secret: bytes,
    job_id: str,
    timeout: float,
) -> str:
    """Espera la confirmación de supervisión del broker ANTES del nieto de la attestation y de la herramienta.

    Devuelve ``"supervised"`` (el bridge asignó este worker al Job Object) o
    ``"cancelled"`` (el job se canceló mientras esperaba; no se despacha nada). Todo
    lo demás —plazo vencido, EOF, mensaje de otro job, tipo inesperado, versión
    incompatible— es un fallo CERRADO: ``VfsWorkerBootstrapError`` sin nieto ni herramienta.
    """
    try:
        mensaje = await asyncio.wait_for(read_authenticated_message(reader, secret), timeout=timeout)
    except TimeoutError as exc:
        raise VfsWorkerBootstrapError(
            f"supervisión no confirmada por el bridge en {timeout:g}s: el worker aborta sin crear el nieto de la attestation ni despachar la herramienta"
        ) from exc
    except (asyncio.IncompleteReadError, ConnectionError, OSError, VfsFrameError) as exc:
        raise VfsWorkerBootstrapError(
            f"se cortó el canal con el broker antes de confirmarse la supervisión: {exc}"
        ) from exc
    if mensaje.get("protocol_version") != VFS_PROTOCOL_VERSION:
        raise VfsWorkerBootstrapError("mensaje incompatible mientras se esperaba la supervisión")
    if mensaje.get("job_id") != job_id:
        raise VfsWorkerBootstrapError("la confirmación de supervisión corresponde a otro job: se rechaza")
    tipo = mensaje.get("type")
    if tipo == "supervised":
        return "supervised"
    if tipo == "cancel":
        return "cancelled"
    raise VfsWorkerBootstrapError(f"mensaje no permitido mientras se esperaba la supervisión: {tipo!r}")


async def _wait_for_cancel(
    reader: asyncio.StreamReader,
    *,
    secret: bytes,
    job_id: str,
) -> None:
    while True:
        message = await read_authenticated_message(reader, secret)
        if message.get("protocol_version") != VFS_PROTOCOL_VERSION:
            raise VfsWorkerBootstrapError("mensaje incompatible recibido por el worker")
        if message.get("type") != "cancel" or message.get("job_id") != job_id:
            raise VfsWorkerBootstrapError("mensaje no permitido recibido por el worker")
        return


async def run_worker_session(
    *,
    manifest_path: pathlib.Path,
    descriptor_path: pathlib.Path,
    expected_job_id: str,
    job_identity: PruebaDeJob,
    handlers: Mapping[str, VfsToolHandler] | None = None,
    session_handlers: Mapping[str, VfsSessionToolHandler] | None = None,
    grandchild_probe: GrandchildProbe | None = None,
    supervision_timeout: float = _SUPERVISION_TIMEOUT_SECONDS,
) -> VfsJobResult | None:
    """Ejecuta el worker y reporta al daemon; ``None`` significa cancelación.

    ``job_identity`` es obligatoria y solo la produce ``verificar_identidad_del_job``: sin una prueba de que
    este proceso vive en el Job que el bridge creó para este launch no hay forma de armar el ``hello``.
    """
    descriptor = await asyncio.to_thread(load_broker_descriptor, descriptor_path)
    manifest = await asyncio.to_thread(
        read_worker_manifest,
        manifest_path,
        secret=descriptor.secret,
    )
    if manifest.descriptor_path.resolve() != descriptor_path.resolve():
        raise VfsWorkerBootstrapError("el manifiesto apunta a otro descriptor")
    if manifest.job.job_id != expected_job_id:
        raise VfsWorkerBootstrapError("job_id del argumento y manifiesto no coinciden")
    if manifest.job.instance_id != descriptor.instance_id:
        raise VfsWorkerBootstrapError("el manifiesto apunta a otra instancia")

    reader, writer = await asyncio.open_connection(descriptor.host, descriptor.port)
    try:
        await write_authenticated_message(
            writer,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "hello",
                "role": "worker",
                "instance_id": descriptor.instance_id,
                "session_id": descriptor.session_id,
                "job_id": manifest.job.job_id,
                # Declara que este worker espera la supervisión antes de despachar: el
                # broker rechaza a los que no lo declaran (sin fallback silencioso).
                "supervision_fence": True,
                # El Job (por nombre) dentro del cual ESTE proceso demostró estar antes de conectar. El broker
                # lo compara con el que pidió para este launch: otro nombre, o ninguno, no se autoriza.
                "job_object": job_identity.nombre,
            },
            descriptor.secret,
        )
        ack = await read_authenticated_message(reader, descriptor.secret)
        if ack.get("protocol_version") != VFS_PROTOCOL_VERSION or ack.get("type") != "hello_ack":
            raise VfsWorkerBootstrapError("el broker rechazó el hello del worker")

        # FENCE DE SUPERVISIÓN. Hasta acá este proceso pudo haber nacido antes de que el
        # bridge lo asignara al Job Object; `execute_worker_manifest` crea el proceso
        # NIETO de la attestation y después la herramienta, así que nada de eso ocurre
        # sin la confirmación (autenticada y ligada a este job_id) del broker.
        supervision = await _esperar_supervision(
            reader,
            secret=descriptor.secret,
            job_id=manifest.job.job_id,
            timeout=supervision_timeout,
        )
        if supervision == "cancelled":
            return None

        # Los eventos mid-job y el job_result comparten el socket: el write lock
        # serializa ambos emisores (hoy secuenciales, mañana no necesariamente).
        write_lock = asyncio.Lock()
        sink = _SinkDeEventos(
            job_id=manifest.job.job_id,
            writer=writer,
            secret=descriptor.secret,
            write_lock=write_lock,
        )
        execution_task = asyncio.create_task(
            execute_worker_manifest(
                manifest,
                handlers=handlers,
                session_handlers=session_handlers,
                grandchild_probe=grandchild_probe,
                event_sink=sink,
            )
        )
        cancel_task = asyncio.create_task(
            _wait_for_cancel(
                reader,
                secret=descriptor.secret,
                job_id=manifest.job.job_id,
            )
        )
        done, _pending = await asyncio.wait(
            {execution_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            error = cancel_task.exception()
            if error is not None:
                execution_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await execution_task
                raise error
            execution_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution_task
            return None

        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        result = execution_task.result()
        async with write_lock:
            await write_authenticated_message(
                writer,
                {
                    "protocol_version": VFS_PROTOCOL_VERSION,
                    "type": "job_result",
                    "result": result.to_dict(),
                },
                descriptor.secret,
            )
        result_ack = await asyncio.wait_for(
            read_authenticated_message(reader, descriptor.secret),
            timeout=_RESULT_ACK_TIMEOUT_SECONDS,
        )
        if (
            result_ack.get("protocol_version") != VFS_PROTOCOL_VERSION
            or result_ack.get("type") != "job_result_ack"
            or result_ack.get("job_id") != manifest.job.job_id
        ):
            raise VfsWorkerBootstrapError("el broker no confirmó la recepción del resultado")
        return result
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


def _parse_worker_args(argv: list[str] | None) -> argparse.Namespace:
    # Sin abreviaturas: el arranque liviano (``vfs_arranque._identidad_declarada``) lee ``--job-object``/``--job-id``
    # con ``allow_abbrev=False``; los dos parsers tienen que coincidir en qué opciones existen.
    parser = argparse.ArgumentParser(prog="sky-claw-vfs-worker", allow_abbrev=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest", type=pathlib.Path)
    group.add_argument("--probe-child", action="store_true")
    parser.add_argument("--descriptor", type=pathlib.Path)
    parser.add_argument("--job-id")
    parser.add_argument("--job-object")
    parser.add_argument("probe_path", nargs="?", type=pathlib.Path)
    parser.add_argument("probe_sha256", nargs="?")
    return parser.parse_args(argv)


def worker_main(argv: list[str] | None = None) -> int:
    """Entry point usable como módulo y desde el executable congelado."""
    args = _parse_worker_args(argv)
    if args.probe_child:
        if args.probe_path is None or args.probe_sha256 is None:
            return 64
        return run_probe_child(args.probe_path, args.probe_sha256)
    if args.descriptor is None or not args.job_id or not args.job_object:
        return 64
    # IDENTIDAD DEL JOB, antes de abrir ningún socket Y antes de configurar el logging (auditoría independiente,
    # H-02): el proceso que ejecuta Python puede ser un HIJO del que el bridge asignó (PyInstaller onefile) y haber
    # nacido antes del assign. Si no puede demostrar que vive en el Job de ESTE launch, no habla con el broker, no
    # crea nietos, no despacha nada y no abre archivos ni listeners de log. El aborto es el MISMO que usa el
    # arranque liviano (``vfs_arranque``), que ya verificó antes de importar este módulo.
    try:
        prueba = verificar_identidad_del_job(args.job_object)
    except JobIdentityError as exc:
        return abortar_por_identidad(args.job_id, exc)
    setup_logging(
        log_dir=default_log_dir() / "workers",
        log_file=nombre_de_log_del_worker(args.job_id),
        process_role="vfs_worker",
        console_stream=None,
    )
    try:
        result = asyncio.run(
            run_worker_session(
                manifest_path=args.manifest,
                descriptor_path=args.descriptor,
                expected_job_id=args.job_id,
                job_identity=prueba,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error(
            "VFS worker bootstrap falló: %s",
            exc,
            exc_info=True,
            extra={
                "event": "vfs_worker_failed",
                "operation": "vfs_worker",
                "job_id": args.job_id,
            },
        )
        result_code = 70
    except Exception:
        logger.critical(
            "VFS worker terminó por una excepción no manejada",
            exc_info=True,
            extra={
                "event": "vfs_worker_unhandled_exception",
                "operation": "vfs_worker",
                "job_id": args.job_id,
            },
        )
        shutdown_logging()
        raise
    except BaseException:
        shutdown_logging()
        raise
    else:
        if result is not None and not result.success:
            logger.error(
                "VFS worker %s termino con fallo",
                result.job_id,
                extra=subprocess_error_extra(
                    operation="vfs_worker",
                    tool="VFS",
                    job_id=result.job_id,
                    exit_code=result.exit_code,
                    stderr=result.stderr,
                ),
            )
        result_code = 2 if result is None else (0 if result.success else 1)
    shutdown_logging()
    return result_code


if __name__ == "__main__":
    raise SystemExit(worker_main())
