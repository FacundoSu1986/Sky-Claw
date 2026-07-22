"""Worker de vida corta que MO2 lanza bajo el mapping USVFS solicitado."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import pathlib
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.loot.cli import LOOTConfig, LOOTNotFoundError, LOOTRunner, LOOTTimeoutError
from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, verify_vfs_attestation
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, JsonValue, VfsJobResult
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest, read_worker_manifest

logger = logging.getLogger(__name__)

_GRANDCHILD_TIMEOUT_SECONDS = 10.0
_MAX_DESCRIPTOR_BYTES = 64 * 1024
_RESULT_ACK_TIMEOUT_SECONDS = 5.0


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
    grandchild_probe: GrandchildProbe | None = None,
) -> VfsJobResult:
    """Attesta worker+nieto y solo entonces despacha la herramienta allowlisted."""
    try:
        proof = await asyncio.to_thread(
            verify_vfs_attestation,
            challenge=manifest.challenge,
            mo2_root=manifest.mo2_root,
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

    selected = dict(handlers) if handlers is not None else _default_handlers()
    handler = selected.get(manifest.job.tool_id)
    if handler is None:
        return _failure(
            manifest,
            f"handler no disponible para tool_id allowlisted {manifest.job.tool_id!r}",
            attestation=attestation,
        )
    try:
        execution = await handler(manifest)
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
    allowed = {"loot_exe", "game", "update_masterlist"}
    unexpected = set(payload) - allowed
    if unexpected:
        raise ValueError(f"payload de loot_sort contiene campos no permitidos: {sorted(unexpected)}")
    loot_exe = pathlib.Path(_payload_string(payload, "loot_exe"))
    if not loot_exe.is_absolute():
        raise ValueError("payload.loot_exe debe ser una ruta absoluta")
    game = payload.get("game", "SkyrimSE")
    if not isinstance(game, str) or game not in {"SkyrimSE", "SkyrimVR"}:
        raise ValueError("payload.game no está permitido")
    update_masterlist = payload.get("update_masterlist", False)
    if type(update_masterlist) is not bool:
        raise ValueError("payload.update_masterlist debe ser bool")
    game_path = manifest.virtual_data_dir.parent.resolve()
    validator = PathValidator(roots=[loot_exe.parent.resolve(), game_path, manifest.mo2_root])
    runner = LOOTRunner(
        LOOTConfig(
            loot_exe=loot_exe.resolve(),
            game_path=game_path,
            game=game,
            timeout=max(1, int(manifest.job.timeout_seconds)),
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


def _default_handlers() -> dict[str, VfsToolHandler]:
    return {"health": _health_handler, "loot_sort": _loot_handler}


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

        execution_task = asyncio.create_task(
            execute_worker_manifest(
                manifest,
                handlers=handlers,
                grandchild_probe=grandchild_probe,
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
    try:
        result = asyncio.run(
            run_worker_session(
                manifest_path=args.manifest,
                descriptor_path=args.descriptor,
                expected_job_id=args.job_id,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("VFS worker bootstrap falló: %s", exc, exc_info=True)
        return 70
    if result is None:
        return 2
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(worker_main())
