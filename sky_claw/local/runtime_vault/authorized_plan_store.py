"""Store durable del plan autoritativo en ``AUTHORIZED_OPERATIONS`` (GP2-S4A).

Implementa la secuencia normativa de ADR 0010 §12.2 paso 7 (7a–7g) sobre el
namespace protegido existente (§11.3):

```text
gates de autorización (authorized_plan.build_authorized_plan)
  ↓
serialización canónica + authorized_plan_digest
  ↓
escritura create-once con SD canónico (temp + WriteFile)
  ↓
FlushFileBuffers == TRUE                       ← GATE (§12.2 7c, §24)
  ↓
verificación por handle + publicación no-reemplazante
  ↓
re-lectura byte a byte + revalidación de schema/digest/bindings (§12.2 7e)
  ↓
DurableAuthorizedPlan  (único objeto que afirma autoridad)
```

Propiedades que este módulo hace difíciles de violar:

- **Sin autoridad antes de la durabilidad.** ``DurableAuthorizedPlan`` sólo se
  acuña con prueba privada de acuñación (patrón ``GoldenMutationLockHandle``):
  si el flush falla, si la revalidación falla o si la escritura es ambigua, NO
  existe objeto de autoridad.
- **Create-once.** Un segundo intento sobre el mismo ``operation_id`` falla
  cerrado (``AuthorizedPlanAlreadyExistsError``); nunca se sustituye en silencio
  un plan autoritativo existente (§23).
- **Sin autoridad de path desde el caller.** Las rutas se derivan internamente
  desde ``ProgramData`` + ``operation_id``; la CLI del helper sigue aceptando
  sólo ``--operation-id`` y ``--staging-digest`` (§12.1/§57).
- **Lock vivo y ligado.** La promoción exige la ``PrivilegedBoundarySession``
  abierta con su ``GoldenMutationLock`` vivo, con identidad de operación e
  identidad física coincidentes con el plan (§14/§45). Un
  ``PrivilegedAuthorizationContext`` aislado no alcanza.
- **Durabilidad honesta.** Se afirma ``AUTHORIZED_PLAN_CONTENT_DURABILITY``
  (``FILE_FLAG_WRITE_THROUGH`` + ``FlushFileBuffers == TRUE``) y se declara
  ``POWER_LOSS_DIRECTORY_ENTRY_DURABILITY_LIMITATION = DECLARED``: Win32 no
  documenta un equivalente soportado a ``fsync(parent_directory)`` (§12.2 7d,
  §28.5). No se afirma power-loss-proof en todo escenario NTFS/cache/controlador.
- **Escritura ambigua clasificada por evidencia.** Si la escritura pudo haber
  ocurrido y después falló algo, no se afirma "el plan está ausente": se reabre
  el path protegido y se clasifica ``DURABLE`` / ``NOT_DURABLE`` /
  ``INDETERMINATE`` (§52).

Plataforma: el gate de plataforma vive en la capa que aporta la durabilidad
Win32 (el writer del namespace: ``CreateFileW`` + SD canónico + ``FlushFileBuffers``
verificado), NO en la política ni en la lectura de evidencia. Con un
``plan_writer`` inyectado la promoción corre en cualquier plataforma (tests
deterministas, o un backend futuro que cumpla el mismo contrato append + flush);
sin writer, en POSIX se lanza ``AuthorizedPlanUnsupportedError`` tipado en lugar de
inventar semántica durable. Los modelos y la serialización viven en
:mod:`authorized_plan` y son portables.
"""

from __future__ import annotations

import pathlib
import sys
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sky_claw.local.runtime_vault.authorization_context import (
    PrivilegedAuthorizationContext,
    PrivilegedBoundarySession,
)
from sky_claw.local.runtime_vault.authorized_plan import (
    AUTHORIZED_PLAN_FILE_NAME,
    AUTHORIZED_PLAN_OBJECT_NAME,
    AuthorizedPlan,
    AuthorizedPlanAlreadyExistsError,
    AuthorizedPlanError,
    AuthorizedPlanNotFoundError,
    AuthorizedPlanSchemaError,
    build_authorized_plan,
    deserialize_authorized_plan,
    serialize_authorized_plan,
)
from sky_claw.local.runtime_vault.trusted_registry import TrustedGoldenRegistry, load_trusted_golden_registry
from sky_claw.local.runtime_vault.trusted_registry_lock import derive_trusted_registry_path

#: Nombre normativo del candidate manifest dentro de ``staging/<op_id>/`` (§11.3).
CANDIDATE_MANIFEST_FILE_NAME = "candidate_manifest.json"

#: Afirmación normativa de durabilidad del CONTENIDO del archivo (§12.2 7d).
AUTHORIZED_PLAN_CONTENT_DURABILITY = "FILE_FLAG_WRITE_THROUGH_PLUS_FLUSHFILEBUFFERS_TRUE"

#: Limitación declarada: Win32 no documenta ``fsync(parent_directory)`` (§12.2 7d/§28.5).
POWER_LOSS_DIRECTORY_ENTRY_DURABILITY_LIMITATION = "DECLARED"


class AuthorizedPlanUnsupportedError(AuthorizedPlanError):
    """Store durable invocado en una plataforma sin las garantías Win32 del namespace."""


class AuthorizedPlanLockBindingError(AuthorizedPlanError):
    """La sesión no tiene un GoldenMutationLock vivo y ligado a esta operación/plan."""


class CandidateManifestUnavailableError(AuthorizedPlanError):
    """El candidate manifest no existe o no es legible en ``UNTRUSTED_STAGING``."""


class DurableAuthorizedPlanWriteError(AuthorizedPlanError):
    """La promoción durable falló; lleva el outcome observable del path protegido."""

    def __init__(self, message: str, *, outcome: DurableWriteOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


class DurableWriteOutcome(StrEnum):
    """Clasificación de evidencia observable de una escritura durable (§52).

    ``INDETERMINATE`` existe para no afirmar "el plan está ausente" cuando la
    escritura pudo haber ocurrido antes de la excepción.
    """

    DURABLE = "durable"
    NOT_DURABLE = "not_durable"
    INDETERMINATE = "indeterminate"


@runtime_checkable
class AuthorizedPlanDurableWriter(Protocol):
    """Puerto de escritura durable create-once del plan autoritativo.

    La implementación productiva delega en la primitiva única del namespace
    (``trusted_namespace.write_secured_file_create_once_at``). El puerto existe
    para que los tests inyecten fallas causales de flush/escritura sin tocar
    ``%ProgramData%`` real, y para que el gate de ``FlushFileBuffers`` sea
    demostrable en cualquier plataforma.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None: ...


class _NamespaceAuthorizedPlanWriter:
    """Writer productivo: primitiva atómica única del namespace protegido.

    La política create-once se distingue de los fallos de I/O por tipo de error:
    ``AuthorizedPlanAlreadyExistsError`` (replay, fail-closed sin sustituir) vs
    ``AuthorizedPlanError`` (fallo de escritura/flush/verificación, que el
    promotor clasifica por evidencia observable).
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        from sky_claw.local.runtime_vault.trusted_namespace import write_secured_file_create_once_at

        write_secured_file_create_once_at(
            dest,
            payload,
            object_name,
            validate=_revalidar_bytes,
            error_factory=AuthorizedPlanError,
            already_exists_error_factory=AuthorizedPlanAlreadyExistsError,
            parent_error_message=f"El directorio de operación '{dest.parent}' no existe o no es confiable",
        )


def _resolve_plan_writer(plan_writer: AuthorizedPlanDurableWriter | None) -> AuthorizedPlanDurableWriter:
    """Resuelve el writer durable y con él la exigencia de plataforma.

    Sin writer inyectado la durabilidad la aporta el namespace Win32 y se exige
    Windows (``AuthorizedPlanUnsupportedError`` fuera de él). Con writer
    inyectado, la durabilidad es responsabilidad del caller: la política de
    promoción (gates, create-once, revalidación) es idéntica en cualquier
    plataforma y no depende del ABI.
    """
    if plan_writer is not None:
        return plan_writer
    _ensure_windows()
    return _NamespaceAuthorizedPlanWriter()


def _revalidar_bytes(raw: bytes) -> None:
    """Callback de revalidación: sólo valida, nunca devuelve valor."""
    deserialize_authorized_plan(raw)


_MINT_PROOF = object()


class DurableAuthorizedPlan:
    """Autoridad durable del plan: sólo existe tras escribir, flushear y revalidar.

    No envuelve ningún handle del sistema: envuelve la EVIDENCIA re-leída del
    disco protegido. Se acuña únicamente desde
    :func:`promote_durable_authorized_plan` (prueba privada), de modo que ningún
    camino de código pueda fabricar autoridad sin durabilidad verificada (§27).
    """

    __slots__ = ("_path", "_plan")

    def __init__(self, plan: AuthorizedPlan, path: pathlib.Path, *, _proof: Any = None) -> None:
        if _proof is not _MINT_PROOF:
            raise AuthorizedPlanError(
                "DurableAuthorizedPlan sólo puede acuñarse tras write + FlushFileBuffers + revalidación"
            )
        if not isinstance(plan, AuthorizedPlan):
            raise AuthorizedPlanSchemaError("plan debe ser AuthorizedPlan")
        if not isinstance(path, pathlib.Path):
            raise AuthorizedPlanSchemaError("path debe ser pathlib.Path")
        self._plan = plan
        self._path = path

    @property
    def plan(self) -> AuthorizedPlan:
        return self._plan

    @property
    def path(self) -> pathlib.Path:
        return self._path

    @property
    def digest(self) -> str:
        """``authorized_plan_digest`` de la copia re-leída del disco."""
        return self._plan.plan_digest

    @property
    def operation_id(self) -> str:
        return self._plan.operation_id

    @property
    def physical_identity(self) -> tuple[int, int]:
        return (self._plan.volume_serial_number, self._plan.root_file_id)

    @property
    def canonical_root(self) -> str:
        return self._plan.canonical_root

    @property
    def volume_serial_number(self) -> int:
        return self._plan.volume_serial_number

    @property
    def root_file_id(self) -> int:
        return self._plan.root_file_id

    @property
    def node_count(self) -> int:
        return self._plan.node_count

    def node_for(self, relative_path: str) -> Any:
        return self._plan.node_for(relative_path)


# ============================================================================
# Derivación de rutas (nunca desde el caller)
# ============================================================================


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise AuthorizedPlanUnsupportedError(
            "El store durable del plan autoritativo sólo existe con las garantías Win32 del namespace"
        )


def derive_authorized_plan_dir(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """``%ProgramData%\\\\Sky-Claw\\\\runtime_vault\\\\operations\\\\<operation_id>``."""
    from sky_claw.local.runtime_vault.trusted_registry_lock import _resolve_runtime_vault_dir

    op = _validate_operation_id(operation_id)
    return pathlib.Path(_resolve_runtime_vault_dir(programdata_resolver=programdata_resolver) / "operations" / op)


def derive_authorized_plan_path(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """Path canónico del plan autoritativo de la operación."""
    return derive_authorized_plan_dir(operation_id, programdata_resolver=programdata_resolver) / (
        AUTHORIZED_PLAN_FILE_NAME
    )


def derive_candidate_manifest_path(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """Path canónico del candidate manifest en ``UNTRUSTED_STAGING`` (sólo lectura)."""
    from sky_claw.local.runtime_vault.trusted_registry_lock import _resolve_runtime_vault_dir

    op = _validate_operation_id(operation_id)
    return pathlib.Path(
        _resolve_runtime_vault_dir(programdata_resolver=programdata_resolver)
        / "staging"
        / op
        / CANDIDATE_MANIFEST_FILE_NAME
    )


class CandidateManifestPublishError(AuthorizedPlanError):
    """La publicacion del candidate manifest en staging no pudo confirmarse."""


class CandidateManifestReparseError(CandidateManifestPublishError):
    """La ruta de staging es o contiene un enlace: fail-closed sin publicar."""


def publish_candidate_manifest(
    operation_id: str,
    manifest_bytes: bytes,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    staging_writer: Any = None,
) -> pathlib.Path:
    """Publica los bytes CANONICOS del candidate manifest en UNTRUSTED_STAGING.

    Es la pieza que faltaba entre planning y S4-A:

    ``planning`` calcula ``SealedGoldenProtectionPlan.candidate_manifest_bytes``
    (serializacion canonica, digest ya ligado) y esta primitive lo deja en
    disco para que ``promote_durable_authorized_plan`` pueda RELEERLO y
    verificarlo. El digest no se pasa por parametro ni se recalcula aca: S4-A
    lo comprueba contra los bytes que relee, que es la propiedad que importa —
    si alguien sustituye el manifest entre la publicacion y la promocion, el
    digest no coincide y la promocion falla cerrada.

    Contrato:

    * **La ruta se deriva internamente** de ``operation_id`` +
      ``programdata_resolver`` (nunca del caller), reusando la MISMA
      derivacion que usa la lectura. Publicar y leer no pueden divergir.
    * **Create-once**: si el manifest ya existe, NO se sustituye. Un replay
      con el mismo contenido es un no-op; un replay con contenido distinto
      falla cerrado en vez de pisar evidencia.
    * **Reparse rechazado**: la ruta de staging y su directorio padre no
      pueden ser symlink ni junction. Staging es UNTRUSTED, pero "untrusted"
      no significa "cualquiera puede redirigir la escritura".
    * **Durabilidad declarada honestamente**: se exige ``FlushFileBuffers``
      verificado y se declara la misma limitacion que el resto del modulo
      (``POWER_LOSS_DIRECTORY_ENTRY_DURABILITY_LIMITATION``): Win32 no
      documenta un equivalente a ``fsync(parent_directory)``.

    Staging sigue siendo UNTRUSTED: esto no lo convierte en autoridad. El
    recovery NUNCA lee de aca (ver los anchors AST de
    ``test_runtime_vault_s4e_wiring.py``); sólo la promocion de S4-A relee el
    manifest, y lo revalida contra el digest del plan y los gates de esquema,
    PPSC, TGR y PRE.
    """
    if not isinstance(manifest_bytes, bytes) or not manifest_bytes:
        raise CandidateManifestPublishError("manifest_bytes debe ser bytes no vacío")
    # La derivación valida el operation_id y devuelve la ruta canónica: el
    # caller no puede influir en dónde se escribe.
    dest = derive_candidate_manifest_path(operation_id, programdata_resolver=programdata_resolver)
    parent = dest.parent

    # Reparse: ni el directorio de la operación ni `staging/` pueden ser un
    # enlace. Sin esto, un junction en staging redirigiria la escritura fuera
    # del namespace — y staging es justo la zona que no es de confianza.
    from sky_claw.app.security.links import link_kind_and_identity_or_raise

    try:
        for candidato in (parent, parent.parent):
            if not candidato.exists():
                continue
            kind, _identity = link_kind_and_identity_or_raise(candidato)
            if kind is not None:
                raise CandidateManifestReparseError(
                    f"la ruta de staging es un enlace ({kind}): fail-closed sin publicar"
                )
    except CandidateManifestPublishError:
        raise
    except OSError as exc:
        raise CandidateManifestPublishError(f"no se pudo verificar la ruta de staging '{parent}': {exc}") from exc

    writer = staging_writer or _default_staging_writer()
    try:
        parent.mkdir(parents=True, exist_ok=True)
        writer.write_create_once(dest, manifest_bytes, CANDIDATE_MANIFEST_FILE_NAME)
    except CandidateManifestPublishError:
        raise
    except FileExistsError:
        # Perdió el create-once. NO es un fallo de escritura: el destino ya
        # existe. Se reconcilia ABAJO — no se asume éxito ni se sustituye.
        pass
    except OSError as exc:
        raise CandidateManifestPublishError(f"no se pudo publicar el candidate manifest en '{dest}': {exc}") from exc
    else:
        # Ganador: confirmar que lo publicado es lo releíble. Es la misma
        # disciplina que la re-lectura del plan durable (paso 6 del §12.2 7e).
        releido = _releer_o_fallar(operation_id, dest, programdata_resolver, "post-publicación")
        if releido != manifest_bytes:
            raise CandidateManifestPublishError(
                f"re-lectura del candidate manifest no coincide en '{dest}': "
                "no se publica autoridad sobre bytes que nadie puede releer"
            )
        return dest

    # Perdió el create-once: el destino ya existe. NO se asume éxito ni se
    # sustituye — se relee y se RECONCILIA.
    #
    # Esto es lo que evita que un crash entre la publicación y la autorización
    # convierta la operación en un bloqueo permanente: el retry con la MISMA
    # operation_id y el MISMO plan republica los mismos bytes y sigue. Un retry
    # con bytes DIFERENTES es un conflicto real y falla cerrado, porque
    # sustituir un manifest ya publicado dejaría que una segunda planificación
    # se aprobara sobre el nombre de la primera.
    releido = _releer_o_fallar(operation_id, dest, programdata_resolver, "reconciliación de replay")
    if releido != manifest_bytes:
        raise CandidateManifestPublishError(
            f"candidate manifest ya publicado en '{dest}' no coincide con los bytes canónicos "
            "de esta operación: conflicto fail-closed, no se sustituye"
        )
    return dest


def _releer_o_fallar(
    operation_id: str,
    dest: pathlib.Path,
    programdata_resolver: Callable[[], object] | None,
    momento: str,
) -> bytes:
    """Re-llee los bytes publicados y falla cerrado si no se puede.

    Una publicación que nadie puede releer no es evidencia: S4-A relee del
    disco, así que confirmar la re-lectura es parte del contrato, no una
    comprobación opcional.
    """
    try:
        return read_candidate_manifest_bytes(operation_id, programdata_resolver=programdata_resolver)
    except Exception as exc:  # noqa: BLE001 — el fallo de re-lectura ES el dato
        raise CandidateManifestPublishError(
            f"candidate manifest publicado pero no releíble en {momento} ('{dest}'): {exc}"
        ) from exc


def _default_staging_writer() -> Any:
    """Writer de staging: create-once + flush verificado, sin SD canónico.

    Deliberadamente DISTINTO del writer del plan. El candidate manifest vive en
    UNTRUSTED_STAGING y NO lleva el SD canónico del namespace protegido: aplicarselo
    seria afirmar una proteccion que el contrato no define para staging, y
    presupondria que el SID propietario existe en la maquina — que es
    justamente lo que todavia no esta provisionado para el helper.

    Lo que si exige: create-once real (fallo si ya existe), contenido escrito
    a un hermano provisional y renombrado —publicacion no-reemplazante— y
    ``FlushFileBuffers`` verificado a traves del Handle o ``os.fsync``.
    """
    from sky_claw.local.runtime_vault.staging_writer import StagingCreateOnceWriter

    return StagingCreateOnceWriter()


def _validate_operation_id(value: object) -> str:
    if not isinstance(value, str):
        raise AuthorizedPlanSchemaError("operation_id debe ser string")
    normalized = value.strip()
    try:
        parsed = uuid.UUID(normalized)
    except ValueError as exc:
        raise AuthorizedPlanSchemaError(f"operation_id '{value}' no es un UUID válido") from exc
    if normalized != str(parsed):
        raise AuthorizedPlanSchemaError("operation_id debe ser UUID canónico con guiones en minúsculas")
    return normalized


# ============================================================================
# Lectura del candidato y del plan autoritativo
# ============================================================================


def read_candidate_manifest_bytes(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> bytes:
    """Re-lee los BYTES del candidate manifest ligados por ``staging_digest`` (§10).

    No se acepta ningún objeto Python derivado previamente del staging: el helper
    siempre vuelve a los bytes y los liga contra la evidencia privilegiada.
    """
    path = derive_candidate_manifest_path(operation_id, programdata_resolver=programdata_resolver)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise CandidateManifestUnavailableError(
            f"No existe candidate manifest para la operación '{operation_id}' en '{path}'"
        ) from exc
    except OSError as exc:
        raise CandidateManifestUnavailableError(f"No se pudo leer el candidate manifest '{path}': {exc}") from exc


def load_authorized_plan(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> AuthorizedPlan:
    """Carga y valida el plan autoritativo desde disco (fail-closed, sin staging).

    Un byte alterado, un digest inválido, una clave desconocida o un nodo
    malformado producen error tipado: nunca se rellena lo que falta desde el
    filesystem actual ni desde ``UNTRUSTED_STAGING`` (§41/§42/§46).
    """
    path = derive_authorized_plan_path(operation_id, programdata_resolver=programdata_resolver)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise AuthorizedPlanNotFoundError(
            f"No existe authorized_plan.json durable para la operación '{operation_id}' en '{path}'"
        ) from exc
    except OSError as exc:
        raise AuthorizedPlanSchemaError(f"No se pudo leer el plan autoritativo '{path}': {exc}") from exc
    return deserialize_authorized_plan(raw)


def load_durable_authorized_plan(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> DurableAuthorizedPlan:
    """Carga el plan autoritativo desde disco y lo acuña como autoridad durable.

    A diferencia de :func:`load_authorized_plan`, devuelve el objeto de autoridad
    (el que puede dar permiso de mutación), porque aquí sí se cumplen sus
    precondiciones: los bytes salen del archivo protegido, se deserializan con
    esquema cerrado, se recomprueba el digest y la ruta se deriva del ProgramData
    root + ``operation_id`` (§27, §41). Un plan ausente o corrupto falla cerrado.
    """
    path = derive_authorized_plan_path(operation_id, programdata_resolver=programdata_resolver)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise AuthorizedPlanNotFoundError(
            f"No existe authorized_plan.json durable para la operación '{operation_id}' en '{path}'"
        ) from exc
    except OSError as exc:
        raise AuthorizedPlanSchemaError(f"No se pudo leer el plan autoritativo '{path}': {exc}") from exc
    plan = deserialize_authorized_plan(raw)
    return DurableAuthorizedPlan(plan, path, _proof=_MINT_PROOF)


def classify_durable_authorized_plan(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> DurableWriteOutcome:
    """Clasifica por evidencia observable el estado durable del plan (§52).

    Nunca lanza por datos de disco: un plan ausente es ``NOT_DURABLE``, un plan
    presente pero corrupto es ``INDETERMINATE`` (no "ausente"), y un plan válido
    es ``DURABLE``. No consulta staging bajo ninguna circunstancia.
    """
    path = derive_authorized_plan_path(operation_id, programdata_resolver=programdata_resolver)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return DurableWriteOutcome.NOT_DURABLE
    except OSError:
        return DurableWriteOutcome.INDETERMINATE
    try:
        deserialize_authorized_plan(raw)
    except AuthorizedPlanError:
        return DurableWriteOutcome.INDETERMINATE
    return DurableWriteOutcome.DURABLE


# ============================================================================
# Promoción durable
# ============================================================================


TrustedRegistryProvider = Callable[[], TrustedGoldenRegistry]


def _resolve_registry(
    source: TrustedGoldenRegistry | TrustedRegistryProvider | None,
    programdata_resolver: Callable[[], object] | None,
) -> TrustedGoldenRegistry:
    if isinstance(source, TrustedGoldenRegistry):
        return source
    if source is None:
        return load_trusted_golden_registry(derive_trusted_registry_path(programdata_resolver=programdata_resolver))
    if callable(source):
        registry = source()
        if not isinstance(registry, TrustedGoldenRegistry):
            raise AuthorizedPlanSchemaError("el proveedor de TGR no devolvió TrustedGoldenRegistry")
        return registry
    raise AuthorizedPlanSchemaError("trusted_registry debe ser TrustedGoldenRegistry, proveedor o None")


def _require_live_bound_lock(
    session: PrivilegedBoundarySession,
    context: PrivilegedAuthorizationContext,
) -> None:
    """Exige sesión abierta + lock vivo + identidad de operación/física ligadas (§14)."""
    if not isinstance(session, PrivilegedBoundarySession):
        raise AuthorizedPlanLockBindingError("session debe ser PrivilegedBoundarySession")
    if not isinstance(context, PrivilegedAuthorizationContext):
        raise AuthorizedPlanSchemaError("context debe ser PrivilegedAuthorizationContext")
    if session.closed:
        raise AuthorizedPlanLockBindingError(
            "La sesión de frontera está cerrada: no se puede promover authority sin el GoldenMutationLock vivo"
        )
    lock = session.lock  # lanza AuthorizationSessionError si la sesión se cerró
    if lock.closed:
        raise AuthorizedPlanLockBindingError("El GoldenMutationLock de la sesión ya fue liberado: REFUSE_TO_PLAN")
    payload = context.ppsc_confirmation.payload
    identity = lock.identity
    if identity.operation_id != context.operation_id:
        raise AuthorizedPlanLockBindingError(
            "La identidad del lock no coincide con operation_id del contexto: REFUSE_TO_PLAN"
        )
    if identity.volume_serial_number != payload.volume_serial_number:
        raise AuthorizedPlanLockBindingError(
            "La identidad física del lock no coincide con el payload PPSC: REFUSE_TO_PLAN"
        )
    if identity.root_file_id != payload.root_file_id:
        raise AuthorizedPlanLockBindingError(
            "La identidad física del lock no coincide con el payload PPSC: REFUSE_TO_PLAN"
        )


def _require_lock_matches_plan(
    session: PrivilegedBoundarySession,
    plan: AuthorizedPlan,
) -> None:
    """El lock físico sigue siendo el del plan promovido (§45): nunca plan A con lock B."""
    identity = session.lock.identity
    if (
        identity.volume_serial_number != plan.volume_serial_number
        or identity.root_file_id != plan.root_file_id
        or identity.operation_id != plan.operation_id
    ):
        raise AuthorizedPlanLockBindingError(
            "El GoldenMutationLock retenido no corresponde a la identidad física del plan: REFUSE_TO_PLAN"
        )


def promote_durable_authorized_plan(
    *,
    context: PrivilegedAuthorizationContext,
    session: PrivilegedBoundarySession,
    trusted_registry: TrustedGoldenRegistry | TrustedRegistryProvider | None = None,
    programdata_resolver: Callable[[], object] | None = None,
    plan_writer: AuthorizedPlanDurableWriter | None = None,
) -> DurableAuthorizedPlan:
    """Promueve el candidato a plan autoritativo COMPLETO, protegido y durable.

    Secuencia (§12.2 paso 7, §26, §27, §51):

    1. Sesión abierta con ``GoldenMutationLock`` vivo y ligado (§14).
    2. Re-lectura de los bytes del candidate manifest + gate de ``staging_digest``.
    3. Gates de esquema, PPSC, TGR, ``node_count`` e integridad PRE por nodo.
    4. El lock físico debe seguir siendo el del plan (§45).
    5. Escritura create-once con ``FlushFileBuffers`` verificado (GATE).
    6. Re-lectura byte a byte + revalidación de schema, digest y bindings.
    7. Recién entonces se acuña ``DurableAuthorizedPlan``.

    Cualquier fallo previo al paso 7 produce ``REFUSE_TO_PLAN`` con CERO escrituras
    de ``authorized_plan.json``, CERO escrituras de journal y CERO mutaciones del
    Golden. Un fallo del flush NO acuña autoridad y NO permite ningún
    ``MUTATING(K)`` posterior.
    """
    _require_live_bound_lock(session, context)

    registry = _resolve_registry(trusted_registry, programdata_resolver)
    candidate_bytes = read_candidate_manifest_bytes(context.operation_id, programdata_resolver=programdata_resolver)
    plan = build_authorized_plan(
        context=context,
        candidate_manifest_bytes=candidate_bytes,
        trusted_registry=registry,
    )
    _require_lock_matches_plan(session, plan)

    payload = serialize_authorized_plan(plan)
    dest = derive_authorized_plan_path(context.operation_id, programdata_resolver=programdata_resolver)
    writer = _resolve_plan_writer(plan_writer)
    if not isinstance(writer, AuthorizedPlanDurableWriter):
        raise AuthorizedPlanSchemaError("plan_writer no cumple el contrato AuthorizedPlanDurableWriter")

    try:
        writer.write_create_once(dest, payload, AUTHORIZED_PLAN_OBJECT_NAME)
    except AuthorizedPlanAlreadyExistsError:
        # Create-once (§23): un replay del mismo operation_id falla cerrado y
        # NUNCA sustituye el plan autoritativo existente.
        raise
    except Exception as exc:  # noqa: BLE001 — boundary deliberada: se clasifica por evidencia y se re-lanza tipado
        outcome = classify_durable_authorized_plan(context.operation_id, programdata_resolver=programdata_resolver)
        raise DurableAuthorizedPlanWriteError(
            f"La escritura durable de authorized_plan.json falló (outcome observable={outcome.value}): {exc}",
            outcome=outcome,
        ) from exc

    return _revalidate_after_write(context=context, dest=dest, payload=payload, plan=plan)


def _revalidate_after_write(
    *,
    context: PrivilegedAuthorizationContext,
    dest: pathlib.Path,
    payload: bytes,
    plan: AuthorizedPlan,
) -> DurableAuthorizedPlan:
    """Post-write revalidation (§26): re-leer, revalidar y recién acuñar autoridad."""
    try:
        raw = dest.read_bytes()
    except OSError as exc:
        raise DurableAuthorizedPlanWriteError(
            f"No se pudo re-leer '{dest}' tras la escritura: {exc}",
            outcome=DurableWriteOutcome.INDETERMINATE,
        ) from exc
    if raw != payload:
        raise DurableAuthorizedPlanWriteError(
            f"Revalidación post-escritura: los bytes en disco no coinciden con los escritos en '{dest}'",
            outcome=DurableWriteOutcome.INDETERMINATE,
        )
    try:
        reloaded = deserialize_authorized_plan(raw)
    except AuthorizedPlanError as exc:
        raise DurableAuthorizedPlanWriteError(
            f"Revalidación post-escritura: el plan re-leído no es válido: {exc}",
            outcome=DurableWriteOutcome.INDETERMINATE,
        ) from exc
    if reloaded != plan:
        raise DurableAuthorizedPlanWriteError(
            "Revalidación post-escritura: el plan re-leído no coincide con el modelo promovido",
            outcome=DurableWriteOutcome.INDETERMINATE,
        )
    payload_ppsc = context.ppsc_confirmation.payload
    if (
        reloaded.operation_id != context.operation_id
        or reloaded.staging_digest != context.staging_digest
        or reloaded.node_count != payload_ppsc.node_count
        or reloaded.tree_digest != payload_ppsc.tree_digest
        or reloaded.volume_serial_number != payload_ppsc.volume_serial_number
        or reloaded.root_file_id != payload_ppsc.root_file_id
        or reloaded.canonical_root != payload_ppsc.canonical_root
        or reloaded.policy_version != payload_ppsc.policy_version
        or reloaded.operator_identity != context.operator_identity
    ):
        raise DurableAuthorizedPlanWriteError(
            "Revalidación post-escritura: los bindings del plan re-leído no coinciden con la evidencia privilegiada",
            outcome=DurableWriteOutcome.INDETERMINATE,
        )
    return DurableAuthorizedPlan(reloaded, dest, _proof=_MINT_PROOF)


__all__ = [
    "AUTHORIZED_PLAN_CONTENT_DURABILITY",
    "CANDIDATE_MANIFEST_FILE_NAME",
    "POWER_LOSS_DIRECTORY_ENTRY_DURABILITY_LIMITATION",
    "AuthorizedPlanDurableWriter",
    "AuthorizedPlanLockBindingError",
    "AuthorizedPlanUnsupportedError",
    "CandidateManifestPublishError",
    "CandidateManifestReparseError",
    "CandidateManifestUnavailableError",
    "DurableAuthorizedPlan",
    "DurableAuthorizedPlanWriteError",
    "DurableWriteOutcome",
    "TrustedRegistryProvider",
    "classify_durable_authorized_plan",
    "derive_authorized_plan_dir",
    "derive_authorized_plan_path",
    "derive_candidate_manifest_path",
    "load_authorized_plan",
    "load_durable_authorized_plan",
    "promote_durable_authorized_plan",
    "publish_candidate_manifest",
    "read_candidate_manifest_bytes",
]
