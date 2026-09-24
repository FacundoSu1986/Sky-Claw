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
import pathlib
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.loot.cli import (
    DEFAULT_LOOT_INTERNAL_GAME_ID,
    LOOT_CLI_GAME_IDENTIFIERS,
    LOOTConfig,
    LOOTNotFoundError,
    LOOTRunner,
    LOOTTimeoutError,
)
from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, verify_vfs_attestation
from sky_claw.local.mo2.vfs_contracts import (
    ALLOWED_VFS_SESSION_TOOL_IDS,
    VFS_PROTOCOL_VERSION,
    VFS_TOOL_EXECUTABLE_NAMES,
    JsonValue,
    VfsJobResult,
    VfsSessionEvent,
    VfsSessionEventError,
    VfsToolExitEvent,
    VfsToolStartedEvent,
)
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest, read_worker_manifest
from sky_claw.local.tools._process import kill_and_reap
from sky_claw.logging_config import (
    default_log_dir,
    setup_logging,
    shutdown_logging,
    subprocess_error_extra,
)

logger = logging.getLogger(__name__)

_GRANDCHILD_TIMEOUT_SECONDS = 10.0
_MAX_DESCRIPTOR_BYTES = 64 * 1024
_RESULT_ACK_TIMEOUT_SECONDS = 5.0
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


def _worker_log_name(job_id: str) -> str:
    """Construye un nombre de log seguro y estable para el job."""
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id).strip("-_")
    return f"vfs-worker-{safe_job_id or 'unknown'}.log"


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
        tool_result={},
    )


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
        tool_result=execution.tool_result,
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


async def _loot_handler(manifest: VfsWorkerManifest) -> VfsToolExecution:
    payload = manifest.job.payload
    allowed = {"loot_exe", "game", "update_masterlist", "loot_data_path"}
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
    result = await runner.sort(update_masterlist=update_masterlist)
    message = (
        "" if result.success else "; ".join(result.errors) or result.raw_stderr or result.raw_stdout or "LOOT falló"
    )
    return VfsToolExecution(
        success=result.success,
        message=message,
        exit_code=result.return_code,
        stdout=result.raw_stdout,
        stderr=result.raw_stderr,
        outputs=manifest.job.mutation_targets,
        tool_result={
            "sorted_plugins": list(result.sorted_plugins),
            "warnings": list(result.warnings),
            "errors": list(result.errors),
            "missing_patches": [dict(item) for item in result.missing_patches],
        },
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
    return {"health": _health_handler, "loot_sort": _loot_handler}


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
    handlers: Mapping[str, VfsToolHandler] | None = None,
    session_handlers: Mapping[str, VfsSessionToolHandler] | None = None,
    grandchild_probe: GrandchildProbe | None = None,
) -> VfsJobResult | None:
    """Ejecuta el worker y reporta al daemon; ``None`` significa cancelación."""
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
            },
            descriptor.secret,
        )
        ack = await read_authenticated_message(reader, descriptor.secret)
        if ack.get("protocol_version") != VFS_PROTOCOL_VERSION or ack.get("type") != "hello_ack":
            raise VfsWorkerBootstrapError("el broker rechazó el hello del worker")

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
    parser = argparse.ArgumentParser(prog="sky-claw-vfs-worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest", type=pathlib.Path)
    group.add_argument("--probe-child", action="store_true")
    parser.add_argument("--descriptor", type=pathlib.Path)
    parser.add_argument("--job-id")
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
    if args.descriptor is None or not args.job_id:
        return 64
    setup_logging(
        log_dir=default_log_dir() / "workers",
        log_file=_worker_log_name(args.job_id),
        process_role="vfs_worker",
        console_stream=None,
    )
    try:
        result = asyncio.run(
            run_worker_session(
                manifest_path=args.manifest,
                descriptor_path=args.descriptor,
                expected_job_id=args.job_id,
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
