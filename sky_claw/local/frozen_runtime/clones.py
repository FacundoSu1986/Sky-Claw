"""Registro y autoridad durable de RuntimeCloneRecord (P5-B).

ADR 0012 §19c, §34.10, §36.7 son las fuentes normativas:
- Registro bajo ``state/clones/<clone-id>.json`` (schema v1).
- Linaje explícito con ``source_generation_id`` (verificado contra state/generations/).
- Máquina de estados: CREATED -> PROVISIONING -> PROVISIONED | INVALID.
- Exclusión física de la Managed Source y de versions/ (SFR-18, SFR-19).
- Exclusión interproceso e intraproceso mediante ``participant_lock`` del FrozenRuntimeRoot.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from typing import Any

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeError,
    InvalidCloneIdError,
    RuntimeCloneAdmissionError,
    RuntimeCloneCollisionError,
    RuntimeCloneCorruptError,
    RuntimeCloneLifecycleError,
    RuntimeCloneNotFoundError,
    RuntimeCloneSchemaError,
)
from sky_claw.local.frozen_runtime.generation_id import validar_generation_id
from sky_claw.local.frozen_runtime.independence import (
    descripcion_de_enlace,
    verify_clone_physical_integrity,
)
from sky_claw.local.frozen_runtime.state import write_json_atomic
from sky_claw.local.frozen_runtime.storage import (
    _RESERVADOS_WINDOWS,
    _contenida,
    _forma_reservada_windows,
    candidates_dir,
    clone_record_path,
    generation_dir,
    state_dir,
    versions_dir,
)
from sky_claw.local.frozen_runtime.storage_models import (
    GenerationVerificationResult,
    GenerationVerificationState,
    IndependenceState,
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
)
from sky_claw.local.runtime_vault.runtime_observation import (
    RuntimeObservationError,
    observe_runtime_identity_from_root,
)

CLONE_RECORD_SCHEMA_VERSION = 1
CANONICAL_ADMITTED_ROLE = "runtime_clone"

__all__ = [
    "CANONICAL_ADMITTED_ROLE",
    "CLONE_ID_PATTERN",
    "CLONE_RECORD_SCHEMA_VERSION",
    "RuntimeCloneLifecycle",
    "RuntimeCloneRecord",
    "VALID_LIFECYCLE_TRANSITIONS",
    "actualizar_lifecycle_clone",
    "admitir_clone_root_path",
    "cargar_runtime_clone_record",
    "leer_runtime_clone_record",
    "registrar_runtime_clone_record",
    "validar_clone_id",
    "verificar_runtime_clone",
]

#: Charset seguro para clone_id: alfanuméricos, guiones, puntos y guiones bajos simples.
CLONE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[a-z0-9._-]*[a-z0-9]+)?$")

#: Transiciones válidas en la máquina de estados de RuntimeCloneLifecycle (§36.7).
VALID_LIFECYCLE_TRANSITIONS: dict[RuntimeCloneLifecycle, frozenset[RuntimeCloneLifecycle]] = {
    RuntimeCloneLifecycle.CREATED: frozenset(
        {
            RuntimeCloneLifecycle.PROVISIONING,
            RuntimeCloneLifecycle.INVALID,
        }
    ),
    RuntimeCloneLifecycle.PROVISIONING: frozenset(
        {
            RuntimeCloneLifecycle.PROVISIONED,
            RuntimeCloneLifecycle.INVALID,
        }
    ),
    RuntimeCloneLifecycle.PROVISIONED: frozenset(
        {
            RuntimeCloneLifecycle.INVALID,
        }
    ),
    RuntimeCloneLifecycle.INVALID: frozenset(),  # terminal
}


def _token_reservado(clone_id: str) -> str | None:
    token = re.split(r"[._-]", clone_id, maxsplit=1)[0]
    return token if token in _RESERVADOS_WINDOWS else None


def validar_clone_id(clone_id: str) -> str:
    """Valida y normaliza un clone_id (casefold); fail-closed ante cualquier ambigüedad."""
    if not isinstance(clone_id, str) or not clone_id:
        raise InvalidCloneIdError("clone_id no puede ser vacío")
    if clone_id != clone_id.strip():
        raise InvalidCloneIdError(f"clone_id con espacios en los bordes: '{clone_id}'")
    if len(clone_id) > 128:
        raise InvalidCloneIdError(f"clone_id excede longitud máxima (128): '{clone_id}'")
    if ".." in clone_id or "/" in clone_id or "\\" in clone_id or ":" in clone_id:
        raise InvalidCloneIdError(f"clone_id contiene caracteres de ruta o traversal: '{clone_id}'")

    normalized = clone_id.casefold()
    if not CLONE_ID_PATTERN.fullmatch(normalized):
        raise InvalidCloneIdError(
            f"clone_id con formato inválido: '{clone_id}' (esperado charset [a-z0-9._-], sin traversal ni separadores)"
        )
    reservado = _token_reservado(normalized)
    if reservado is not None:
        raise InvalidCloneIdError(f"clone_id con nombre reservado de Windows: '{reservado}'")
    return normalized


def validar_runtime_setup_id(setup_id: str) -> str:
    """Valida un identificador de RuntimeSetup."""
    if not isinstance(setup_id, str) or not setup_id.strip():
        raise RuntimeCloneSchemaError("runtime_setup_id no puede ser vacío")
    if ".." in setup_id or "/" in setup_id or "\\" in setup_id:
        raise RuntimeCloneSchemaError(f"runtime_setup_id con traversal o separadores: '{setup_id}'")
    return setup_id.strip()


def admitir_clone_root_path(
    root_path: pathlib.Path | str,
    *,
    storage_root: pathlib.Path | None = None,
    managed_source_root: pathlib.Path | None = None,
) -> pathlib.Path:
    """Admisión fail-closed de la ruta operativa de un Runtime Clone.

    Reglas:
    - Ruta absoluta y normalizada, sin componentes relativos ('..' o '.').
    - Sin symlinks, junctions ni reparse points en la ruta ni sus ancestros.
    - Fuera de steamapps/common (Managed Source) y de cualquier directorio steamapps.
    - Fuera de versions/ del storage (SFR-19: una Generation nunca es runtime ejecutado).
    - Fuera de candidates/ y state/ del storage.
    """
    raw_path_str = os.fspath(root_path)
    partes_crudo = [p for p in re.split(r"[\\/]", raw_path_str) if p]
    if ".." in partes_crudo or "." in partes_crudo:
        raise RuntimeCloneAdmissionError(
            f"la ruta del clone contiene componentes relativos ('..' o '.'): '{root_path}'"
        )

    ruta = pathlib.Path(root_path)
    if not ruta.is_absolute():
        raise RuntimeCloneAdmissionError(f"la ruta del clone debe ser absoluta: '{root_path}'")

    norm_str = os.path.normpath(os.path.abspath(raw_path_str))
    norm_ruta = pathlib.Path(norm_str)

    # Inspección de enlaces / reparse points sobre la ruta declarada, la canónica y todos sus ancestros
    motivo = descripcion_de_enlace(ruta)
    if motivo is not None:
        raise RuntimeCloneAdmissionError(f"root de clone redirigido por reparse point/enlace: {motivo}")
    for ancestro in (norm_ruta, *norm_ruta.parents):
        if not ancestro.exists():
            continue
        motivo_ancestro = descripcion_de_enlace(ancestro)
        if motivo_ancestro is not None:
            raise RuntimeCloneAdmissionError(
                f"root de clone redirigido por reparse point/enlace en '{ancestro}': {motivo_ancestro}"
            )

    forma = _forma_reservada_windows(norm_ruta.name)
    if forma is not None:
        raise RuntimeCloneAdmissionError(f"nombre de ruta de clone reservado en Windows: {forma}")

    # Exclusión de steamapps y steamapps/common sobre la ruta canónica y todos sus ancestros
    for componente in (norm_ruta, *norm_ruta.parents):
        if componente.name.casefold() == "common" and componente.parent.name.casefold() == "steamapps":
            raise RuntimeCloneAdmissionError(
                f"el root del clone está dentro de un área administrada por Steam: '{componente}'"
            )
        if componente.name.casefold() == "steamapps":
            raise RuntimeCloneAdmissionError(
                f"el root del clone está dentro de un área administrada por Steam: '{componente}'"
            )

    # Exclusión respecto del storage root
    if storage_root is not None:
        s_root = pathlib.Path(os.path.normpath(os.path.abspath(os.fspath(storage_root))))
        if _contenida(norm_ruta, versions_dir(s_root)):
            raise RuntimeCloneAdmissionError(
                "el root del clone está dentro de versions/ (violación SFR-19: Generation nunca es runtime ejecutado)"
            )
        if _contenida(norm_ruta, candidates_dir(s_root)):
            raise RuntimeCloneAdmissionError("el root del clone está dentro de candidates/")
        if _contenida(norm_ruta, state_dir(s_root)):
            raise RuntimeCloneAdmissionError("el root del clone está dentro de state/")
        if _contenida(norm_ruta, s_root):
            raise RuntimeCloneAdmissionError(f"el root del clone está dentro de (o es) el storage root: '{norm_ruta}'")
        if _contenida(s_root, norm_ruta):
            raise RuntimeCloneAdmissionError(
                f"el storage root está dentro del root del clone (ancestro no permitido): '{norm_ruta}'"
            )

    # Exclusión respecto de la Managed Source
    if managed_source_root is not None:
        ms_root = pathlib.Path(os.path.normpath(os.path.abspath(os.fspath(managed_source_root))))
        if _contenida(norm_ruta, ms_root):
            raise RuntimeCloneAdmissionError(
                f"el root del clone está dentro de (o es) la Managed Source: '{norm_ruta}'"
            )
        if _contenida(ms_root, norm_ruta):
            raise RuntimeCloneAdmissionError(f"la Managed Source está dentro del root del clone: '{ms_root}'")

    return norm_ruta


def _payload_de_clone_record(record: RuntimeCloneRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "clone_id": record.clone_id,
        "source_generation_id": record.source_generation_id,
        "lifecycle": record.lifecycle.value,
        "intended_runtime_setup_id": record.intended_runtime_setup_id,
        "verified_runtime_setup_id": record.verified_runtime_setup_id,
        "root_path": record.root_path,
        "admitted_role": record.admitted_role,
        "created_at_ns": record.created_at_ns,
    }


def _clone_record_de_payload(data: dict[str, Any], *, source_label: str) -> RuntimeCloneRecord:
    if not isinstance(data, dict):
        raise RuntimeCloneCorruptError(f"{source_label}: el registro debe ser un objeto JSON")

    # Validación de schema_version
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise RuntimeCloneSchemaError(f"{source_label}: schema_version debe ser int")
    if schema != CLONE_RECORD_SCHEMA_VERSION:
        raise RuntimeCloneSchemaError(
            f"{source_label}: schema desconocido {schema}; soportado: {CLONE_RECORD_SCHEMA_VERSION} (fail-closed)"
        )

    # Campos requeridos
    requeridos = (
        "schema_version",
        "clone_id",
        "source_generation_id",
        "lifecycle",
        "intended_runtime_setup_id",
        "verified_runtime_setup_id",
        "root_path",
        "admitted_role",
        "created_at_ns",
    )
    for campo in requeridos:
        if campo not in data:
            raise RuntimeCloneSchemaError(f"{source_label}: falta el campo obligatorio '{campo}'")

    raw_clone_id = data["clone_id"]
    if not isinstance(raw_clone_id, str):
        raise RuntimeCloneSchemaError(f"{source_label}: clone_id debe ser string")
    clone_id = validar_clone_id(raw_clone_id)

    raw_gen_id = data["source_generation_id"]
    if not isinstance(raw_gen_id, str):
        raise RuntimeCloneSchemaError(f"{source_label}: source_generation_id debe ser string")
    source_gen_id = validar_generation_id(raw_gen_id)

    raw_lifecycle = data["lifecycle"]
    if not isinstance(raw_lifecycle, str) or raw_lifecycle not in RuntimeCloneLifecycle.__members__:
        raise RuntimeCloneSchemaError(
            f"{source_label}: lifecycle desconocido '{raw_lifecycle}'; "
            f"soportados: {list(RuntimeCloneLifecycle.__members__.keys())}"
        )
    lifecycle = RuntimeCloneLifecycle(raw_lifecycle)

    raw_intended = data["intended_runtime_setup_id"]
    intended_setup = validar_runtime_setup_id(raw_intended)

    raw_verified = data["verified_runtime_setup_id"]
    if raw_verified is not None:
        if not isinstance(raw_verified, str):
            raise RuntimeCloneSchemaError(f"{source_label}: verified_runtime_setup_id debe ser string o null")
        verified_setup = validar_runtime_setup_id(raw_verified)
    else:
        verified_setup = None

    # Invariante de setup vs lifecycle (§19c):
    # PROVISIONED exige verified_runtime_setup_id no null y coincidente con intended
    if lifecycle is RuntimeCloneLifecycle.PROVISIONED:
        if verified_setup is None:
            raise RuntimeCloneSchemaError(
                f"{source_label}: lifecycle PROVISIONED exige verified_runtime_setup_id no null"
            )
        if verified_setup != intended_setup:
            raise RuntimeCloneSchemaError(
                f"{source_label}: setup mismatch (intended={intended_setup!r} != verified={verified_setup!r})"
            )
    elif lifecycle in (RuntimeCloneLifecycle.CREATED, RuntimeCloneLifecycle.PROVISIONING):
        if verified_setup is not None:
            raise RuntimeCloneSchemaError(
                f"{source_label}: lifecycle {lifecycle.value} no admite verified_runtime_setup_id"
            )

    root_path_raw = data["root_path"]
    if not isinstance(root_path_raw, str) or not root_path_raw.strip():
        raise RuntimeCloneSchemaError(f"{source_label}: root_path debe ser string no vacío")

    admitted_role = data["admitted_role"]
    if admitted_role != CANONICAL_ADMITTED_ROLE:
        raise RuntimeCloneAdmissionError(
            f"{source_label}: admitted_role inválido '{admitted_role}' "
            f"(exigido: '{CANONICAL_ADMITTED_ROLE}'; violación SFR-19)"
        )

    created_at_ns = data["created_at_ns"]
    if not isinstance(created_at_ns, int) or isinstance(created_at_ns, bool) or created_at_ns < 0:
        raise RuntimeCloneSchemaError(f"{source_label}: created_at_ns debe ser int >= 0")

    return RuntimeCloneRecord(
        schema_version=schema,
        clone_id=clone_id,
        source_generation_id=source_gen_id,
        lifecycle=lifecycle,
        intended_runtime_setup_id=intended_setup,
        verified_runtime_setup_id=verified_setup,
        root_path=root_path_raw,
        admitted_role=admitted_role,
        created_at_ns=created_at_ns,
    )


def registrar_runtime_clone_record(
    root: pathlib.Path,
    record: RuntimeCloneRecord,
    *,
    timeout: float = 0.0,
) -> RuntimeCloneRecord:
    """Registra un RuntimeCloneRecord bajo state/clones/<clone-id>.json.

    Idempotente si el registro ya existe con identidad idéntica.
    Falla con RuntimeCloneCollisionError si ya existe con datos distintos (no-clobber).
    Exclusión mediante participant_lock del FrozenRuntimeRoot.
    """
    if (
        not isinstance(record.schema_version, int)
        or isinstance(record.schema_version, bool)
        or record.schema_version != CLONE_RECORD_SCHEMA_VERSION
    ):
        raise RuntimeCloneSchemaError(
            f"schema_version inválido {record.schema_version}; soportado: {CLONE_RECORD_SCHEMA_VERSION}"
        )

    cid = validar_clone_id(record.clone_id)
    gid = validar_generation_id(record.source_generation_id)

    if not isinstance(record.lifecycle, RuntimeCloneLifecycle):
        raise RuntimeCloneSchemaError(f"lifecycle inválido: {record.lifecycle!r}")

    intended = validar_runtime_setup_id(record.intended_runtime_setup_id)
    if record.verified_runtime_setup_id is not None:
        if not isinstance(record.verified_runtime_setup_id, str):
            raise RuntimeCloneSchemaError("verified_runtime_setup_id debe ser string o None")
        verified = validar_runtime_setup_id(record.verified_runtime_setup_id)
    else:
        verified = None

    if not isinstance(record.created_at_ns, int) or isinstance(record.created_at_ns, bool) or record.created_at_ns < 0:
        raise RuntimeCloneSchemaError("created_at_ns debe ser int >= 0")

    if record.admitted_role != CANONICAL_ADMITTED_ROLE:
        raise RuntimeCloneAdmissionError(f"admitted_role inválido: '{record.admitted_role}'")

    # Invariante de setup vs lifecycle
    if record.lifecycle is RuntimeCloneLifecycle.PROVISIONED:
        if verified is None:
            raise RuntimeCloneSchemaError("PROVISIONED exige verified_runtime_setup_id no null")
        if verified != intended:
            raise RuntimeCloneSchemaError(f"setup mismatch: intended '{intended}' != verified '{verified}'")
    elif record.lifecycle in (RuntimeCloneLifecycle.CREATED, RuntimeCloneLifecycle.PROVISIONING):
        if verified is not None:
            raise RuntimeCloneSchemaError(f"lifecycle {record.lifecycle.value} no admite verified_runtime_setup_id")

    # Validación de admisión de la ruta del clone
    if not isinstance(record.root_path, str) or not record.root_path.strip():
        raise RuntimeCloneSchemaError("root_path debe ser string no vacío")
    admitted_root = admitir_clone_root_path(record.root_path, storage_root=root)

    # Validar existencia de la Generation de origen en el storage
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeStorageError
    from sky_claw.local.frozen_runtime.generations import leer_generation_metadata

    try:
        gen_meta = leer_generation_metadata(root, gid)
    except (FrozenRuntimeError, OSError) as exc:
        raise FrozenRuntimeStorageError(
            f"metadata ausente para la generation '{gid}' declarada en clone '{cid}': {exc}"
        ) from exc
    if gen_meta.generation_id != gid:
        raise FrozenRuntimeStorageError(f"generation_id en metadata '{gen_meta.generation_id}' no coincide con '{gid}'")

    canonical_record = RuntimeCloneRecord(
        schema_version=record.schema_version,
        clone_id=cid,
        source_generation_id=gid,
        lifecycle=record.lifecycle,
        intended_runtime_setup_id=intended,
        verified_runtime_setup_id=verified,
        root_path=os.fspath(admitted_root),
        admitted_role=record.admitted_role,
        created_at_ns=record.created_at_ns,
    )

    # Validar que la serialización round-trip sea consistente
    payload = _payload_de_clone_record(canonical_record)
    _clone_record_de_payload(payload, source_label=f"registro de clone '{cid}'")

    destino = clone_record_path(root, cid)

    from sky_claw.local.frozen_runtime.root_lock import participant_lock
    from sky_claw.local.frozen_runtime.state import reservar_ruta_json_exclusiva

    with participant_lock(root, timeout=timeout) as handle:
        handle.assert_owned()

        try:
            existente = _leer_record_de_ruta(destino)
        except RuntimeCloneNotFoundError:
            try:
                reservar_ruta_json_exclusiva(destino)
            except FileExistsError as exc:
                raise RuntimeCloneCollisionError(
                    f"clone_id '{cid}' ya existe (colisión en reserva exclusiva): {exc}"
                ) from exc
            write_json_atomic(destino, payload)
            return canonical_record

        if existente == canonical_record:
            return existente
        raise RuntimeCloneCollisionError(f"clone_id '{cid}' ya existe con identidad distinta (no-clobber fail-closed)")


def actualizar_lifecycle_clone(
    root: pathlib.Path,
    clone_id: str,
    nuevo_lifecycle: RuntimeCloneLifecycle,
    *,
    verified_runtime_setup_id: str | None = None,
    timeout: float = 0.0,
) -> RuntimeCloneRecord:
    """Actualiza la máquina de estados de un RuntimeCloneRecord bajo exclusión de root."""
    if not isinstance(nuevo_lifecycle, RuntimeCloneLifecycle):
        raise RuntimeCloneSchemaError(f"lifecycle inválido: {nuevo_lifecycle!r}")

    cid = validar_clone_id(clone_id)
    destino = clone_record_path(root, cid)

    if verified_runtime_setup_id is not None:
        if not isinstance(verified_runtime_setup_id, str):
            raise RuntimeCloneSchemaError("verified_runtime_setup_id debe ser string o None")
        v_setup_cand = validar_runtime_setup_id(verified_runtime_setup_id)
    else:
        v_setup_cand = None

    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    with participant_lock(root, timeout=timeout) as handle:
        handle.assert_owned()

        existente = leer_runtime_clone_record(root, cid)
        if existente.lifecycle == nuevo_lifecycle:
            return existente

        permitidas = VALID_LIFECYCLE_TRANSITIONS.get(existente.lifecycle, frozenset())
        if nuevo_lifecycle not in permitidas:
            raise RuntimeCloneLifecycleError(
                f"transición inválida de {existente.lifecycle.value} a {nuevo_lifecycle.value} "
                f"(permitidas: {[p.value for p in permitidas]})" + (" (estado terminal)" if not permitidas else "")
            )

        if nuevo_lifecycle in (RuntimeCloneLifecycle.CREATED, RuntimeCloneLifecycle.PROVISIONING):
            if v_setup_cand is not None:
                raise RuntimeCloneSchemaError(f"lifecycle {nuevo_lifecycle.value} no admite verified_runtime_setup_id")
            v_setup = None
        elif nuevo_lifecycle is RuntimeCloneLifecycle.PROVISIONED:
            v_setup = v_setup_cand or existente.verified_runtime_setup_id
            if not v_setup:
                raise RuntimeCloneLifecycleError("PROVISIONED exige verified_runtime_setup_id no vacío")
            if v_setup != existente.intended_runtime_setup_id:
                raise RuntimeCloneLifecycleError(
                    f"setup mismatch: intended '{existente.intended_runtime_setup_id}' != verified '{v_setup}'"
                )
        else:
            # INVALID conserva verified_runtime_setup_id si existía o usa el provisto (validado)
            v_setup = v_setup_cand or existente.verified_runtime_setup_id

        actualizado = RuntimeCloneRecord(
            schema_version=existente.schema_version,
            clone_id=existente.clone_id,
            source_generation_id=existente.source_generation_id,
            lifecycle=nuevo_lifecycle,
            intended_runtime_setup_id=existente.intended_runtime_setup_id,
            verified_runtime_setup_id=v_setup,
            root_path=existente.root_path,
            admitted_role=existente.admitted_role,
            created_at_ns=existente.created_at_ns,
        )

        payload = _payload_de_clone_record(actualizado)
        _clone_record_de_payload(payload, source_label=f"actualización lifecycle clone '{cid}'")
        write_json_atomic(destino, payload)
        return actualizado


def _leer_record_de_ruta(ruta: pathlib.Path) -> RuntimeCloneRecord:
    """Lee y parsea un registro desde su ruta, validando redirección fail-closed."""
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: no se pudo inspeccionar el archivo de registro: {exc}") from exc
    if kind is not None:
        raise RuntimeCloneCorruptError(f"{ruta}: el archivo de registro es un enlace/reparse ({kind}): fail-closed")

    for ancestro in (ruta.parent, *ruta.parent.parents):
        if not ancestro.exists():
            continue
        motivo_ancestro = descripcion_de_enlace(ancestro)
        if motivo_ancestro is not None:
            raise RuntimeCloneCorruptError(
                f"{ruta}: namespace de clones redirigido por reparse point/enlace en ancestro '{ancestro}': {motivo_ancestro}"
            )

    if not ruta.exists():
        raise RuntimeCloneNotFoundError(f"registro de clone no encontrado: '{ruta}'")

    try:
        raw = ruta.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: contenido no UTF-8: {exc}") from exc
    except OSError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: no se pudo leer el archivo: {exc}") from exc

    if not raw.strip():
        raise RuntimeCloneCorruptError(f"{ruta}: archivo de registro vacío o corrupto (0 bytes)")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeCloneCorruptError(f"{ruta}: JSON malformado o truncado: {exc}") from exc

    record = _clone_record_de_payload(data, source_label=str(ruta))
    canonical_id = ruta.stem
    if record.clone_id != canonical_id:
        raise RuntimeCloneCorruptError(
            f"{ruta}: clone_id en payload '{record.clone_id}' no coincide con el namespace canónico '{canonical_id}': fail-closed"
        )
    return record


def leer_runtime_clone_record(root: pathlib.Path, clone_id: str) -> RuntimeCloneRecord:
    """Lectura estricta y tipada de un RuntimeCloneRecord (fail-closed ante ausencia o corrupción)."""
    cid = validar_clone_id(clone_id)
    destino = clone_record_path(root, cid)
    return _leer_record_de_ruta(destino)


def cargar_runtime_clone_record(root: pathlib.Path, clone_id: str) -> RuntimeCloneRecord | None:
    """Carga de un RuntimeCloneRecord; devuelve None si no existe, lanza ante corrupción."""
    cid = validar_clone_id(clone_id)
    destino = clone_record_path(root, cid)
    try:
        return _leer_record_de_ruta(destino)
    except RuntimeCloneNotFoundError:
        return None


def verificar_runtime_clone(
    root: pathlib.Path,
    clone_id: str,
    *,
    managed_source_root: pathlib.Path | None = None,
) -> GenerationVerificationResult:
    """Verifica físicamente el Runtime Clone contra su Generation registrada."""
    try:
        record = leer_runtime_clone_record(root, clone_id)
    except (FrozenRuntimeError, OSError) as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"no se pudo cargar el registro del clone '{clone_id}': {exc}",
        )

    # 0. El clone debe estar en estado PROVISIONED (ADR 0012 §29.3, SFR-17)
    if record.lifecycle is not RuntimeCloneLifecycle.PROVISIONED:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"el lifecycle del clone '{clone_id}' no es PROVISIONED ({record.lifecycle.value}): fail-closed",
        )

    # 1. Verificar existencia, vinculación e integridad de la Generation de origen (ADR 0012 §29.3, SFR-17)
    from sky_claw.local.frozen_runtime.generations import verificar_generation

    gen_verif = verificar_generation(root, record.source_generation_id)
    if gen_verif.state is not GenerationVerificationState.VALID:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=(
                f"Generation de origen no existe o no es VALID "
                f"({record.source_generation_id}, {gen_verif.state.value}): {gen_verif.message}"
            ),
            recorded=gen_verif.recorded,
        )

    gen_meta = gen_verif.recorded
    if gen_meta is None or gen_meta.generation_id != record.source_generation_id:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=(
                f"Generation de origen con generation_id discordante "
                f"('{getattr(gen_meta, 'generation_id', None)}' != '{record.source_generation_id}')"
            ),
            recorded=gen_meta,
        )

    # 2. Admisión de la ruta física
    try:
        clone_root = admitir_clone_root_path(
            record.root_path, storage_root=root, managed_source_root=managed_source_root
        )
    except RuntimeCloneAdmissionError as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"admisión física del clone rechazada: {exc}",
            recorded=gen_meta,
        )

    if not clone_root.is_dir():
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"la ruta del clone no existe o no es un directorio: '{clone_root}'",
            recorded=gen_meta,
        )

    # 3. Integridad física y aislamiento del Clone (SFR-18, ADR 0012 §35.4/§35.11)
    gen_dir = generation_dir(root, record.source_generation_id)
    integridad = verify_clone_physical_integrity(
        clone_root,
        generation_root=gen_dir if gen_dir.is_dir() else None,
        managed_source_root=managed_source_root,
    )
    if integridad.state is IndependenceState.VIOLATED:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"integridad física del clone violada: {integridad.message}",
            recorded=gen_meta,
        )
    if integridad.state is IndependenceState.INDETERMINATE:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"integridad física del clone no demostrable: {integridad.message}",
            recorded=gen_meta,
        )

    # 4. Observación de identidad fresca de runtime
    try:
        obs = observe_runtime_identity_from_root(clone_root, expected_game_key="skyrimse")
    except RuntimeObservationError as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"falló la observación de identidad de runtime en el clone: {exc}",
            recorded=gen_meta,
        )

    if obs.runtime_identity != gen_meta.runtime_identity:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=(
                f"identidad de runtime observada '{obs.runtime_identity.game_version}' "
                f"difiere de la registrada en Generation '{gen_meta.runtime_identity.game_version}'"
            ),
            recorded=gen_meta,
            observed_identity=obs.runtime_identity,
        )

    # 4. Verificación de archivos críticos
    for crit in gen_meta.critical_files:
        c_path = clone_root / crit.rel_path
        if not c_path.exists():
            return GenerationVerificationResult(
                state=GenerationVerificationState.INVALID,
                message=f"archivo crítico ausente en clone: '{crit.rel_path}'",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )
        try:
            st = c_path.stat()
            if st.st_size != crit.size:
                return GenerationVerificationResult(
                    state=GenerationVerificationState.INVALID,
                    message=f"tamaño de archivo crítico '{crit.rel_path}' difiere de la Generation",
                    recorded=gen_meta,
                    observed_identity=obs.runtime_identity,
                )
            hasher = hashlib.sha256()
            with c_path.open("rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
            digest_calculado = hasher.hexdigest().lower()
            if digest_calculado != crit.digest.lower():
                return GenerationVerificationResult(
                    state=GenerationVerificationState.INVALID,
                    message=(
                        f"digest de archivo crítico '{crit.rel_path}' difiere de la Generation: "
                        f"{digest_calculado} != {crit.digest}"
                    ),
                    recorded=gen_meta,
                    observed_identity=obs.runtime_identity,
                )
        except OSError as exc:
            return GenerationVerificationResult(
                state=GenerationVerificationState.INDETERMINATE,
                message=f"no se pudo inspeccionar archivo crítico '{crit.rel_path}': {exc}",
                recorded=gen_meta,
                observed_identity=obs.runtime_identity,
            )

    return GenerationVerificationResult(
        state=GenerationVerificationState.VALID,
        message="Runtime Clone verificado exitosamente",
        recorded=gen_meta,
        observed_identity=obs.runtime_identity,
    )
