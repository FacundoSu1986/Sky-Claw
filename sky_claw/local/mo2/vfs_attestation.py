"""Prueba fail-closed de que un worker observa el overlay del perfil MO2."""

from __future__ import annotations

import hashlib
import os
import pathlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from sky_claw.antigravity.security.path_validator import PathViolationError, assert_safe_component

_PROFILE_STATE_FILES = ("modlist.txt", "plugins.txt", "loadorder.txt", "settings.ini", "settings.txt")
_IGNORED_ROOT_FILES = frozenset({"meta.ini"})


class VfsAttestationError(RuntimeError):
    """No se pudo probar una vista USVFS coherente con el perfil pedido."""


def _validated_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
        raise VfsAttestationError(f"{field} debe ser un SHA-256 hexadecimal")
    return value.lower()


@dataclass(frozen=True, slots=True)
class VfsAttestationChallenge:
    """Canary físico esperado dentro de la vista virtual del worker."""

    profile: str
    source_mod: str
    relative_path: pathlib.PurePosixPath
    sha256: str
    profile_fingerprint: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> VfsAttestationChallenge:
        profile = raw.get("profile")
        source_mod = raw.get("source_mod")
        relative_text = raw.get("relative_path")
        sha256 = raw.get("sha256")
        fingerprint = raw.get("profile_fingerprint")
        if not isinstance(profile, str) or not isinstance(source_mod, str):
            raise VfsAttestationError("profile y source_mod deben ser strings")
        if not isinstance(relative_text, str) or not relative_text:
            raise VfsAttestationError("relative_path debe ser un string no vacío")
        relative = pathlib.PurePosixPath(relative_text)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise VfsAttestationError("relative_path debe quedar dentro de Data")
        return cls(
            profile=_validated_profile(profile),
            source_mod=_validated_profile(source_mod),
            relative_path=relative,
            sha256=_validated_sha256(sha256, field="sha256"),
            profile_fingerprint=_validated_sha256(fingerprint, field="profile_fingerprint"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "source_mod": self.source_mod,
            "relative_path": self.relative_path.as_posix(),
            "sha256": self.sha256,
            "profile_fingerprint": self.profile_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class VfsAttestationProof:
    """Evidencia observada por el worker antes de ejecutar una herramienta."""

    profile: str
    source_mod: str
    relative_path: pathlib.PurePosixPath
    visible_sha256: str
    profile_fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "source_mod": self.source_mod,
            "relative_path": self.relative_path.as_posix(),
            "visible_sha256": self.visible_sha256,
            "profile_fingerprint": self.profile_fingerprint,
        }


def _validated_profile(profile: str) -> str:
    try:
        return assert_safe_component(profile, field="profile")
    except PathViolationError as exc:
        raise VfsAttestationError(str(exc)) from exc


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise VfsAttestationError(f"no se pudo leer {path}: {exc}") from exc
    return digest.hexdigest()


def _enabled_mods(modlist_path: pathlib.Path) -> tuple[str, ...]:
    try:
        lines = modlist_path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise VfsAttestationError(f"no se pudo leer el modlist del perfil: {exc}") from exc
    enabled: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "-", "*")):
            continue
        if not line.startswith("+") or not line[1:].strip():
            raise VfsAttestationError(f"línea inválida en modlist.txt: {line!r}")
        enabled.append(line[1:].strip())
    return tuple(enabled)


def _iter_mod_files(mod_root: pathlib.Path) -> Iterator[tuple[pathlib.Path, pathlib.Path]]:
    try:
        for current, dirs, files in os.walk(mod_root):
            dirs[:] = sorted(name for name in dirs if not name.endswith(".mohidden"))
            current_path = pathlib.Path(current)
            for name in sorted(files):
                source = current_path / name
                relative = source.relative_to(mod_root)
                if len(relative.parts) == 1 and name.casefold() in _IGNORED_ROOT_FILES:
                    continue
                if name.endswith(".mohidden") or source.is_symlink() or not source.is_file():
                    continue
                yield relative, source
    except OSError as exc:
        raise VfsAttestationError(f"no se pudo enumerar el mod {mod_root.name!r}: {exc}") from exc


def _profile_fingerprint(
    *,
    profile_dir: pathlib.Path,
    profile: str,
    source_mod: str,
    relative_path: pathlib.PurePosixPath,
    canary_sha256: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"skyclaw-vfs-profile-v1\0")
    digest.update(profile.encode("utf-8"))
    for filename in _PROFILE_STATE_FILES:
        path = profile_dir / filename
        digest.update(b"\0file\0")
        digest.update(filename.encode("ascii"))
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"\0missing\0")
        except OSError as exc:
            raise VfsAttestationError(f"no se pudo fingerprintar {path}: {exc}") from exc
    digest.update(b"\0canary\0")
    digest.update(source_mod.encode("utf-8"))
    digest.update(relative_path.as_posix().encode("utf-8"))
    digest.update(canary_sha256.encode("ascii"))
    return digest.hexdigest()


def build_attestation_challenge(
    *,
    mo2_root: pathlib.Path,
    profile: str,
    physical_data_dir: pathlib.Path,
) -> VfsAttestationChallenge:
    """Elige un archivo efectivo de mod que el ``Data`` físico no contiene."""
    profile_name = _validated_profile(profile)
    root = mo2_root.resolve()
    data = physical_data_dir.resolve()
    profile_dir = root / "profiles" / profile_name
    enabled = _enabled_mods(profile_dir / "modlist.txt")
    if not enabled:
        raise VfsAttestationError("el perfil no tiene mods habilitados para construir un canary elegible")

    # modlist.txt crece de menor a mayor prioridad en el contrato vigente del
    # proyecto. Al bajar desde el final, descartamos archivos reemplazados por
    # overwrite o por un mod de prioridad mayor.
    higher_roots: list[pathlib.Path] = [root / "overwrite"]
    for mod_name in reversed(enabled):
        safe_mod = _validated_profile(mod_name)
        mod_root = root / "mods" / safe_mod
        if not mod_root.is_dir():
            raise VfsAttestationError(f"el mod habilitado {mod_name!r} no existe en {root / 'mods'}")
        for relative, source in _iter_mod_files(mod_root):
            if (data / relative).exists():
                continue
            if any((higher / relative).exists() for higher in higher_roots if higher.is_dir()):
                continue
            sha256 = _sha256_file(source)
            relative_posix = pathlib.PurePosixPath(*relative.parts)
            fingerprint = _profile_fingerprint(
                profile_dir=profile_dir,
                profile=profile_name,
                source_mod=mod_name,
                relative_path=relative_posix,
                canary_sha256=sha256,
            )
            return VfsAttestationChallenge(
                profile=profile_name,
                source_mod=mod_name,
                relative_path=relative_posix,
                sha256=sha256,
                profile_fingerprint=fingerprint,
            )
        higher_roots.append(mod_root)

    raise VfsAttestationError(
        "no existe un canary elegible: todo archivo de mod también está en Data físico o queda sobrescrito"
    )


def verify_vfs_attestation(
    *,
    challenge: VfsAttestationChallenge,
    mo2_root: pathlib.Path,
    profile: str,
    virtual_data_dir: pathlib.Path,
) -> VfsAttestationProof:
    """Verifica fingerprint y visibilidad/hash antes de cualquier mutación."""
    profile_name = _validated_profile(profile)
    if profile_name != challenge.profile:
        raise VfsAttestationError(f"perfil incorrecto: worker={profile_name!r}, challenge={challenge.profile!r}")
    root = mo2_root.resolve()
    relative = pathlib.Path(*challenge.relative_path.parts)
    source = root / "mods" / _validated_profile(challenge.source_mod) / relative
    current_source_sha = _sha256_file(source)
    if current_source_sha != challenge.sha256:
        raise VfsAttestationError("el canary cambió después del preview")
    current_fingerprint = _profile_fingerprint(
        profile_dir=root / "profiles" / profile_name,
        profile=profile_name,
        source_mod=challenge.source_mod,
        relative_path=challenge.relative_path,
        canary_sha256=current_source_sha,
    )
    if current_fingerprint != challenge.profile_fingerprint:
        raise VfsAttestationError("fingerprint del perfil cambió después del preview")

    visible = virtual_data_dir.resolve() / relative
    if not visible.is_file():
        raise VfsAttestationError(f"canary no visible bajo USVFS: {challenge.relative_path.as_posix()}")
    visible_sha = _sha256_file(visible)
    if visible_sha != challenge.sha256:
        raise VfsAttestationError("el canary visible no coincide con el hash esperado")
    return VfsAttestationProof(
        profile=profile_name,
        source_mod=challenge.source_mod,
        relative_path=challenge.relative_path,
        visible_sha256=visible_sha,
        profile_fingerprint=current_fingerprint,
    )
