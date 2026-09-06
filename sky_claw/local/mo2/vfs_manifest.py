"""Manifiesto firmado que consume el worker desechable bajo USVFS."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sky_claw.local.mo2.vfs_attestation import VfsAttestationChallenge
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, JsonValue, VfsJob


class VfsManifestError(RuntimeError):
    """El manifiesto del worker está incompleto, es incompatible o fue alterado."""


def _absolute_path(raw: object, *, field: str) -> pathlib.Path:
    if not isinstance(raw, str) or not raw:
        raise VfsManifestError(f"{field} debe ser una ruta absoluta")
    path = pathlib.Path(raw)
    if not path.is_absolute():
        raise VfsManifestError(f"{field} debe ser una ruta absoluta")
    return path.resolve()


@dataclass(frozen=True, slots=True, init=False)
class VfsWorkerManifest:
    """Todo lo que necesita el worker, salvo el secreto guardado en descriptor."""

    protocol_version: int
    job: VfsJob
    challenge: VfsAttestationChallenge
    data_root: pathlib.Path
    mods_dir: pathlib.Path
    install_root: pathlib.Path
    virtual_data_dir: pathlib.Path
    descriptor_path: pathlib.Path

    def __init__(
        self,
        *,
        protocol_version: int,
        job: VfsJob,
        challenge: VfsAttestationChallenge,
        data_root: pathlib.Path | None = None,
        mods_dir: pathlib.Path | None = None,
        install_root: pathlib.Path | None = None,
        mo2_root: pathlib.Path | None = None,
        virtual_data_dir: pathlib.Path,
        descriptor_path: pathlib.Path,
    ) -> None:
        resolved_data = data_root or mo2_root
        if resolved_data is None:
            raise ValueError("se requiere data_root o mo2_root")
        object.__setattr__(self, "protocol_version", protocol_version)
        object.__setattr__(self, "job", job)
        object.__setattr__(self, "challenge", challenge)
        object.__setattr__(self, "data_root", resolved_data.resolve())
        object.__setattr__(self, "mods_dir", (mods_dir or (resolved_data / "mods")).resolve())
        object.__setattr__(self, "install_root", (install_root or mo2_root or resolved_data).resolve())
        object.__setattr__(self, "virtual_data_dir", virtual_data_dir.resolve())
        object.__setattr__(self, "descriptor_path", descriptor_path.resolve())

    @property
    def mo2_root(self) -> pathlib.Path:
        """Alias de compatibilidad hacia install_root / data_root."""
        return self.install_root or self.data_root

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> VfsWorkerManifest:
        version = raw.get("protocol_version")
        if type(version) is not int or version != VFS_PROTOCOL_VERSION:
            raise VfsManifestError(
                f"versión de protocolo incompatible en manifiesto: esperada {VFS_PROTOCOL_VERSION}, recibida {version!r}"
            )
        raw_job = raw.get("job")
        raw_challenge = raw.get("challenge")
        if not isinstance(raw_job, Mapping) or not isinstance(raw_challenge, Mapping):
            raise VfsManifestError("job y challenge son obligatorios en el manifiesto")
        job = VfsJob.from_dict(raw_job)
        challenge = VfsAttestationChallenge.from_dict(raw_challenge)
        if job.profile != challenge.profile:
            raise VfsManifestError("job y challenge no apuntan al mismo perfil")
        if job.expected_fingerprint != challenge.profile_fingerprint:
            raise VfsManifestError("job y challenge no comparten fingerprint")

        data_root_raw = raw.get("data_root")
        mods_dir_raw = raw.get("mods_dir")
        install_root_raw = raw.get("install_root")
        if data_root_raw is None or mods_dir_raw is None:
            raise VfsManifestError("data_root y mods_dir son obligatorios en el manifiesto")

        data_root = _absolute_path(data_root_raw, field="data_root")
        mods_dir = _absolute_path(mods_dir_raw, field="mods_dir")
        install_root = _absolute_path(install_root_raw or data_root_raw, field="install_root")

        return cls(
            protocol_version=version,
            job=job,
            challenge=challenge,
            data_root=data_root,
            mods_dir=mods_dir,
            install_root=install_root,
            virtual_data_dir=_absolute_path(raw.get("virtual_data_dir"), field="virtual_data_dir"),
            descriptor_path=_absolute_path(raw.get("descriptor_path"), field="descriptor_path"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        challenge: dict[str, JsonValue] = {key: value for key, value in self.challenge.to_dict().items()}
        return {
            "protocol_version": self.protocol_version,
            "job": self.job.to_dict(),
            "challenge": challenge,
            "data_root": str(self.data_root),
            "mods_dir": str(self.mods_dir),
            "install_root": str(self.install_root),
            "mo2_root": str(self.mo2_root),
            "virtual_data_dir": str(self.virtual_data_dir),
            "descriptor_path": str(self.descriptor_path),
        }


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VfsManifestError(f"manifiesto no serializable: {exc}") from exc


def write_worker_manifest(
    path: pathlib.Path,
    manifest: VfsWorkerManifest,
    *,
    secret: bytes,
    hardener: Callable[[pathlib.Path], None],
) -> None:
    """Escribe atómicamente el manifiesto y endurece su ACL antes del swap."""
    payload = manifest.to_dict()
    payload_bytes = _canonical_json(payload)
    signature = hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    envelope = _canonical_json({"manifest": payload, "signature": signature})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(envelope)
        hardener(tmp)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def read_worker_manifest(path: pathlib.Path, *, secret: bytes) -> VfsWorkerManifest:
    """Autentica el contenido antes de construir DTOs o usar rutas del archivo."""
    if path.is_symlink():
        raise VfsManifestError("el manifiesto no puede ser un symlink")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VfsManifestError(f"no se pudo leer el manifiesto: {exc}") from exc
    if not isinstance(envelope, dict):
        raise VfsManifestError("el manifiesto firmado debe ser un objeto JSON")
    payload = envelope.get("manifest")
    signature = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise VfsManifestError("el manifiesto no contiene payload y firma válidos")
    expected = hmac.new(secret, _canonical_json(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise VfsManifestError("firma del manifiesto inválida")
    return VfsWorkerManifest.from_dict(payload)
