"""``operation_lock_binding.json`` — evidencia durable MÍNIMA y PRE-plan (GP2-S4C).

Por qué existe
--------------
ADR 0010 §12.2 fija el orden normativo del helper elevado:

    paso 5  validaciones de precondiciones del plan
    paso 6  adquirir el GoldenMutationLock exclusivo
    paso 7  producir el FULL AUTHORIZED PLAN (gate antes del primer MUTATING(1))

Entre el paso 6 y el paso 7 existe una ventana en la que el lock está VIVO y
``authorized_plan.json`` todavía NO existe (tampoco el journal). Si el proceso
muere ahí, el lock queda huérfano con ``phase != RELEASED`` y, como su clave es
``VolumeSerialNumber + root_file_id`` —no ``operation_id``—, el recovery no
tenía ninguna evidencia durable de la que derivar la identidad física: clasificaba
``plan=NOT_DURABLE / journal=ABSENT`` y devolvía ``NO_TRANSACTION`` sin tocar el
lock. El Golden quedaba bloqueado de forma permanente (toda nueva operación
recibe ``GoldenLockOrphanedError`` y el recovery de esa operación responde
``NO_TRANSACTION``): un ciclo cerrado sin salida.

Este binding cierra esa ventana publicando, ANTES de adquirir el lock, la
única evidencia que sí existe en ese instante: a qué operación pertenece qué
identidad física de Golden.

Qué autoridad tiene
-------------------
Únicamente: *«para esta operation_id, ésta es la identidad física del Golden
cuyo lock puede necesitar recuperación»*.

Qué autoridad NO tiene
----------------------
NO es ``AuthorizedPlan`` (no autoriza mutar nada), NO es permiso de mutación,
NO es staging, NO es el Trusted Golden Registry, NO es evidencia de que una ACL
haya sido modificada y NO habilita ningún rollback: sin
``authorized_plan.json`` no hay PRE autoritativo, luego **nunca** hay rollback
ACL. Tampoco se consulta ``UNTRUSTED_STAGING`` en ningún momento.

Integridad
----------
Como ``authorized_plan.json`` y ``protection_journal.json``: objeto del
namespace protegido ``AUTHORIZED_OPERATIONS`` (DACL canónica: Admins/SYSTEM
full, AU read-only), publicación **create-once** con ``CreateHardLinkW``
(single-winner determinista, sin fallback a ``os.replace``), ``WriteFile`` +
``FlushFileBuffers`` verificado, verificación por handle, relectura byte a byte
y esquema cerrado. El payload lleva su propio ``binding_digest`` (SHA-256 de los
bytes canónicos del contenido, estilo ``plan_digest``) para que una
sustitución wholesale del archivo también falle cerrada, y ``lock_key`` se
re-deriva de ``(volume_serial_number, root_file_id)`` al cargar, de modo que
ninguna sustitución puede reapuntar la identidad sin romper el binding.

Ante evidencia ambigua (binding corrupto, ilegible o divergente) NO se toca el
lock ni el Golden: la clasificación es ``INDETERMINATE`` y el operador decide.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sky_claw.local.runtime_vault.golden_mutation_lock import derive_golden_lock_key
from sky_claw.local.runtime_vault.models import RuntimeVaultError

#: Versión del esquema del binding. Cambiarla es un corte de contrato explícito.
OPERATION_LOCK_BINDING_SCHEMA_VERSION = 1

#: Nombre canónico dentro de ``operations/<operation_id>/``.
OPERATION_LOCK_BINDING_FILE_NAME = "operation_lock_binding.json"

_UINT64_MAX = 0xFFFFFFFFFFFFFFFF
_UINT128_MAX = (1 << 128) - 1
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: Claves del documento persistido (esquema CERRADO).
_BINDING_ROOT_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "operation_id",
        "volume_serial_number",
        "root_file_id",
        "lock_key",
        "created_at",
        "binding_digest",
    }
)
#: Claves que entran en el digest (todo menos el propio digest).
_BINDING_CONTENT_KEYS: frozenset[str] = _BINDING_ROOT_KEYS - {"binding_digest"}


# ============================================================================
# Jerarquía de errores
# ============================================================================


class OperationLockBindingError(RuntimeVaultError):
    """Base de los errores del binding de operación↔lock."""


class OperationLockBindingSchemaError(OperationLockBindingError):
    """Estructura, tipos o digest del binding no conformes."""


class OperationLockBindingAlreadyExistsError(OperationLockBindingError):
    """Ya existe un binding publicado: la política create-once no sustituye."""


class OperationLockBindingUnsupportedError(OperationLockBindingError):
    """Plataforma sin las garantías Win32 del namespace protegido."""


class OperationLockBindingEvidence(StrEnum):
    """Clasificación por evidencia observable (nunca lanza por datos de disco)."""

    DURABLE = "durable"
    ABSENT = "absent"
    INDETERMINATE = "indeterminate"


# ============================================================================
# Modelo
# ============================================================================


@dataclass(frozen=True, slots=True)
class OperationLockBinding:
    """Binding inmutable entre ``operation_id`` e identidad física del Golden."""

    schema_version: int
    operation_id: str
    volume_serial_number: int
    root_file_id: int
    lock_key: str
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version != OPERATION_LOCK_BINDING_SCHEMA_VERSION:
            raise OperationLockBindingSchemaError(
                f"schema_version {self.schema_version!r} no es la versión soportada "
                f"{OPERATION_LOCK_BINDING_SCHEMA_VERSION}"
            )
        _validate_canonical_operation_id(self.operation_id)
        _validate_uint(self.volume_serial_number, "volume_serial_number", _UINT64_MAX)
        _validate_uint(self.root_file_id, "root_file_id", _UINT128_MAX)
        if not isinstance(self.lock_key, str) or not self.lock_key:
            raise OperationLockBindingSchemaError("lock_key debe ser string no vacío")
        esperado = derive_golden_lock_key(self.volume_serial_number, self.root_file_id)
        if self.lock_key != esperado:
            raise OperationLockBindingSchemaError(
                f"lock_key '{self.lock_key}' no corresponde a (volume_serial_number={self.volume_serial_number}, "
                f"root_file_id={self.root_file_id}): binding de identidad incoherente"
            )
        if not isinstance(self.created_at, str) or not _ISO_Z_RE.match(self.created_at):
            raise OperationLockBindingSchemaError("created_at debe ser timestamp ISO-8601 UTC con sufijo 'Z'")

    @property
    def physical_identity(self) -> tuple[int, int]:
        return (self.volume_serial_number, self.root_file_id)


def build_operation_lock_binding(
    *,
    operation_id: str,
    volume_serial_number: int,
    root_file_id: int,
    created_at: str,
) -> OperationLockBinding:
    """Construye el binding con ``lock_key`` DERIVADO (nunca provisto por el caller)."""
    return OperationLockBinding(
        schema_version=OPERATION_LOCK_BINDING_SCHEMA_VERSION,
        operation_id=operation_id,
        volume_serial_number=volume_serial_number,
        root_file_id=root_file_id,
        lock_key=derive_golden_lock_key(volume_serial_number, root_file_id),
        created_at=created_at,
    )


# ============================================================================
# Serialización canónica
# ============================================================================


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rechaza claves JSON duplicadas (mismo contrato que el plan autoritativo)."""
    resultado: dict[str, Any] = {}
    for clave, valor in pairs:
        if clave in resultado:
            raise ValueError(f"clave duplicada en el documento JSON: '{clave}'")
        resultado[clave] = valor
    return resultado


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _ahora_iso_z() -> str:
    """Sello temporal ISO-8601 UTC con sufijo 'Z' (mismo formato que el journal)."""
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _content_dict(binding: OperationLockBinding) -> dict[str, Any]:
    return {
        "schema_version": binding.schema_version,
        "operation_id": binding.operation_id,
        "volume_serial_number": binding.volume_serial_number,
        "root_file_id": binding.root_file_id,
        "lock_key": binding.lock_key,
        "created_at": binding.created_at,
    }


def serialize_operation_lock_binding_content(binding: OperationLockBinding) -> bytes:
    """Bytes canónicos del contenido (entrada del digest)."""
    return _canonical_json(_content_dict(binding))


def compute_operation_lock_binding_digest(binding: OperationLockBinding) -> str:
    """SHA-256 de los bytes canónicos del contenido (estilo ``plan_digest``)."""
    return hashlib.sha256(serialize_operation_lock_binding_content(binding)).hexdigest()


def serialize_operation_lock_binding(binding: OperationLockBinding) -> bytes:
    """Documento completo persistido: contenido canónico + ``binding_digest``."""
    payload = _content_dict(binding)
    payload["binding_digest"] = compute_operation_lock_binding_digest(binding)
    return _canonical_json(payload)


def deserialize_operation_lock_binding(raw: bytes) -> OperationLockBinding:
    """Carga fail-closed: JSON UTF-8 sin claves duplicadas, esquema cerrado, digest
    coincidente y bytes IDENTICOS a la serialización canónica."""
    if not isinstance(raw, bytes) or not raw:
        raise OperationLockBindingSchemaError("operation_lock_binding.json no puede estar vacío")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OperationLockBindingSchemaError(f"operation_lock_binding.json no es JSON UTF-8 válido: {exc}") from exc
    if not isinstance(payload, dict):
        raise OperationLockBindingSchemaError("operation_lock_binding.json debe ser un objeto JSON")
    observed = set(payload.keys())
    if observed != _BINDING_ROOT_KEYS:
        ausentes = sorted(_BINDING_ROOT_KEYS - observed)
        desconocidas = sorted(observed - _BINDING_ROOT_KEYS)
        raise OperationLockBindingSchemaError(
            f"esquema de operation_lock_binding.json no cerrado: ausentes={ausentes}, desconocidas={desconocidas}"
        )
    digest = payload["binding_digest"]
    if not isinstance(digest, str) or not _SHA256_HEX_RE.match(digest):
        raise OperationLockBindingSchemaError("binding_digest debe ser SHA-256 hexadecimal minúsculo")

    binding = build_operation_lock_binding(
        operation_id=_require_str(payload["operation_id"], "operation_id"),
        volume_serial_number=_require_uint(payload["volume_serial_number"], "volume_serial_number", _UINT64_MAX),
        root_file_id=_require_uint(payload["root_file_id"], "root_file_id", _UINT128_MAX),
        created_at=_require_str(payload["created_at"], "created_at"),
    )
    # El lock_key se RE-DERIVA (nunca se cree del archivo) y el modelo lo cruza
    # contra (volume_serial_number, root_file_id).
    declarado = _require_str(payload["lock_key"], "lock_key")
    if declarado != binding.lock_key:
        raise OperationLockBindingSchemaError("lock_key declarado no corresponde a la identidad declarada")
    if payload["schema_version"] != OPERATION_LOCK_BINDING_SCHEMA_VERSION:
        raise OperationLockBindingSchemaError(f"schema_version {payload['schema_version']!r} no soportado en disco")
    calculado = compute_operation_lock_binding_digest(binding)
    if calculado != digest:
        raise OperationLockBindingSchemaError(
            "binding_digest no coincide con el contenido: evidencia sustituida o corrupta"
        )
    if raw != serialize_operation_lock_binding(binding):
        raise OperationLockBindingSchemaError(
            "los bytes en disco no son la serialización canónica del binding (reformateado)"
        )
    return binding


# ============================================================================
# Validaciones locales
# ============================================================================


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise OperationLockBindingSchemaError(f"{field} debe ser string")
    return value


def _require_uint(value: Any, field: str, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationLockBindingSchemaError(f"{field} debe ser entero")
    _validate_uint(value, field, max_value)
    return int(value)


def _validate_uint(value: Any, field: str, max_value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationLockBindingSchemaError(f"{field} debe ser entero")
    if value < 0 or value > max_value:
        raise OperationLockBindingSchemaError(f"{field} fuera de rango uint: {value}")


def _validate_canonical_operation_id(value: Any) -> str:
    if not isinstance(value, str):
        raise OperationLockBindingSchemaError("operation_id debe ser string")
    normalizado = value.strip()
    try:
        parseado = uuid.UUID(normalizado)
    except ValueError as exc:
        raise OperationLockBindingSchemaError(f"operation_id {value!r} no es un UUID válido") from exc
    if normalizado != str(parseado):
        raise OperationLockBindingSchemaError("operation_id debe ser UUID canónico con guiones en minúsculas")
    return normalizado


# ============================================================================
# Rutas (namespace protegido)
# ============================================================================


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise OperationLockBindingUnsupportedError(
            "El binding de operación↔lock sólo existe con las garantías Win32 del namespace protegido"
        )


def derive_operation_lock_binding_dir(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """``%ProgramData%\\Sky-Claw\\runtime_vault\\operations\\<operation_id>``."""
    from sky_claw.local.runtime_vault.trusted_registry_lock import _resolve_runtime_vault_dir

    op = _validate_canonical_operation_id(operation_id)
    return pathlib.Path(_resolve_runtime_vault_dir(programdata_resolver=programdata_resolver) / "operations" / op)


def derive_operation_lock_binding_path(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """Path canónico del binding dentro del directorio protegido de la operación."""
    return derive_operation_lock_binding_dir(operation_id, programdata_resolver=programdata_resolver) / (
        OPERATION_LOCK_BINDING_FILE_NAME
    )


# ============================================================================
# Clasificación y carga durable
# ============================================================================


def classify_operation_lock_binding(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> OperationLockBindingEvidence:
    """Clasifica por evidencia observable. NUNCA lanza por datos de disco.

    Ausente es ``ABSENT``; presente pero corrupto/esquema inválido/digest
    inconsistente es ``INDETERMINATE`` (nunca "ausente"), y sólo un binding
    válido, coherente Y perteneciente a esta ``operation_id`` es ``DURABLE``.
    Un binding ajeno en la ruta de esta operación es evidencia contradictoria
    (``INDETERMINATE``), no evidencia utilizable. No consulta staging.
    """
    path = derive_operation_lock_binding_path(operation_id, programdata_resolver=programdata_resolver)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return OperationLockBindingEvidence.ABSENT
    except OSError:
        return OperationLockBindingEvidence.INDETERMINATE
    try:
        binding = deserialize_operation_lock_binding(raw)
    except OperationLockBindingError:
        return OperationLockBindingEvidence.INDETERMINATE
    if binding.operation_id != _validate_canonical_operation_id(operation_id):
        return OperationLockBindingEvidence.INDETERMINATE
    return OperationLockBindingEvidence.DURABLE


_MINT_PROOF = object()


class DurableOperationLockBinding:
    """Binding durable: sólo existe tras publicar create-once, flushear y revalidar.

    No envuelve ningún handle del sistema: envuelve la EVIDENCIA re-leída del
    disco protegido. Se acuña únicamente desde
    :func:`promote_operation_lock_binding` (prueba privada), de modo que ningún
    camino de código pueda fabricar identidad de recovery sin durabilidad
    verificada.
    """

    __slots__ = ("_binding", "_destination", "_digest")

    def __init__(
        self,
        binding: OperationLockBinding,
        destination: pathlib.Path,
        digest: str,
        *,
        _proof: Any = None,
    ) -> None:
        if _proof is not _MINT_PROOF:
            raise OperationLockBindingError(
                "DurableOperationLockBinding sólo se acuña tras publicar + FlushFileBuffers + revalidación"
            )
        if not isinstance(binding, OperationLockBinding):
            raise OperationLockBindingSchemaError("binding debe ser OperationLockBinding")
        if not isinstance(destination, pathlib.Path):
            raise OperationLockBindingSchemaError("destination debe ser pathlib.Path")
        self._binding = binding
        self._destination = destination
        self._digest = digest

    @property
    def binding(self) -> OperationLockBinding:
        return self._binding

    @property
    def destination(self) -> pathlib.Path:
        return self._destination

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def operation_id(self) -> str:
        return self._binding.operation_id

    @property
    def volume_serial_number(self) -> int:
        return self._binding.volume_serial_number

    @property
    def root_file_id(self) -> int:
        return self._binding.root_file_id

    @property
    def physical_identity(self) -> tuple[int, int]:
        return self._binding.physical_identity


def load_operation_lock_binding(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> OperationLockBinding:
    """Lee y valida el binding; lanza tipado si falta o es incoherente."""
    path = derive_operation_lock_binding_path(operation_id, programdata_resolver=programdata_resolver)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise OperationLockBindingSchemaError(f"no existe binding de operación en '{path}'") from exc
    binding = deserialize_operation_lock_binding(raw)
    if binding.operation_id != _validate_canonical_operation_id(operation_id):
        raise OperationLockBindingSchemaError("el binding publicado pertenece a otra operation_id")
    return binding


def load_durable_operation_lock_binding(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> DurableOperationLockBinding:
    """Acuña la autoridad durable del binding tras re-validar los bytes de disco."""
    binding = load_operation_lock_binding(operation_id, programdata_resolver=programdata_resolver)
    path = derive_operation_lock_binding_path(operation_id, programdata_resolver=programdata_resolver)
    return DurableOperationLockBinding(
        binding,
        path,
        compute_operation_lock_binding_digest(binding),
        _proof=_MINT_PROOF,
    )


# ============================================================================
# Publicación durable create-once
# ============================================================================


@runtime_checkable
class OperationLockBindingWriter(Protocol):
    """Publicador create-once de un objeto plano protegido del namespace."""

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        """Publica ``payload`` en ``dest`` UNA vez, con ``FlushFileBuffers`` verificado."""
        ...


class _NamespaceOperationLockBindingWriter:
    """Implementación real sobre la primitiva create-once del namespace protegido."""

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        from sky_claw.local.runtime_vault.trusted_namespace import write_secured_file_create_once_at

        def _error(mensaje: str) -> BaseException:
            return OperationLockBindingError(mensaje)

        def _already(mensaje: str) -> BaseException:
            return OperationLockBindingAlreadyExistsError(mensaje)

        def _validar(raw: bytes) -> None:
            # La primitiva espera un validador que sólo compruebe; el binding se
            # revalida aparte para acuñar autoridad (y para recuperar el modelo).
            deserialize_operation_lock_binding(raw)

        write_secured_file_create_once_at(
            dest,
            payload,
            object_name,
            validate=_validar,
            error_factory=_error,
            already_exists_error_factory=_already,
            parent_error_message="El directorio de operaciones no existe o no es confiable",
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnóstico
        return "_NamespaceOperationLockBindingWriter()"


def _resolve_binding_writer(
    binding_writer: OperationLockBindingWriter | None,
) -> OperationLockBindingWriter:
    if binding_writer is None:
        _ensure_windows()
        return _NamespaceOperationLockBindingWriter()
    if not isinstance(binding_writer, OperationLockBindingWriter):
        raise OperationLockBindingSchemaError("binding_writer no cumple el contrato OperationLockBindingWriter")
    return binding_writer


def promote_operation_lock_binding(
    *,
    operation_id: str,
    volume_serial_number: int,
    root_file_id: int,
    created_at: str | None = None,
    programdata_resolver: Callable[[], object] | None = None,
    binding_writer: OperationLockBindingWriter | None = None,
) -> DurableOperationLockBinding:
    """Publica el binding PRE-plan de forma create-once, protegida y durable.

    Secuencia (espejo de ``promote_durable_authorized_plan``): construir con
    ``lock_key`` derivado → escribir create-once con ``FlushFileBuffers``
    verificado → re-leer byte a byte → re-validar esquema, digest e identidad →
    recién entonces acuñar autoridad. Un fallo de flush NO publica y NO acuña.

    ``created_at=None`` sella el instante con el reloj del proceso; los tests lo
    fijan explícitamente para que la publicación sea determinista.
    """
    binding = build_operation_lock_binding(
        operation_id=operation_id,
        volume_serial_number=volume_serial_number,
        root_file_id=root_file_id,
        created_at=created_at if created_at is not None else _ahora_iso_z(),
    )
    payload = serialize_operation_lock_binding(binding)
    dest = derive_operation_lock_binding_path(operation_id, programdata_resolver=programdata_resolver)
    writer = _resolve_binding_writer(binding_writer)
    if not isinstance(writer, OperationLockBindingWriter):
        raise OperationLockBindingSchemaError("binding_writer no cumple el contrato OperationLockBindingWriter")

    try:
        writer.write_create_once(dest, payload, OPERATION_LOCK_BINDING_FILE_NAME)
    except OperationLockBindingAlreadyExistsError:
        # Create-once: un replay del mismo operation_id NUNCA sustituye el
        # binding existente; el llamador decide (el establecimiento verifica que
        # el binding existente sea el mismo).
        raise
    except Exception as exc:  # noqa: BLE001 — boundary deliberada: se clasifica y se re-lanza tipado
        evidencia = classify_operation_lock_binding(operation_id, programdata_resolver=programdata_resolver)
        raise OperationLockBindingError(
            f"La escritura durable de {OPERATION_LOCK_BINDING_FILE_NAME} falló "
            f"(evidencia observable={evidencia.value}): {exc}"
        ) from exc

    return _revalidate_after_write(binding=binding, dest=dest, payload=payload)


def _revalidate_after_write(
    *,
    binding: OperationLockBinding,
    dest: pathlib.Path,
    payload: bytes,
) -> DurableOperationLockBinding:
    """Post-write revalidation: re-leer, re-validar y recién acuñar."""
    try:
        raw = dest.read_bytes()
    except OSError as exc:
        raise OperationLockBindingError(f"No se pudo re-leer '{dest}' tras la escritura: {exc}") from exc
    if raw != payload:
        raise OperationLockBindingError(f"Revalidación post-escritura falló: los bytes de '{dest}' no coinciden")
    recargado = deserialize_operation_lock_binding(raw)
    if recargado != binding:
        raise OperationLockBindingError(f"El binding re-leído no equivale al publicado en '{dest}'")
    return DurableOperationLockBinding(
        recargado, dest, compute_operation_lock_binding_digest(recargado), _proof=_MINT_PROOF
    )


__all__ = [
    "OPERATION_LOCK_BINDING_FILE_NAME",
    "OPERATION_LOCK_BINDING_SCHEMA_VERSION",
    "DurableOperationLockBinding",
    "OperationLockBinding",
    "OperationLockBindingAlreadyExistsError",
    "OperationLockBindingError",
    "OperationLockBindingEvidence",
    "OperationLockBindingSchemaError",
    "OperationLockBindingUnsupportedError",
    "OperationLockBindingWriter",
    "build_operation_lock_binding",
    "classify_operation_lock_binding",
    "compute_operation_lock_binding_digest",
    "derive_operation_lock_binding_dir",
    "derive_operation_lock_binding_path",
    "deserialize_operation_lock_binding",
    "load_durable_operation_lock_binding",
    "load_operation_lock_binding",
    "promote_operation_lock_binding",
    "serialize_operation_lock_binding",
    "serialize_operation_lock_binding_content",
]
