"""Ciclo de vida del Candidate: crear, verificar y descubrir (P3).

Pipeline (SFR-15):

    Managed Source
      -> evidencia PRE independiente (estable + membership sellada)
      -> Candidate BUILDING (persistido ANTES de copiar)
      -> copia fisica independiente
      -> verificacion fresca del Candidate (contenido + integridad fisica)
      -> evidencia POST independiente
      -> PRE == Candidate == POST  => READY

**El Candidate nunca es la autoridad contra la que se valida.** La expectativa
viene del PRE y del POST de la Managed Source; el Candidate aporta evidencia
observada de si mismo, y solo se compara contra esa expectativa externa.

Por que el codigo NO ofrece una API tipo "copiar y marcar READY": la unica via
a READY es :func:`_evaluar_triada`, que exige las tres evidencias. No existe un
`success=True` que se alcance sin PRE y POST persistidos
(:meth:`CandidateMetadata.exigir_listo_para_persistencia` lo hace lanzar).
"""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import time
from collections.abc import Callable

from sky_claw.app.security.links import ContencionFisicaVioladaError, exigir_contencion_fisica, link_kind_or_raise
from sky_claw.local.frozen_runtime.candidate_id import (
    CANDIDATE_ID_PREFIX,
    default_candidate_id_factory,
    nuevo_candidate_id,
    validar_candidate_id,
)
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente, payload_dir
from sky_claw.local.frozen_runtime.errors import (
    CandidateCopyError,
    CandidateCorruptMetadataError,
    CandidateIdCollisionError,
    CandidateVerificationError,
    FrozenRuntimeError,
    FrozenRuntimeObservationError,
    FrozenRuntimeStorageError,
    InvalidCandidateIdError,
)
from sky_claw.local.frozen_runtime.independence import (
    descripcion_de_enlace,
    exigir_namespace_escribible,
    verify_generation_physical_integrity,
)
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipEvidence,
    SealedTreeObservation,
    SourceObservationError,
    observar_arbol_sellado,
)
from sky_claw.local.frozen_runtime.models import (
    CRITICAL_EXE_BY_GAME,
    ManagedSource,
    ManagedSourceProvider,
    ProviderMetadataObservation,
    SourceSnapshotEvidence,
)
from sky_claw.local.frozen_runtime.stabilization import (
    DEFAULT_QUIET_WINDOW_SECONDS,
    obtain_stable_source_snapshot,
)
from sky_claw.local.frozen_runtime.state import write_json_atomic
from sky_claw.local.frozen_runtime.storage import (
    admitir_directorio_storage,
    admitir_storage_root,
    candidates_dir,
    state_dir,
)
from sky_claw.local.frozen_runtime.storage_models import (
    CandidateInventory,
    CandidateMetadata,
    CandidateRecord,
    CandidateResult,
    CandidateSourceEvidence,
    CandidateState,
    GenerationVerificationState,
    IndependenceState,
)
from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest

CANDIDATE_SCHEMA_VERSION = 1
CANDIDATE_METADATA_SUBDIR = "candidates"

logger = logging.getLogger(__name__)


def candidates_state_dir(root: pathlib.Path) -> pathlib.Path:
    """Directorio de metadata de Candidates, FUERA de ``payload/``.

    La metadata no puede vivir dentro del payload: contaminaria el ``TreeDigest``
    y la ``DirectoryMembership`` que P3 acaba de sellar, y entonces el digest
    comparado ya no seria el del arbol copiado.
    """
    return state_dir(root) / CANDIDATE_METADATA_SUBDIR


def candidate_dir(root: pathlib.Path, candidate_id: str) -> pathlib.Path:
    """Directorio del Candidate (contiene ``payload/``), con id validado."""
    return candidates_dir(root) / validar_candidate_id(candidate_id)


def candidate_metadata_path(root: pathlib.Path, candidate_id: str) -> pathlib.Path:
    """Ruta de la metadata persistente de un Candidate."""
    return candidates_state_dir(root) / f"{validar_candidate_id(candidate_id)}.json"


def _exigir_candidates_state_dir(root: pathlib.Path) -> pathlib.Path:
    """Admite fail-closed el directorio de metadata antes de escribir en el."""
    directorio = candidates_state_dir(root)
    # El namespace del ANCESTRO se verifica ANTES del mkdir: si `state/` fue
    # reemplazado por un junction y `state/candidates/` todavia no existe,
    # `admitir_directorio_storage` veria solo la hoja ausente como admisible y
    # `mkdir(parents=True)` crearia el directorio en el destino EXTERNO. El
    # `exigir_namespace_escribible` posterior lo rechazaria, pero ya habria
    # mutado fuera del root (Codex sobre #682).
    try:
        exigir_contencion_fisica(pathlib.Path(root), state_dir(root), exigir_existencia=True)
    except ContencionFisicaVioladaError as exc:
        raise FrozenRuntimeStorageError(f"el namespace de metadata no cuelga fisicamente de '{root}': {exc}") from exc
    exigir_namespace_escribible(state_dir(root))
    admision = admitir_directorio_storage(directorio)
    if not admision.success:
        raise FrozenRuntimeStorageError(f"el directorio de metadata de Candidates fue rechazado: {admision.message}")
    if not directorio.exists():
        directorio.mkdir(parents=True, exist_ok=True)
    exigir_namespace_escribible(directorio)
    return directorio


def _archivos_criticos(game_key: str, files: tuple[FileIdentity, ...]) -> tuple[FileIdentity, ...]:
    """Filtra la evidencia critica desde el inventario ya sellado."""
    esperado = CRITICAL_EXE_BY_GAME.get(game_key)
    if esperado is None:
        raise FrozenRuntimeObservationError(f"sin catalogo de archivos criticos para '{game_key}'")
    encontrados = tuple(f for f in files if f.rel_path.casefold() == esperado.casefold())
    if not encontrados:
        raise FrozenRuntimeObservationError(f"falta el archivo critico '{esperado}' en el inventario observado")
    return encontrados


def _observar_fuente_estable(
    source: ManagedSource,
    *,
    quiet_window_seconds: float,
    sleep: Callable[[float], None],
) -> CandidateSourceEvidence:
    """Observacion INDEPENDIENTE y estable de la Managed Source (PRE o POST).

    Reutiliza la estabilizacion P1 (``obtain_stable_source_snapshot``) como gate
    -- no "observe once and assume stable" -- y luegoCaptura la membresia
    con :func:`observar_arbol_sellado`, que sella archivos + directorios +
    identidad como una sola observacion.

    El cruce entre ambas es la garantia concreta: si el arbol se sellado no
    coincide con el arbol que la ventana de estabilizacio declaro quieto, la
    fuente se movio entre las dos capturas y la evidencia se descarta en vez de
    emitir una mezcla de dos instantes. NUNCA se reusa el PRE cacheado como POST:
    cada posicion hace su propia llamada completa.
    """
    estable = obtain_stable_source_snapshot(source, quiet_window_seconds=quiet_window_seconds, sleep=sleep)
    if not estable.success or estable.snapshot is None:
        raise CandidateVerificationError(f"la Managed Source no pudo verificarse estable: {estable.stability.message}")
    snapshot: SourceSnapshotEvidence = estable.snapshot
    try:
        sellado = observar_arbol_sellado(source.root, game_key=source.game_key)
    except SourceObservationError as exc:
        raise CandidateVerificationError(f"no se pudo sellar la Managed Source: {exc}") from exc

    if sellado.tree_digest != snapshot.tree_digest:
        raise CandidateVerificationError(
            "la Managed Source cambio entre la ventana de estabilizacion y la captura sellada "
            f"({snapshot.tree_digest.digest} -> {sellado.tree_digest.digest}): la evidencia no es coherente"
        )
    if sellado.runtime_identity != snapshot.runtime_identity:
        raise CandidateVerificationError(
            "la identidad de runtime cambio entre la ventana de estabilizacion y la captura sellada"
        )

    return CandidateSourceEvidence(
        provider=source.provider.value,
        appid=source.appid,
        game_key=source.game_key,
        runtime_identity=snapshot.runtime_identity,
        tree_digest=snapshot.tree_digest,
        directory_membership=sellado.directory_membership,
        critical_files=snapshot.critical_files,
        provider_metadata=snapshot.provider_metadata,
        observed_at_ns=snapshot.observed_at_ns,
        files=snapshot.files,
    )


def _membership_a_dict(evidencia: object) -> dict[str, object]:
    """Serializa la membership (digest + conteo + lista canonica)."""
    membresia = evidencia
    if not isinstance(membresia, DirectoryMembershipEvidence):
        raise CandidateVerificationError("membership con tipo inesperado")
    return {
        "digest": membresia.digest,
        "directory_count": membresia.directory_count,
        "directories": list(membresia.directories),
    }


def _evidencia_a_dict(evidencia: CandidateSourceEvidence) -> dict[str, object]:
    """Serializa una evidencia de fuente completa (post-restart auditable)."""
    return {
        "provider": evidencia.provider,
        "appid": evidencia.appid,
        "game_key": evidencia.game_key,
        "runtime_identity": {
            "game_key": evidencia.runtime_identity.game_key,
            "game_version": evidencia.runtime_identity.game_version,
        },
        "tree_digest": {
            "digest": evidencia.tree_digest.digest,
            "files": evidencia.tree_digest.files,
            "bytes": evidencia.tree_digest.bytes,
        },
        "directory_membership": _membership_a_dict(evidencia.directory_membership),
        "critical_files": [
            {"rel_path": f.rel_path, "size": f.size, "digest": f.digest} for f in evidencia.critical_files
        ],
        "files": [{"rel_path": f.rel_path, "size": f.size, "digest": f.digest} for f in evidencia.files],
        "provider_metadata": {
            "provider": evidencia.provider_metadata.provider.value,
            "appid": evidencia.provider_metadata.appid,
            "buildid": evidencia.provider_metadata.buildid,
            "state_flags": evidencia.provider_metadata.state_flags,
            "bytes_to_download": evidencia.provider_metadata.bytes_to_download,
            "bytes_downloaded": evidencia.provider_metadata.bytes_downloaded,
            "update_result": evidencia.provider_metadata.update_result,
            "install_dir": evidencia.provider_metadata.install_dir,
            "manifest_readable": evidencia.provider_metadata.manifest_readable,
            "manifest_parse_error": evidencia.provider_metadata.manifest_parse_error,
        },
        "observed_at_ns": evidencia.observed_at_ns,
    }


def serializar_metadata_candidate(metadata: CandidateMetadata) -> dict[str, object]:
    """Serializa la metadata completa del Candidate a JSON (schema v1)."""
    metadata.exigir_listo_para_persistencia()
    return {
        "schema_version": metadata.schema_version,
        "candidate_id": metadata.candidate_id,
        "state": metadata.state.value,
        "created_at_ns": metadata.created_at_ns,
        "updated_at_ns": metadata.updated_at_ns,
        "source_provider": metadata.source_provider,
        "source_appid": metadata.source_appid,
        "pre_source_evidence": (
            _evidencia_a_dict(metadata.pre_source_evidence) if metadata.pre_source_evidence else None
        ),
        "candidate_evidence": (_evidencia_a_dict(metadata.candidate_evidence) if metadata.candidate_evidence else None),
        "post_source_evidence": (
            _evidencia_a_dict(metadata.post_source_evidence) if metadata.post_source_evidence else None
        ),
        "failure_reason": metadata.failure_reason,
    }


def _membership_desde_dict(bruto: object, *, etiqueta: str) -> DirectoryMembershipEvidence:
    if not isinstance(bruto, dict):
        raise CandidateCorruptMetadataError(f"{etiqueta}: directory_membership debe ser un objeto")
    faltan = [clave for clave in ("digest", "directory_count", "directories") if clave not in bruto]
    if faltan:
        raise CandidateCorruptMetadataError(f"{etiqueta}: faltan claves de membership: {faltan}")
    directorios = bruto["directories"]
    if not isinstance(directorios, list) or not all(isinstance(d, str) for d in directorios):
        raise CandidateCorruptMetadataError(f"{etiqueta}: directories debe ser una lista de strings")
    conteo = bruto["directory_count"]
    if not isinstance(conteo, int) or isinstance(conteo, bool):
        raise CandidateCorruptMetadataError(f"{etiqueta}: directory_count debe ser int")
    digest = bruto["digest"]
    if not isinstance(digest, str):
        raise CandidateCorruptMetadataError(f"{etiqueta}: digest de membership debe ser string")
    try:
        return DirectoryMembershipEvidence(
            digest=digest,
            directory_count=conteo,
            directories=tuple(directorios),
        )
    except SourceObservationError as exc:
        raise CandidateCorruptMetadataError(f"{etiqueta}: membership inconsistente: {exc}") from exc


def _identidades(bruto: object, *, campo: str, etiqueta: str) -> tuple[FileIdentity, ...]:
    """Deserializa una lista de identidades de archivo, fail-closed.

    Una entrada malformada LANZA en vez de filtrarse en silencio: descartarla
    dejaria una evidencia "completa" que en realidad enumera menos archivos de
    los que dice, y esa evidencia es la que se compara contra el Candidate.
    """
    if bruto is None:
        return ()
    if not isinstance(bruto, list):
        raise CandidateCorruptMetadataError(f"{etiqueta}: {campo} debe ser una lista")
    salida: list[FileIdentity] = []
    for indice, entrada in enumerate(bruto):
        if not isinstance(entrada, dict) or not {"rel_path", "size", "digest"} <= set(entrada):
            raise CandidateCorruptMetadataError(f"{etiqueta}: {campo}[{indice}] no tiene rel_path/size/digest")
        try:
            salida.append(
                FileIdentity(
                    rel_path=str(entrada["rel_path"]),
                    size=int(entrada["size"]),
                    digest=str(entrada["digest"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise CandidateCorruptMetadataError(f"{etiqueta}: {campo}[{indice}] tiene tipos invalidos: {exc}") from exc
    return tuple(salida)


def _evidencia_desde_dict(bruto: object, *, etiqueta: str) -> CandidateSourceEvidence:
    """Deserializa evidencia de fuente; fail-closed ante cualquier ausencia."""
    if not isinstance(bruto, dict):
        raise CandidateCorruptMetadataError(f"{etiqueta}: la evidencia debe ser un objeto JSON")
    faltan = [
        clave
        for clave in (
            "provider",
            "appid",
            "game_key",
            "runtime_identity",
            "tree_digest",
            "directory_membership",
            "critical_files",
            "provider_metadata",
            "observed_at_ns",
            # La enumeracion SELLADA completa es parte de la evidencia P3 (es
            # lo que la copia transfiere y lo que hace auditable al PRE). Si se
            # acepta su ausencia, `files` queda en () y la evidencia persistida
            # prometeria una cobertura que ya no tiene.
            "files",
        )
        if clave not in bruto
    ]
    if faltan:
        raise CandidateCorruptMetadataError(f"{etiqueta}: faltan campos obligatorios: {faltan}")

    identidad = bruto["runtime_identity"]
    if not isinstance(identidad, dict) or "game_key" not in identidad or "game_version" not in identidad:
        raise CandidateCorruptMetadataError(f"{etiqueta}: runtime_identity incompleta")
    digest = bruto["tree_digest"]
    if not isinstance(digest, dict) or not {"digest", "files", "bytes"} <= set(digest):
        raise CandidateCorruptMetadataError(f"{etiqueta}: tree_digest incompleta")
    criticos = bruto["critical_files"]
    if not isinstance(criticos, list):
        raise CandidateCorruptMetadataError(f"{etiqueta}: critical_files debe ser una lista")
    metadatos = bruto["provider_metadata"]
    if not isinstance(metadatos, dict):
        raise CandidateCorruptMetadataError(f"{etiqueta}: provider_metadata debe ser un objeto")
    observado = bruto["observed_at_ns"]
    if not isinstance(observado, int) or isinstance(observado, bool):
        raise CandidateCorruptMetadataError(f"{etiqueta}: observed_at_ns debe ser int")
    proveedor = bruto["provider"]
    if not isinstance(proveedor, str):
        raise CandidateCorruptMetadataError(f"{etiqueta}: provider debe ser string")

    try:
        return CandidateSourceEvidence(
            provider=proveedor,
            appid=str(bruto["appid"]),
            game_key=str(bruto["game_key"]),
            runtime_identity=RuntimeIdentity(
                game_key=str(identidad["game_key"]), game_version=str(identidad["game_version"])
            ),
            tree_digest=TreeDigest(
                digest=str(digest["digest"]),
                files=int(digest["files"]),
                bytes=int(digest["bytes"]),
            ),
            directory_membership=_membership_desde_dict(bruto["directory_membership"], etiqueta=etiqueta),
            critical_files=_identidades(criticos, campo="critical_files", etiqueta=etiqueta),
            files=_identidades(bruto["files"], campo="files", etiqueta=etiqueta),
            provider_metadata=ProviderMetadataObservation(
                provider=ManagedSourceProvider(proveedor),
                appid=str(bruto["appid"]),
                buildid=metadatos.get("buildid") if isinstance(metadatos.get("buildid"), str) else None,
                state_flags=(metadatos.get("state_flags") if isinstance(metadatos.get("state_flags"), str) else None),
                bytes_to_download=(
                    metadatos.get("bytes_to_download") if isinstance(metadatos.get("bytes_to_download"), int) else None
                ),
                bytes_downloaded=(
                    metadatos.get("bytes_downloaded") if isinstance(metadatos.get("bytes_downloaded"), int) else None
                ),
                update_result=(
                    metadatos.get("update_result") if isinstance(metadatos.get("update_result"), str) else None
                ),
                install_dir=(metadatos.get("install_dir") if isinstance(metadatos.get("install_dir"), str) else None),
                manifest_readable=bool(metadatos.get("manifest_readable", True)),
                observed_at_ns=observado,
            ),
            observed_at_ns=observado,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateCorruptMetadataError(f"{etiqueta}: evidencia de fuente malformada: {exc}") from exc


def _exigir_metadata_no_redirigida(ruta: pathlib.Path) -> None:
    """Fail-closed: ni el archivo de metadata ni su namespace pueden ser enlaces.

    Se comprueba con ``lstat`` ANTES de cualquier ``exists()``/``read_text()``.
    El orden importa: ``exists()`` sigue enlaces, asi que un symlink colgante
    (target inexistente) se leeria como "metadata ausente" y un junction en el
    namespace haria que la lectura ocurriera FUERA del FrozenRuntimeRoot.

    Misma filosofia que el loader de ``active.json`` de P2, aplicada aqui.
    """
    try:
        kind = link_kind_or_raise(ruta)
    except OSError as exc:
        raise CandidateCorruptMetadataError(f"{ruta}: no se pudo inspeccionar la metadata: {exc}") from exc
    if kind is not None:
        raise CandidateCorruptMetadataError(
            f"{ruta}: la metadata es un enlace ({kind}): no se sigue ni se ignora (fail-closed)"
        )
    motivo_padre = descripcion_de_enlace(ruta.parent)
    if motivo_padre is not None:
        raise CandidateCorruptMetadataError(f"{ruta}: namespace de la metadata redirigido: {motivo_padre}")


def leer_metadata_candidate(path: pathlib.Path) -> CandidateMetadata:
    """Carga fail-closed la metadata de un Candidate.

    Ausente/corrupta/schema desconocido LANZA (:class:`CandidateCorruptMetadataError`):
    un Candidate sin metadata legible es UNKNOWN, jamas READY. TODA la corrupcion
    persistida se expresa con esa unica clase -- incluidos los ids invalidos y el
    READY incompleto -- para que los callers tengan una sola excepcion que
    convertir en UNKNOWN.
    """
    ruta = pathlib.Path(path)
    # El chequeo de redireccion va PRIMERO: `exists()` sigue enlaces y trataria
    # un symlink colgante como "no hay metadata".
    _exigir_metadata_no_redirigida(ruta)
    if not ruta.exists():
        raise CandidateCorruptMetadataError(f"{ruta}: no hay metadata de Candidate (UNKNOWN, nunca READY)")
    try:
        crudo = ruta.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateCorruptMetadataError(f"{ruta}: no se pudo leer la metadata: {exc}") from exc
    if not crudo.strip():
        raise CandidateCorruptMetadataError(f"{ruta}: metadata de Candidate vacia (0 bytes)")
    try:
        data = json.loads(crudo)
    except json.JSONDecodeError as exc:
        raise CandidateCorruptMetadataError(f"{ruta}: metadata malformada o truncada: {exc}") from exc
    if not isinstance(data, dict):
        raise CandidateCorruptMetadataError(f"{ruta}: la metadata debe ser un objeto JSON")

    esquema = data.get("schema_version")
    if not isinstance(esquema, int) or isinstance(esquema, bool):
        raise CandidateCorruptMetadataError(f"{ruta}: schema_version debe ser int")
    if esquema != CANDIDATE_SCHEMA_VERSION:
        raise CandidateCorruptMetadataError(
            f"{ruta}: schema desconocido {esquema}; soportado: {CANDIDATE_SCHEMA_VERSION}"
        )
    estado_bruto = data.get("state")
    try:
        estado = CandidateState(estado_bruto) if isinstance(estado_bruto, str) else None
    except ValueError as exc:
        raise CandidateCorruptMetadataError(f"{ruta}: estado de Candidate desconocido: {estado_bruto}") from exc
    if estado is None:
        raise CandidateCorruptMetadataError(f"{ruta}: falta el campo obligatorio 'state'")

    def _evidencia(clave: str) -> CandidateSourceEvidence | None:
        valor = data.get(clave)
        if valor is None:
            return None
        return _evidencia_desde_dict(valor, etiqueta=f"{ruta}:{clave}")

    def _candidate_id_desde_datos() -> str:
        """El candidate_id embebido es parte de la identidad: su fallo es corrupcion.

        `InvalidCandidateIdError` no es corrupcion de metadata por si sola, asi que
        se convierte explicitamente: de lo contrario escapaba crudo y abortaba el
        descubrimiento entero en vez de registrar un Candidate UNKNOWN.
        """
        bruto_id = data.get("candidate_id")
        if not isinstance(bruto_id, str):
            raise CandidateCorruptMetadataError(f"{ruta}: falta el campo obligatorio 'candidate_id' o no es string")
        try:
            return validar_candidate_id(bruto_id)
        except InvalidCandidateIdError as exc:
            raise CandidateCorruptMetadataError(f"{ruta}: candidate_id invalido en metadata: {exc}") from exc

    try:
        metadata = CandidateMetadata(
            schema_version=esquema,
            candidate_id=_candidate_id_desde_datos(),
            state=estado,
            created_at_ns=int(data.get("created_at_ns", 0)),
            updated_at_ns=int(data.get("updated_at_ns", 0)),
            source_provider=str(data.get("source_provider", "")),
            source_appid=str(data.get("source_appid", "")),
            pre_source_evidence=_evidencia("pre_source_evidence"),
            candidate_evidence=_evidencia("candidate_evidence"),
            post_source_evidence=_evidencia("post_source_evidence"),
            failure_reason=(str(data["failure_reason"]) if isinstance(data.get("failure_reason"), str) else None),
        )
    except CandidateCorruptMetadataError:
        raise
    except (TypeError, ValueError) as exc:
        raise CandidateCorruptMetadataError(f"{ruta}: metadata con tipos invalidos: {exc}") from exc

    # Un READY persistido sin las TRES evidencias es metadata CORRUPTA por
    # construccion: se rechaza al LEER, no solo al escribir. El guard de
    # verificacion se convierte a corrupcion tipada -- el caller tiene una sola
    # excepcion que traducir a UNKNOWN, en vez de dos de familias distintas.
    try:
        metadata.exigir_listo_para_persistencia()
    except CandidateVerificationError as exc:
        raise CandidateCorruptMetadataError(f"{ruta}: metadata READY incompleta: {exc}") from exc
    # La identidad del Candidate tiene que estar atada a su nombre: si la metadata
    # de B se copia sobre el JSON de A (misma fuente, caso normal), sin esto
    # `verificar_candidate(A)` devolveria VALID llevando la identidad de B y la
    # promocion futura actuaria sobre un Candidate ambiguo (Codex sobre #682).
    if ruta.stem.startswith(CANDIDATE_ID_PREFIX):
        nombre_id = validar_candidate_id(ruta.stem)
        if metadata.candidate_id != nombre_id:
            raise CandidateCorruptMetadataError(
                f"{ruta}: el candidate_id embebido ({metadata.candidate_id}) no coincide "
                f"con el nombre del archivo ({nombre_id}): identidad ambigua (fail-closed)"
            )
    return metadata


def _buildid_contradictoire(*valores: str | None) -> bool:
    """buildid AUXILIAR: solo genera veredicto si AMBOS fueron observados y difieren.

    ``None`` significa "el manifest no lo expone": no es evidencia de cambio, asi
    que no puede producir un veredicto (semantica P2.1c).
    """
    return all(v is not None for v in valores) and len(set(valores)) > 1


def _diferencias_triada(
    pre: CandidateSourceEvidence,
    candidato: CandidateSourceEvidence,
    post: CandidateSourceEvidence,
) -> list[str]:
    """Enumera TODAS las diferencias entre PRE, Candidate y POST."""
    diferencias: list[str] = []
    if pre.tree_digest != candidato.tree_digest:
        diferencias.append(
            f"TreeDigest PRE({pre.tree_digest.digest[:12]}) != Candidate({candidato.tree_digest.digest[:12]})"
        )
    if candidato.tree_digest != post.tree_digest:
        diferencias.append(
            f"TreeDigest Candidate({candidato.tree_digest.digest[:12]}) != POST({post.tree_digest.digest[:12]})"
        )
    if pre.tree_digest != post.tree_digest:
        diferencias.append(f"TreeDigest PRE({pre.tree_digest.digest[:12]}) != POST({post.tree_digest.digest[:12]})")
    if pre.runtime_identity != candidato.runtime_identity:
        diferencias.append("RuntimeIdentity PRE != Candidate")
    if candidato.runtime_identity != post.runtime_identity:
        diferencias.append("RuntimeIdentity Candidate != POST")
    # Procedencia: la evidencia de POST debe venir de la MISMA Managed Source
    # concreta. Sin esto, metadata cuyo POST fue re-bound a otro appid/juego
    # podria re-verificar VALID si la identidad del payload coincide (Codex #682).
    for etiqueta_par, a, b in (
        ("PRE/Candidate", pre, candidato),
        ("Candidate/POST", candidato, post),
        ("PRE/POST", pre, post),
    ):
        if (a.provider, a.appid, a.game_key) != (b.provider, b.appid, b.game_key):
            diferencias.append(f"procedencia {etiqueta_par} distinta (provider/appid/game_key)")
    if pre.critical_files != candidato.critical_files:
        diferencias.append("critical evidence PRE != Candidate")
    if candidato.critical_files != post.critical_files:
        diferencias.append("critical evidence Candidate != POST")
    if pre.directory_membership != candidato.directory_membership:
        diferencias.append(
            f"DirectoryMembership PRE({pre.directory_membership.digest[:12]}) != "
            f"Candidate({candidato.directory_membership.digest[:12]})"
        )
    if candidato.directory_membership != post.directory_membership:
        diferencias.append(
            f"DirectoryMembership Candidate({candidato.directory_membership.digest[:12]}) != "
            f"POST({post.directory_membership.digest[:12]})"
        )
    if _buildid_contradictoire(pre.buildid, post.buildid):
        diferencias.append(f"buildid PRE({pre.buildid}) != POST({post.buildid})")
    return diferencias


def _evaluar_triada(
    pre: CandidateSourceEvidence,
    candidato: CandidateSourceEvidence,
    post: CandidateSourceEvidence,
) -> tuple[GenerationVerificationState, str]:
    """Unico camino a READY: las TRES evidencias deben coincidir.

    La comparacion es por identidad COMPLETA (digest + files + bytes; digest +
    conteo + lista) y en las TRES pares (PRE/Candidate, Candidate/POST,
    PRE/POST). Comparar solo PRE contra POST, o solo el Candidate contra si
    mismo, dejaria abierta la auto-autorizacion que SFR-15 prohibe.
    """
    diferencias = _diferencias_triada(pre, candidato, post)
    if diferencias:
        return (
            GenerationVerificationState.INVALID,
            "el Candidate no puede ser READY: " + "; ".join(diferencias),
        )
    return (
        GenerationVerificationState.VALID,
        "PRE == Candidate == POST en TreeDigest, RuntimeIdentity, evidencia critica y DirectoryMembership",
    )


def _verificar_integridad_fisica(payload: pathlib.Path) -> tuple[GenerationVerificationState, str]:
    """Verificacion fisica fresca del Candidate (SFR-18), sin Managed Source.

    Un Candidate con los bytes correctos puede seguir siendo fisicamente
    compartido (hardlink) o redirigido (reparse): el digest no lo ve.
    """
    resultado = verify_generation_physical_integrity(payload)
    if resultado.state is IndependenceState.INDEPENDENT:
        return GenerationVerificationState.VALID, resultado.message
    if resultado.state is IndependenceState.VIOLATED:
        return GenerationVerificationState.INVALID, resultado.message
    return GenerationVerificationState.INDETERMINATE, resultado.message


def _campos_divergentes(esperada: CandidateSourceEvidence, observada: CandidateSourceEvidence) -> list[str]:
    """Nombra los campos que difieren entre dos evidencias.

    Sin esto el diagnostico seria "algo no coincide" y obligaria a re-derivar a mano
    cual de las dimensiones se rompio. Es el mismo criterio por campo que usa
    `_diferencias_triada`, para que ambos mensajes sean comparables.
    """
    campos = (
        ("Provider", "provider"),
        ("Appid", "appid"),
        ("GameKey", "game_key"),
        ("RuntimeIdentity", "runtime_identity"),
        ("TreeDigest", "tree_digest"),
        ("DirectoryMembership", "directory_membership"),
        ("CriticalFiles", "critical_files"),
        ("Files", "archivos"),
    )
    return [etiqueta for etiqueta, atributo in campos if getattr(esperada, atributo) != getattr(observada, atributo)]


def _identidad_evidencia(evidencia: CandidateSourceEvidence) -> tuple[object, ...]:
    """Identidad comparable de una evidencia (sin `observed_at_ns`).

    `observed_at_ns` es el momento de la observacion y SIEMPRE difiere entre la
    corrida original y una re-observacion posterior; incluirlo haria que toda
    re-verificacion fuese distinta de si misma.
    """
    return (
        evidencia.provider,
        evidencia.appid,
        evidencia.game_key,
        evidencia.runtime_identity,
        evidencia.tree_digest,
        evidencia.directory_membership,
        evidencia.critical_files,
        evidencia.archivos,
    )


def _evaluar_coherencia_persistida(metadata: CandidateMetadata) -> tuple[GenerationVerificationState, str]:
    """La evidencia HISTORICA persistida debe ser coherente consigo misma.

    `verificar_candidate` compara PRE / Candidate-fresco / POST. Eso no alcanza:
    la metadata tambien afirma "esto es lo que se copio en su momento"
    (`candidate_evidence`), y si ese registro historico quedo corrupto la
    re-verificacion del contenido no lo detectaria.

    Ademas exige que el binding de nivel superior de la metadata sea coherente
    con PRE/POST, para que el header no pueda describir una Managed Source
    distinta a la de su propia evidencia.
    """
    pre, persistida, post = metadata.pre_source_evidence, metadata.candidate_evidence, metadata.post_source_evidence
    if pre is None or post is None or persistida is None:
        return (GenerationVerificationState.INVALID, "la metadata persistida no tiene las TRES evidencias")
    if (metadata.source_provider, metadata.source_appid) != (pre.provider, pre.appid):
        return (
            GenerationVerificationState.INVALID,
            f"el binding de fuente de la metadata ({metadata.source_provider}/{metadata.source_appid}) "
            f"no coincide con la evidencia PRE ({pre.provider}/{pre.appid})",
        )
    if _identidad_evidencia(pre) != _identidad_evidencia(persistida):
        return (
            GenerationVerificationState.INVALID,
            "la evidencia de Candidate persistida no coincide con la evidencia PRE persistida "
            f"(difieren: {', '.join(_campos_divergentes(pre, persistida))})",
        )
    if _identidad_evidencia(persistida) != _identidad_evidencia(post):
        return (
            GenerationVerificationState.INVALID,
            "la evidencia de Candidate persistida no coincide con la evidencia POST persistida "
            f"(difieren en la procedencia: {', '.join(_campos_divergentes(persistida, post))})",
        )
    return (
        GenerationVerificationState.VALID,
        "la evidencia persistida PRE/Candidate/POST es coherente consigo misma",
    )


def _observar_candidate(
    payload: pathlib.Path,
    *,
    game_key: str,
    appid: str,
    provider: str,
) -> CandidateSourceEvidence:
    """Evidencia OBSERVADA del Candidate.

    IMPORTANTE (SFR-15): esto es evidencia de si mismo, jamas autoridad. Se
    compara contra PRE y POST de la Managed Source; no puede selo sola.
    """
    # TODA la construccion de evidencia del Candidate vive dentro de la frontera
    # tipada. `_archivos_criticos` puede lanzar `FrozenRuntimeObservationError`
    # (juego valido pero sin catalogo critico correspondiente): si queda fuera, esa
    # excepcion cruda escapa de `verificar_candidate` en vez de volverse veredicto.
    try:
        sellado: SealedTreeObservation = observar_arbol_sellado(payload, game_key=game_key)
        criticos = _archivos_criticos(game_key, sellado.files)
    except (SourceObservationError, FrozenRuntimeObservationError) as exc:
        raise CandidateVerificationError(f"no se pudo observar el Candidate: {exc}") from exc
    return CandidateSourceEvidence(
        provider=provider,
        appid=appid,
        game_key=game_key,
        runtime_identity=sellado.runtime_identity,
        tree_digest=sellado.tree_digest,
        directory_membership=sellado.directory_membership,
        critical_files=criticos,
        provider_metadata=ProviderMetadataObservation(
            provider=ManagedSourceProvider(provider),
            appid=appid,
        ),
        observed_at_ns=sellado.observed_at_ns,
        files=sellado.files,
    )


def _reservar_candidate_id(root: pathlib.Path, candidate_id: str) -> pathlib.Path:
    """Reserva el ``candidate_id`` con semantica de NO-CLOBBER.

    La primitiva de single-winner es ``mkdir(exist_ok=False)``: crea el directorio
    del Candidate o falla con ``FileExistsError``, sin reemplazar jamas lo que ya
    estaba. No se usa `exists()` seguido de escritura porque eso abre una ventana
    TOCTOU entre el control y la mutacion -- exactamente la ventana que permite
    que un ``id_factory`` repetido destruya un Candidate READY antes de que la
    colision del payload llegue a descubrirse.

    El orden importa: la reserva ocurre ANTES de persistir la metadata BUILDING,
    asi que un id repetido falla sin haber escrito un solo byte del Candidate
    anterior.

    Que NO cubre esto: exclusion entre procesos distintos (el lock cross-process
    global de la corrida es P4, explicitamente fuera de este slice). Lo que si
    garantiza es la propiedad puntual e independiente de la primitiva -- una
    colision concreta no destruye un Candidate existente.
    """
    raiz = pathlib.Path(root)
    _exigir_candidates_state_dir(raiz)
    directorio = candidate_dir(raiz, candidate_id)
    try:
        directorio.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise CandidateIdCollisionError(
            f"el candidate_id '{candidate_id}' ya esta reservado por un Candidate existente: "
            "no se reemplaza (fail-closed)"
        ) from exc
    except OSError as exc:
        raise FrozenRuntimeStorageError(f"no se pudo reservar el candidate_id '{candidate_id}': {exc}") from exc
    # La reserva se hace dentro del root verificado: despues de crearla, el
    # directorio recien nacido tiene que colgar fisicamente del root (misma
    # disciplina que la copia).
    try:
        exigir_contencion_fisica(raiz, directorio, exigir_existencia=True)
    except ContencionFisicaVioladaError as exc:
        raise FrozenRuntimeStorageError(
            f"el directorio reservado '{directorio}' no cuelga fisicamente del root: {exc}"
        ) from exc
    return directorio


def _persistir_metadata(root: pathlib.Path, metadata: CandidateMetadata) -> None:
    """Persiste la metadata del Candidate FUERA del payload, de forma atomica.

    Reusa ``write_json_atomic`` (una sola implementacion de escritura atomica en
    el paquete) y re-admite el namespace justo antes de mutar.

    `write_json_atomic` ya tipa el fallo de su propio `mkstemp`, pero los fallos
    de `json.dump`, `flush`, `fsync` y `os.replace` salen como `OSError` crudo.
    Ese error se convierte a la familia de storage para que ningun mutador de
    metadata pueda filtrar un error de filesystem crudo: el caller decide el
    veredicto (INDETERMINATE/INVALID), nunca el tipo de excepcion. Solo se
    captura `OSError` a proposito -- `KeyboardInterrupt`, `SystemExit` y
    `MemoryError` deben seguir propagarse.
    """
    _exigir_candidates_state_dir(root)
    exigir_namespace_escribible(candidates_state_dir(root))
    try:
        write_json_atomic(
            candidate_metadata_path(root, metadata.candidate_id),
            serializar_metadata_candidate(metadata),
        )
    except OSError as exc:
        raise FrozenRuntimeStorageError(
            f"no se pudo persistir la metadata del Candidate '{metadata.candidate_id}': {exc}"
        ) from exc


def _marcar_invalid(
    root: pathlib.Path,
    metadata: CandidateMetadata,
    motivo: str,
    *,
    pre: CandidateSourceEvidence | None = None,
    candidato: CandidateSourceEvidence | None = None,
    post: CandidateSourceEvidence | None = None,
) -> CandidateResult:
    """Persiste INVALID con su motivo. NO borra el contenido fallido."""
    invalido = CandidateMetadata(
        schema_version=metadata.schema_version,
        candidate_id=metadata.candidate_id,
        state=CandidateState.INVALID,
        created_at_ns=metadata.created_at_ns,
        updated_at_ns=time.time_ns(),
        source_provider=metadata.source_provider,
        source_appid=metadata.source_appid,
        pre_source_evidence=pre,
        candidate_evidence=candidato,
        post_source_evidence=post,
        failure_reason=motivo,
    )
    try:
        _persistir_metadata(root, invalido)
    except FrozenRuntimeError:
        # La metadata de INVALID es diagnostico: si ni eso se puede escribir,
        # el resultado igual es INVALID (fail-closed), nunca READY. No se borra
        # el contenido fallido: P3 no introduce cleanup destructivo.
        with contextlib.suppress(Exception):
            logger.warning(
                "frozen_runtime no se pudo persistir la metadata INVALID del Candidate",
                extra={
                    "event": "frozen_runtime_candidate_invalid_metadata",
                    "candidate_id": metadata.candidate_id,
                },
            )
    return CandidateResult(
        state=GenerationVerificationState.INVALID,
        message=motivo,
        candidate_id=metadata.candidate_id,
        metadata=invalido,
        pre_source_evidence=pre,
        candidate_evidence=candidato,
        post_source_evidence=post,
    )


def crear_candidate(
    source: ManagedSource,
    root: pathlib.Path,
    *,
    quiet_window_seconds: float = DEFAULT_QUIET_WINDOW_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    id_factory: Callable[[], str] = default_candidate_id_factory,
) -> CandidateResult:
    """Crea un Candidate y lo deja READY solo si PRE == Candidate == POST.

    Secuencia (y por que este orden):

    1. PRE: evidencia INDEPENDIENTE y estable de la Managed Source.
    2. BUILDING persistido ANTES de copiar. Si el proceso muere con la copia a
       medias, el Candidate queda BUILDING en disco y nunca se confunde con
       READY (semantica de crash).
    3. Copia fisica independiente de la enumeracion SELLADA de PRE.
    4. Verificacion fresca del Candidate (contenido + integridad fisica).
    5. POST: evidencia INDEPENDIENTE y fresca de la Managed Source (nunca el
       PRE cacheado).
    6. Triada: solo si todo coincide se persiste READY; si no, INVALID con el
       motivo. NO se borra el contenido fallido.

    Nunca escribe en la Managed Source ni fuera del FrozenRuntimeRoot.
    """
    raiz = pathlib.Path(root)
    ahora = time.time_ns()
    # MANAGED_SOURCE_WRITES=NO se defiende ACA, no en el caller: si el root de
    # storage esta dentro de la Managed Source (o la contiene), persistir
    # BUILDING y copiar el payload mutaria el arbol que Steam administra. El
    # init acepta `managed_source_root` opcional, asi que un root inicializado
    # sin el pasaria la admision de storage y solo se detectaria aca.
    admision = admitir_storage_root(raiz, managed_source_root=source.root)
    if not admision.success:
        return CandidateResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"el root de storage no es admisible contra esta Managed Source: {admision.message}",
        )
    try:
        pre = _observar_fuente_estable(source, quiet_window_seconds=quiet_window_seconds, sleep=sleep)
    except FrozenRuntimeError as exc:
        return CandidateResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"no se pudo obtener evidencia PRE independiente de la Managed Source: {exc}",
        )

    candidate_id = nuevo_candidate_id(id_factory)
    # RESERVA ANTES DE ESCRIBIR. Sin esto, un `id_factory` que devuelve un id ya
    # usado persistiria la metadata BUILDING encima de la del Candidate previo
    # (incluido uno READY) y la colision del payload recien se descubriria --
    # cuando el dano ya esta hecho.
    try:
        _reservar_candidate_id(raiz, candidate_id)
    except CandidateIdCollisionError as exc:
        return CandidateResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"el candidate_id ya esta en uso: {exc}",
            candidate_id=candidate_id,
            pre_source_evidence=pre,
        )
    except FrozenRuntimeStorageError as exc:
        return CandidateResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"no se pudo reservar el candidate_id: {exc}",
            candidate_id=candidate_id,
            pre_source_evidence=pre,
        )
    metadata = CandidateMetadata(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        candidate_id=candidate_id,
        state=CandidateState.BUILDING,
        created_at_ns=ahora,
        updated_at_ns=ahora,
        source_provider=source.provider.value,
        source_appid=source.appid,
        pre_source_evidence=pre,
        candidate_evidence=None,
        post_source_evidence=None,
        failure_reason=None,
    )
    try:
        _persistir_metadata(raiz, metadata)
    except (FrozenRuntimeStorageError, CandidateVerificationError) as exc:
        return CandidateResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"no se pudo persistir el Candidate BUILDING: {exc}",
            candidate_id=candidate_id,
            pre_source_evidence=pre,
        )

    destino = payload_dir(candidate_dir(raiz, candidate_id))
    try:
        copiar_arbol_independiente(
            source.root,
            destino,
            pre.archivos,
            pre.directory_membership.directories,
            contenedor=raiz,
        )
    except (CandidateCopyError, FrozenRuntimeStorageError, SourceObservationError) as exc:
        return _marcar_invalid(raiz, metadata, f"la copia fallo: {exc}", pre=pre)

    try:
        candidato_evidencia = _observar_candidate(
            destino,
            game_key=pre.game_key,
            appid=pre.appid,
            provider=pre.provider,
        )
    except (CandidateVerificationError, SourceObservationError) as exc:
        return _marcar_invalid(raiz, metadata, f"el Candidate no se pudo observar: {exc}", pre=pre)

    estado_fisico, mensaje_fisico = _verificar_integridad_fisica(destino)
    if estado_fisico is not GenerationVerificationState.VALID:
        return _marcar_invalid(
            raiz,
            metadata,
            f"el Candidate no es fisicamente independiente: {mensaje_fisico}",
            pre=pre,
            candidato=candidato_evidencia,
        )

    try:
        post = _observar_fuente_estable(source, quiet_window_seconds=quiet_window_seconds, sleep=sleep)
    except FrozenRuntimeError as exc:
        return _marcar_invalid(raiz, metadata, f"no se pudo obtener evidencia POST independiente: {exc}", pre=pre)

    veredicto, mensaje = _evaluar_triada(pre, candidato_evidencia, post)
    if veredicto is not GenerationVerificationState.VALID:
        return _marcar_invalid(raiz, metadata, mensaje, pre=pre, candidato=candidato_evidencia, post=post)

    listo = CandidateMetadata(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        candidate_id=candidate_id,
        state=CandidateState.READY,
        created_at_ns=ahora,
        updated_at_ns=time.time_ns(),
        source_provider=source.provider.value,
        source_appid=source.appid,
        pre_source_evidence=pre,
        candidate_evidence=candidato_evidencia,
        post_source_evidence=post,
        failure_reason=None,
    )
    try:
        _persistir_metadata(raiz, listo)
    except (FrozenRuntimeStorageError, CandidateVerificationError) as exc:
        return _marcar_invalid(raiz, metadata, f"no se pudo persistir READY: {exc}", pre=pre)

    return CandidateResult(
        state=GenerationVerificationState.VALID,
        message=mensaje,
        candidate_id=candidate_id,
        metadata=listo,
        pre_source_evidence=pre,
        candidate_evidence=candidato_evidencia,
        post_source_evidence=post,
        observed_digest=candidato_evidencia.tree_digest,
        expected_digest=pre.tree_digest,
    )


def verificar_candidate(root: pathlib.Path, candidate_id: str) -> CandidateResult:
    """Re-verifica un Candidate persistido contra su propia evidencia PRE/POST.

    NO vuelve a observar la Managed Source: verifica que la evidencia persistida
    siga siendo coherente y que el payload en disco aun la satisfaga. El POST
    vive en la metadata (es la evidencia del momento de la copia), asi que un
    Candidate READY conserva la trazabilidad completa despues de un restart sin
    necesitar que Steam siga disponible.
    """
    raiz = pathlib.Path(root)
    cid = validar_candidate_id(candidate_id)
    try:
        metadata = leer_metadata_candidate(candidate_metadata_path(raiz, cid))
    except CandidateCorruptMetadataError as exc:
        return CandidateResult(
            state=GenerationVerificationState.UNKNOWN,
            message=f"metadata de Candidate no utilizable: {exc}",
            candidate_id=cid,
        )

    destino = payload_dir(candidate_dir(raiz, cid))

    # El estado de ciclo de vida PERSISTIDO se resuelve PRIMERO y es decisivo por
    # si mismo: un BUILDING (crash) o un INVALID ya son un hecho conocido por la
    # metadata, y no necesitan inspeccion fisica para concluir. Preguntar primero
    # por la integridad fisica mezclaba los dos planos y degradaba un hecho
    # deterministico a INDETERMINATE (P3-E).
    if metadata.state is CandidateState.BUILDING:
        # Un crash dejo la copia a medias: BUILDING NUNCA se trata como READY.
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message="el Candidate quedo en BUILDING (copia incompleta o proceso interrumpido): nunca READY",
            candidate_id=cid,
            metadata=metadata,
        )
    if metadata.state is CandidateState.INVALID:
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message=metadata.failure_reason or "el Candidate fue marcado INVALID en su creacion",
            candidate_id=cid,
            metadata=metadata,
        )

    # Si el arbol del payload dejo de existir, es un HECHO deterministico (no "no
    # se pudo inspeccionar"): la metadata afirmo que se copio y persistio ese arbol,
    # y ya no hay NADA en disco que satisfaga esa evidencia. Degradar esa perdida
    # concreta a un veredicto ambiguo (INDETERMINATE) permitira que un caller la
    # trate como "reintentar mas tarde" en vez de como INVALID (P3-E / Codex #904).
    if not destino.is_dir():
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message="el arbol del Candidate ya no existe: la evidencia persistida no tiene payload que la satisfaga",
            candidate_id=cid,
            metadata=metadata,
        )

    estado_fisico, mensaje_fisico = _verificar_integridad_fisica(destino)
    # Se conserva la distincion epistemologica: `VIOLATED` es una afirmacion de
    # comparticion/redireccion (INVALID), `INDETERMINATE` es "no se pudo
    # inspeccionar" y no autoriza a afirmar corrupcion.
    if estado_fisico is GenerationVerificationState.INDETERMINATE:
        return CandidateResult(
            state=GenerationVerificationState.INDETERMINATE,
            message=f"no se pudo determinar la integridad fisica del Candidate: {mensaje_fisico}",
            candidate_id=cid,
            metadata=metadata,
        )
    if estado_fisico is not GenerationVerificationState.VALID:
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message=f"el Candidate persistido no es fisicamente independiente: {mensaje_fisico}",
            candidate_id=cid,
            metadata=metadata,
        )
    if metadata.pre_source_evidence is None or metadata.post_source_evidence is None:
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message="el Candidate READY no tiene evidencia PRE/POST persistida completa",
            candidate_id=cid,
            metadata=metadata,
        )
    # Antes de mirar el disco: la evidencia historica persistida tiene que ser
    # coherente consigo misma y con el binding de la metadata.
    veredicto_persistida, mensaje_persistida = _evaluar_coherencia_persistida(metadata)
    if veredicto_persistida is not GenerationVerificationState.VALID:
        return CandidateResult(
            state=veredicto_persistida,
            message=mensaje_persistida,
            candidate_id=cid,
            metadata=metadata,
        )

    try:
        candidato = _observar_candidate(
            destino,
            game_key=metadata.pre_source_evidence.game_key,
            appid=metadata.pre_source_evidence.appid,
            provider=metadata.pre_source_evidence.provider,
        )
    except FrozenRuntimeObservationError as exc:
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message=f"la evidencia critica del Candidate no pudo construirse: {exc}",
            candidate_id=cid,
            metadata=metadata,
        )
    except (CandidateVerificationError, SourceObservationError) as exc:
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message=f"no se pudo observar el Candidate persistido: {exc}",
            candidate_id=cid,
            metadata=metadata,
        )

    # El Candidate en disco tiene que seguir siendo el que la metadata afirma
    # que se copio, y no solo algo equivalente a PRE/POST.
    if metadata.candidate_evidence is not None and (
        _identidad_evidencia(candidato) != _identidad_evidencia(metadata.candidate_evidence)
    ):
        divergentes = _campos_divergentes(metadata.candidate_evidence, candidato)
        return CandidateResult(
            state=GenerationVerificationState.INVALID,
            message=(
                "el Candidate en disco ya no coincide con la evidencia de copia persistida "
                f"(difieren: {', '.join(divergentes) or 'identidad'})"
            ),
            candidate_id=cid,
            metadata=metadata,
        )

    veredicto, mensaje = _evaluar_triada(metadata.pre_source_evidence, candidato, metadata.post_source_evidence)
    return CandidateResult(
        state=veredicto,
        message=mensaje,
        candidate_id=cid,
        metadata=metadata,
        pre_source_evidence=metadata.pre_source_evidence,
        candidate_evidence=candidato,
        post_source_evidence=metadata.post_source_evidence,
        observed_digest=candidato.tree_digest,
        expected_digest=metadata.pre_source_evidence.tree_digest,
    )


def descubrir_candidates(root: pathlib.Path) -> CandidateInventory:
    """Lista los Candidates registrados en ``state/candidates/`` sin promover nada.

    El descubrimiento se recorre desde la METADATA, no desde ``candidates/``:
    BUILDING se persiste ANTES de que la copia cree el directorio del payload
    (§31), asi que un Candidate que murio en esa ventana tiene metadata y todavia
    no tiene arbol. Recorrer los payloads lo haria invisible justo en el estado
    que mas importa detectar.

    Cada registro queda clasificado por su metadata persistida; uno sin metadata
    legible es UNKNOWN (nunca READY), y el motivo queda en el registro.
    """
    raiz = pathlib.Path(root)
    base = candidates_state_dir(raiz)
    registros: list[CandidateRecord] = []
    if not base.is_dir():
        return CandidateInventory(root=raiz, records=())
    for archivo in sorted(base.glob("*.json"), key=lambda p: p.name):
        # Un archivo con nombre no conforme se REGISTRA como UNKNOWN; no puede
        # abortar el descubrimiento entero, porque un `notes.json` suelto
        # hidingria todos los Candidates reales.
        try:
            directorio = candidate_dir(raiz, archivo.stem)
            cid = validar_candidate_id(archivo.stem)
        except InvalidCandidateIdError as exc:
            registros.append(
                CandidateRecord(
                    candidate_id=None,
                    directory=pathlib.Path(archivo),
                    metadata=None,
                    state=GenerationVerificationState.UNKNOWN,
                    message=f"metadata de Candidate con nombre no confiable: {exc}",
                )
            )
            continue
        try:
            metadata = leer_metadata_candidate(archivo)
        except CandidateCorruptMetadataError as exc:
            registros.append(
                CandidateRecord(
                    candidate_id=cid,
                    directory=directorio,
                    metadata=None,
                    state=GenerationVerificationState.UNKNOWN,
                    message=str(exc),
                )
            )
            continue
        registros.append(
            CandidateRecord(
                candidate_id=metadata.candidate_id,
                directory=directorio,
                metadata=metadata,
                state=(
                    # El inventario NO es verificacion: una metadata READY
                    # persistida no prueba que el payload siga intacto (pudo
                    # borrarse, truncarse o volverse un hardlink). Igual que el
                    # inventario hermano de Generations, se reporta UNKNOWN hasta
                    # que `verificar_candidate` corra la verificacion fresca.
                    GenerationVerificationState.UNKNOWN
                    if metadata.state is CandidateState.READY
                    else GenerationVerificationState.INVALID
                ),
                message=metadata.failure_reason or f"estado persistido: {metadata.state.value}",
            )
        )
    return CandidateInventory(root=raiz, records=tuple(registros))


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CandidateInventory",
    "CandidateMetadata",
    "CandidateRecord",
    "CandidateResult",
    "CandidateSourceEvidence",
    "CandidateState",
    "candidate_dir",
    "candidate_metadata_path",
    "candidates_state_dir",
    "crear_candidate",
    "descubrir_candidates",
    "leer_metadata_candidate",
    "payload_dir",
    "serializar_metadata_candidate",
    "verificar_candidate",
]
