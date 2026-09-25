"""Trusted Golden Registry (TGR) — Raíz de Confianza Persistente (GP2-S3a).

Implementa el modelo inmutable, serialización canónica, validación fail-closed,
lookup normativo y reemplazo atómico para el registro de Golden Masters confiables
conforme a ADR 0010 §11.0, §11.3 y §11.4:
- TGR = TRUST ROOT. STAGING = UNTRUSTED.
- Formato determinista UTF-8 con schema_version explícita "1.0".
- Claves canónicas del ADR: canonical_root, VolumeSerialNumber, root_file_id,
  tree_digest, policy_version, registered_by, registered_at.
- Tipos de datos del ABI GP2: VolumeSerialNumber uint64, root_file_id uint128 nativo (admite 0).
- TreeDigest completo persistido y comparado (digest, files, bytes).
- registered_by con identidad estable (String SID o ASCII canónico), sin display names localizados.
- Reemplazo atómico vía archivo temporal en el mismo directorio protegido y revalidación post-replace.
- Separación estricta de autoridad: GP2 apply NUNCA escribe TGR.
"""

from __future__ import annotations

import datetime
import json
import ntpath
import os
import pathlib
import re
import string
import sys
from dataclasses import dataclass
from typing import Any

from sky_claw.local.runtime_vault.models import RuntimeVaultError, TreeDigest

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class TrustedRegistryError(RuntimeVaultError):
    """Base de las excepciones de dominio del Trusted Golden Registry."""


class TrustedRegistryParseError(TrustedRegistryError):
    """Error al parsear el JSON del registro: fail-closed ante cualquier corrupción."""


class TrustedRegistrySchemaError(TrustedRegistryError):
    """Esquema inválido, claves faltantes/desconocidas o tipos fuera de rango."""


class TrustedRegistryDuplicateError(TrustedRegistryError):
    """Conflicto de unicidad por canonical_root o identidad física (VolumeSerialNumber, root_file_id)."""


class TrustedGoldenNotFoundError(TrustedRegistryError):
    """No existe entrada en el TGR que corresponda a la ruta o identidad solicitada."""


class TrustedGoldenMismatchError(TrustedRegistryError):
    """Discrepancia entre la entrada registrada y los bindings observados (produce REFUSE_TO_PLAN)."""


class TrustedRegistryUnsupportedError(TrustedRegistryError):
    """Operación de seguridad de plataforma no soportada fuera de Windows."""


# ============================================================================
# Constantes Normativas
# ============================================================================

TGR_SCHEMA_VERSION = "1.0"
MAX_UINT64 = (1 << 64) - 1
MAX_UINT128 = (1 << 128) - 1

_ENTRY_MANDATORY_KEYS = frozenset(
    {
        "VolumeSerialNumber",
        "canonical_root",
        "policy_version",
        "registered_at",
        "registered_by",
        "root_file_id",
        "tree_digest",
    }
)

_ROOT_MANDATORY_KEYS = frozenset({"entries", "schema_version"})
_TREE_DIGEST_KEYS = frozenset({"bytes", "digest", "files"})


# ============================================================================
# Normalización y Validadores Internos
# ============================================================================


def _normalize_windows_root(value: str | os.PathLike[str]) -> str:
    """Normaliza un canonical_root Windows garantizando que sea absoluto con volumen local."""
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise TrustedRegistrySchemaError("canonical_root debe ser str o PathLike") from exc
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise TrustedRegistrySchemaError("canonical_root debe ser una ruta Windows local no vacía")
    raw = raw.strip()
    if raw.startswith(("\\\\", "//")):
        raise TrustedRegistrySchemaError("canonical_root UNC/remoto no está soportado por GP2 v1")
    normalized = ntpath.normpath(raw)
    drive, tail = ntpath.splitdrive(normalized)
    if not drive or not tail.startswith(("\\", "/")):
        raise TrustedRegistrySchemaError("canonical_root debe ser una ruta Windows absoluta con volumen local")
    return normalized


def _validate_iso8601_utc(ts: str) -> str:
    """Valida que un timestamp sea ISO 8601 UTC determinista terminado en 'Z'."""
    if not isinstance(ts, str) or not ts.strip():
        raise TrustedRegistrySchemaError("registered_at debe ser un string no vacío")
    clean_ts = ts.strip()
    if not clean_ts.endswith("Z"):
        raise TrustedRegistrySchemaError(f"registered_at '{clean_ts}' debe finalizar con 'Z' para UTC explícito")
    # Intentar parsear el ISO datetime
    iso_prefix = clean_ts[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(iso_prefix)
    except ValueError as exc:
        raise TrustedRegistrySchemaError(f"registered_at '{clean_ts}' no es un timestamp ISO 8601 válido") from exc
    if dt.tzinfo != datetime.UTC:
        raise TrustedRegistrySchemaError(f"registered_at '{clean_ts}' debe pertenecer a la zona horaria UTC")
    return clean_ts


def _validate_sha256_hex(digest: str) -> str:
    """Valida que un hash sea sha256 hexadecimal de 64 caracteres en minúsculas."""
    if not isinstance(digest, str):
        raise TrustedRegistrySchemaError("digest debe ser un string")
    clean_digest = digest.strip().lower()
    if len(clean_digest) != 64 or not all(c in string.hexdigits for c in clean_digest):
        raise TrustedRegistrySchemaError(f"digest '{digest}' no es un SHA-256 hexadecimal válido de 64 caracteres")
    return clean_digest


_CANONICAL_SID_REGEX = re.compile(r"^S-1-\d+(?:-\d+)+$")


def _validate_canonical_sid(sid: Any) -> str:
    """Valida que registered_by sea un String SID canónico de Windows (ej. 'S-1-5-18')."""
    if not isinstance(sid, str):
        raise TrustedRegistrySchemaError("registered_by debe ser un string con formato SID canónico")
    clean_sid = sid.strip()
    if not _CANONICAL_SID_REGEX.match(clean_sid):
        raise TrustedRegistrySchemaError(
            f"registered_by '{sid}' no es un SID canónico válido (debe tener formato 'S-1-...' y componentes numéricos)"
        )
    return clean_sid


# ============================================================================
# Modelos Inmutables del TGR
# ============================================================================


@dataclass(frozen=True, slots=True)
class TrustedGoldenEntry:
    """Entrada inmutable del Trusted Golden Registry (ADR 0010 §11.0 / §11.3).

    Bindings normativos:
    - canonical_root: ruta física normalizada (unidad local obligatoria).
    - volume_serial_number: uint64 nativo de FILE_ID_INFO.
    - root_file_id: uint128 nativo de FILE_ID_128 (admite 0).
    - tree_digest: TreeDigest completo (digest, files, bytes) de la última RV-2 aprobada.
    - policy_version: versión de la política (ej. 'gp2-v1').
    - registered_by: identidad de registro estable (String SID canónico S-1-...).
    - registered_at: timestamp ISO 8601 UTC con 'Z'.
    """

    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    tree_digest: TreeDigest
    policy_version: str
    registered_by: str
    registered_at: str

    def __post_init__(self) -> None:
        norm_root = _normalize_windows_root(self.canonical_root)
        object.__setattr__(self, "canonical_root", norm_root)

        if isinstance(self.volume_serial_number, bool) or not isinstance(self.volume_serial_number, int):
            raise TrustedRegistrySchemaError("VolumeSerialNumber debe ser un entero uint64")
        if not (0 <= self.volume_serial_number <= MAX_UINT64):
            raise TrustedRegistrySchemaError(
                f"VolumeSerialNumber {self.volume_serial_number} fuera del rango uint64 [0, {MAX_UINT64}]"
            )

        if isinstance(self.root_file_id, bool) or not isinstance(self.root_file_id, int):
            raise TrustedRegistrySchemaError("root_file_id debe ser un entero uint128")
        if not (0 <= self.root_file_id <= MAX_UINT128):
            raise TrustedRegistrySchemaError(
                f"root_file_id {self.root_file_id} fuera del rango uint128 [0, {MAX_UINT128}]"
            )

        if not isinstance(self.tree_digest, TreeDigest):
            raise TrustedRegistrySchemaError("tree_digest debe ser una instancia de TreeDigest")
        norm_digest = _validate_sha256_hex(self.tree_digest.digest)
        if (
            isinstance(self.tree_digest.files, bool)
            or not isinstance(self.tree_digest.files, int)
            or self.tree_digest.files < 0
        ):
            raise TrustedRegistrySchemaError("tree_digest.files debe ser un entero no negativo")
        if (
            isinstance(self.tree_digest.bytes, bool)
            or not isinstance(self.tree_digest.bytes, int)
            or self.tree_digest.bytes < 0
        ):
            raise TrustedRegistrySchemaError("tree_digest.bytes debe ser un entero no negativo")

        object.__setattr__(
            self,
            "tree_digest",
            TreeDigest(
                digest=norm_digest,
                files=self.tree_digest.files,
                bytes=self.tree_digest.bytes,
            ),
        )

        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise TrustedRegistrySchemaError("policy_version debe ser un string no vacío")

        norm_reg_by = _validate_canonical_sid(self.registered_by)
        object.__setattr__(self, "registered_by", norm_reg_by)

        norm_ts = _validate_iso8601_utc(self.registered_at)
        object.__setattr__(self, "registered_at", norm_ts)


@dataclass(frozen=True, slots=True)
class TrustedGoldenRegistry:
    """Contenedor inmutable del Trusted Golden Registry."""

    entries: tuple[TrustedGoldenEntry, ...] = ()
    schema_version: str = TGR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TGR_SCHEMA_VERSION:
            raise TrustedRegistrySchemaError(
                f"schema_version '{self.schema_version}' no soportado (esperado '{TGR_SCHEMA_VERSION}')"
            )

        # Invariante de unicidad dual: canonical_root e identidad física
        roots_seen: set[str] = set()
        physical_seen: set[tuple[int, int]] = set()

        for entry in self.entries:
            if not isinstance(entry, TrustedGoldenEntry):
                raise TrustedRegistrySchemaError("Cada entrada debe ser una instancia de TrustedGoldenEntry")

            # Comparación case-insensitive de canonical_root en Windows
            root_key = entry.canonical_root.upper()
            if root_key in roots_seen:
                raise TrustedRegistryDuplicateError(
                    f"Conflicto de canonical_root duplicado en TGR: '{entry.canonical_root}'"
                )
            roots_seen.add(root_key)

            phys_key = (entry.volume_serial_number, entry.root_file_id)
            if phys_key in physical_seen:
                raise TrustedRegistryDuplicateError(
                    f"Conflicto de identidad física duplicada en TGR: VolumeSerialNumber={entry.volume_serial_number}, "
                    f"root_file_id={entry.root_file_id}"
                )
            physical_seen.add(phys_key)


# ============================================================================
# Serialización y Deserialización Canónica
# ============================================================================


def trusted_golden_entry_to_dict(entry: TrustedGoldenEntry) -> dict[str, Any]:
    """Serializa una entrada TGR al mismo objeto JSON que usa el registro completo.

    §11.4: el registro protegido de Golden Admission conserva las entradas
    ``before``/``after`` exactas, así que la representación de una entrada
    tiene que ser la MISMA que la del TGR — no una copia paralela.
    """
    return {
        "VolumeSerialNumber": entry.volume_serial_number,
        "canonical_root": entry.canonical_root,
        "policy_version": entry.policy_version,
        "registered_at": entry.registered_at,
        "registered_by": entry.registered_by,
        "root_file_id": entry.root_file_id,
        "tree_digest": {
            "bytes": entry.tree_digest.bytes,
            "digest": entry.tree_digest.digest,
            "files": entry.tree_digest.files,
        },
    }


def trusted_golden_entry_from_dict(data: object) -> TrustedGoldenEntry:
    """Deserializa y valida exhaustivamente una entrada TGR (Fail-Closed, esquema cerrado)."""
    if not isinstance(data, dict):
        raise TrustedRegistrySchemaError("Cada elemento de 'entries' debe ser un diccionario")

    item_keys = set(data.keys())
    if item_keys != _ENTRY_MANDATORY_KEYS:
        unknown_item = item_keys - _ENTRY_MANDATORY_KEYS
        missing_item = _ENTRY_MANDATORY_KEYS - item_keys
        if unknown_item:
            raise TrustedRegistrySchemaError(f"Clave desconocida en entrada: {unknown_item}")
        if missing_item:
            raise TrustedRegistrySchemaError(f"Clave obligatoria ausente en entrada: {missing_item}")

    raw_td = data["tree_digest"]
    if not isinstance(raw_td, dict):
        raise TrustedRegistrySchemaError("El campo 'tree_digest' debe ser un diccionario")
    td_keys = set(raw_td.keys())
    if td_keys != _TREE_DIGEST_KEYS:
        unknown_td = td_keys - _TREE_DIGEST_KEYS
        missing_td = _TREE_DIGEST_KEYS - td_keys
        if unknown_td:
            raise TrustedRegistrySchemaError(f"Clave desconocida en tree_digest: {unknown_td}")
        if missing_td:
            raise TrustedRegistrySchemaError(f"Clave obligatoria ausente en tree_digest: {missing_td}")

    return TrustedGoldenEntry(
        canonical_root=data["canonical_root"],
        volume_serial_number=data["VolumeSerialNumber"],
        root_file_id=data["root_file_id"],
        tree_digest=TreeDigest(
            digest=str(raw_td["digest"]),
            files=raw_td["files"],
            bytes=raw_td["bytes"],
        ),
        policy_version=data["policy_version"],
        registered_by=data["registered_by"],
        registered_at=data["registered_at"],
    )


def serialize_trusted_golden_registry(registry: TrustedGoldenRegistry) -> bytes:
    """Serializa el registro a bytes canónicos UTF-8 de forma determinista y reproducible."""
    # Ordenamiento determinista: por canonical_root.upper(), VolumeSerialNumber, root_file_id
    sorted_entries = sorted(
        registry.entries,
        key=lambda e: (e.canonical_root.upper(), e.volume_serial_number, e.root_file_id),
    )

    data: dict[str, Any] = {
        "entries": [trusted_golden_entry_to_dict(e) for e in sorted_entries],
        "schema_version": registry.schema_version,
    }

    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def deserialize_trusted_golden_registry(raw: bytes | str) -> TrustedGoldenRegistry:
    """Deserializa y valida exhaustivamente un TGR desde bytes o string JSON (Fail-Closed)."""
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TrustedRegistryParseError("JSON inválido: decodificación UTF-8 falló") from exc
    else:
        raise TrustedRegistrySchemaError("Los datos del registro deben ser bytes o str")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrustedRegistryParseError(f"JSON inválido: {exc}") from exc

    if not isinstance(parsed, dict):
        raise TrustedRegistrySchemaError("La raíz del registro debe ser un diccionario")

    # Esquema cerrado en la raíz
    root_keys = set(parsed.keys())
    if root_keys != _ROOT_MANDATORY_KEYS:
        unknown = root_keys - _ROOT_MANDATORY_KEYS
        missing = _ROOT_MANDATORY_KEYS - root_keys
        if unknown:
            raise TrustedRegistrySchemaError(f"Clave desconocida en raíz del registro: {unknown}")
        if missing:
            raise TrustedRegistrySchemaError(f"Clave obligatoria ausente en raíz: {missing}")

    schema_version = parsed["schema_version"]
    if schema_version != TGR_SCHEMA_VERSION:
        raise TrustedRegistrySchemaError(f"schema_version '{schema_version}' no es válido")

    raw_entries = parsed["entries"]
    if not isinstance(raw_entries, list):
        raise TrustedRegistrySchemaError("El campo 'entries' debe ser una lista")

    entries: list[TrustedGoldenEntry] = []
    for item in raw_entries:
        entries.append(trusted_golden_entry_from_dict(item))

    return TrustedGoldenRegistry(entries=tuple(entries), schema_version=schema_version)


# ============================================================================
# Lookup y Verificación de Bindings
# ============================================================================


def verify_trusted_golden_binding(
    registry: TrustedGoldenRegistry,
    canonical_root: str | os.PathLike[str],
    volume_serial_number: int,
    root_file_id: int,
    tree_digest: TreeDigest,
) -> TrustedGoldenEntry:
    """Verifica que un plan posea una entrada en el TGR y coincidan exactamente los 4 bindings.

    Regla ADR 0010 §11.0 / §11.4:
    Exige coincidencia estricta de:
    1. canonical_root
    2. VolumeSerialNumber
    3. root_file_id
    4. tree_digest (completo: digest, files, bytes)
    Cualquier mismatch produce TrustedGoldenMismatchError (REFUSE_TO_PLAN en el helper).
    Si no existe entrada para el root -> TrustedGoldenNotFoundError.
    """
    normalized_root = _normalize_windows_root(canonical_root)
    root_upper = normalized_root.upper()

    matching_entry: TrustedGoldenEntry | None = None
    for entry in registry.entries:
        if entry.canonical_root.upper() == root_upper:
            matching_entry = entry
            break

    if matching_entry is None:
        raise TrustedGoldenNotFoundError(
            f"No existe entrada registrada en el TGR para canonical_root '{normalized_root}'"
        )

    # 1. VolumeSerialNumber
    if matching_entry.volume_serial_number != volume_serial_number:
        raise TrustedGoldenMismatchError(
            f"VolumeSerialNumber mismatch para '{normalized_root}': "
            f"registrado={matching_entry.volume_serial_number}, observado={volume_serial_number}"
        )

    # 2. root_file_id
    if matching_entry.root_file_id != root_file_id:
        raise TrustedGoldenMismatchError(
            f"root_file_id mismatch para '{normalized_root}': "
            f"registrado={matching_entry.root_file_id}, observado={root_file_id}"
        )

    # 3. tree_digest (comparación canónica normalizada en minúsculas)
    if (
        matching_entry.tree_digest.digest.lower() != tree_digest.digest.lower()
        or matching_entry.tree_digest.files != tree_digest.files
        or matching_entry.tree_digest.bytes != tree_digest.bytes
    ):
        raise TrustedGoldenMismatchError(
            f"tree_digest mismatch para '{normalized_root}': "
            f"registrado={matching_entry.tree_digest}, observado={tree_digest}"
        )

    return matching_entry


# ============================================================================
# Operaciones de Almacenamiento Atómico sobre Disco
# ============================================================================


def load_trusted_golden_registry(path: pathlib.Path | str) -> TrustedGoldenRegistry:
    """Carga y valida el TGR desde un archivo en disco (Fail-Closed)."""
    file_path = pathlib.Path(path)
    if sys.platform == "win32":
        from sky_claw.local.runtime_vault.trusted_namespace import _check_object_exists_no_reparse

        if not _check_object_exists_no_reparse(file_path):
            raise TrustedGoldenNotFoundError(f"Archivo de TGR no existe: '{file_path}'")
    else:
        if not file_path.exists():
            raise TrustedGoldenNotFoundError(f"Archivo de TGR no existe: '{file_path}'")
    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        raise TrustedRegistryError(f"Error al leer archivo de TGR '{file_path}': {exc}") from exc
    return deserialize_trusted_golden_registry(raw_bytes)


def _validar_esquema_tgr(raw: bytes) -> None:
    """Callback de validación de bytes (contrato ``validate`` de la primitiva atómica).

    Sólo valida: descarta el registro devuelto porque la primitiva espera un
    callable que no devuelve nada.
    """
    deserialize_trusted_golden_registry(raw)


def _write_trusted_registry_atomically_at(
    registry: TrustedGoldenRegistry,
    target_path: pathlib.Path | str,
) -> None:
    """Escribe de forma atómica el TGR en el mismo directorio protegido (ADR 0010 §11.3 / §11.4).

    La secuencia completa (temporal nacido con SD canónico → WriteFile →
    FlushFileBuffers → verificación por handle → os.replace → re-verificación →
    relectura byte a byte + validación de esquema) vive en UNA sola primitiva:
    ``trusted_namespace.write_secured_file_atomically_at``, compartida con el
    registro de Golden Admission. Este wrapper sólo aporta lo propio del TGR:

    - el guard de plataforma Windows (fail-closed sin tocar el disco),
    - su tipo de error histórico (``TrustedRegistryError``),
    - la validación de esquema vía ``deserialize_trusted_golden_registry``.

    No re-implementa la secuencia: duplicarla es exactamente la forma en que
    las garantías de un hermano quedan sin aplicar al otro.
    """
    dest = pathlib.Path(target_path)
    if sys.platform != "win32":
        raise TrustedRegistryUnsupportedError(
            "_write_trusted_registry_atomically_at solo está soportado en Windows con garantías Win32 de seguridad"
        )

    from sky_claw.local.runtime_vault.trusted_namespace import write_secured_file_atomically_at

    canonical_bytes = serialize_trusted_golden_registry(registry)
    write_secured_file_atomically_at(
        dest,
        canonical_bytes,
        "trusted_goldens.json",
        validate=_validar_esquema_tgr,
        error_factory=TrustedRegistryError,
        parent_error_message=f"El directorio padre para TGR no existe o no es confiable: '{dest.parent}'",
    )


__all__ = [
    "MAX_UINT64",
    "MAX_UINT128",
    "TGR_SCHEMA_VERSION",
    "TrustedGoldenEntry",
    "TrustedGoldenMismatchError",
    "TrustedGoldenNotFoundError",
    "TrustedGoldenRegistry",
    "TrustedRegistryDuplicateError",
    "TrustedRegistryError",
    "TrustedRegistryParseError",
    "TrustedRegistrySchemaError",
    "TrustedRegistryUnsupportedError",
    "deserialize_trusted_golden_registry",
    "load_trusted_golden_registry",
    "serialize_trusted_golden_registry",
    "trusted_golden_entry_from_dict",
    "trusted_golden_entry_to_dict",
    "verify_trusted_golden_binding",
]
