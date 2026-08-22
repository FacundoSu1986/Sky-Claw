"""Contrato durable del handoff de deployment TexGen → DynDOLOD (D2, PR #493).

El handoff es la identidad durable del artifact empaquetado ``mods/TexGen Output``
cuando el pipeline corta por visibilidad (``needs_deployment``). Vive en la MISMA
``journal.db`` que las transacciones —nunca en una DB aparte— porque el cierre
atómico "TX + handoff" necesita un solo boundary SQLite.

Separación de responsabilidades:

- **Este módulo es el contrato**: DDL, estados, dataclass, clave canónica del
  artifact físico y el reconciler de arranque. No abre conexiones propias: toda
  escritura pasa por la API pública de :class:`~sky_claw.app.db.journal.OperationJournal`
  (principio 5: nadie actualiza ``transactions`` en crudo ni toca el estado
  Python del journal desde afuera).
- **``OperationJournal`` es el ejecutor**: incorpora el DDL a su ``_SCHEMA_SQL``
  y expone los métodos atómicos (``crear_handoff_de_deployment``,
  ``completar_handoff_de_resume``, ``transicionar_handoff``, …).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import stat
from dataclasses import dataclass
from enum import StrEnum

# =============================================================================
# ESQUEMA
# =============================================================================

HANDOFFS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deployment_handoffs (
    handoff_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_tx_id       INTEGER NOT NULL REFERENCES transactions(transaction_id),
    state              TEXT NOT NULL DEFAULT 'awaiting_deployment',
    artifact_path      TEXT NOT NULL,
    game_key           TEXT NOT NULL,
    mods_root_key      TEXT NOT NULL,
    data_key           TEXT NOT NULL,
    expected_profile   TEXT NOT NULL,
    expected_digest    TEXT,
    expected_files     INTEGER,
    expected_bytes     INTEGER,
    observed_digest    TEXT,
    observed_files     INTEGER,
    observed_bytes     INTEGER,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at       TEXT,
    superseded_at      TEXT,
    superseded_by      INTEGER REFERENCES deployment_handoffs(handoff_id),
    CONSTRAINT chk_handoff_state CHECK (
        state IN ('awaiting_deployment','indeterminate','superseding','superseded','completed')
    ),
    -- R-002B: los estados que reclaman identidad autorizada (awaiting,
    -- superseding, completed) la exigen NOT NULL; 'indeterminate' no la tiene
    -- POR CONTRATO, y 'superseded' es historia de auditoría — una fila que
    -- quedó INDETERMINATE y luego fue reemplazada legítimamente llega a
    -- 'superseded' con expected_* NULL y debe poder terminar así.
    CONSTRAINT chk_handoff_expected_identity CHECK (
        state IN ('indeterminate', 'superseded')
        OR (expected_digest IS NOT NULL AND expected_files IS NOT NULL AND expected_bytes IS NOT NULL)
    ),
    CONSTRAINT chk_handoff_indeterminate_sin_autoridad CHECK (
        state != 'indeterminate' OR expected_digest IS NULL
    ),
    CONSTRAINT chk_handoff_expected_coherente CHECK (
        (expected_digest IS NULL AND expected_files IS NULL AND expected_bytes IS NULL)
        OR (expected_digest IS NOT NULL AND expected_files IS NOT NULL AND expected_bytes IS NOT NULL)
    ),
    CONSTRAINT chk_handoff_observed_coherente CHECK (
        (observed_digest IS NULL AND observed_files IS NULL AND observed_bytes IS NULL)
        OR (observed_digest IS NOT NULL AND observed_files IS NOT NULL AND observed_bytes IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_handoffs_owner_activo
    ON deployment_handoffs(artifact_path)
    WHERE state IN ('awaiting_deployment','indeterminate','superseding');

CREATE INDEX IF NOT EXISTS idx_handoffs_source_tx
    ON deployment_handoffs(source_tx_id);
"""

# =============================================================================
# RECEIPTS DE STALE SWEEP (F-001, PR #493 patch 0006)
# =============================================================================
#
# El sweep convierte PENDING→ROLLED_BACK en el arranque. El receipt es la
# marca DURABLE de esa causalidad: "esta TX fue convertida POR stale sweep".
# Sin ella, la única memoria del barrido vivía en RAM (``swept_this_open``) y
# un crash posterior —o un arranque sin configuración que omite el reconciler,
# o una excepción best-effort— dejaba un resume falso-verde (DynDOLOD llamada
# sobre un artifact cuya corrida quedó interrumpida).
#
# El receipt prueba SOLO causalidad: jamás identidad autorizada (Principio 3),
# por eso un receipt + manifest relevante únicamente puede conducir a
# INDETERMINATE, nunca a expected_*.

SWEEP_RECEIPTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stale_pending_sweep_receipts (
    receipt_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(transaction_id),
    state          TEXT NOT NULL DEFAULT 'unresolved',
    swept_at       TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT,
    CONSTRAINT chk_sweep_receipt_state CHECK (
        state IN ('unresolved','consumed','no_artifact','superseded')
    )
);

CREATE INDEX IF NOT EXISTS idx_sweep_receipts_state
    ON stale_pending_sweep_receipts(state);
"""

# =============================================================================
# ESTADOS
# =============================================================================


class HandoffState(StrEnum):
    """Estados del handoff de deployment.

    Los tres activos no son semánticamente equivalentes (C5): con
    ``run_texgen=False`` sólo ``AWAITING_DEPLOYMENT`` es resumible;
    ``INDETERMINATE`` y ``SUPERSEDING`` fallan cerrado. ``SUPERSEDED`` y
    ``COMPLETED`` son historia terminal de auditoría.
    """

    AWAITING_DEPLOYMENT = "awaiting_deployment"
    INDETERMINATE = "indeterminate"
    SUPERSEDING = "superseding"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"


#: Estados que reclaman ownership físico del artifact (el índice único parcial
#: congela este conjunto en SQL; este frozenset es el espejo Python que los
#: consumidores pueden enumerar — y el ancla de tests lo congela por igualdad).
HANDOFF_STATES_ACTIVOS: frozenset[HandoffState] = frozenset(
    {HandoffState.AWAITING_DEPLOYMENT, HandoffState.INDETERMINATE, HandoffState.SUPERSEDING}
)


class SweepReceiptState(StrEnum):
    """Estados del receipt durable de stale sweep (F-001, patch 0006).

    - ``UNRESOLVED``: la conversión PENDING→ROLLED_BACK por sweep quedó
      registrada y su evidencia aún no se resolvió — el oracle la cuenta;
    - ``CONSUMED``: el reconciler materializó un INDETERMINATE con ella
      (INSERT del handoff + consumo en el MISMO boundary);
    - ``NO_ARTIFACT``: con paths resolubles se demostró que el artifact NO
      existe — no hay mutación física preservada que proteger;
    - ``SUPERSEDED``: una provenance autorizada posterior reemplazó ese
      intento interrumpido — el receipt viejo no intoxica el futuro.
    """

    UNRESOLVED = "unresolved"
    CONSUMED = "consumed"
    NO_ARTIFACT = "no_artifact"
    SUPERSEDED = "superseded"


# =============================================================================
# REGISTRO
# =============================================================================


@dataclass(frozen=True, slots=True)
class DeploymentHandoff:
    """Fila de ``deployment_handoffs``.

    ``expected_*`` es la identidad AUTORIZADA del artifact (lo que TX produjo);
    ``observed_*`` es una observación del disco en un momento dado. Nunca se
    promociona lo segundo a lo primero por código (C3): el INSERT autorizado
    sólo sale del veredicto de una corrida propia.
    """

    handoff_id: int
    source_tx_id: int
    state: HandoffState
    artifact_path: str
    game_key: str
    mods_root_key: str
    data_key: str
    expected_profile: str
    expected_digest: str | None
    expected_files: int | None
    expected_bytes: int | None
    observed_digest: str | None
    observed_files: int | None
    observed_bytes: int | None
    created_at: str
    updated_at: str
    completed_at: str | None
    superseded_at: str | None
    superseded_by: int | None


# =============================================================================
# CLAVE CANÓNICA DEL ARTIFACT FÍSICO
# =============================================================================


def clave_de_artifact(ruta: pathlib.Path) -> str:
    """Clave canónica del artifact físico: resuelta + normalizada por caso (Windows).

    La exclusividad activa sigue al RECURSO FÍSICO, no al perfil: dos handoffs
    activos de perfiles distintos sobre el mismo ``<mo2>/mods/TexGen Output``
    son el mismo conflicto de ownership. ``normcase`` hace que las variantes de
    caso del mismo path colisionen en el índice único (Windows); en POSIX es
    identidad, que es la semántica correcta de su filesystem.

    F-002 (patch 0006): es la ÚNICA identidad física del sistema — el
    ownership lookup Y el oracle de orphans comparan con esta misma función
    (resolve + normcase), incluidos los strings históricos del ActionManifest
    (que se canonicalizan al LEERSE; la historia almacenada no se reescribe).
    Una normalización hermana link-safe hizo que un MO2 configurado vía
    symlink/junction matcheara en el lookup y fallara en el oracle; no volver
    a introducir una segunda definición de identidad.
    """
    return os.path.normcase(os.path.normpath(str(ruta.resolve(strict=False))))


class ArtifactReachability(StrEnum):
    """Resultado tri-state del alcance físico del artifact (F-01, patch 0007).

    Un booleano ``exists()``/``is_dir()`` NO sirve para decidir provenance:
    devuelve False tanto para "no existe" como para "no alcanzable", y
    ``NO_ARTIFACT`` es una afirmación TERMINAL que sólo puede emitirse con
    ausencia DEMOSTRADA (Principio 2).

    - ``PRESENT``: el artifact existe y es alcanzable;
    - ``ABSENT``: la ausencia fue positivamente demostrada — el contenedor
      físico esperado es alcanzable y válido, y el target no existe;
    - ``UNKNOWN``: no se puede afirmar ausencia (root inaccesible,
      PermissionError, device offline, share no disponible, OSError genérico,
      error de resolución, reparse point colgante…). UNKNOWN jamás autoriza
      cerrar un receipt: queda UNRESOLVED.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OrphanEvidenceUnresolved:
    """Evidencia durable de orphan SIN estado de artifact determinable (0008).

    ``reconciliar_orphan_de_artifact`` devuelve esto — y NO ``None`` — cuando
    EXISTE evidencia vigente (TX PENDING o receipt UNRESOLVED que nombra el
    artifact) pero ``alcance_del_artifact()`` es UNKNOWN: no se puede afirmar
    ni PRESENT ni ABSENT, y por tanto tampoco se puede cerrar el receipt ni
    declarar legacy.

    ``None`` queda reservado para UN único significado: no existe evidencia
    durable que obligue a bloquear (o la que existía se cerró legítimamente
    como NO_ARTIFACT). Los callers aplican su política: el hot path lo mapea
    a un bloqueo fail-closed; el startup lo trata como "todavía no resoluble"
    y continúa best-effort.
    """

    candidatas: tuple[int, ...] = ()


def alcance_del_artifact(artifact_path: pathlib.Path) -> ArtifactReachability:
    """Reachability tri-state del artifact físico (F-01, patch 0007).

    Verifica el MISMO recurso físico que :func:`clave_de_artifact` (Principio
    3): resuelve igual (``resolve(strict=False)`` + normcase en el caller), sin
    una tercera normalización. La probe es síncrona a propósito (syscalls de
    FS): el reconciler la invoca vía ``asyncio.to_thread``.

    Contrato 7-4 C: el artifact protegido es el MOD (``mods/TexGen Output``) —
    el mismo target que el handoff ownership. Un mod presente con ``textures``
    ausente o a medias es PRESENTE (evidencia de mutación inconclusa, no
    ausencia): el reconciler fabrica INDETERMINATE fail-closed, jamás
    NO_ARTIFACT.
    """
    try:
        resuelto = artifact_path.resolve(strict=False)
    except OSError:
        # Resolución fallida (reparse point roto, etc.): incierto.
        return ArtifactReachability.UNKNOWN
    try:
        info = resuelto.stat()
    except FileNotFoundError:
        return _ausencia_demostrada(resuelto)
    except OSError:
        # PermissionError, device offline, IO error: incierto.
        return ArtifactReachability.UNKNOWN
    if not stat.S_ISDIR(info.st_mode):
        # Existe algo en el lugar del artifact pero no es el directorio que el
        # contrato protege: la ausencia del artifact esperado queda demostrada
        # (el contenedor respondió a stat()).
        return ArtifactReachability.ABSENT
    return ArtifactReachability.PRESENT


def _ausencia_demostrada(resuelto: pathlib.Path) -> ArtifactReachability:
    """El target no existe; la ausencia sólo es DEMOSTRABLE si el contenedor
    físico esperado es alcanzable y válido. Cualquier ambigüedad → UNKNOWN."""
    try:
        info_padre = resuelto.parent.stat()
    except OSError:
        # El contenedor no responde (ausente o inaccesible): no se puede
        # demostrar que el artifact no exista. Confundir "no puedo
        # encontrarlo" con "probé que no existe" es el false-green.
        return ArtifactReachability.UNKNOWN
    if stat.S_ISDIR(info_padre.st_mode):
        return ArtifactReachability.ABSENT
    # El contenedor existe pero no es un directorio: configuración ambigua.
    return ArtifactReachability.UNKNOWN


# =============================================================================
# SQL compartido — el ÚNICO dueño de las columnas de la tabla es este módulo;
# OperationJournal compone estos fragmentos, no escribe su propia variante.
# =============================================================================

_COLUMNAS_HANDOFF = (
    "handoff_id, source_tx_id, state, artifact_path, game_key, mods_root_key, data_key, "
    "expected_profile, expected_digest, expected_files, expected_bytes, "
    "observed_digest, observed_files, observed_bytes, "
    "created_at, updated_at, completed_at, superseded_at, superseded_by"
)

# nosec B608: _COLUMNAS_HANDOFF es un literal cerrado de este módulo (única
# asignación, materializado en import time, sin input de caller) y los datos de
# runtime siguen parametrizados con placeholders ``?`` en el WHERE.
_SELECT_HANDOFF_ACTIVO_SQL = f"""
    SELECT {_COLUMNAS_HANDOFF}
    FROM deployment_handoffs
    WHERE artifact_path = ?
      AND state IN ('awaiting_deployment','indeterminate','superseding')
    LIMIT 1
"""  # nosec B608

_INSERT_HANDOFF_SQL = """
    INSERT INTO deployment_handoffs (
        source_tx_id, state, artifact_path, game_key, mods_root_key, data_key,
        expected_profile, expected_digest, expected_files, expected_bytes,
        observed_digest, observed_files, observed_bytes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def handoff_desde_fila(fila: tuple[object, ...]) -> DeploymentHandoff:
    """Mapea una fila de ``deployment_handoffs`` al dataclass, en el orden de
    :data:`_COLUMNAS_HANDOFF`."""
    (
        handoff_id,
        source_tx_id,
        state,
        artifact_path,
        game_key,
        mods_root_key,
        data_key,
        expected_profile,
        expected_digest,
        expected_files,
        expected_bytes,
        observed_digest,
        observed_files,
        observed_bytes,
        created_at,
        updated_at,
        completed_at,
        superseded_at,
        superseded_by,
    ) = fila
    return DeploymentHandoff(
        handoff_id=int(handoff_id),
        source_tx_id=int(source_tx_id),
        state=HandoffState(state),
        artifact_path=str(artifact_path),
        game_key=str(game_key),
        mods_root_key=str(mods_root_key),
        data_key=str(data_key),
        expected_profile=str(expected_profile),
        expected_digest=str(expected_digest) if expected_digest is not None else None,
        expected_files=int(expected_files) if expected_files is not None else None,
        expected_bytes=int(expected_bytes) if expected_bytes is not None else None,
        observed_digest=str(observed_digest) if observed_digest is not None else None,
        observed_files=int(observed_files) if observed_files is not None else None,
        observed_bytes=int(observed_bytes) if observed_bytes is not None else None,
        created_at=str(created_at),
        updated_at=str(updated_at),
        completed_at=str(completed_at) if completed_at is not None else None,
        superseded_at=str(superseded_at) if superseded_at is not None else None,
        superseded_by=int(superseded_by) if superseded_by is not None else None,
    )


def fila_de_registro(registro: DeploymentHandoff) -> tuple[object, ...]:
    """Parámetros del INSERT para un registro (sin ``handoff_id``, que es
    autogenerado)."""
    return (
        registro.source_tx_id,
        registro.state.value,
        registro.artifact_path,
        registro.game_key,
        registro.mods_root_key,
        registro.data_key,
        registro.expected_profile,
        registro.expected_digest,
        registro.expected_files,
        registro.expected_bytes,
        registro.observed_digest,
        registro.observed_files,
        registro.observed_bytes,
    )


# =============================================================================
# RECONCILER DE ARRANQUE (crash window RUN 1 y huérfanos de supersede)
# =============================================================================


async def reconciliar_orphan_de_artifact(
    *,
    journal: object,
    mod_texgen: pathlib.Path,
    game_key: str,
    mods_root_key: str,
    data_key: str,
    expected_profile: str,
    digest_arbol: object,
) -> DeploymentHandoff | OrphanEvidenceUnresolved | None:
    """UNA sola primitive de detección/reconciliación de orphan — consumida por
    el reconciler de arranque Y por el hot path del resume (F-002). No hay una
    segunda heurística divergente.

    Semántica (tres resultados DISJUNTOS, patch 0008):

    - ``None``: no existe evidencia durable que obligue a bloquear (o la que
      existía se cerró legítimamente como NO_ARTIFACT) — el caller puede
      declarar legacy. UN único significado;
    - ``DeploymentHandoff``: existe/se creó un handoff durable relevante;
    - ``OrphanEvidenceUnresolved``: existe evidencia vigente y durable (TX
      PENDING que nombra el mod, o receipt UNRESOLVED de stale sweep) pero
      ``alcance_del_artifact()`` es UNKNOWN — no se puede afirmar ni PRESENT
      ni ABSENT. El receipt NO se cierra. La política la pone el caller: el
      hot path lo mapea a un bloqueo fail-closed; el startup lo trata como
      "todavía no resoluble" y continúa.

    Lifecycle de los receipts (Principio 4): al materializar el INDETERMINATE,
    el receipt UNRESOLVED que aportó la evidencia se CONSUME en el MISMO
    boundary. ``NO_ARTIFACT`` es terminal y sólo se emite con ausencia
    DEMOSTRADA por la probe tri-state :func:`alcance_del_artifact` (F-01,
    patch 0007): contenedor accesible y válido + target realmente ausente.
    Un root inaccesible o un error ambiguo deja el receipt UNRESOLVED — jamás
    NO_ARTIFACT.
    """
    clave = clave_de_artifact(mod_texgen)
    activo = await journal.consultar_handoff_activo(clave)  # type: ignore[attr-defined]
    textures = mod_texgen / "textures"

    if activo is not None and activo.state is HandoffState.SUPERSEDING:
        observado = None
        try:
            observado = await asyncio.to_thread(digest_arbol, textures)
        except OSError:
            observado = None
        coincide = (
            observado is not None
            and activo.expected_digest is not None
            and (observado.digest, observado.files, observado.bytes)
            == (activo.expected_digest, activo.expected_files, activo.expected_bytes)
        )
        if coincide:
            await journal.transicionar_handoff(  # type: ignore[attr-defined]
                activo.handoff_id,
                desde=HandoffState.SUPERSEDING,
                hacia=HandoffState.AWAITING_DEPLOYMENT,
            )
        else:
            await _degradar_a_indeterminate(journal, activo, observado)
        return await journal.consultar_handoff_activo(clave)  # type: ignore[attr-defined]

    if activo is not None:
        if activo.state is HandoffState.AWAITING_DEPLOYMENT and not textures.is_dir():
            import logging

            logging.getLogger("SkyClaw.DeploymentHandoffs").warning(
                "Handoff AWAITING_DEPLOYMENT %d sin artifact en '%s': el resume fallará cerrado "
                "con ARTIFACT_MISSING hasta que exista evidencia nueva.",
                activo.handoff_id,
                mod_texgen,
            )
        return activo

    # La evidencia se consulta ANTES del gate del filesystem: sin ella no hay
    # nada que resolver, y con ella la decisión de ausencia pasa por la probe
    # tri-state (F-01, patch 0007) — jamás por un booleano ``is_dir()``.
    candidatas = await journal.transacciones_que_nombran(clave)  # type: ignore[attr-defined]
    if not candidatas:
        return None

    alcance = await asyncio.to_thread(alcance_del_artifact, mod_texgen)
    if alcance is ArtifactReachability.UNKNOWN:
        # Root inaccesible / error ambiguo: NO se afirma nada terminal — los
        # receipts siguen UNRESOLVED y el resultado NO se colapsa en None: el
        # caller debe distinguir "sin evidencia" (legacy) de "evidencia no
        # resoluble ahora" (bloqueo fail-closed en el hot path).
        return OrphanEvidenceUnresolved(candidatas=tuple(candidatas))
    if alcance is ArtifactReachability.ABSENT:
        # Ausencia DEMOSTRADA (contenedor accesible y válido + target
        # realmente ausente): no queda mutación física preservada que
        # proteger — el receipt puede cerrarse como NO_ARTIFACT.
        await journal.cerrar_receipts_como_no_artifact(candidatas)  # type: ignore[attr-defined]
        return None

    # PRESENT (contrato 7-4 C): el mod existe aunque ``textures`` esté a
    # medias o falte — es evidencia de mutación inconclusa, no ausencia.
    observado = None
    try:
        observado = await asyncio.to_thread(digest_arbol, textures)
    except OSError:
        observado = None

    import logging

    logging.getLogger("SkyClaw.DeploymentHandoffs").warning(
        "Interrupción presumida de una corrida TexGen (TX %s) con artifact vivo en '%s': "
        "se crea INDETERMINATE sin identidad autorizada. run_texgen=False fallará cerrado; "
        "una regeneración explícita lo supersede.",
        candidatas[0],
        mod_texgen,
    )
    handoff_id = await journal.registrar_handoff_indeterminado(  # type: ignore[attr-defined]
        registro=_registro_indeterminado(
            source_tx_id=candidatas[0],
            artifact_path=clave,
            game_key=game_key,
            mods_root_key=mods_root_key,
            data_key=data_key,
            expected_profile=expected_profile,
            observado=observado,
        ),
        # Principio 4: INDETERMINATE + receipts → CONSUMED en UN boundary —
        # si el INSERT pierde contra un owner concurrente, el consumo tampoco
        # queda afirmado a medias.
        consumir_receipts_de=candidatas,
    )
    if handoff_id is None:
        # Perdió contra un owner concurrente: devolver el estado real.
        return await journal.consultar_handoff_activo(clave)  # type: ignore[attr-defined]
    return await journal.consultar_handoff_activo(clave)  # type: ignore[attr-defined]


async def reconciliar_handoffs_de_deployment(
    *,
    journal: object,
    mod_texgen: pathlib.Path,
    game_key: str,
    mods_root_key: str,
    data_key: str,
    expected_profile: str,
    digest_arbol: object,
) -> None:
    """Reconcilia el estado durable del handoff con el filesystem al arrancar.

    Delega TODA la lógica en :func:`reconciliar_orphan_de_artifact` — la misma
    primitive que el hot path del resume consulta — para que startup y hot path
    no puedan divergir. Sólo CREA ``INDETERMINATE``, y sólo con evidencia
    VIGENTE y DURABLE: TX PENDING que nombra el mod o receipt UNRESOLVED de
    stale sweep (F-001, patch 0006); el manifiesto (pre-mutación) prueba
    INTENCIÓN, no provenance.

    F-01b (patch 0008): un resultado ``OrphanEvidenceUnresolved`` (evidencia +
    alcance UNKNOWN) se consume acá como "todavía no resoluble" — se loguea y
    el arranque CONTINÚA; el receipt queda UNRESOLVED para el próximo intento.
    Jamás se convierte en excepción fatal.

    Best-effort por contrato: el caller (startup) loguea y continúa ante
    cualquier excepción; un fallo de reconciliación jamás aborta el arranque.
    """
    resultado = await reconciliar_orphan_de_artifact(
        journal=journal,
        mod_texgen=mod_texgen,
        game_key=game_key,
        mods_root_key=mods_root_key,
        data_key=data_key,
        expected_profile=expected_profile,
        digest_arbol=digest_arbol,
    )
    if isinstance(resultado, OrphanEvidenceUnresolved):
        import logging

        logging.getLogger("SkyClaw.DeploymentHandoffs").warning(
            "Evidencia de orphan para '%s' (TX %s) no resoluble ahora: el artifact no es "
            "alcanzable (UNKNOWN) y los receipts siguen UNRESOLVED. Se reintentará en el "
            "próximo arranque/resume.",
            mod_texgen,
            resultado.candidatas,
        )


async def _degradar_a_indeterminate(journal: object, activo: DeploymentHandoff, observado: object) -> None:
    await journal.transicionar_handoff(  # type: ignore[attr-defined]
        activo.handoff_id,
        desde=HandoffState.SUPERSEDING,
        hacia=HandoffState.INDETERMINATE,
        expected_digest=None,
        expected_files=None,
        expected_bytes=None,
        observed_digest=getattr(observado, "digest", None),
        observed_files=getattr(observado, "files", None),
        observed_bytes=getattr(observado, "bytes", None),
    )


def _registro_indeterminado(
    *,
    source_tx_id: int,
    artifact_path: str,
    game_key: str,
    mods_root_key: str,
    data_key: str,
    expected_profile: str,
    observado: object,
) -> DeploymentHandoff:
    return DeploymentHandoff(
        handoff_id=0,
        source_tx_id=source_tx_id,
        state=HandoffState.INDETERMINATE,
        artifact_path=artifact_path,
        game_key=game_key,
        mods_root_key=mods_root_key,
        data_key=data_key,
        expected_profile=expected_profile,
        expected_digest=None,
        expected_files=None,
        expected_bytes=None,
        observed_digest=getattr(observado, "digest", None),
        observed_files=getattr(observado, "files", None),
        observed_bytes=getattr(observado, "bytes", None),
        created_at="",
        updated_at="",
        completed_at=None,
        superseded_at=None,
        superseded_by=None,
    )


__all__ = [
    "HANDOFFS_SCHEMA_SQL",
    "HANDOFF_STATES_ACTIVOS",
    "SWEEP_RECEIPTS_SCHEMA_SQL",
    "ArtifactReachability",
    "DeploymentHandoff",
    "HandoffState",
    "OrphanEvidenceUnresolved",
    "SweepReceiptState",
    "alcance_del_artifact",
    "clave_de_artifact",
    "fila_de_registro",
    "handoff_desde_fila",
    "reconciliar_handoffs_de_deployment",
    "reconciliar_orphan_de_artifact",
]
