"""Contratos serializables del broker de ejecución bajo USVFS.

Bridge, daemon y worker comparten la frontera local del mismo usuario, pero
cualquier mensaje se valida de nuevo al cruzar IPC. El bridge nunca recibe un
executable arbitrario; solo identificadores de herramienta cerrados.
"""

from __future__ import annotations

import math
import pathlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

from sky_claw.antigravity.security.path_validator import PathViolationError, assert_safe_component

VFS_PROTOCOL_VERSION = 1
ALLOWED_VFS_TOOL_IDS = frozenset({"health", "loot_sort"})
ALLOWED_ROLLBACK_STATES = frozenset({"not_started", "not_required", "pending", "completed", "failed"})

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class VfsProtocolError(ValueError):
    """Un mensaje IPC no cumple el contrato versionado del broker."""


def _safe_component(value: object, *, field: str) -> str:
    try:
        return assert_safe_component(value if isinstance(value, str) else None, field=field)
    except PathViolationError as exc:
        raise VfsProtocolError(str(exc)) from exc


def _require_string(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "string" if allow_empty else "string no vacío"
        raise VfsProtocolError(f"{field} debe ser {suffix}")
    return value


def _require_protocol_version(value: object) -> int:
    if type(value) is not int or value != VFS_PROTOCOL_VERSION:
        raise VfsProtocolError(f"versión de protocolo incompatible: {value!r}; esperada {VFS_PROTOCOL_VERSION}")
    return value


def _require_fingerprint(value: object) -> str:
    fingerprint = _require_string(value, field="expected_fingerprint").lower()
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise VfsProtocolError("expected_fingerprint debe ser un SHA-256 hexadecimal")
    return fingerprint


def _json_value(value: object, *, field: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise VfsProtocolError(f"{field} no admite NaN ni infinito")
        return value
    if isinstance(value, list):
        return [_json_value(item, field=f"{field}[]") for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise VfsProtocolError(f"{field} solo admite claves string")
            result[key] = _json_value(item, field=f"{field}.{key}")
        return result
    raise VfsProtocolError(f"{field} contiene un valor no serializable: {type(value).__name__}")


def _mapping(value: object, *, field: str) -> dict[str, JsonValue]:
    parsed = _json_value(value, field=field)
    if not isinstance(parsed, dict):
        raise VfsProtocolError(f"{field} debe ser un objeto JSON")
    return parsed


def _absolute_paths(value: object, *, field: str) -> tuple[pathlib.Path, ...]:
    if not isinstance(value, (list, tuple)):
        raise VfsProtocolError(f"{field} debe ser una lista de rutas")
    result: list[pathlib.Path] = []
    for raw in value:
        path_text = _require_string(raw if isinstance(raw, str) else str(raw), field=field)
        path = pathlib.Path(path_text)
        if not path.is_absolute():
            raise VfsProtocolError(f"{field} solo admite rutas absolutas")
        result.append(path.resolve())
    return tuple(result)


@dataclass(frozen=True, slots=True)
class VfsJob:
    """Trabajo validado que Sky-Claw permite ejecutar dentro de USVFS."""

    protocol_version: int
    job_id: str
    instance_id: str
    profile: str
    tool_id: str
    payload: dict[str, JsonValue]
    timeout_seconds: float
    expected_fingerprint: str
    mutation_targets: tuple[pathlib.Path, ...]

    @classmethod
    def create(
        cls,
        *,
        instance_id: str,
        profile: str,
        tool_id: str,
        payload: Mapping[str, object],
        timeout_seconds: float,
        expected_fingerprint: str,
        mutation_targets: tuple[pathlib.Path, ...],
    ) -> VfsJob:
        return cls._validated(
            protocol_version=VFS_PROTOCOL_VERSION,
            job_id=str(uuid.uuid4()),
            instance_id=instance_id,
            profile=profile,
            tool_id=tool_id,
            payload=payload,
            timeout_seconds=timeout_seconds,
            expected_fingerprint=expected_fingerprint,
            mutation_targets=mutation_targets,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> VfsJob:
        return cls._validated(
            protocol_version=raw.get("protocol_version"),
            job_id=raw.get("job_id"),
            instance_id=raw.get("instance_id"),
            profile=raw.get("profile"),
            tool_id=raw.get("tool_id"),
            payload=raw.get("payload"),
            timeout_seconds=raw.get("timeout_seconds"),
            expected_fingerprint=raw.get("expected_fingerprint"),
            mutation_targets=raw.get("mutation_targets"),
        )

    @classmethod
    def _validated(
        cls,
        *,
        protocol_version: object,
        job_id: object,
        instance_id: object,
        profile: object,
        tool_id: object,
        payload: object,
        timeout_seconds: object,
        expected_fingerprint: object,
        mutation_targets: object,
    ) -> VfsJob:
        version = _require_protocol_version(protocol_version)
        job = _safe_component(job_id, field="job_id")
        instance = _safe_component(instance_id, field="instance_id")
        profile_name = _safe_component(profile, field="profile")
        tool = _require_string(tool_id, field="tool_id")
        if tool not in ALLOWED_VFS_TOOL_IDS:
            raise VfsProtocolError(f"tool_id no permitido: {tool!r}")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise VfsProtocolError("timeout_seconds debe ser numérico")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 86_400:
            raise VfsProtocolError("timeout_seconds debe estar entre 0 y 86400")
        return cls(
            protocol_version=version,
            job_id=job,
            instance_id=instance,
            profile=profile_name,
            tool_id=tool,
            payload=_mapping(payload, field="payload"),
            timeout_seconds=timeout,
            expected_fingerprint=_require_fingerprint(expected_fingerprint),
            mutation_targets=_absolute_paths(mutation_targets, field="mutation_targets"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "job_id": self.job_id,
            "instance_id": self.instance_id,
            "profile": self.profile,
            "tool_id": self.tool_id,
            "payload": self.payload,
            "timeout_seconds": self.timeout_seconds,
            "expected_fingerprint": self.expected_fingerprint,
            "mutation_targets": [str(path) for path in self.mutation_targets],
        }


@dataclass(frozen=True, slots=True)
class VfsJobResult:
    """Resultado canónico del worker, incluidos evidencia y rollback."""

    protocol_version: int
    job_id: str
    success: bool
    message: str
    exit_code: int | None
    stdout: str
    stderr: str
    outputs: tuple[pathlib.Path, ...]
    rollback_state: str
    attestation: dict[str, JsonValue] | None
    tool_result: dict[str, JsonValue]

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> VfsJobResult:
        version = _require_protocol_version(raw.get("protocol_version"))
        job_id = _safe_component(raw.get("job_id"), field="job_id")
        success = raw.get("success")
        if type(success) is not bool:
            raise VfsProtocolError("success debe ser bool")
        message = _require_string(raw.get("message"), field="message", allow_empty=True)
        exit_code = raw.get("exit_code")
        if exit_code is not None and type(exit_code) is not int:
            raise VfsProtocolError("exit_code debe ser int o null")
        stdout = _require_string(raw.get("stdout"), field="stdout", allow_empty=True)
        stderr = _require_string(raw.get("stderr"), field="stderr", allow_empty=True)
        rollback = _require_string(raw.get("rollback_state"), field="rollback_state")
        if rollback not in ALLOWED_ROLLBACK_STATES:
            raise VfsProtocolError(f"rollback_state no permitido: {rollback!r}")
        raw_attestation = raw.get("attestation")
        attestation = None if raw_attestation is None else _mapping(raw_attestation, field="attestation")
        tool_result = _mapping(raw.get("tool_result", {}), field="tool_result")
        return cls(
            protocol_version=version,
            job_id=job_id,
            success=success,
            message=message,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            outputs=_absolute_paths(raw.get("outputs"), field="outputs"),
            rollback_state=rollback,
            attestation=attestation,
            tool_result=tool_result,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "job_id": self.job_id,
            "success": self.success,
            "message": self.message,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "outputs": [str(path) for path in self.outputs],
            "rollback_state": self.rollback_state,
            "attestation": self.attestation,
            "tool_result": self.tool_result,
        }
