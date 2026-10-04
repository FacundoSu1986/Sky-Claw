"""Registro de Generations: identity, metadata, discovery y drift (P2).

- **Metadata** (``state/generations/<generation-id>.json``, schema v1): vive
  FUERA del árbol de la Generation a propósito — el árbol permanece
  byte-idéntico al snapshot del que nació, así que la verificación compara el
  digest registrado contra un inventario fresco sin filtrados especiales.
- **SFR-15 conceptual**: el metadata NO auto-autoriza; ``VALID`` sólo sale de
  comparar evidencia fresca contra la registrada.
- **SFR-17**: la Generation publicada es lógicamente inmutable; el drift es
  **on-demand** (sin watcher continuo) y P4/P5 ejecutarán esta primitive antes
  de activar/rollback/reutilizar. ``DRIFTED != VALID`` y no es target de
  rollback sin re-verificación exitosa. Sin auto-repair; sin mutar metadata.
- **Colisiones**: si el id ya existe, sólo es idempotente si full tree digest
  + runtime identity + critical evidence coinciden; si no,
  ``GenerationCollisionError`` (nunca se sobreescribe en silencio).
"""

from __future__ import annotations

import json
import pathlib

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeStorageError,
    GenerationCollisionError,
    InvalidGenerationIdError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.generation_id import (
    construir_generation_id,
    generation_id_desde_version,
    validar_generation_id,
)
from sky_claw.local.frozen_runtime.independence import verify_generation_physical_integrity
from sky_claw.local.frozen_runtime.models import SourceSnapshotEvidence
from sky_claw.local.frozen_runtime.state import write_json_atomic
from sky_claw.local.frozen_runtime.storage import generation_dir, generations_state_dir, versions_dir
from sky_claw.local.frozen_runtime.storage_models import (
    GenerationInventory,
    GenerationMetadata,
    GenerationRecord,
    GenerationVerificationResult,
    GenerationVerificationState,
    IndependenceState,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import FileIdentity, InventoryError, RuntimeIdentity, TreeDigest
from sky_claw.local.runtime_vault.runtime_observation import (
    RuntimeObservationError,
    observe_runtime_identity_from_root,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

METADATA_SCHEMA_VERSION = 1

__all__ = [
    "METADATA_SCHEMA_VERSION",
    "construir_generation_id",
    "descubrir_generations",
    "generation_id_desde_version",
    "generacion_id_desde_evidencia",
    "leer_generation_metadata",
    "registrar_generation_metadata",
    "validar_generation_id",
    "verificar_generation",
]


# ── Metadata: modelo ↔ payload ───────────────────────────────────────────


def _metadata_de_evidencia(evidence: SourceSnapshotEvidence, generation_id: str) -> GenerationMetadata:
    return GenerationMetadata(
        schema_version=METADATA_SCHEMA_VERSION,
        generation_id=generation_id,
        display_version=generation_id.split("__", 1)[0],
        runtime_identity=evidence.runtime_identity,
        tree_digest=evidence.tree_digest,
        critical_files=evidence.critical_files,
        provider=evidence.provider.value,
        provider_appid=evidence.provider_metadata.appid,
        provider_buildid=evidence.provider_metadata.buildid,
        created_at_ns=evidence.observed_at_ns,
    )


def _payload_de_metadata(metadata: GenerationMetadata) -> dict[str, object]:
    return {
        "schema_version": metadata.schema_version,
        "generation_id": metadata.generation_id,
        "display_version": metadata.display_version,
        "runtime_identity": {
            "game_key": metadata.runtime_identity.game_key,
            "game_version": metadata.runtime_identity.game_version,
        },
        "tree_digest": {
            "digest": metadata.tree_digest.digest,
            "files": metadata.tree_digest.files,
            "bytes": metadata.tree_digest.bytes,
        },
        "critical_files": [
            {"rel_path": c.rel_path, "size": c.size, "digest": c.digest} for c in metadata.critical_files
        ],
        "provider": metadata.provider,
        "provider_appid": metadata.provider_appid,
        "provider_buildid": metadata.provider_buildid,
        "created_at_ns": metadata.created_at_ns,
    }


def _metadata_de_payload(payload: dict[str, object], *, source_label: str) -> GenerationMetadata:
    if not isinstance(payload, dict):
        raise StateCorruptError(f"{source_label}: la metadata debe ser un objeto JSON")
    schema = payload.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise StateSchemaError(f"{source_label}: schema_version debe ser int")
    if schema != METADATA_SCHEMA_VERSION:
        raise StateSchemaError(f"{source_label}: schema de metadata desconocido {schema} (fail-closed)")
    generation_id = payload.get("generation_id")
    if not isinstance(generation_id, str):
        raise StateSchemaError(f"{source_label}: generation_id debe ser string")
    validar_generation_id(generation_id)
    identidad = payload.get("runtime_identity")
    if not isinstance(identidad, dict):
        raise StateSchemaError(f"{source_label}: runtime_identity debe ser objeto")
    game_key = identidad.get("game_key")
    game_version = identidad.get("game_version")
    if not isinstance(game_key, str) or not game_key or not isinstance(game_version, str) or not game_version:
        raise StateSchemaError(f"{source_label}: runtime_identity incompleto")
    digest_obj = payload.get("tree_digest")
    if not isinstance(digest_obj, dict):
        raise StateSchemaError(f"{source_label}: tree_digest debe ser objeto")
    digest = digest_obj.get("digest")
    files = digest_obj.get("files")
    bytes_total = digest_obj.get("bytes")
    if not isinstance(digest, str) or not digest:
        raise StateSchemaError(f"{source_label}: tree_digest.digest debe ser string no vacío")
    if (
        not isinstance(files, int)
        or isinstance(files, bool)
        or not isinstance(bytes_total, int)
        or isinstance(bytes_total, bool)
    ):
        raise StateSchemaError(f"{source_label}: tree_digest con tipos incorrectos")
    criticos_raw = payload.get("critical_files")
    if not isinstance(criticos_raw, list) or not criticos_raw:
        raise StateSchemaError(f"{source_label}: critical_files debe ser lista no vacía")
    criticos: list[FileIdentity] = []
    for c in criticos_raw:
        if not isinstance(c, dict):
            raise StateSchemaError(f"{source_label}: critical_files con entrada no objeto")
        rel = c.get("rel_path")
        size = c.get("size")
        cdigest = c.get("digest")
        if (
            not isinstance(rel, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(cdigest, str)
        ):
            raise StateSchemaError(f"{source_label}: critical_files con tipos incorrectos")
        criticos.append(FileIdentity(rel_path=rel, size=size, digest=cdigest))
    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider:
        raise StateSchemaError(f"{source_label}: provider debe ser string")
    creado = payload.get("created_at_ns")
    if not isinstance(creado, int) or isinstance(creado, bool) or creado < 0:
        raise StateSchemaError(f"{source_label}: created_at_ns debe ser int >= 0")
    provider_appid = payload.get("provider_appid")
    provider_buildid = payload.get("provider_buildid")
    if provider_appid is not None and not isinstance(provider_appid, str):
        raise StateSchemaError(f"{source_label}: provider_appid debe ser string o null")
    if provider_buildid is not None and not isinstance(provider_buildid, str):
        raise StateSchemaError(f"{source_label}: provider_buildid debe ser string o null")
    return GenerationMetadata(
        schema_version=schema,
        generation_id=generation_id,
        display_version=generation_id.split("__", 1)[0],
        runtime_identity=RuntimeIdentity(game_key=game_key, game_version=game_version),
        tree_digest=TreeDigest(digest=digest, files=files, bytes=bytes_total),
        critical_files=tuple(criticos),
        provider=provider,
        provider_appid=provider_appid,
        provider_buildid=provider_buildid,
        created_at_ns=creado,
    )


# ── API pública ──────────────────────────────────────────────────────────


def generacion_id_desde_evidencia(evidence: SourceSnapshotEvidence) -> str:
    """Deriva el generation-id (contenido-bound) desde un SourceSnapshotEvidence.

    El display (major.minor.patch) sale de la RuntimeIdentity observada; el
    digest, del TreeDigest del snapshot. El buildid del proveedor jamás forma
    parte del id.
    """
    return generation_id_desde_version(evidence.runtime_identity.game_version, evidence.tree_digest)


def registrar_generation_metadata(root: pathlib.Path, evidence: SourceSnapshotEvidence) -> GenerationMetadata:
    """Persiste la metadata de una Generation (``state/generations/<id>.json``).

    Idempotente si la identidad completa coincide; ``GenerationCollisionError``
    si el id ya existe con identidad distinta (fail-closed). Escritura atómica.
    """
    generation_id = validar_generation_id(generacion_id_desde_evidencia(evidence))
    metadata = _metadata_de_evidencia(evidence, generation_id)
    destino = generations_state_dir(root) / f"{generation_id}.json"
    if destino.exists():
        existente = _metadata_de_payload(_leer_json(destino), source_label=str(destino))
        # Identidad COMPLETA (digest + files + bytes) y críticos comparados SIN
        # dependencia de orden: inventory_tree no promete orden estable de
        # archivos, así que la idempotencia no puede depender del orden.
        if (
            existente.tree_digest == metadata.tree_digest
            and existente.runtime_identity == metadata.runtime_identity
            and _mismos_criticos(existente.critical_files, metadata.critical_files)
        ):
            return existente
        raise GenerationCollisionError(
            f"generation-id '{generation_id}' ya existe con identidad completa distinta "
            "(digest/identidad/evidencia crítica no coinciden): fail-closed, no se sobreescribe"
        )
    write_json_atomic(destino, _payload_de_metadata(metadata))
    return metadata


def _mismos_criticos(a: tuple[FileIdentity, ...], b: tuple[FileIdentity, ...]) -> bool:
    """Igualdad de evidencia crítica sin depender del orden de la tupla."""

    def clave(c: FileIdentity) -> tuple[str, int, str]:
        return (c.rel_path.casefold(), c.size, c.digest)

    return sorted(map(clave, a)) == sorted(map(clave, b))


def _criticos_coinciden(registrados: tuple[FileIdentity, ...], observados: tuple[FileIdentity, ...]) -> bool:
    indice = {f.rel_path.casefold(): f for f in observados}
    for critico in registrados:
        observado = indice.get(critico.rel_path.casefold())
        if observado is None or observado.digest != critico.digest or observado.size != critico.size:
            return False
    return True


def _link_o_none(path: pathlib.Path) -> str | None:
    """Descripción del enlace si *path* es symlink/junction/reparse; None si no.

    ``Path.is_symlink()`` no detecta junctions en Windows: la primitive
    canónica sí. Un OSError de inspección se reporta como no inspeccionable
    (fail-closed para los consumidores).
    """
    try:
        return link_kind_or_raise(path)
    except OSError as exc:
        return f"no inspeccionable: {exc}"


def _leer_json(path: pathlib.Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise StateCorruptError(f"{path}: contenido no UTF-8: {exc}") from exc
    except OSError as exc:
        raise FrozenRuntimeStorageError(f"{path}: no se pudo leer: {exc}") from exc
    if not raw.strip():
        raise StateCorruptError(f"{path}: metadata vacía (archivo de 0 bytes)")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(f"{path}: JSON malformado o truncado: {exc}") from exc
    if not isinstance(data, dict):
        raise StateCorruptError(f"{path}: la metadata debe ser un objeto JSON")
    return data


def leer_generation_metadata(root: pathlib.Path, generation_id: str) -> GenerationMetadata:
    """Lee la metadata de una Generation (fail-closed; ausente ⇒ error tipado)."""
    ident = validar_generation_id(generation_id)
    path = generations_state_dir(root) / f"{ident}.json"
    if not path.exists():
        raise FrozenRuntimeStorageError(f"metadata ausente para la generation '{ident}': '{path}'")
    return _metadata_de_payload(_leer_json(path), source_label=str(path))


def descubrir_generations(root: pathlib.Path) -> GenerationInventory:
    """Lista ``versions/`` clasificando cada entrada; no activa nada.

    Clasificación: con metadata legible ⇒ ``KNOWN`` (metadata cargada); sin
    metadata ⇒ ``UNKNOWN``; enlace/reparse ⇒ ``INVALID``; archivo donde se
    espera directorio ⇒ ``INVALID``. La verificación fresca (VALID/DRIFTED) es
    on-demand vía :func:`verificar_generation`.
    """
    raiz = versions_dir(root)
    records: list[GenerationRecord] = []
    if not raiz.is_dir():
        return GenerationInventory(root=raiz, records=())
    for entrada in sorted(raiz.iterdir(), key=lambda p: p.name):
        link = _link_o_none(entrada)
        if link is not None:
            records.append(
                GenerationRecord(
                    generation_id=None,
                    directory=entrada,
                    metadata=None,
                    state=GenerationVerificationState.INVALID,
                    message=f"enlace/reparse bajo versions/: {link} (fail-closed)",
                )
            )
            continue
        if not entrada.is_dir():
            records.append(
                GenerationRecord(
                    generation_id=None,
                    directory=entrada,
                    metadata=None,
                    state=GenerationVerificationState.INVALID,
                    message="archivo donde se espera un directorio de Generation",
                )
            )
            continue
        metadata_path = generations_state_dir(root) / f"{entrada.name}.json"
        if not metadata_path.exists():
            records.append(
                GenerationRecord(
                    generation_id=None,
                    directory=entrada,
                    metadata=None,
                    state=GenerationVerificationState.UNKNOWN,
                    message="directorio de Generation sin metadata (contenido desconocido; no se borra)",
                )
            )
            continue
        try:
            metadata = _metadata_de_payload(_leer_json(metadata_path), source_label=str(metadata_path))
        except FrozenRuntimeStorageError as exc:
            records.append(
                GenerationRecord(
                    generation_id=entrada.name if validar_id_seguro(entrada.name) else None,
                    directory=entrada,
                    metadata=None,
                    state=GenerationVerificationState.INDETERMINATE,
                    message=f"metadata ilegible: {exc}",
                )
            )
            continue
        if metadata.generation_id != entrada.name:
            records.append(
                GenerationRecord(
                    generation_id=None,
                    directory=entrada,
                    metadata=metadata,
                    state=GenerationVerificationState.INVALID,
                    message=(
                        f"metadata declara generation_id {metadata.generation_id!r} pero vive en "
                        f"'{entrada.name}': incoherente (fail-closed)"
                    ),
                )
            )
            continue
        records.append(
            GenerationRecord(
                generation_id=metadata.generation_id,
                directory=entrada,
                metadata=metadata,
                state=GenerationVerificationState.UNKNOWN,
                message="Generation conocida; verificación fresca on-demand",
            )
        )
    return GenerationInventory(root=raiz, records=tuple(records))


def validar_id_seguro(nombre: str) -> bool:
    try:
        validar_generation_id(nombre)
    except InvalidGenerationIdError:
        return False
    return True


def verificar_generation(root: pathlib.Path, generation_id: str) -> GenerationVerificationResult:
    """Verificación on-demand de drift de una Generation (P2 hook para P4/P5).

    Reobserva identidad + inventario sellado frescos y los compara contra la
    metadata registrada. ``VALID`` sólo si digest, identidad y evidencia
    crítica coinciden; ``DRIFTED`` ante cualquier diferencia; ``INDETERMINATE``
    si algo es inobservable. Sin auto-repair; sin mutar metadata.

    Costo aceptado (P2): recorre el árbol DOS veces (integridad física +
    inventario). Para una instalación completa esto son segundos por corrida
    on-demand; la optimización (un solo walk que alimente ambos) queda para
    P3/P4 con números del rig real, sin debilitar la frescura de la evidencia.
    """
    ident = validar_generation_id(generation_id)
    directorio = generation_dir(root, ident)
    if not directorio.is_dir():
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"la generation '{ident}' no existe bajo versions/: '{directorio}'",
        )
    metadata_path = generations_state_dir(root) / f"{ident}.json"
    if not metadata_path.exists():
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"metadata ausente para '{ident}': no hay expectativa registrada que comparar",
        )
    try:
        metadata = _metadata_de_payload(_leer_json(metadata_path), source_label=str(metadata_path))
    except FrozenRuntimeStorageError as exc:
        # Familia completa (corrupto, schema, id inválido, I/O): nunca escapa
        # de la API tipada; la verificación devuelve INDETERMINATE fail-closed.
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"metadata corrupta o ilegible para '{ident}': no se puede afirmar nada (fail-closed): {exc}",
        )
    if metadata.generation_id != ident:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"metadata con generation_id distinto ({metadata.generation_id!r} != {ident!r})",
            recorded=metadata,
        )
    # SFR-18 on-demand (P2-B2): VALID exige integridad física FRESCA, no sólo
    # digest+identidad. Un hardlink o un reparse insertado después de publicar
    # no cambia el digest pero viola el contrato físico: nunca VALID.
    integridad = verify_generation_physical_integrity(directorio)
    if integridad.state is IndependenceState.VIOLATED:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INVALID,
            message=f"integridad física violada para '{ident}' (SFR-18): {integridad.message}",
            recorded=metadata,
        )
    if integridad.state is IndependenceState.INDETERMINATE:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"integridad física no demostrable para '{ident}': {integridad.message}",
            recorded=metadata,
        )
    try:
        files = inventory_tree(directorio)
    except InventoryError as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"inventario imposible para '{ident}' (posible mutación/reparse en curso): {exc}",
            recorded=metadata,
        )
    observado = tree_digest_from_files(files)
    # Identidad completa: digest + files + bytes (TreeDigest equality), no
    # sólo el digest — metadata con conteos alterados no debe dar VALID.
    if observado != metadata.tree_digest:
        return GenerationVerificationResult(
            state=GenerationVerificationState.DRIFTED,
            message=f"el árbol de '{ident}' difiere de la identidad de árbol registrada (DRIFTED, SFR-17)",
            recorded=metadata,
            observed_digest=observado,
        )
    if not _criticos_coinciden(metadata.critical_files, files):
        return GenerationVerificationResult(
            state=GenerationVerificationState.DRIFTED,
            message=f"la evidencia crítica registrada de '{ident}' no coincide con el inventario fresco (DRIFTED)",
            recorded=metadata,
            observed_digest=observado,
        )
    try:
        fresh = observe_runtime_identity_from_root(directorio, expected_game_key=metadata.runtime_identity.game_key)
    except RuntimeObservationError as exc:
        return GenerationVerificationResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"identidad de runtime no observable para '{ident}': {exc}",
            recorded=metadata,
            observed_digest=observado,
        )
    if fresh.runtime_identity != metadata.runtime_identity:
        return GenerationVerificationResult(
            state=GenerationVerificationState.DRIFTED,
            message=(
                f"la identidad de runtime de '{ident}' difiere de la registrada "
                f"({metadata.runtime_identity.game_version} → {fresh.runtime_identity.game_version})"
            ),
            recorded=metadata,
            observed_digest=observado,
            observed_identity=fresh.runtime_identity,
        )
    return GenerationVerificationResult(
        state=GenerationVerificationState.VALID,
        message=f"la generation '{ident}' coincide con su identidad registrada",
        recorded=metadata,
        observed_digest=observado,
        observed_identity=fresh.runtime_identity,
    )
