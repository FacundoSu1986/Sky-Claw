"""Provisionamiento P7-A1: copia Stock verificada → Generation → Runtime Clone.

## Qué agrega (y qué NO)

Antes de este módulo el proyecto **no tenía implementado** el tramo que P7
necesita para preparar un Golden Master sin Steam:

- ``candidatos → Generation``: no existía writer de ``versions/<generation-id>/``
  (sólo metadata; los tests copiaban el árbol a mano).
- ``Generation → Clone``: no existía creador del árbol del Clone (sólo
  ``registrar_runtime_clone_record``, que registra un directorio ya existente).
- ``RuntimeSetupManifest``: no existía; ``PROVISIONED`` era sólo igualdad de
  strings.

Este módulo agrega EXACTAMENTE el camino de provisionamiento local:

1. ``construir_referencia_stock`` / ``cargar_referencia_stock``: una referencia
   INDEPENDIENTE del árbol (digest de árbol + identidad de runtime + crítica),
   que el operador guarda fuera de la copia. Sin referencia no hay verificación.
2. ``adoptar_copia_stock``: re-verifica la copia contra esa referencia usando
   ``runtime_vault.golden.verify_golden_master`` (HASHING_A_FOLDER_DOES_NOT_MAKE_
   IT_A_GOLDEN_MASTER) y exige además ausencia de reparse/hardlinks.
3. ``publicar_generation_desde_stock``: copia el árbol sellado a
   ``versions/<generation-id>/`` con la primitive de copia del repo
   (``copiar_arbol_independiente``), verifica fresca la copia y recién entonces
   publica metadata + un sidecar de procedencia con la referencia usada.
4. ``crear_runtime_clone_desde_generation``: copia la Generation verificada a un
   root de Clone fuera del storage, exige independencia física real
   (``st_nlink==1`` y sin inodos compartidos con Generation/source) y registra
   el Clone como ``PROVISIONED`` con evidencia de setup SKSE content-bound.

## Trust model (declarado, no insinuado)

- La referencia Stock es un **ancla de confianza del operador**: puede haberse
  construido a partir de la propia copia en un paso previo. Sirve para detectar
  drift posterior y para que el escritor no pueda auto-autorizar un árbol
  distinto; NO demuestra por sí sola que la copia provenga de Steam. Esa
  procedencia queda registrada como ``provider="stock"`` (nunca "steam") y
  debe cruzarse contra una captura de Managed Source cuando Steam esté IDLE
  (paso posterior, con aprobación del propietario).
- El módulo NO toca Managed Source, appmanifest, staging/downloading/temp de
  Steam, MO2, ``active.json`` ni el binder. El Desired Runtime y el binding
  siguen siendo operaciones del binder con aprobación explícita y hoy están
  deshabilitadas (``EMISION_PRODUCTIVA_HABILITADA=False``).
- Sin storage inicializado, con journal de transición vigente o con registros
  de recuperación pendientes, todas las mutaciones se rechazan.
- Toda escritura cae dentro del storage (o del ``clone_parent`` explícito) y
  bajo el root lock del FrozenRuntimeRoot. ``P7-A1`` no crea storage productivo:
  este módulo sólo se ejercita contra directorios sintéticos y, en la sesión
  real, queda detrás de aprobación del propietario.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import time
from collections.abc import Callable, Sequence

from sky_claw.local.frozen_runtime.clones import (
    CANONICAL_ADMITTED_ROLE,
    CLONE_RECORD_SCHEMA_VERSION,
    _actualizar_lifecycle_clone_bajo_lock,
    _registrar_runtime_clone_record_bajo_lock,
    admitir_clone_root_path,
    cargar_runtime_clone_record,
    leer_runtime_clone_record,
    validar_clone_id,
    verificar_runtime_clone,
)
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.effective_runtime.compatibility import assess_runtime_compatibility
from sky_claw.local.frozen_runtime.effective_runtime.models import RuntimeCompatibility
from sky_claw.local.frozen_runtime.errors import (
    CandidateCopyError,
    FrozenRuntimeError,
    FrozenRuntimeStorageError,
    ProvisioningBlockedError,
    ProvisioningError,
    ProvisioningSpaceError,
    RuntimeSetupError,
    StockReferenceInvalidError,
    StockSourceError,
)
from sky_claw.local.frozen_runtime.generation_id import construir_generation_id, validar_generation_id
from sky_claw.local.frozen_runtime.generations import (
    _registrar_metadata_bajo_lock,
    construir_generation_metadata_desde_identidad,
    leer_generation_metadata,
    verificar_generation,
)
from sky_claw.local.frozen_runtime.independence import (
    descripcion_de_enlace,
    verificar_sin_multilinks,
    verify_clone_physical_integrity,
    verify_generation_independence,
)
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    DirectoryMembershipEvidence,
    capturar_membership_directorios,
    construir_evidencia_membership,
    exigir_coherencia_archivos_membership,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.state import reservar_ruta_json_exclusiva, write_json_atomic
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    admitir_storage_root,
    generation_dir,
    recovery_state_dir,
    transition_journal_path,
    versions_dir,
)
from sky_claw.local.frozen_runtime.storage_models import (
    GenerationMetadata,
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
)
from sky_claw.local.runtime_vault.golden import verify_golden_master
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    FileIdentity,
    InventoryError,
    InventoryLinkError,
    RuntimeIdentity,
    TreeDigest,
    VerificationState,
)
from sky_claw.local.runtime_vault.runtime_observation import (
    RuntimeObservationError,
    observe_runtime_identity_from_root,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

#: Provider honesto de una copia local: NUNCA "steam".
PROVIDER_STOCK = "stock"

REFERENCE_SCHEMA_VERSION = 2
SIDECAR_SCHEMA_VERSION = 2
STOCK_REFERENCE_KIND_DEFAULT = "operator_inventory"
CRITICAL_RELPATHS_DEFAULT: tuple[str, ...] = ("SkyrimSE.exe", "steam_api64.dll")

#: Margen de espacio por defecto al estimar una copia (512 MiB).
DEFAULT_MARGIN_BYTES = 512 * 1024 * 1024

_SIDECAR_SUFFIX = ".source.json"

__all__ = [
    "PROVIDER_STOCK",
    "CopiaStockVerificada",
    "ProvisionamientoResult",
    "RuntimeSetupEvidence",
    "StockReference",
    "adoptar_copia_stock",
    "construir_referencia_stock",
    "crear_runtime_clone_desde_generation",
    "cargar_referencia_stock",
    "publicar_generation_desde_stock",
    "verificar_runtime_setup",
]


# ── helpers de rutas ───────────────────────────────────────────────────────


def _normcase_abspath(path: pathlib.Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _contenida(interior: pathlib.Path, contenedor: pathlib.Path) -> bool:
    i = _normcase_abspath(interior)
    c = _normcase_abspath(contenedor)
    if i == c:
        return True
    return i.startswith(c.rstrip(os.sep) + os.sep)


def _dentro_de_area_steam(path: pathlib.Path) -> str | None:
    for componente in (path, *path.parents):
        if componente.name.casefold() == "steamapps":
            return str(componente)
    return None


def _identidad_observada(raiz: pathlib.Path, *, game_key: str = "skyrimse") -> RuntimeIdentity:
    """Observación fresca del runtime (una sola primitive, inyectable en tests)."""
    return observe_runtime_identity_from_root(raiz, expected_game_key=game_key).runtime_identity


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ── referencia independiente Stock ─────────────────────────────────────────


@dataclasses.dataclass(frozen=True, slots=True)
class StockReference:
    """Referencia INDEPENDIENTE contra la que se verifica una copia Stock.

    ``reference_digest`` se recomputa siempre al cargar/publicar: una referencia
    editada a mano no puede autorizar un árbol distinto. La expectativa de árbol
    es un ``TreeDigest`` completo (relpath+tamaño+sha256 por archivo); la de
    runtime, una ``RuntimeIdentity``; la de **directorios**, la
    ``DirectoryMembershipEvidence`` completa (el TreeDigest es ciego a directorios
    vacíos: sin membership, borrar/agregar un directorio vacío pasaría inadvertido);
    y los críticos se verifican contra el inventario sellado (sin re-hashear).
    """

    schema_version: int
    reference_kind: str
    reference_digest: str
    expected_tree: TreeDigest
    expected_runtime: RuntimeIdentity
    directory_membership: DirectoryMembershipEvidence
    critical_expectations: tuple[CriticalFileExpectation, ...]

    def como_dict(self) -> dict[str, object]:
        return _payload_referencia(self)

    def con_digest_recalculado(self) -> StockReference:
        return dataclasses.replace(self, reference_digest=_digest_referencia(self))


def _payload_referencia(reference: StockReference) -> dict[str, object]:
    return {
        "schema_version": reference.schema_version,
        "reference_kind": reference.reference_kind,
        "reference_digest": reference.reference_digest,
        "expected_tree": {
            "digest": reference.expected_tree.digest,
            "files": reference.expected_tree.files,
            "bytes": reference.expected_tree.bytes,
        },
        "expected_runtime": {
            "game_key": reference.expected_runtime.game_key,
            "game_version": reference.expected_runtime.game_version,
        },
        "directory_membership": {
            "digest": reference.directory_membership.digest,
            "directory_count": reference.directory_membership.directory_count,
            "directories": list(reference.directory_membership.directories),
        },
        "critical_expectations": [
            {
                "rel_path": exp.rel_path,
                "expected_digest": exp.expected_digest,
                "expected_size": exp.expected_size,
            }
            for exp in reference.critical_expectations
        ],
    }


def _digest_referencia(reference: StockReference) -> str:
    payload = _payload_referencia(reference)
    payload.pop("reference_digest", None)
    return _sha256_bytes(_canonical_json(payload))


def construir_referencia_stock(
    source_root: pathlib.Path,
    *,
    reference_kind: str = STOCK_REFERENCE_KIND_DEFAULT,
    critical_relpaths: Sequence[str] = CRITICAL_RELPATHS_DEFAULT,
    identity_observer: Callable[[pathlib.Path], RuntimeIdentity] | None = None,
) -> StockReference:
    """Construye una referencia Stock sellada (READ-ONLY sobre ``source_root``).

    Es el paso de CAPTURA: el operador lo corre una vez, revisa/guarda el JSON
    fuera de la copia y luego lo usa como ancla en ``adoptar_copia_stock``. El
    hashing del árbol es el inventario sellado de Runtime Vault: una copia que
    cambie después no puede coincidir con esta referencia.
    """
    raiz = pathlib.Path(source_root)
    if not isinstance(reference_kind, str) or not reference_kind.strip():
        raise StockReferenceInvalidError("reference_kind no puede ser vacío")
    try:
        files = inventory_tree(raiz)
        membership = capturar_membership_directorios(raiz)
        exigir_coherencia_archivos_membership(files, membership)
    except (InventoryError, InventoryLinkError, DirectoryMembershipError) as exc:
        raise StockSourceError(f"la copia Stock no es inspeccionable: {exc}") from exc
    independencia = verificar_sin_multilinks(raiz, etiqueta="copia Stock")
    if not independencia.success:
        raise StockSourceError(f"la copia Stock no es físicamente independiente: {independencia.message}")
    observador = identity_observer or _identidad_observada
    try:
        identidad = observador(raiz)
    except RuntimeObservationError as exc:
        raise StockSourceError(f"no se pudo observar la identidad del runtime en la copia Stock: {exc}") from exc
    por_relpath = {f.rel_path.casefold(): f for f in files}
    expectativas: list[CriticalFileExpectation] = []
    for rel in critical_relpaths:
        encontrado = por_relpath.get(rel.casefold())
        if encontrado is None:
            if rel.casefold() == "skyrimse.exe":
                raise StockSourceError("la copia Stock no contiene 'SkyrimSE.exe'")
            continue
        expectativas.append(
            CriticalFileExpectation(
                rel_path=encontrado.rel_path,
                expected_digest=encontrado.digest,
                expected_size=encontrado.size,
            )
        )
    referencia = StockReference(
        schema_version=REFERENCE_SCHEMA_VERSION,
        reference_kind=reference_kind,
        reference_digest="0" * 64,
        expected_tree=tree_digest_from_files(files),
        expected_runtime=identidad,
        directory_membership=membership,
        critical_expectations=tuple(expectativas),
    )
    return referencia.con_digest_recalculado()


def _reference_from_dict(payload: object) -> StockReference:
    """Parsea una referencia con schema CERRADO (fail-closed ante campos de más)."""
    if not isinstance(payload, dict):
        raise StockReferenceInvalidError("la referencia Stock debe ser un objeto JSON")
    esperadas = {
        "schema_version",
        "reference_kind",
        "expected_tree",
        "expected_runtime",
        "directory_membership",
        "critical_expectations",
        "reference_digest",
    }
    faltan = esperadas - set(payload)
    sobran = set(payload) - esperadas
    if faltan or sobran:
        raise StockReferenceInvalidError(
            f"referencia Stock con campos inválidos (faltan={sorted(faltan)}, sobran={sorted(sobran)})"
        )
    if payload["schema_version"] != REFERENCE_SCHEMA_VERSION:
        raise StockReferenceInvalidError(f"schema_version de referencia no soportado: {payload['schema_version']!r}")
    if not isinstance(payload["reference_kind"], str) or not payload["reference_kind"].strip():
        raise StockReferenceInvalidError("reference_kind inválido")
    if not isinstance(payload["reference_digest"], str):
        raise StockReferenceInvalidError("reference_digest inválido")
    tree = payload["expected_tree"]
    runtime = payload["expected_runtime"]
    if not isinstance(tree, dict) or set(tree) != {"digest", "files", "bytes"}:
        raise StockReferenceInvalidError("expected_tree con schema inválido")
    if not isinstance(runtime, dict) or set(runtime) != {"game_key", "game_version"}:
        raise StockReferenceInvalidError("expected_runtime con schema inválido")
    membership = payload["directory_membership"]
    if not isinstance(membership, dict) or set(membership) != {"digest", "directory_count", "directories"}:
        raise StockReferenceInvalidError("directory_membership con schema inválido")
    directorios_brutos = membership["directories"]
    if not isinstance(directorios_brutos, list):
        raise StockReferenceInvalidError("directory_membership.directories debe ser lista")
    if not all(isinstance(d, str) for d in directorios_brutos):
        raise StockReferenceInvalidError(
            "directory_membership.directories debe contener SÓLO strings (fail-closed, sin casts silenciosos)"
        )
    try:
        membership_evidencia = construir_evidencia_membership(tuple(directorios_brutos))
    except (TypeError, ValueError) as exc:
        raise StockReferenceInvalidError(f"directory_membership inválida: {exc}") from exc
    if (
        membership_evidencia.digest != membership["digest"]
        or membership_evidencia.directory_count != membership["directory_count"]
    ):
        raise StockReferenceInvalidError(
            "directory_membership no coincide con su digest/conteo (referencia alterada; fail-closed)"
        )
    criticos = payload["critical_expectations"]
    if not isinstance(criticos, list):
        raise StockReferenceInvalidError("critical_expectations debe ser lista")
    expectations: list[CriticalFileExpectation] = []
    for bruto in criticos:
        if not isinstance(bruto, dict) or set(bruto) != {"rel_path", "expected_digest", "expected_size"}:
            raise StockReferenceInvalidError("critical_expectation con schema inválido")
        try:
            expectations.append(
                CriticalFileExpectation(
                    rel_path=bruto["rel_path"],
                    expected_digest=bruto["expected_digest"],
                    expected_size=bruto["expected_size"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise StockReferenceInvalidError(f"critical_expectation inválida: {exc}") from exc
    try:
        referencia = StockReference(
            schema_version=REFERENCE_SCHEMA_VERSION,
            reference_kind=payload["reference_kind"],
            reference_digest=payload["reference_digest"],
            expected_tree=TreeDigest(
                digest=tree["digest"],
                files=int(tree["files"]),
                bytes=int(tree["bytes"]),
            ),
            expected_runtime=RuntimeIdentity(
                game_key=runtime["game_key"],
                game_version=runtime["game_version"],
            ),
            directory_membership=membership_evidencia,
            critical_expectations=tuple(expectations),
        )
    except (TypeError, ValueError) as exc:
        raise StockReferenceInvalidError(f"referencia Stock inválida: {exc}") from exc
    if not isinstance(referencia.reference_digest, str) or len(referencia.reference_digest) != 64:
        raise StockReferenceInvalidError("reference_digest debe ser sha256 hex de 64 caracteres")
    if referencia.reference_digest != _digest_referencia(referencia):
        raise StockReferenceInvalidError(
            "reference_digest no coincide con el contenido de la referencia: fue alterada (fail-closed)"
        )
    return referencia


def cargar_referencia_stock(payload: dict[str, object] | str | bytes) -> StockReference:
    """Carga una referencia desde dict/JSON y valida digest + schema cerrado."""
    if isinstance(payload, (str, bytes)):
        try:
            crudo = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StockReferenceInvalidError(f"referencia Stock no es JSON válido: {exc}") from exc
    else:
        crudo = payload
    return _reference_from_dict(crudo)


# ── evidencia de copia adoptada y de setup ─────────────────────────────────


@dataclasses.dataclass(frozen=True, slots=True)
class CopiaStockVerificada:
    """Copia Stock que coincide con la referencia y es físicamente independiente."""

    root: pathlib.Path
    provider: str
    reference: StockReference
    reference_digest: str
    reference_kind: str
    runtime_identity: RuntimeIdentity
    files: tuple[FileIdentity, ...]
    tree_digest: TreeDigest
    critical_files: tuple[FileIdentity, ...]
    directory_membership: DirectoryMembershipEvidence
    observed_at_ns: int


@dataclasses.dataclass(frozen=True, slots=True)
class RuntimeSetupEvidence:
    """Evidencia mínima y content-bound del setup de ejecución de un Clone.

    P7-A1 no implementa el ``RuntimeSetupManifest`` completo del ADR (sigue
    faltando); esta evidencia cubre lo que P7-B exige para arrancar: ejecutable
    observable, SKSE cargable contra la build exacta y hashes de los binarios
    que forman el setup. ``setup_id`` se deriva del contenido, no se inventa.
    """

    setup_id: str
    runtime_identity: RuntimeIdentity
    critical_files: tuple[FileIdentity, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ProvisionamientoResult:
    """Resultado tipado de una operación de provisionamiento (nunca bool opaco)."""

    success: bool
    message: str
    generation_id: str | None = None
    generation_metadata: GenerationMetadata | None = None
    clone_id: str | None = None
    clone_root: pathlib.Path | None = None
    runtime_setup_id: str | None = None
    clone_record: RuntimeCloneRecord | None = None


def _error(message: str) -> ProvisionamientoResult:
    return ProvisionamientoResult(success=False, message=message)


# ── gates de storage ───────────────────────────────────────────────────────


def _exigir_sin_transicion(root: pathlib.Path) -> None:
    if transition_journal_path(root).exists():
        raise ProvisioningBlockedError(
            f"hay un journal de transición en '{transition_journal_path(root)}': no se provisiona "
            "con una transición a medio cerrar (fail-closed)"
        )
    recovery = recovery_state_dir(root)
    if recovery.is_dir() and any(recovery.iterdir()):
        raise ProvisioningBlockedError(
            f"hay registros de recuperación pendientes en '{recovery}': resolverlos antes de provisionar"
        )


def _exigir_storage_operativo(storage_root: pathlib.Path) -> pathlib.Path:
    raiz = pathlib.Path(storage_root)
    admission = admitir_storage_root(raiz)
    if not admission.success:
        raise ProvisioningBlockedError(f"storage root no admitido: {admission.message}")
    ruta = admission.root if admission.root is not None else raiz
    if not active_state_path(ruta).is_file():
        raise ProvisioningBlockedError(
            f"el storage '{ruta}' no está inicializado: falta 'state/active.json' "
            "(inicializarlo es una operación APARTE con aprobación del propietario)"
        )
    if not versions_dir(ruta).is_dir():
        raise ProvisioningBlockedError(f"el storage '{ruta}' no tiene 'versions/': layout incompleto")
    _exigir_sin_transicion(ruta)
    return ruta


def _exigir_espacio(destino: pathlib.Path, bytes_requeridos: int, margen_bytes: int) -> None:
    if bytes_requeridos < 0 or margen_bytes < 0:
        raise ProvisioningError("estimación de espacio con valores negativos")
    requerido = bytes_requeridos + margen_bytes
    try:
        uso = shutil.disk_usage(os.fspath(destino))
    except OSError as exc:
        raise ProvisioningBlockedError(f"no se pudo consultar espacio libre en '{destino}': {exc}") from exc
    if uso.free < requerido:
        raise ProvisioningSpaceError(
            f"espacio insuficiente en el volumen de '{destino}': libre={uso.free} bytes, "
            f"requerido={requerido} bytes (árbol {bytes_requeridos} + margen {margen_bytes})"
        )


# ── adopción de la copia Stock ─────────────────────────────────────────────


def adoptar_copia_stock(
    source_root: pathlib.Path,
    reference: StockReference,
    *,
    storage_root: pathlib.Path | None = None,
    managed_source_root: pathlib.Path | None = None,
    identity_observer: Callable[[pathlib.Path], RuntimeIdentity] | None = None,
) -> CopiaStockVerificada:
    """Verifica la copia contra la referencia independiente y la deja apta.

    Es READ-ONLY sobre la copia. La verificación tiene dos pasadas:
    1. ``verify_golden_master`` (Runtime Vault) contra ``expected_tree``,
       ``expected_runtime`` y las expectativas críticas de la referencia.
    2. Re-medición fresca posterior: el árbol no puede haber cambiado entre la
       verificación y la captura de la evidencia que se va a copiar.

    Rechaza además reparse/junctions/hardlinks dentro de la copia, solape con el
    storage y ubicación dentro de ``steamapps`` (área administrada por Steam).
    """
    if reference.schema_version != REFERENCE_SCHEMA_VERSION:
        raise StockReferenceInvalidError("schema_version de referencia no soportado")
    if reference.reference_digest != _digest_referencia(reference):
        raise StockReferenceInvalidError("reference_digest no coincide con la referencia: fue alterada")

    raiz = pathlib.Path(os.path.abspath(os.fspath(source_root)))
    motivo = descripcion_de_enlace(raiz)
    if motivo is not None:
        raise StockSourceError(f"la copia Stock está redirigida por enlace: {motivo}")
    if not raiz.is_dir():
        raise StockSourceError(f"la copia Stock no existe o no es un directorio: '{raiz}'")
    area_steam = _dentro_de_area_steam(raiz)
    if area_steam is not None:
        raise StockSourceError(f"la copia Stock está dentro de un área administrada por Steam: '{area_steam}'")
    if storage_root is not None:
        storage = pathlib.Path(storage_root)
        if _contenida(raiz, storage) or _contenida(storage, raiz):
            raise StockSourceError("la copia Stock y el storage no pueden contener uno al otro")
    if managed_source_root is not None:
        fuente = pathlib.Path(managed_source_root)
        if _contenida(raiz, fuente) or _contenida(fuente, raiz):
            raise StockSourceError("la copia Stock y la Managed Source no pueden contener una a la otra")

    try:
        files = inventory_tree(raiz)
        membership = capturar_membership_directorios(raiz)
        exigir_coherencia_archivos_membership(files, membership)
    except (InventoryError, InventoryLinkError, DirectoryMembershipError) as exc:
        raise StockSourceError(f"la copia Stock no es inspeccionable: {exc}") from exc
    independencia = verificar_sin_multilinks(raiz, etiqueta="copia Stock")
    if not independencia.success:
        raise StockSourceError(f"la copia Stock no es físicamente independiente: {independencia.message}")

    observador = identity_observer or _identidad_observada
    try:
        identidad = observador(raiz)
    except RuntimeObservationError as exc:
        raise StockSourceError(f"no se pudo observar la identidad del runtime en la copia Stock: {exc}") from exc

    verificacion = verify_golden_master(
        raiz,
        expected_tree=reference.expected_tree,
        expected_runtime=reference.expected_runtime,
        observed_runtime=identidad,
        critical_expectations=reference.critical_expectations,
    )
    if verificacion.state is not VerificationState.VERIFIED:
        raise StockSourceError(
            f"la copia Stock NO coincide con la referencia independiente ({verificacion.state.value}): "
            f"{verificacion.message}"
        )

    # Segunda pasada fresca: cierra la ventana entre verificar y sellar la evidencia.
    try:
        files_finales = inventory_tree(raiz)
        membership_final = capturar_membership_directorios(raiz)
        exigir_coherencia_archivos_membership(files_finales, membership_final)
    except (InventoryError, InventoryLinkError, DirectoryMembershipError) as exc:
        raise StockSourceError(f"la copia Stock cambió durante la verificación: {exc}") from exc
    digest_final = tree_digest_from_files(files_finales)
    if digest_final != reference.expected_tree:
        raise StockSourceError("la copia Stock cambió entre la verificación y la re-medición (fail-closed)")
    if membership_final != membership:
        raise StockSourceError("la estructura de directorios de la copia Stock cambió durante la verificación")
    if membership_final != reference.directory_membership:
        raise StockSourceError(
            "la estructura de directorios de la copia Stock NO coincide con la referencia independiente "
            "(directorios vacíos agregados/eliminados; el TreeDigest no los ve): fail-closed"
        )
    try:
        identidad_final = observador(raiz)
    except RuntimeObservationError as exc:
        raise StockSourceError(f"no se pudo re-observar la identidad del runtime: {exc}") from exc
    if identidad_final != identidad:
        raise StockSourceError("la identidad del runtime cambió durante la verificación (fail-closed)")

    por_relpath = {f.rel_path: f for f in files_finales}
    criticos = tuple(por_relpath[exp.rel_path] for exp in reference.critical_expectations)
    return CopiaStockVerificada(
        root=raiz,
        provider=PROVIDER_STOCK,
        reference=reference,
        reference_digest=reference.reference_digest,
        reference_kind=reference.reference_kind,
        runtime_identity=identidad_final,
        files=files_finales,
        tree_digest=digest_final,
        critical_files=criticos,
        directory_membership=membership_final,
        observed_at_ns=time.time_ns(),
    )


# ── publicación de Generation ──────────────────────────────────────────────


def _sidecar_path(root: pathlib.Path, generation_id: str) -> pathlib.Path:
    from sky_claw.local.frozen_runtime.storage import generations_state_dir

    return generations_state_dir(root) / f"{generation_id}{_SIDECAR_SUFFIX}"


def _payload_sidecar(copia: CopiaStockVerificada, generation_id: str) -> dict[str, object]:
    """Sidecar v2: embebe la referencia COMPLETA (no sólo su digest).

    Sin el payload completo es imposible recomputar ``reference_digest`` y los
    64 caracteres pasan a ser un texto no falsable. El loader reusa
    ``_reference_from_dict`` (mismo parser que la referencia de entrada) y
    cruza el árbol/runtime/membership/críticos contra la referencia y la
    metadata de autoridad.
    """
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "generation_id": generation_id,
        "provider": copia.provider,
        "reference": _payload_referencia(copia.reference),
        "tree_digest": {
            "digest": copia.tree_digest.digest,
            "files": copia.tree_digest.files,
            "bytes": copia.tree_digest.bytes,
        },
        "runtime_identity": {
            "game_key": copia.runtime_identity.game_key,
            "game_version": copia.runtime_identity.game_version,
        },
        "directory_membership": {
            "digest": copia.directory_membership.digest,
            "directory_count": copia.directory_membership.directory_count,
            "directories": list(copia.directory_membership.directories),
        },
        "critical_files": [{"rel_path": f.rel_path, "size": f.size, "digest": f.digest} for f in copia.critical_files],
        "observed_at_ns": copia.observed_at_ns,
    }


def _verificar_arbol_en_destino(
    destino: pathlib.Path,
    *,
    tree_digest: TreeDigest,
    membership: DirectoryMembershipEvidence,
    runtime_identity: RuntimeIdentity,
    etiqueta: str,
    identity_observer: Callable[[pathlib.Path], RuntimeIdentity] | None = None,
) -> tuple[FileIdentity, ...]:
    """Re-verifica un árbol copiado: digest, membership, links e identidad."""
    try:
        files = inventory_tree(destino)
        membership_observada = capturar_membership_directorios(destino)
    except (InventoryError, InventoryLinkError, DirectoryMembershipError) as exc:
        raise ProvisioningError(f"{etiqueta}: no se pudo re-inventariar el destino: {exc}") from exc
    digest = tree_digest_from_files(files)
    if digest != tree_digest:
        raise ProvisioningError(
            f"{etiqueta}: digest del destino no coincide (observado {digest.digest} != esperado {tree_digest.digest})"
        )
    if membership_observada != membership:
        raise ProvisioningError(f"{etiqueta}: la estructura de directorios del destino no coincide")
    independencia = verificar_sin_multilinks(destino, etiqueta=etiqueta)
    if not independencia.success:
        raise ProvisioningError(f"{etiqueta}: no es un árbol físicamente independiente: {independencia.message}")
    observador = identity_observer or _identidad_observada
    try:
        identidad = observador(destino)
    except RuntimeObservationError as exc:
        raise ProvisioningError(f"{etiqueta}: no se pudo observar la identidad del runtime: {exc}") from exc
    if identidad != runtime_identity:
        raise ProvisioningError(
            f"{etiqueta}: identidad de runtime distinta (observada {identidad.game_version} != "
            f"esperada {runtime_identity.game_version})"
        )
    return files


def publicar_generation_desde_stock(
    storage_root: pathlib.Path,
    copia: CopiaStockVerificada,
    *,
    margen_bytes: int = DEFAULT_MARGIN_BYTES,
) -> ProvisionamientoResult:
    """Publica ``versions/<generation-id>/`` + sidecar + metadata.

    Toda la operación ocurre dentro de UNA sección de ``participant_lock``:
    copy → re-verificación fresca → independencia → sidecar → metadata →
    verificación final. Los registros internos se persisten con las variantes
    ``*_bajo_lock`` (el lock NO es reentrante: llamar a los registradores
    públicos acá levantaría ``FrozenRuntimeLockBusyError``).

    Orden de commit: SIDECAR ANTES que metadata. ``verificar_generation`` y el
    preflight del binder consideran VALID una Generation por su metadata, así
    que publicar metadata sin sidecar dejaría una Generation "válida" sin
    procedencia recuperable. Con este orden, la metadata es el punto de commit
    y una caída previa deja como máximo un árbol + sidecar sin metadata
    (UNDISCOVERED / fail-closed), nunca metadata huérfana de procedencia.

    Un fallo deja artefactos parciales SIN borrado destructivo (la política del
    repo prohíbe GC automático); el operador los reconcilia con aprobación.
    """
    try:
        raiz = _exigir_storage_operativo(storage_root)
        generation_id = validar_generation_id(
            construir_generation_id(copia.runtime_identity.game_version, copia.tree_digest)
        )
        destino = generation_dir(raiz, generation_id)
        sidecar = _sidecar_path(raiz, generation_id)
        with participant_lock(raiz, timeout=0.0) as handle:
            handle.assert_owned()
            _exigir_sin_transicion(raiz)
            if destino.exists():
                return _error(f"la Generation '{generation_id}' ya existe en el storage (no-clobber)")
            if sidecar.exists():
                return _error(f"sidecar de procedencia huérfano para '{generation_id}': no-clobber (reconciliar)")
            _exigir_espacio(versions_dir(raiz), copia.tree_digest.bytes, margen_bytes)
            try:
                copiar_arbol_independiente(
                    copia.root,
                    destino,
                    copia.files,
                    copia.directory_membership.directories,
                    contenedor=raiz,
                )
            except CandidateCopyError as exc:
                return _error(f"la copia del árbol falló: {exc}")

            _verificar_arbol_en_destino(
                destino,
                tree_digest=copia.tree_digest,
                membership=copia.directory_membership,
                runtime_identity=copia.runtime_identity,
                etiqueta="Generation",
            )
            independencia = verify_generation_independence(destino, managed_source_root=copia.root)
            if not independencia.success:
                raise ProvisioningError(
                    f"la Generation publicada no es independiente de la copia Stock: {independencia.message}"
                )
            try:
                reservar_ruta_json_exclusiva(sidecar)
                write_json_atomic(sidecar, _payload_sidecar(copia, generation_id))
            except (FrozenRuntimeStorageError, OSError) as exc:
                return _error(
                    f"sidecar de procedencia falló ANTES de publicar metadata: {exc} "
                    "(la Generation no tiene metadata: no es descubrible ni VERIFIED)"
                )
            metadata = construir_generation_metadata_desde_identidad(
                runtime_identity=copia.runtime_identity,
                tree_digest=copia.tree_digest,
                critical_files=copia.critical_files,
                provider=copia.provider,
                provider_appid=None,
                provider_buildid=None,
            )
            metadata = _registrar_metadata_bajo_lock(raiz, metadata)
            verificacion = verificar_generation(raiz, generation_id)
            if not verificacion.success:
                return _error(f"la Generation publicada no re-verifica como VALID: {verificacion.message}")
            return ProvisionamientoResult(
                success=True,
                message=f"Generation '{generation_id}' publicada y verificada (provider={copia.provider})",
                generation_id=generation_id,
                generation_metadata=metadata,
            )
    except ProvisioningError as exc:
        return _error(str(exc))
    except (FrozenRuntimeError, OSError) as exc:
        return _error(f"fallo de provisionamiento al publicar la Generation: {exc}")


# ── carga del sidecar de procedencia ───────────────────────────────────────


@dataclasses.dataclass(frozen=True, slots=True)
class _ProcedenciaGeneracion:
    reference: StockReference
    tree_digest: TreeDigest
    runtime_identity: RuntimeIdentity
    directory_membership: DirectoryMembershipEvidence
    critical_files: tuple[FileIdentity, ...]
    observed_at_ns: int


def _exigir_sha256(valor: object, *, campo: str) -> str:
    if not isinstance(valor, str) or re.fullmatch(r"[0-9a-f]{64}", valor) is None:
        raise ProvisioningError(f"sidecar de procedencia: {campo} no es sha256 hex minúsculas de 64 caracteres")
    return valor


def _exigir_entero_no_negativo(valor: object, *, campo: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        raise ProvisioningError(f"sidecar de procedencia: {campo} debe ser entero >= 0")
    return valor


def _parsear_tree_digest(raw: object) -> TreeDigest:
    if not isinstance(raw, dict) or set(raw) != {"digest", "files", "bytes"}:
        raise ProvisioningError("sidecar de procedencia: tree_digest con schema inválido")
    digest = _exigir_sha256(raw["digest"], campo="tree_digest.digest")
    files = _exigir_entero_no_negativo(raw["files"], campo="tree_digest.files")
    bytes_ = _exigir_entero_no_negativo(raw["bytes"], campo="tree_digest.bytes")
    try:
        return TreeDigest(digest=digest, files=files, bytes=bytes_)
    except (TypeError, ValueError) as exc:
        raise ProvisioningError(f"sidecar de procedencia: tree_digest inválido: {exc}") from exc


def _parsear_runtime_identity(raw: object) -> RuntimeIdentity:
    if not isinstance(raw, dict) or set(raw) != {"game_key", "game_version"}:
        raise ProvisioningError("sidecar de procedencia: runtime_identity con schema inválido")
    game_key = raw["game_key"]
    game_version = raw["game_version"]
    if not isinstance(game_key, str) or not game_key.strip():
        raise ProvisioningError("sidecar de procedencia: runtime_identity.game_key inválido")
    if not isinstance(game_version, str) or not game_version.strip():
        raise ProvisioningError("sidecar de procedencia: runtime_identity.game_version inválido")
    try:
        return RuntimeIdentity(game_key=game_key, game_version=game_version)
    except (TypeError, ValueError) as exc:
        raise ProvisioningError(f"sidecar de procedencia: runtime_identity inválida: {exc}") from exc


def _parsear_membership(raw: object) -> DirectoryMembershipEvidence:
    if not isinstance(raw, dict) or set(raw) != {"digest", "directory_count", "directories"}:
        raise ProvisioningError("sidecar de procedencia: directory_membership con schema inválido")
    directorios_brutos = raw["directories"]
    if not isinstance(directorios_brutos, list) or not all(isinstance(d, str) for d in directorios_brutos):
        raise ProvisioningError("sidecar de procedencia: directory_membership.directories debe ser lista de strings")
    _exigir_sha256(raw["digest"], campo="directory_membership.digest")
    _exigir_entero_no_negativo(raw["directory_count"], campo="directory_membership.directory_count")
    try:
        evidencia = construir_evidencia_membership(tuple(directorios_brutos))
    except (TypeError, ValueError) as exc:
        raise ProvisioningError(f"sidecar de procedencia: directory_membership inválida: {exc}") from exc
    if evidencia.digest != raw["digest"] or evidencia.directory_count != raw["directory_count"]:
        raise ProvisioningError("sidecar de procedencia: directory_membership no coincide con su digest/conteo")
    return evidencia


def _parsear_criticos(raw: object) -> tuple[FileIdentity, ...]:
    if not isinstance(raw, list) or not raw:
        raise ProvisioningError("sidecar de procedencia: critical_files debe ser una lista NO vacía")
    identidades: list[FileIdentity] = []
    vistos: set[str] = set()
    for bruto in raw:
        if not isinstance(bruto, dict) or set(bruto) != {"rel_path", "size", "digest"}:
            raise ProvisioningError("sidecar de procedencia: critical_file con schema inválido")
        rel_path = bruto["rel_path"]
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ProvisioningError("sidecar de procedencia: critical_file.rel_path inválido")
        clave = rel_path.casefold()
        if clave in vistos:
            raise ProvisioningError(f"sidecar de procedencia: critical_file duplicado: '{rel_path}'")
        vistos.add(clave)
        size = _exigir_entero_no_negativo(bruto["size"], campo=f"critical_file '{rel_path}'.size")
        digest = _exigir_sha256(bruto["digest"], campo=f"critical_file '{rel_path}'.digest")
        try:
            identidades.append(FileIdentity(rel_path=rel_path, size=size, digest=digest))
        except (TypeError, ValueError) as exc:
            raise ProvisioningError(f"sidecar de procedencia: critical_file inválido: {exc}") from exc
    return tuple(identidades)


def _exigir_criticos_de_la_referencia(
    referencia: StockReference,
    criticos: tuple[FileIdentity, ...],
) -> None:
    esperados = {
        exp.rel_path.casefold(): (exp.expected_digest.lower(), exp.expected_size)
        for exp in referencia.critical_expectations
    }
    observados = {f.rel_path.casefold(): (f.digest.lower(), f.size) for f in criticos}
    if set(esperados) != set(observados):
        raise ProvisioningError(
            "sidecar de procedencia: critical_files no coincide con los críticos de la referencia Stock"
        )
    for rel, (digest_esperado, size_esperado) in esperados.items():
        digest_observado, size_observado = observados[rel]
        if digest_observado != digest_esperado or (size_esperado is not None and size_observado != size_esperado):
            raise ProvisioningError(
                f"sidecar de procedencia: critical_file '{rel}' no coincide con la referencia Stock"
            )


def _cargar_sidecar(root: pathlib.Path, generation_id: str) -> _ProcedenciaGeneracion:
    ruta = _sidecar_path(root, generation_id)
    if not ruta.exists():
        raise ProvisioningError(
            f"falta el sidecar de procedencia '{ruta}': la Generation no declara referencia Stock "
            "(fail-closed; no se inventa procedencia)"
        )
    motivo = descripcion_de_enlace(ruta)
    if motivo is not None:
        raise ProvisioningError(f"sidecar de procedencia redirigido por enlace/reparse: {motivo}")
    try:
        crudo = ruta.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProvisioningError(f"no se pudo leer el sidecar de procedencia '{ruta}': {exc}") from exc
    try:
        data = json.loads(crudo)
    except json.JSONDecodeError as exc:
        raise ProvisioningError(f"sidecar de procedencia corrupto: {exc}") from exc
    esperadas = {
        "schema_version",
        "generation_id",
        "provider",
        "reference",
        "tree_digest",
        "runtime_identity",
        "directory_membership",
        "critical_files",
        "observed_at_ns",
    }
    if not isinstance(data, dict) or set(data) != esperadas:
        raise ProvisioningError("sidecar de procedencia con schema inválido (campos faltantes o de más)")
    if data["schema_version"] != SIDECAR_SCHEMA_VERSION:
        raise ProvisioningError(
            f"sidecar de procedencia con schema {data['schema_version']!r}: obsoleto, re-publicar la Generation "
            f"con schema {SIDECAR_SCHEMA_VERSION} (fail-closed; no se migra procedencia a ciegas)"
        )
    if data["generation_id"] != generation_id:
        raise ProvisioningError("sidecar de procedencia no corresponde a la Generation pedida")
    if data["provider"] != PROVIDER_STOCK:
        raise ProvisioningError(f"sidecar de procedencia con provider inesperado: {data['provider']!r}")

    referencia = _reference_from_dict(data["reference"])
    tree_digest = _parsear_tree_digest(data["tree_digest"])
    runtime_identity = _parsear_runtime_identity(data["runtime_identity"])
    membership = _parsear_membership(data["directory_membership"])
    criticos = _parsear_criticos(data["critical_files"])
    observed_at_ns = _exigir_entero_no_negativo(data["observed_at_ns"], campo="observed_at_ns")

    if referencia.expected_tree != tree_digest:
        raise ProvisioningError(
            "sidecar de procedencia: tree_digest no coincide con la referencia Stock embebida (fail-closed)"
        )
    if referencia.expected_runtime != runtime_identity:
        raise ProvisioningError("sidecar de procedencia: runtime_identity no coincide con la referencia Stock embebida")
    if referencia.directory_membership != membership:
        raise ProvisioningError(
            "sidecar de procedencia: directory_membership no coincide con la referencia Stock embebida"
        )
    _exigir_criticos_de_la_referencia(referencia, criticos)
    return _ProcedenciaGeneracion(
        reference=referencia,
        tree_digest=tree_digest,
        runtime_identity=runtime_identity,
        directory_membership=membership,
        critical_files=criticos,
        observed_at_ns=observed_at_ns,
    )


# ── setup mínimo del Clone ─────────────────────────────────────────────────


def _identidad_archivo_clave(root: pathlib.Path, rel_path: str) -> FileIdentity:
    ruta = pathlib.Path(root) / pathlib.PurePosixPath(rel_path)
    try:
        st = os.lstat(ruta)
    except OSError as exc:
        raise RuntimeSetupError(f"falta el archivo de setup '{rel_path}': {exc}") from exc
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & 0x00000400 or os.path.islink(ruta):
        raise RuntimeSetupError(f"el archivo de setup '{rel_path}' es un reparse/enlace (fail-closed)")
    if not os.path.isfile(ruta):
        raise RuntimeSetupError(f"el archivo de setup '{rel_path}' no es un archivo regular")
    if st.st_nlink != 1:
        raise RuntimeSetupError(f"el archivo de setup '{rel_path}' tiene {st.st_nlink} nombres (hardlink)")
    hasher = hashlib.sha256()
    try:
        with open(ruta, "rb") as fh:
            while chunk := fh.read(1024 * 1024):
                hasher.update(chunk)
    except OSError as exc:
        raise RuntimeSetupError(f"no se pudo leer el archivo de setup '{rel_path}': {exc}") from exc
    return FileIdentity(rel_path=rel_path, size=int(st.st_size), digest=hasher.hexdigest())


def verificar_runtime_setup(
    clone_root: pathlib.Path,
    *,
    expected_game_key: str = "skyrimse",
) -> RuntimeSetupEvidence:
    """Verifica el setup mínimo de P7 en un Clone y deriva un ``setup_id`` content-bound.

    Requiere: ``SkyrimSE.exe`` observable, loader de SKSE y DLL de runtime de la
    build exacta (``assess_runtime_compatibility == COMPATIBLE``). El
    ``setup_id`` es ``"setup-" + sha256`` del payload canónico de esas
    identidades: mismo contenido ⇒ mismo id; cualquier byte distinto cambia el
    id. No promete el ``RuntimeSetupManifest`` completo del ADR (sigue faltando).
    """
    raiz = pathlib.Path(clone_root)
    if not raiz.is_dir():
        raise RuntimeSetupError(f"el Clone no es un directorio: '{raiz}'")
    motivo = descripcion_de_enlace(raiz)
    if motivo is not None:
        raise RuntimeSetupError(f"el Clone está redirigido por enlace: {motivo}")
    try:
        identidad = _identidad_observada(raiz, game_key=expected_game_key)
    except RuntimeObservationError as exc:
        raise RuntimeSetupError(f"no se pudo observar la identidad del runtime del Clone: {exc}") from exc
    compatibilidad = assess_runtime_compatibility(raiz, identity=lambda _r: identidad)
    if compatibilidad.state is not RuntimeCompatibility.COMPATIBLE:
        raise RuntimeSetupError(
            f"SKSE no es compatible con el runtime del Clone ({compatibilidad.state.value}): {compatibilidad.reason}"
        )
    if not compatibilidad.skse_loader or not compatibilidad.skse_dll:
        raise RuntimeSetupError("la compatibilidad COMPATIBLE no declaró loader y DLL de SKSE")
    criticos = (
        _identidad_archivo_clave(raiz, compatibilidad.skse_loader),
        _identidad_archivo_clave(raiz, compatibilidad.skse_dll),
        _identidad_archivo_clave(raiz, "SkyrimSE.exe"),
    )
    payload = {
        "schema_version": 1,
        "runtime_identity": {"game_key": identidad.game_key, "game_version": identidad.game_version},
        "files": [{"rel_path": f.rel_path, "size": f.size, "digest": f.digest} for f in criticos],
    }
    setup_id = "setup-" + _sha256_bytes(_canonical_json(payload))
    return RuntimeSetupEvidence(setup_id=setup_id, runtime_identity=identidad, critical_files=criticos)


# ── creación del Runtime Clone ─────────────────────────────────────────────


def _criticos_coinciden(a: tuple[FileIdentity, ...], b: tuple[FileIdentity, ...]) -> bool:
    """Igualdad de evidencia crítica sin depender del orden ni del case del digest."""

    def clave(f: FileIdentity) -> tuple[str, int, str]:
        return (f.rel_path.casefold(), f.size, f.digest.lower())

    return sorted(map(clave, a)) == sorted(map(clave, b))


def _exigir_sidecar_contra_metadata(
    sidecar: _ProcedenciaGeneracion,
    metadata: GenerationMetadata,
) -> None:
    """Cruza el sidecar contra la metadata de AUTORIDAD (fail-closed).

    El sidecar no es autoridad por sí solo: debe coincidir con la metadata
    publicada (tree/runtime/críticos) y esa metadata no puede declarar un
    generation_id que no derive de su propia identidad. La membership ya se
    cruzó contra la referencia embebida al cargar el sidecar; el llamador la
    cruza además contra el árbol observado (tres vías).
    """
    if metadata.provider != PROVIDER_STOCK:
        raise ProvisioningError(
            f"la metadata de la Generation no declara provider '{PROVIDER_STOCK}': {metadata.provider!r}"
        )
    esperado_gid = construir_generation_id(metadata.runtime_identity.game_version, metadata.tree_digest)
    if metadata.generation_id != esperado_gid:
        raise ProvisioningError("metadata de Generation: generation_id no deriva de su runtime/tree registrados")
    if sidecar.tree_digest != metadata.tree_digest:
        raise ProvisioningError("sidecar de procedencia: tree_digest no coincide con la metadata de la Generation")
    if sidecar.runtime_identity != metadata.runtime_identity:
        raise ProvisioningError("sidecar de procedencia: runtime_identity no coincide con la metadata de la Generation")
    if not _criticos_coinciden(sidecar.critical_files, metadata.critical_files):
        raise ProvisioningError("sidecar de procedencia: critical_files no coincide con la metadata de la Generation")


def _registro_compatible(existente: RuntimeCloneRecord, gid: str, admitido: pathlib.Path) -> bool:
    """Un registro existente es reutilizable sólo si apunta al MISMO linaje/root y no es INVALID."""
    if existente.source_generation_id != gid or existente.root_path != os.fspath(admitido):
        return False
    return existente.lifecycle in (RuntimeCloneLifecycle.PROVISIONING, RuntimeCloneLifecycle.PROVISIONED)


def _marcar_clone_invalid_bajo_lock(raiz: pathlib.Path, cid: str) -> None:
    with contextlib.suppress(FrozenRuntimeError, OSError):
        _actualizar_lifecycle_clone_bajo_lock(raiz, cid, RuntimeCloneLifecycle.INVALID)


def _recuperar_clone_existente(
    raiz: pathlib.Path,
    destino: pathlib.Path,
    admitido: pathlib.Path,
    gid: str,
    cid: str,
    metadata: GenerationMetadata,
    sidecar: _ProcedenciaGeneracion,
    managed_source_root: pathlib.Path | None,
) -> ProvisionamientoResult:
    """Recovery/idempotencia cuando el árbol del Clone ya existe.

    Casos: (a) PROVISIONING tras un fallo entre registro y PROVISIONED →
    completar el MISMO clone; (b) PROVISIONED → idempotente si el setup
    verificado coincide; cualquier otro estado/linaje/root → fail-closed.
    """
    gen_dir = generation_dir(raiz, gid)
    try:
        _verificar_arbol_en_destino(
            destino,
            tree_digest=metadata.tree_digest,
            membership=sidecar.reference.directory_membership,
            runtime_identity=metadata.runtime_identity,
            etiqueta="Clone",
        )
        independencia = verify_clone_physical_integrity(
            destino,
            generation_root=gen_dir,
            managed_source_root=managed_source_root,
        )
    except ProvisioningError as exc:
        return _error(f"no se puede recuperar el Clone existente: {exc}")
    if not independencia.success:
        return _error(f"el Clone existente no es físicamente independiente: {independencia.message}")
    setup = verificar_runtime_setup(destino)
    existente = cargar_runtime_clone_record(raiz, cid)
    if existente is None:
        return _error(f"el destino del Clone existe pero no hay registro para '{cid}': reconciliación manual requerida")
    if existente.source_generation_id != gid or existente.root_path != os.fspath(admitido):
        return _error(f"registro de Clone '{cid}' incompatible con la Generation/root pedidos: no se reutiliza")
    if existente.lifecycle is RuntimeCloneLifecycle.PROVISIONED:
        if existente.verified_runtime_setup_id != setup.setup_id:
            return _error(f"registro PROVISIONED de '{cid}' con setup verificado distinto al observado: fail-closed")
        return ProvisionamientoResult(
            success=True,
            message=f"Runtime Clone '{cid}' ya estaba PROVISIONED y verifica (idempotente)",
            generation_id=gid,
            generation_metadata=metadata,
            clone_id=cid,
            clone_root=destino,
            runtime_setup_id=setup.setup_id,
            clone_record=existente,
        )
    if existente.lifecycle is RuntimeCloneLifecycle.PROVISIONING:
        if existente.intended_runtime_setup_id != setup.setup_id:
            return _error(f"registro PROVISIONING de '{cid}' con setup intencionado distinto al observado: fail-closed")
        _actualizar_lifecycle_clone_bajo_lock(
            raiz,
            cid,
            RuntimeCloneLifecycle.PROVISIONED,
            verified_runtime_setup_id=setup.setup_id,
        )
        verificacion_clone = verificar_runtime_clone(raiz, cid, managed_source_root=managed_source_root)
        if not verificacion_clone.success:
            _marcar_clone_invalid_bajo_lock(raiz, cid)
            return _error(f"el Clone recuperado no re-verifica como VALID: {verificacion_clone.message}")
        return ProvisionamientoResult(
            success=True,
            message=f"Runtime Clone '{cid}' recuperado de PROVISIONING y PROVISIONED (setup={setup.setup_id})",
            generation_id=gid,
            generation_metadata=metadata,
            clone_id=cid,
            clone_root=destino,
            runtime_setup_id=setup.setup_id,
            clone_record=leer_runtime_clone_record(raiz, cid),
        )
    return _error(f"registro de Clone '{cid}' en estado no recuperable ({existente.lifecycle.value}): fail-closed")


def crear_runtime_clone_desde_generation(
    storage_root: pathlib.Path,
    generation_id: str,
    clone_parent: pathlib.Path,
    clone_id: str,
    *,
    managed_source_root: pathlib.Path | None = None,
    margen_bytes: int = DEFAULT_MARGIN_BYTES,
) -> ProvisionamientoResult:
    """Crea un Runtime Clone independiente desde una Generation publicada.

    Toda la operación (validación de Generation + sidecar, re-inventario,
    pre-chequeo de id ocupado, copia, verificación física, setup y publicación
    del registro) ocurre dentro de UNA sección de ``participant_lock`` y usa las
    variantes ``*_bajo_lock`` de los registradores (el lock no es reentrante).

    Orden fail-closed:
    1. metadata + sidecar v2 cruzados contra la referencia embebida y el árbol
       observado (membership a tres vías: referencia == sidecar == observado);
    2. si el ``clone_id`` ya está ocupado por un linaje/root incompatible, se
       falla ANTES de copiar (no se crean huérfanos deterministas);
    3. el setup (SKSE del build exacto) se verifica sobre la Generation ANTES de
       copiar el árbol;
    4. copia de contenido, verificación fresca, independencia física y setup del
       Clone;
    5. si el árbol ya existe, se aplica recuperación/idempotencia explícita
       (PROVISIONING → PROVISIONED); si no, se registra y publica ``PROVISIONED``
       y se re-verifica antes de devolver éxito.

    No escribe ``active.json``, no toca MO2 y no ejecuta el binder.
    """
    try:
        raiz = _exigir_storage_operativo(storage_root)
        gid = validar_generation_id(generation_id)
        padre = pathlib.Path(os.path.abspath(os.fspath(clone_parent)))
        motivo = descripcion_de_enlace(padre)
        if motivo is not None:
            return _error(f"el directorio destino del Clone está redirigido: {motivo}")
        if not padre.is_dir():
            return _error(f"el directorio destino del Clone no existe o no es directorio: '{padre}'")
        cid = validar_clone_id(clone_id)
        destino = padre / cid
        try:
            admitido = admitir_clone_root_path(
                destino,
                storage_root=raiz,
                managed_source_root=managed_source_root,
            )
        except FrozenRuntimeError as exc:
            return _error(f"la ruta del Clone no es admisible: {exc}")

        with participant_lock(raiz, timeout=0.0) as handle:
            handle.assert_owned()
            _exigir_sin_transicion(raiz)
            verificacion = verificar_generation(raiz, gid)
            if not verificacion.success:
                return _error(f"la Generation '{gid}' no es VALID: {verificacion.message}")
            metadata = verificacion.recorded or leer_generation_metadata(raiz, gid)
            sidecar = _cargar_sidecar(raiz, gid)
            _exigir_sidecar_contra_metadata(sidecar, metadata)

            gen_dir = generation_dir(raiz, gid)
            try:
                gen_files = inventory_tree(gen_dir)
                gen_membership = capturar_membership_directorios(gen_dir)
            except (InventoryError, InventoryLinkError, DirectoryMembershipError) as exc:
                return _error(f"la Generation no es inspeccionable para copiar: {exc}")
            if tree_digest_from_files(gen_files) != metadata.tree_digest:
                return _error("el árbol de la Generation no coincide con su digest registrado (drift)")
            if gen_membership != sidecar.reference.directory_membership:
                return _error(
                    "la estructura de directorios de la Generation no coincide con la referencia Stock "
                    "(posible drift de directorios vacíos)"
                )

            existente = cargar_runtime_clone_record(raiz, cid)
            if existente is not None:
                if not _registro_compatible(existente, gid, admitido):
                    return _error(
                        f"ya existe un registro de Clone '{cid}' incompatible "
                        f"(lifecycle={existente.lifecycle.value}): no se reutiliza un id ocupado"
                    )
                if existente.lifecycle is RuntimeCloneLifecycle.PROVISIONED and not destino.exists():
                    return _error(
                        f"el registro de '{cid}' dice PROVISIONED pero falta el árbol del Clone: "
                        "reconciliación manual requerida (fail-closed)"
                    )
            # Pre-verificación del setup sobre la Generation: evita crear un
            # árbol huérfano cuando el runtime no es ejecutable por SKSE.
            setup_generacion = verificar_runtime_setup(gen_dir)
            if destino.exists():
                return _recuperar_clone_existente(
                    raiz,
                    destino,
                    admitido,
                    gid,
                    cid,
                    metadata,
                    sidecar,
                    managed_source_root,
                )

            _exigir_espacio(padre, metadata.tree_digest.bytes, margen_bytes)
            try:
                copiar_arbol_independiente(
                    gen_dir,
                    destino,
                    gen_files,
                    gen_membership.directories,
                    contenedor=padre,
                )
            except CandidateCopyError as exc:
                return _error(f"la copia del Clone falló: {exc}")

            _verificar_arbol_en_destino(
                destino,
                tree_digest=metadata.tree_digest,
                membership=sidecar.reference.directory_membership,
                runtime_identity=metadata.runtime_identity,
                etiqueta="Clone",
            )
            independencia = verify_clone_physical_integrity(
                destino,
                generation_root=gen_dir,
                managed_source_root=managed_source_root,
            )
            if not independencia.success:
                return _error(f"el Clone no es físicamente independiente: {independencia.message}")
            setup = verificar_runtime_setup(destino)
            if setup.setup_id != setup_generacion.setup_id:
                return _error("el setup del Clone copiado no coincide con el setup verificado de la Generation")

            if existente is None:
                _registrar_runtime_clone_record_bajo_lock(
                    raiz,
                    RuntimeCloneRecord(
                        schema_version=CLONE_RECORD_SCHEMA_VERSION,
                        clone_id=cid,
                        source_generation_id=gid,
                        lifecycle=RuntimeCloneLifecycle.PROVISIONING,
                        intended_runtime_setup_id=setup.setup_id,
                        verified_runtime_setup_id=None,
                        root_path=os.fspath(admitido),
                        admitted_role=CANONICAL_ADMITTED_ROLE,
                        created_at_ns=time.time_ns(),
                    ),
                    managed_source_root=managed_source_root,
                )
            _actualizar_lifecycle_clone_bajo_lock(
                raiz,
                cid,
                RuntimeCloneLifecycle.PROVISIONED,
                verified_runtime_setup_id=setup.setup_id,
            )
            verificacion_clone = verificar_runtime_clone(raiz, cid, managed_source_root=managed_source_root)
            if not verificacion_clone.success:
                _marcar_clone_invalid_bajo_lock(raiz, cid)
                return _error(f"el Clone registrado no re-verifica como VALID: {verificacion_clone.message}")
            final = leer_runtime_clone_record(raiz, cid)
            return ProvisionamientoResult(
                success=True,
                message=f"Runtime Clone '{cid}' creado y PROVISIONED (setup={setup.setup_id})",
                generation_id=gid,
                generation_metadata=metadata,
                clone_id=cid,
                clone_root=destino,
                runtime_setup_id=setup.setup_id,
                clone_record=final,
            )
    except ProvisioningError as exc:
        return _error(str(exc))
    except (FrozenRuntimeError, OSError) as exc:
        return _error(f"fallo de provisionamiento al crear el Clone: {exc}")
