"""GP2-S4D: manifiesto de backup durable del Golden (modelo + store).

Contrato normativo ADR 0010 §12.2 paso 9, §19.2 (``ARCHIVING_BACKUP``), §20
(filas C7/C8) y §23.2.

Qué es (y qué NO es)
-------------------
El backup de GP2 **no** es un ZIP genérico ni una copia del árbol del Golden. Es
un **manifiesto canónico** con la evidencia de la que GP3 (futuro) necesita para
restaurar:

* la ruta canónica de la raíz y su identidad física
  (``VolumeSerialNumber`` + ``root FileId``);
* el ``TreeDigest`` autorizado (dimensiones incluidas: digest + files + bytes);
* la versión de política que produjo la Target DACL;
* la **tabla completa de ``NodeSecurityBackup``**: por nodo, el SD PRE en bytes
  (que es la fuente de restauración), su SHA-256, owner, group, flags de control
  del DACL, el flag ``pre_dacl_protected`` y la identidad física
  (volumen + FileId) con el ``relative_path`` canónico.

ADR 0010 §20 C7 fija la fuente de la copia: ``authorized_plan.json``. NUNCA
staging, NUNCA el ``candidate manifest``, NUNCA un ``path`` del caller. Por eso
este módulo no recibe paths: deriva el destino de la identidad del plan y recibe
un :class:`AuthorizedPlan` (o su autoridad durable) como única entrada.

Propiedades de durabilidad (en orden, sin atajos)
--------------------------------------------------
1. El padre DEBE existir y ser confiable (sin ``mkdir`` implícito).
2. ``create-once``: la publicación es single-winner. Si el destino ya existe, el
   store NO lo sustituye — lo RECLASIFICA (ausente / válido / corrupto /
   ajeno) y sólo acepta un objeto existente si demuestra equivalencia exacta de
   bytes **y** todos los bindings (§14).
3. ``write`` + ``FlushFileBuffers`` verificado como GATE: sin flush confirmado no
   se publica ni se acuña autoridad.
4. Verificación por handle del objeto publicado.
5. Relectura byte a byte + recomputación del digest + ``validate`` del caller.
6. Recién entonces se acuña :class:`DurableGoldenBackupArchive`.

Limitación declarada (heredada, no la borra nadie)
--------------------------------------------------
``FlushFileBuffers`` sobre el handle del ARCHIVO no demuestra por sí solo la
durabilidad de la **entrada de directorio** frente a power-loss: el nombre puede
quedar sin persistir aunque los bytes sí estén. Lo que este módulo demuestra es
``PROCESS_CRASH_RECOVERY`` (create-once + flush + relectura). No se afirma
``POWER_LOSS_SAFE``. Es la misma limitación que ya declara ADR 0010 §19.2 para
``authorized_plan.json`` y el journal; no se degrada ni se maquilla acá.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from sky_claw.local.runtime_vault.authorized_plan import AuthorizedPlan
from sky_claw.local.runtime_vault.authorized_plan_store import DurableAuthorizedPlan, DurableWriteOutcome
from sky_claw.local.runtime_vault.golden_protection_plan import (
    node_security_backup_from_dict,
    node_security_backup_to_dict,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError, TreeDigest
from sky_claw.local.runtime_vault.trusted_namespace import GOLDEN_BACKUP_MANIFEST_OBJECT

#: Versión del esquema del manifiesto de backup. Distinta de
#: ``AUTHORIZED_PLAN_SCHEMA_VERSION`` a propósito: el backup es un artefacto
#: DERIVADO y su forma puede evolucionar sin arrastrar al plan autoritativo.
#: GP3 debe rechazar un ``schema_version`` que no conozca en vez de asumirlo.
GOLDEN_BACKUP_SCHEMA_VERSION: Final[str] = "gp2-golden-backup-v1"

#: Sufijo del objeto dentro de ``golden_backups/<vol>_<fid>/<policy_version>/``
#: (ADR 0010 §23.2). El ``operation_id`` va en el NOMBRE, no dentro del
#: contenido, para que dos operaciones concurrentes sobre el mismo Golden no
#: compitan por el mismo objeto.
GOLDEN_BACKUP_FILE_SUFFIX: Final[str] = "_manifest.json"

#: Escala del nombre de archivo dentro de ``golden_backups/``. El ADR lo escribe
#: como ``<vol_serial>_<root_file_id>``; la escala se fija acá de forma cerrada
#: porque un nombre ambiguo (dos enteros separados por ``_``) sería parseable de
#: varias formas y rompería la unicidad single-winner del create-once.
_VOLUME_SERIAL_SCALE: Final[int] = 16
_ROOT_FILE_ID_SCALE: Final[int] = 32

#: Conjunto CERRADO de claves raíz del manifiesto. Fail-closed: cualquier campo
#: de más es corrupción o un esquema futuro que este código no debe interpretar
#: como si fuera autoridad actual.
_ARCHIVE_ROOT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "operation_id",
        "canonical_root",
        "volume_serial_number",
        "root_file_id",
        "tree_digest",
        "policy_version",
        "authorized_plan_digest",
        "node_count",
        "nodes",
    }
)

_TREE_DIGEST_KEYS: Final[frozenset[str]] = frozenset({"digest", "files", "bytes"})


# ============================================================================
# Excepciones tipadas
# ============================================================================


class GoldenBackupError(RuntimeVaultError):
    """Base de errores del backup durable del Golden."""


class GoldenBackupUnsupportedError(GoldenBackupError):
    """Plataforma sin las garantías Win32 que el store necesita."""


class GoldenBackupSchemaError(GoldenBackupError):
    """El manifiesto no cumple el esquema cerrado o sus bindings no cuadran."""


class GoldenBackupNotFoundError(GoldenBackupError):
    """No existe manifiesto de backup para esta operación (evidencia ausente)."""


class GoldenBackupIndeterminateError(GoldenBackupError):
    """Hay un objeto en el destino pero NO se puede demostrar que sea el nuestro.

    Cubre los tres casos que §20 exige no sobrescribir: objeto corrupto, objeto
    de OTRA ``operation_id`` y objeto cuyo digest no coincide con los bytes.
    Clasificar es la única salida; reemplazar no lo es.
    """


class GoldenBackupAlreadyExistsError(GoldenBackupError):
    """El destino existe y no es un replay equivalente de esta misma operación."""


class GoldenBackupWriteError(GoldenBackupError):
    """Fallo de I/O al publicar el manifiesto (no se acuña autoridad)."""


class GoldenBackupDurabilityError(GoldenBackupError):
    """``FlushFileBuffers`` no confirmado: el backup NO es durable y NO se publica.

    Equivalente al contrato del plan (§24) y del journal: sin flush verificado
    no existe backup, y sin backup no hay ``COMMITTED``.
    """


# ============================================================================
# Modelo inmutable
# ============================================================================


@dataclass(frozen=True, slots=True)
class GoldenBackupArchive:
    """Manifiesto de backup autoritativo e inmutable (ADR 0010 §23.2).

    Es un VALUE OBJECT: describe el contenido, no su durabilidad en disco. La
    durabilidad la representa :class:`DurableGoldenBackupArchive`, que sólo
    existe después de create-once + flush + relectura.
    """

    operation_id: str
    canonical_root: str
    volume_serial_number: int
    root_file_id: int
    tree_digest: TreeDigest
    policy_version: str
    authorized_plan_digest: str
    nodes: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise GoldenBackupSchemaError("operation_id debe ser un string no vacío")
        if not isinstance(self.canonical_root, str) or not self.canonical_root.strip():
            raise GoldenBackupSchemaError("canonical_root debe ser un string no vacío")
        for nombre, valor in (
            ("volume_serial_number", self.volume_serial_number),
            ("root_file_id", self.root_file_id),
        ):
            if not isinstance(valor, int) or isinstance(valor, bool) or valor <= 0:
                raise GoldenBackupSchemaError(f"{nombre} debe ser un entero positivo")
        if not isinstance(self.tree_digest, TreeDigest):
            raise GoldenBackupSchemaError("tree_digest debe ser TreeDigest")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise GoldenBackupSchemaError("policy_version debe ser un string no vacío")
        if not isinstance(self.authorized_plan_digest, str) or len(self.authorized_plan_digest) != 64:
            raise GoldenBackupSchemaError("authorized_plan_digest debe ser un sha256 hex de 64 caracteres")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise GoldenBackupSchemaError("nodes debe ser una tupla no vacía de NodeSecurityBackup")

        seen: set[str] = set()
        for node in self.nodes:
            if node.volume_serial_number != self.volume_serial_number:
                raise GoldenBackupSchemaError(
                    f"el nodo '{node.relative_path}' declara un VolumeSerialNumber distinto al de la raíz"
                )
            if node.relative_path in seen:
                raise GoldenBackupSchemaError(f"relative_path duplicado en el backup: '{node.relative_path}'")
            seen.add(node.relative_path)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def canonical_bytes(self) -> bytes:
        """Serialización canónica y determinista del manifiesto.

        Determinismo por construcción: ``sort_keys=True`` y sin espacios
        sobrantes. Dos procesos que construyan el backup del mismo plan producen
        bytes idénticos, que es lo que hace posible la revalidación de replay
        (aceptar un objeto existente sólo con equivalencia exacta) sin depender
        del reloj ni del orden de recorrido.
        """
        cuerpo = {
            "schema_version": GOLDEN_BACKUP_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "canonical_root": self.canonical_root,
            "volume_serial_number": self.volume_serial_number,
            "root_file_id": self.root_file_id,
            "tree_digest": {
                "digest": self.tree_digest.digest,
                "files": self.tree_digest.files,
                "bytes": self.tree_digest.bytes,
            },
            "policy_version": self.policy_version,
            "authorized_plan_digest": self.authorized_plan_digest,
            "node_count": self.node_count,
            "nodes": [node_security_backup_to_dict(node) for node in self.nodes],
        }
        return json.dumps(cuerpo, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

    @property
    def archive_digest(self) -> str:
        """SHA-256 de los bytes canónicos. Es el digest que el journal ata a COMMITTED."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def binds_to(self, plan: AuthorizedPlan) -> bool:
        """¿Este manifiesto demuestra ser el backup de ESTE plan?

        Comparación por IDENTIDAD, no por path: ``operation_id`` + raíz canónica +
        identidad física + ``TreeDigest`` + ``policy_version`` + digest del plan.
        Un archivo con la misma forma pero otra operación no pasa.
        """
        return (
            self.operation_id == plan.operation_id
            and self.canonical_root.upper() == plan.canonical_root.upper()
            and self.volume_serial_number == plan.volume_serial_number
            and self.root_file_id == plan.root_file_id
            and self.tree_digest == plan.tree_digest
            and self.policy_version == plan.policy_version
            and self.authorized_plan_digest == plan.plan_digest
            and self.node_count == plan.node_count
        )


def build_golden_backup_archive(plan: AuthorizedPlan) -> GoldenBackupArchive:
    """Construye el manifiesto de backup desde el plan autoritativo (ADR §20 C7).

    Única fuente: el ``DurableAuthorizedPlan``/``AuthorizedPlan``. La tabla de
    nodos se copia ORDENADA por ``relative_path`` para que el orden de
    construcción no dependa del recorrido del planner.
    """
    if not isinstance(plan, AuthorizedPlan):
        raise GoldenBackupSchemaError("plan debe ser AuthorizedPlan")
    return GoldenBackupArchive(
        operation_id=plan.operation_id,
        canonical_root=plan.canonical_root,
        volume_serial_number=plan.volume_serial_number,
        root_file_id=plan.root_file_id,
        tree_digest=plan.tree_digest,
        policy_version=plan.policy_version,
        authorized_plan_digest=plan.plan_digest,
        nodes=tuple(sorted(plan.nodes, key=lambda node: node.relative_path)),
    )


def _validate_sha256(digest: str, field_name: str) -> str:
    if not isinstance(digest, str) or len(digest) != 64:
        raise GoldenBackupSchemaError(f"{field_name} debe ser un sha256 hex de 64 caracteres")
    if any(char not in "0123456789abcdef" for char in digest):
        raise GoldenBackupSchemaError(f"{field_name} no es hexadecimal en minúsculas")
    return digest


def _validate_uint(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GoldenBackupSchemaError(f"{field_name} debe ser un entero positivo")
    return value


def deserialize_golden_backup_archive(raw: bytes) -> GoldenBackupArchive:
    """Deserializa con esquema CERRADO y revalida la integridad de cada nodo.

    Fail-closed por estructura: claves de más o de menos, tipos inesperados,
    digests que no cuadran y PRE SD que no re-deriva su propio SHA-256 (esa
    última la valida ``NodeSecurityBackup.__post_init__``) son todos rechazo, no
    "me lo banco y sigo". Un backup que no se puede interpretar no es evidencia:
    es un archivo.
    """
    if not isinstance(raw, bytes) or not raw:
        raise GoldenBackupSchemaError("el manifiesto debe ser bytes no vacío")
    try:
        cuerpo = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoldenBackupSchemaError(f"el manifiesto no es JSON UTF-8 válido: {exc}") from exc
    if not isinstance(cuerpo, dict):
        raise GoldenBackupSchemaError("el manifiesto debe ser un objeto JSON")
    if set(cuerpo) != _ARCHIVE_ROOT_KEYS:
        raise GoldenBackupSchemaError(
            f"claves raíz inesperadas: esperado={sorted(_ARCHIVE_ROOT_KEYS)}, observado={sorted(cuerpo)}"
        )
    if cuerpo["schema_version"] != GOLDEN_BACKUP_SCHEMA_VERSION:
        raise GoldenBackupSchemaError(
            f"schema_version '{cuerpo['schema_version']}' no es '{GOLDEN_BACKUP_SCHEMA_VERSION}': "
            "un esquema desconocido NO se interpreta como autoridad actual"
        )

    tree_raw = cuerpo["tree_digest"]
    if not isinstance(tree_raw, dict) or set(tree_raw) != _TREE_DIGEST_KEYS:
        raise GoldenBackupSchemaError("tree_digest debe ser un objeto con digest/files/bytes")

    nodes_raw = cuerpo["nodes"]
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise GoldenBackupSchemaError("nodes debe ser una lista no vacía")

    nodes = tuple(node_security_backup_from_dict(entry) for entry in nodes_raw)

    archive = GoldenBackupArchive(
        operation_id=str(cuerpo["operation_id"]),
        canonical_root=str(cuerpo["canonical_root"]),
        volume_serial_number=_validate_uint(cuerpo["volume_serial_number"], "volume_serial_number"),
        root_file_id=_validate_uint(cuerpo["root_file_id"], "root_file_id"),
        tree_digest=TreeDigest(
            digest=_validate_sha256(str(tree_raw["digest"]), "tree_digest.digest"),
            files=_validate_uint(tree_raw["files"], "tree_digest.files"),
            bytes=_validate_uint(tree_raw["bytes"], "tree_digest.bytes"),
        ),
        policy_version=str(cuerpo["policy_version"]),
        authorized_plan_digest=_validate_sha256(str(cuerpo["authorized_plan_digest"]), "authorized_plan_digest"),
        nodes=nodes,
    )
    if cuerpo["node_count"] != archive.node_count:
        raise GoldenBackupSchemaError(
            f"node_count {cuerpo['node_count']} no coincide con la tabla de nodos ({archive.node_count})"
        )
    if cuerpo["nodes"] != [node_security_backup_to_dict(node) for node in nodes]:
        raise GoldenBackupSchemaError("la tabla de nodos re-serializada no es idéntica a la almacenada")
    if archive.canonical_bytes() != raw:
        raise GoldenBackupSchemaError("los bytes canónicos del manifiesto no coinciden con los leídos del disco")
    return archive


# ============================================================================
# Derivación de rutas (nunca desde el caller)
# ============================================================================


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise GoldenBackupUnsupportedError(
            "El backup durable del Golden requiere las garantías Win32 del namespace de confianza"
        )


def _format_scope(volume_serial_number: int, root_file_id: int) -> str:
    """``<vol_serial>_<root_file_id>`` con escala fija, tal como lo escribe §23.2.

    La escala fija no es cosmética: sin ella, ``(1, 23)`` y ``(12, 3)`` producirían
    el mismo nombre y dos identidades físicas distintas competirían por el mismo
    objeto. El create-once convertiría eso en un ``INDETERMINATE`` espurio.
    """
    return f"{volume_serial_number:0{_VOLUME_SERIAL_SCALE}x}_{root_file_id:0{_ROOT_FILE_ID_SCALE}x}"


def derive_golden_backup_dir(
    volume_serial_number: int,
    root_file_id: int,
    policy_version: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """``golden_backups/<vol_serial>_<root_file_id>/<policy_version>/`` (ADR §23.2).

    Se deriva de la identidad del plan y del resolver de ProgramData. El caller
    NUNCA pasa una ruta: un ``path`` de backup controlable por el caller sería
    una vía de_stage: escribiría evidencia autoritativa donde el atacante elija.
    """
    _validate_uint(volume_serial_number, "volume_serial_number")
    _validate_uint(root_file_id, "root_file_id")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise GoldenBackupSchemaError("policy_version debe ser un string no vacío")
    from sky_claw.local.runtime_vault.trusted_registry_lock import _resolve_runtime_vault_dir

    root = pathlib.Path(_resolve_runtime_vault_dir(programdata_resolver=programdata_resolver))
    return root / "golden_backups" / _format_scope(volume_serial_number, root_file_id) / policy_version


def derive_golden_backup_path(
    plan: AuthorizedPlan, *, programdata_resolver: Callable[[], object] | None = None
) -> pathlib.Path:
    """Ruta completa del manifiesto de backup de ESTE plan (§23.2)."""
    return (
        derive_golden_backup_dir(
            plan.volume_serial_number,
            plan.root_file_id,
            plan.policy_version,
            programdata_resolver=programdata_resolver,
        )
        / f"{plan.operation_id}{GOLDEN_BACKUP_FILE_SUFFIX}"
    )


# ============================================================================
# Seam de escritura durable
# ============================================================================


@runtime_checkable
class GoldenBackupDurableWriter(Protocol):
    """Publicación create-once de un archivo plano protegido del namespace.

    Misma forma que ``AuthorizedPlanDurableWriter`` y
    ``OperationLockBindingWriter``: la política (create-once, flush, relectura,
    revalidación) vive en el store y es idéntica en cualquier plataforma; sólo
    la PRIMITIVA deWin32 se inyecta. Eso es lo que permite que el RIG cross-process
    de S4-D ejercite create-once y revalidación REALES sobre el filesystem sin
    necesidad de proprietor SYSTEM.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        """Publica ``payload`` en ``dest`` UNA sola vez. Lanza si ya existe."""
        ...


class _NamespaceGoldenBackupWriter:
    """Adaptador productivo: compone la primitiva Win32 auditada del namespace.

    NO reimplementa la secuencia — delega en
    ``write_secured_file_create_once_at``, que ya es create-once (publicación con
    ``CreateHardLinkW``, no ``os.replace``), nace con SD canónico, flushea con
    ``FILE_FLAG_WRITE_THROUGH``, verifica por handle y relee byte a byte. S4-D no
    necesita una segunda versión de esa secuencia y no debe tener una.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        from sky_claw.local.runtime_vault.trusted_namespace import write_secured_file_create_once_at

        write_secured_file_create_once_at(
            dest,
            payload,
            object_name,
            error_factory=GoldenBackupWriteError,
            already_exists_error_factory=GoldenBackupAlreadyExistsError,
            parent_error_message=(
                f"El directorio de backup '{dest.parent}' no existe: "
                "el namespace debe aprovisionar golden_backups/<scope>/<policy_version>/ antes de archivar"
            ),
        )


def _resolve_backup_writer(writer: GoldenBackupDurableWriter | None) -> GoldenBackupDurableWriter:
    if writer is not None:
        return writer
    _ensure_windows()
    return _NamespaceGoldenBackupWriter()


# ============================================================================
# Autoridad durable
# ============================================================================

_MINT_PROOF = object()


class DurableGoldenBackupArchive:
    """Autoridad durable del backup: sólo existe tras create-once + flush + relectura.

    No envuelve un handle: envuelve la evidencia RE-LEÍDA del disco protegido,
    igual que :class:`DurableAuthorizedPlan`. La prueba de acuñación es privada,
    de modo que ningún camino pueda fabricar un backup "durable" sin haber leído
    de vuelta los bytes que sí están en disco (§27).
    """

    __slots__ = ("_archive", "_path")

    def __init__(self, archive: GoldenBackupArchive, path: pathlib.Path, *, _proof: Any = None) -> None:
        if _proof is not _MINT_PROOF:
            raise GoldenBackupSchemaError(
                "DurableGoldenBackupArchive sólo puede acuñarse tras create-once + flush + relectura"
            )
        if not isinstance(archive, GoldenBackupArchive):
            raise GoldenBackupSchemaError("archive debe ser GoldenBackupArchive")
        if not isinstance(path, pathlib.Path):
            raise GoldenBackupSchemaError("path debe ser pathlib.Path")
        self._archive = archive
        self._path = path

    @property
    def archive(self) -> GoldenBackupArchive:
        return self._archive

    @property
    def path(self) -> pathlib.Path:
        return self._path

    @property
    def archive_digest(self) -> str:
        """Digest de los bytes canónicos LEÍDOS del disco, no del objeto en memoria."""
        return self._archive.archive_digest

    @property
    def operation_id(self) -> str:
        return self._archive.operation_id

    @property
    def canonical_root(self) -> str:
        return self._archive.canonical_root

    @property
    def physical_identity(self) -> tuple[int, int]:
        return (self._archive.volume_serial_number, self._archive.root_file_id)

    @property
    def node_count(self) -> int:
        return self._archive.node_count

    @property
    def policy_version(self) -> str:
        return self._archive.policy_version

    def has_node(self, relative_path: str) -> bool:
        return any(node.relative_path == relative_path for node in self._archive.nodes)

    def node_for(self, relative_path: str) -> Any:
        for node in self._archive.nodes:
            if node.relative_path == relative_path:
                return node
        raise GoldenBackupSchemaError(f"el backup no contiene el nodo '{relative_path}'")


# ============================================================================
# Clasificación observable (nunca lanza por datos de disco)
# ============================================================================


def classify_durable_golden_backup(
    plan: AuthorizedPlan,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> DurableWriteOutcome:
    """Clasifica el estado durable del backup por evidencia observable (§52).

    Ausente es ``NOT_DURABLE``; presente pero ilegible/corrupto/ajeno es
    ``INDETERMINATE`` — no "ausente". Un objeto de otra ``operation_id`` que
    ocupa el destino es ``INDETERMINATE``, nunca ``NOT_DURABLE``: la distinción
    importa porque ``NOT_DURABLE`` autoriza un create-once nuevo, y eso sería
    escribir sobre evidencia que no es nuestra.
    """
    path = derive_golden_backup_path(plan, programdata_resolver=programdata_resolver)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return DurableWriteOutcome.NOT_DURABLE
    except OSError:
        return DurableWriteOutcome.INDETERMINATE
    try:
        archive = deserialize_golden_backup_archive(raw)
    except GoldenBackupError:
        return DurableWriteOutcome.INDETERMINATE
    return DurableWriteOutcome.DURABLE if archive.binds_to(plan) else DurableWriteOutcome.INDETERMINATE


def load_durable_golden_backup(
    plan: AuthorizedPlan,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    path: pathlib.Path | None = None,
) -> DurableGoldenBackupArchive:
    """Carga el backup desde disco y lo acuña COMO autoridad durable.

    Re-lee los bytes, los re-serializa, exige que los canónicos coincidan y
    exige que todos los bindings contra el plan cuadren. Un backup que existe
    pero no ata con el plan no es un backup: es ``INDETERMINATE``.
    """
    destino = derive_golden_backup_path(plan, programdata_resolver=programdata_resolver) if path is None else path
    try:
        raw = destino.read_bytes()
    except FileNotFoundError as exc:
        raise GoldenBackupNotFoundError(
            f"No existe manifiesto de backup durable para la operación '{plan.operation_id}' en '{destino}'"
        ) from exc
    except OSError as exc:
        raise GoldenBackupIndeterminateError(
            f"No se pudo leer el manifiesto de backup '{destino}': evidencia ambigua, no ausente"
        ) from exc

    try:
        archive = deserialize_golden_backup_archive(raw)
    except GoldenBackupError as exc:
        raise GoldenBackupIndeterminateError(f"El manifiesto de backup '{destino}' no es interpretable: {exc}") from exc

    if not archive.binds_to(plan):
        raise GoldenBackupIndeterminateError(
            f"El manifiesto de backup '{destino}' no ata con el plan autoritativo de "
            f"'{plan.operation_id}': evidencia de otra operación o drift de identidad (fail-closed)"
        )
    return DurableGoldenBackupArchive(archive, destino, _proof=_MINT_PROOF)


# ============================================================================
# Publicación create-once
# ============================================================================


@dataclass(frozen=True, slots=True)
class GoldenBackupWriteOutcome:
    """Resultado de intentar publicar el backup.

    Distingue tres desenlaces que un ``bool`` no podría: se publicó ahora, se
    revalidó un objeto preexistente de la MISMA operación (replay), o no se
    pudo publicar. Los dos primeros son éxito durable; el tercero no.
    """

    durable: DurableGoldenBackupArchive
    republished: bool
    revalidated_existing: bool


def archive_golden_backup(
    plan: AuthorizedPlan | DurableAuthorizedPlan,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    writer: GoldenBackupDurableWriter | None = None,
) -> GoldenBackupWriteOutcome:
    """Publica el manifiesto de backup create-once y devuelve la autoridad durable.

    Secuencia (§12.2 paso 9, §23.2, §20 C7/C8):

    1. Construye el manifiesto desde el plan autoritativo (nunca desde staging).
    2. Serializa canónicamente.
    3. Clasifica el destino. Si ya hay un objeto, NUNCA lo sustituye: sólo lo
       acepta si su contenido re-serializa a los MISMOS bytes y ata con todos los
       bindings (replay de la misma ``operation_id``); en cualquier otro caso
       falla cerrado con :class:`GoldenBackupIndeterminateError`.
    4. Si está ausente, publica create-once y RECLASIFICA por evidencia de disco
       (no confía en el valor de retorno del writer).
    5. Re-lee, revalida y acuña :class:`DurableGoldenBackupArchive`.
    """
    plan_real = plan.plan if isinstance(plan, DurableAuthorizedPlan) else plan
    if not isinstance(plan_real, AuthorizedPlan):
        raise GoldenBackupSchemaError("plan debe ser AuthorizedPlan o DurableAuthorizedPlan")

    archive = build_golden_backup_archive(plan_real)
    payload = archive.canonical_bytes()
    dest = derive_golden_backup_path(plan_real, programdata_resolver=programdata_resolver)
    resolved_writer = _resolve_backup_writer(writer)

    estado = classify_durable_golden_backup(plan_real, programdata_resolver=programdata_resolver)
    if estado is DurableWriteOutcome.DURABLE:
        # Replay: el objeto existe, ata con el plan y sus bytes canónicos
        # coinciden. Aceptarlo es la ÚNICA forma de que un crash post-archive
        # pre-COMMITTED pueda continuar sin sobrescribir (§14).
        durable = load_durable_golden_backup(plan_real, programdata_resolver=programdata_resolver, path=dest)
        return GoldenBackupWriteOutcome(
            durable=durable,
            republished=False,
            revalidated_existing=True,
        )
    if estado is DurableWriteOutcome.INDETERMINATE:
        raise GoldenBackupIndeterminateError(
            f"Ya hay evidencia en '{dest}' que NO se puede demostrar como el backup de "
            f"'{plan_real.operation_id}' (corrupta, parcial o de otra operación): "
            "fail-closed; nunca se sobrescribe evidencia existente"
        )

    try:
        resolved_writer.write_create_once(dest, payload, GOLDEN_BACKUP_MANIFEST_OBJECT)
    except GoldenBackupDurabilityError:
        raise
    except GoldenBackupAlreadyExistsError:
        # Carrera single-winner: otro publicador ganó entre el classify y el
        # write. Reclasificar es la respuesta correcta — no "reintentar con
        # replace", que destruiría la evidencia del que ganó.
        estado_post = classify_durable_golden_backup(plan_real, programdata_resolver=programdata_resolver)
        if estado_post is DurableWriteOutcome.DURABLE:
            durable = load_durable_golden_backup(plan_real, programdata_resolver=programdata_resolver, path=dest)
            return GoldenBackupWriteOutcome(durable=durable, republished=False, revalidated_existing=True)
        raise GoldenBackupIndeterminateError(
            f"Colisión create-once en '{dest}' y el objeto resultante no ata con '{plan_real.operation_id}'"
        ) from None
    except GoldenBackupError as exc:
        raise GoldenBackupWriteError(f"No se pudo publicar el backup en '{dest}': {exc}") from exc
    except OSError as exc:
        # Un ``OSError`` crudo del writer (padre ausente, sharing violation,
        # disco lleno) NO puede escapar como error no tipado: el caller de S4-D
        # clasifica por tipo, y un FileNotFoundError sin tipar se leería como
        # "el backup no existe" en vez de "la publicación falló".
        raise GoldenBackupWriteError(f"No se pudo publicar el backup en '{dest}': {exc}") from exc

    # Reclasificación post-publicación: la autoridad se decide por EVIDENCIA DE
    # DISCO, no por el retorno del writer. Un writer que報告a éxito sin dejar
    # bytes legibles no publica nada.
    durable = load_durable_golden_backup(plan_real, programdata_resolver=programdata_resolver, path=dest)
    return GoldenBackupWriteOutcome(durable=durable, republished=True, revalidated_existing=False)


__all__ = [
    "GOLDEN_BACKUP_FILE_SUFFIX",
    "GOLDEN_BACKUP_MANIFEST_OBJECT",
    "GOLDEN_BACKUP_SCHEMA_VERSION",
    "DurableGoldenBackupArchive",
    "GoldenBackupAlreadyExistsError",
    "GoldenBackupArchive",
    "GoldenBackupDurableWriter",
    "GoldenBackupDurabilityError",
    "GoldenBackupError",
    "GoldenBackupIndeterminateError",
    "GoldenBackupNotFoundError",
    "GoldenBackupSchemaError",
    "GoldenBackupUnsupportedError",
    "GoldenBackupWriteError",
    "GoldenBackupWriteOutcome",
    "archive_golden_backup",
    "build_golden_backup_archive",
    "classify_durable_golden_backup",
    "derive_golden_backup_dir",
    "derive_golden_backup_path",
    "deserialize_golden_backup_archive",
    "load_durable_golden_backup",
]
