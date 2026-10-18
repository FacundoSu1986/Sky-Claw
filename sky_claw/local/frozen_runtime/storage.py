"""Root de storage de Frozen Runtime: admisión, layout, inicialización (P2).

Decisiones de contrato (ADR 0012; resuelve Q5 y per-user/per-machine de P0):

- **Per-user** (operación de usuario normal, sin helper privilegiado; MO2 es
  user-level; ownership/lifecycle simple). No hay servicio global.
- **Default**: ``~/.sky_claw/frozen-runtime`` — hermano de
  ``Config.DEFAULT_CONFIG_DIR`` (``~/.sky_claw``) y de
  ``Config.runtime_state_dir()`` por el MISMO motivo que ellos: es un lugar
  cuya ubicación no depende de desde dónde se lanzó el proceso. El config del
  repo ya rechaza defaults en ``%TEMP%``/``Documents``/cwd/game-dir; este
  default sigue la misma convención. Configurable: las generaciones pueden
  pesar decenas de GB, así que el root puede apuntarse a otra unidad (la copia
  cross-volume la maneja P3; los renames atómicos quedan dentro del root).
- Layout canónico: ``versions/<generation-id>/``, ``candidates/<candidate-id>/``
  (namespace reservado; P2 no crea Candidates funcionales), ``state/``.
- Inicialización idempotente y NO destructiva: nunca borra contenido
  desconocido (SFR-10/§32; sin garbage collection).

Escrituras permitidas: SÓLO dentro del FrozenRuntimeRoot (storage propio de
Sky-Claw). Prohibido escribir en Managed Source / Steam / MO2 (ancla AST).
"""

from __future__ import annotations

import os
import pathlib

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeStorageError
from sky_claw.local.frozen_runtime.independence import descripcion_de_enlace
from sky_claw.local.frozen_runtime.state import STATE_FILE_NAME, write_json_atomic
from sky_claw.local.frozen_runtime.storage_models import (
    StorageAdmissionResult,
    StorageAdmissionState,
    StorageInitResult,
)

VERSIONS_DIR_NAME = "versions"
CANDIDATES_DIR_NAME = "candidates"
STATE_DIR_NAME = "state"

# P4-S2: el init es idempotente y la concurrencia es legítima; se serializa
# esperando acotadamente en vez de fallar de inmediato con LOCK_BUSY.
INIT_LOCK_TIMEOUT_SECONDS = 30.0

_RESERVADOS_WINDOWS: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def default_storage_root() -> pathlib.Path:
    """Root per-user por defecto (convención ``~/.sky_claw`` del repo)."""
    return pathlib.Path.home() / ".sky_claw" / "frozen-runtime"


def versions_dir(root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(root) / VERSIONS_DIR_NAME


def candidates_dir(root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(root) / CANDIDATES_DIR_NAME


def state_dir(root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(root) / STATE_DIR_NAME


def active_state_path(root: pathlib.Path) -> pathlib.Path:
    return state_dir(root) / STATE_FILE_NAME


def generations_state_dir(root: pathlib.Path) -> pathlib.Path:
    return state_dir(root) / "generations"


def transition_journal_path(root: pathlib.Path) -> pathlib.Path:
    """Journal vigente de transición (``state/transition.json``; ADR §34.4)."""
    return state_dir(root) / "transition.json"


def transitions_state_dir(root: pathlib.Path) -> pathlib.Path:
    """Store INMUTABLE de registros finalizados (``state/transitions/``; §37.5)."""
    return state_dir(root) / "transitions"


def finalization_seqs_state_dir(root: pathlib.Path) -> pathlib.Path:
    """Store de reservas por ``finalization_seq`` (``state/finalization-seqs/``; §38.3)."""
    return state_dir(root) / "finalization-seqs"


def approval_ledger_path(root: pathlib.Path) -> pathlib.Path:
    """Ledger de trabajo de la aprobación (``state/approval.json``; §37.3).

    Es **caché reconciliable**, no autoridad: su contenido se deriva de los dos
    stores inmutables (§37.6/§40.2) y ``approval_revision`` es un token de
    concurrencia, no una barrera anti-replay.
    """
    return state_dir(root) / "approval.json"


def issuances_state_dir(root: pathlib.Path) -> pathlib.Path:
    """Store INMUTABLE de witnesses de emisión (``state/issuances/``; §40.2).

    Un archivo por ``approval_seq``. El ``CREATE`` del witness **es** la emisión:
    sobrevive a la restauración del ledger y es lo que hace cumplir
    ``MAX_ISSUED_APPROVALS_PER_ROOT = 1`` por no-clobber.
    """
    return state_dir(root) / "issuances"


def approvals_state_dir(root: pathlib.Path) -> pathlib.Path:
    """Store INMUTABLE de tombstones terminales (``state/approvals/``; §38.2).

    Un archivo por aprobación terminal (``<seq>-<id>.json``). Es la autoridad
    anti-rollback: prueba que una aprobación TERMINÓ incluso si nunca finalizó una
    transición. ``APPROVAL_TOMBSTONE_GC = PROHIBIDO``.
    """
    return state_dir(root) / "approvals"


def generation_dir(root: pathlib.Path, generation_id: str) -> pathlib.Path:
    return versions_dir(root) / generation_id


def _normcase_abspath(path: pathlib.Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _contenida(interior: pathlib.Path, contenedor: pathlib.Path) -> bool:
    """True si *interior* es *contenedor* o está debajo (comparación normcase)."""
    i = _normcase_abspath(interior)
    c = _normcase_abspath(contenedor)
    if i == c:
        return True
    return i.startswith(c.rstrip(os.sep) + os.sep)


def _forma_reservada_windows(nombre: str) -> str | None:
    """Devuelve el motivo si el nombre final es una forma ambigua de Windows."""
    limpio = nombre.rstrip(" .")
    if limpio != nombre:
        return f"punto o espacio final ambiguo en '{nombre}'"
    token = limpio.split(".", 1)[0].split("__", 1)[0].casefold()
    if token in _RESERVADOS_WINDOWS:
        return f"nombre reservado de Windows: '{token}'"
    return None


def admitir_storage_root(
    root: pathlib.Path,
    *,
    managed_source_root: pathlib.Path | None = None,
) -> StorageAdmissionResult:
    """Admisión fail-closed del root de storage.

    Reglas: ruta absoluta y normalizada; sin symlink/junction/reparse en el
    root ni ancestros; si existe, debe ser directorio; sin formas reservadas
    de Windows; sin solapamiento con la Managed Source (ni contención en
    ningún sentido); y fuera de cualquier ``steamapps/common/<game>`` de
    Steam.
    """
    ruta = pathlib.Path(root)
    if not ruta.is_absolute():
        # abspath (NO resolve): no sigue symlinks/junctions; el chequeo de
        # enlaces debe ver la ruta ORIGINAL configurada, no su target.
        ruta = pathlib.Path(os.path.abspath(os.fspath(ruta)))
    motivo = descripcion_de_enlace(ruta)
    if motivo is not None:
        return StorageAdmissionResult(
            state=StorageAdmissionState.REJECTED,
            message=f"root de storage redirigido: {motivo}",
            root=ruta,
        )
    if ruta.exists() and not ruta.is_dir():
        return StorageAdmissionResult(
            state=StorageAdmissionState.REJECTED,
            message=f"existe un archivo donde se esperaba el directorio root: '{ruta}'",
            root=ruta,
        )
    forma = _forma_reservada_windows(ruta.name)
    if forma is not None:
        return StorageAdmissionResult(
            state=StorageAdmissionState.REJECTED, message=f"nombre de root inválido: {forma}", root=ruta
        )
    if managed_source_root is not None:
        fuente = pathlib.Path(managed_source_root)
        if _contenida(ruta, fuente):
            return StorageAdmissionResult(
                state=StorageAdmissionState.REJECTED,
                message=f"el root de storage está dentro de (o es) la Managed Source: '{ruta}'",
                root=ruta,
            )
        if _contenida(fuente, ruta):
            return StorageAdmissionResult(
                state=StorageAdmissionState.REJECTED,
                message=f"la Managed Source está dentro del root de storage: '{fuente}'",
                root=ruta,
            )
    # El chequeo incluye la ruta MISMA y sus ancestros: `steamapps/common`
    # exacto (sin <game>) también es área administrada por Steam (P2-M1).
    for componente in (ruta, *ruta.parents):
        if componente.name.casefold() == "common" and componente.parent.name.casefold() == "steamapps":
            return StorageAdmissionResult(
                state=StorageAdmissionState.REJECTED,
                message=f"el root de storage está dentro de un área administrada por Steam: '{componente}'",
                root=ruta,
            )
    return StorageAdmissionResult(state=StorageAdmissionState.ADMITTED, message="root de storage admitido", root=ruta)


def admitir_directorio_storage(path: pathlib.Path) -> StorageAdmissionResult:
    """Admisión fail-closed de un componente persistente del storage.

    Distingue: ausente ⇒ ADMITTED (puede crearse); directorio real ⇒ ADMITTED;
    archivo ⇒ REJECTED; symlink/junction/reparse ⇒ REJECTED (namespace
    redirigido: no se sigue el target ni se escribe en él); no inspeccionable
    ⇒ REJECTED. Primitive común para root/versions/candidates/state/
    state/generations (P2-B1): ninguna escritura de Frozen Runtime puede
    escapar del FrozenRuntimeRoot por un link inyectado.
    """
    ruta = pathlib.Path(path)
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        return StorageAdmissionResult(
            state=StorageAdmissionState.REJECTED,
            message=f"no se pudo inspeccionar '{ruta}': {exc} (fail-closed)",
            root=ruta,
        )
    if kind is not None:
        return StorageAdmissionResult(
            state=StorageAdmissionState.REJECTED,
            message=f"'{ruta}' es un enlace/reparse ({kind}): namespace redirigido (fail-closed)",
            root=ruta,
        )
    if not ruta.exists():
        return StorageAdmissionResult(
            state=StorageAdmissionState.ADMITTED, message=f"'{ruta}' no existe: puede crearse", root=ruta
        )
    if not ruta.is_dir():
        return StorageAdmissionResult(
            state=StorageAdmissionState.REJECTED,
            message=f"existe un archivo donde se esperaba el directorio '{ruta}'",
            root=ruta,
        )
    return StorageAdmissionResult(
        state=StorageAdmissionState.ADMITTED, message=f"'{ruta}' es un directorio real", root=ruta
    )


def initialize_frozen_runtime_storage(
    root: pathlib.Path,
    *,
    managed_source_root: pathlib.Path | None = None,
) -> StorageInitResult:
    """Inicializa el layout de storage de forma idempotente y no destructiva.

    Crea ``root/versions``, ``root/candidates``, ``root/state`` con permisos
    normales y escribe ``state/active.json`` v1 (``desired=None``) SÓLO si el
    estado no existe. Nunca borra contenido desconocido; ante conflicto
    (archivo donde se espera directorio, reparse, etc.) falla cerrado.
    """
    admission = admitir_storage_root(root, managed_source_root=managed_source_root)
    ruta = admission.root if admission.root is not None else pathlib.Path(root)
    if not admission.success:
        return StorageInitResult(admission=admission, root=ruta)

    # Exclusión (P4-S2): la inicialización crea directorios y escribe el estado
    # inicial; dos procesos inicializando el mismo root deben serializarse. El
    # root lock crea `state/` si no existe (misma admisión fail-closed), así que
    # es compatible con la precondición del inicializador. Import diferido para
    # no crear el ciclo storage → root_lock → storage.
    #
    # TIMEOUT: el init es idempotente y la concurrencia es legítima (dos
    # arranques simultáneos del mismo root no son un error), así que el contrato
    # correcto es ESPERAR acotadamente y serializar, no fallar de inmediato con
    # LOCK_BUSY. Un `timeout=0` convertiría una carrera benigna en un fallo.
    #
    # CONTRATO DE RESULTADO: `initialize_frozen_runtime_storage` documenta
    # resultado TIPADO (`StorageInitResult.success`) y no excepción. El lock
    # re-aplica la admisión anti-reparse al crear `state/` y falla con
    # `FrozenRuntimeLockAdmissionError`; si eso escapara, un caller que sólo mira
    # `resultado.success` recibiría una excepción en vez del rechazo tipado, y
    # los caminos de reparse-redirect (sr03/sr04/sr09) cambiarían de forma. Cada
    # fallo del lock se traduce a un `StorageAdmissionResult` RECHAZADO.
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockError
    from sky_claw.local.frozen_runtime.root_lock import participant_lock

    try:
        with participant_lock(ruta, timeout=INIT_LOCK_TIMEOUT_SECONDS) as handle:
            handle.assert_owned()
            return _initialize_frozen_runtime_storage_locked(ruta, admission=admission)
    except FrozenRuntimeLockError as exc:
        return StorageInitResult(
            admission=StorageAdmissionResult(
                state=StorageAdmissionState.REJECTED,
                message=f"no se pudo adquirir la exclusión del FrozenRuntimeRoot: {exc}",
                root=ruta,
            ),
            root=ruta,
        )


def _initialize_frozen_runtime_storage_locked(
    ruta: pathlib.Path,
    *,
    admission: StorageAdmissionResult,
) -> StorageInitResult:
    """Cuerpo de la inicialización, ya bajo el root lock (no re-adquiere)."""
    # Cada componente persistente se admite fail-closed ANTES de crear/escribir
    # (P2-B1): un junction/symlink en `state/` o `state/generations/` no puede
    # redirigir escrituras fuera del FrozenRuntimeRoot.
    #
    # P4-S3: el store del journal (`state/transitions/` y
    # `state/finalization-seqs/`) se crea acá, bajo el lock y con la misma
    # admisión. El journal NO crea directorios en su camino de escritura: si el
    # store falta, falla cerrado con ``JournalStoreNoInicializadoError``.
    #
    # P4-S4: los dos stores INMUTABLES de la autoridad de aprobación
    # (`state/issuances/` y `state/approvals/`) siguen la MISMA regla: los crea
    # el storage bajo su admisión, y la autoridad falla cerrado si faltan. El
    # ledger (`state/approval.json`) no necesita directorio propio — vive en
    # `state/`, que ya está en la lista.
    componentes: tuple[pathlib.Path, ...] = (
        ruta,
        versions_dir(ruta),
        candidates_dir(ruta),
        state_dir(ruta),
        generations_state_dir(ruta),
        transitions_state_dir(ruta),
        finalization_seqs_state_dir(ruta),
        issuances_state_dir(ruta),
        approvals_state_dir(ruta),
    )
    creados: list[pathlib.Path] = []
    try:
        for directorio in componentes:
            admision_dir = admitir_directorio_storage(directorio)
            if not admision_dir.success:
                return StorageInitResult(admission=admision_dir, root=ruta, created_dirs=tuple(creados))
            if not directorio.exists():
                directorio.mkdir(parents=True, exist_ok=True)
                creados.append(directorio)
    except OSError as exc:
        return StorageInitResult(
            admission=StorageAdmissionResult(
                state=StorageAdmissionState.REJECTED,
                message=f"no se pudo inicializar el storage (permisos/filesystem): {exc}",
                root=ruta,
            ),
            root=ruta,
            created_dirs=tuple(creados),
        )

    state_initialized = False
    if not active_state_path(ruta).exists():
        try:
            write_json_atomic(
                active_state_path(ruta),
                {"schema_version": 1, "desired_active_generation": None, "updated_at_ns": 0},
            )
        except (FrozenRuntimeStorageError, OSError) as exc:
            # El init documenta resultado tipado fail-closed: un fallo de
            # escritura del estado inicial no debe escapar como excepción a
            # callers que sólo miran StorageInitResult.success.
            return StorageInitResult(
                admission=StorageAdmissionResult(
                    state=StorageAdmissionState.REJECTED,
                    message=f"no se pudo escribir el estado inicial: {exc}",
                    root=ruta,
                ),
                root=ruta,
                created_dirs=tuple(creados),
            )
        state_initialized = True

    return StorageInitResult(
        admission=admission, root=ruta, created_dirs=tuple(creados), state_initialized=state_initialized
    )


def same_volume(a: pathlib.Path, b: pathlib.Path) -> bool:
    """True si dos rutas viven en el mismo volumen (primitive para P3/P4).

    En Windows, ``st_dev`` es el número de serie del volumen; las operaciones
    que dependan de rename atómico deben ocurrir dentro del mismo volumen del
    FrozenRuntimeRoot. Falta alguna ruta ⇒ fail-closed con error tipado. La
    Managed Source PUEDE vivir en otro volumen (la copia cross-volume la
    maneja P3).

    **Precondición del caller (P3/P4):** ambas rutas deben existir; creá el
    staging/los directorios ANTES de consultar. La primitive no crea nada.
    """
    try:
        dev_a = os.stat(a).st_dev
        dev_b = os.stat(b).st_dev
    except OSError as exc:
        raise FrozenRuntimeStorageError(f"no se pudo comparar volúmenes de '{a}' y '{b}': {exc}") from exc
    return dev_a == dev_b
