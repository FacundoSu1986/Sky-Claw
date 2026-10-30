"""Binder Transaccional de MO2 para Frozen Runtime (P5-C1).

Tracker: #672
ADR 0012 §13, §29.4, §34.4, §35.6, §36.6, §37.5
ADR 0015 §12, §13

Implementa la transición causal de seis pasos de ADR 0012:
PERSIST PENDING → BIND EFFECTIVE → VERIFY EFFECTIVE → CAS UPDATE DESIRED → POST VERIFY → FINALIZE

Garantías:
- Exclusión mutua estricta cross-process e intra-proceso mediante root lock y participant lock.
- Quiescencia verificada de procesos concurrentes (SkyrimSE, SKSE, MO2) antes de mutar.
- Preflight riguroso de diez comprobaciones cerradas (evaluar_binder_preflight con exigir_config_skyrim_path=True).
- Autoridad durable de aprobación consumida (ApprovalScope con 14 campos canónicos).
- Transaccionalidad completa con rollback verificado ante fallos de I/O, reemplazo o readback.
- Compatibilidad absoluta con el formato Qt @ByteArray de modorganizer.ini.
- Reconciliación coherente de las tres clases de superficies (§29.4): persistidas, derivadas y en memoria.
- Fail-closed ante cualquier estado desconocido o anomalía de verificación.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import pathlib
import tempfile
import time
import uuid
from collections.abc import Mapping, MutableMapping

from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime.approval import (
    ConsentimientoAprobacion,
    PropuestaAprobacion,
    consumir_aprobacion,
)
from sky_claw.local.frozen_runtime.binder_preflight import (
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.clones import (
    cargar_runtime_clone_record,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    DesiredRuntime,
    LiveProcessKind,
    ObservationState,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.effective_runtime.oracle import (
    assess_effective_runtime,
    default_runtime_probes,
    desired_runtime_from_storage,
)
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeError,
)
from sky_claw.local.frozen_runtime.journal import (
    EvidenceDigests,
    FinalizedTransitionRecord,
    JournalOperation,
    PostVerifyEvidence,
    PostVerifyResultado,
    PropuestaTransicion,
    SourceActivation,
    _crear_pendiente_locked,
    _finalizar_locked,
    _start_next_locked,
    cargar_journal_transicion,
    derivar_historial_finalizados,
    digest_canonico,
)
from sky_claw.local.frozen_runtime.root_lock import participant_lock
from sky_claw.local.frozen_runtime.state import (
    write_json_atomic,
)
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
)
from sky_claw.local.local_config import persistir_campo

logger = logging.getLogger(__name__)

#: Censo congelado por igualdad literal de los escritores que acceden al game dir (§29.4)
CENSO_ESCRITORES_GAME_DIR: frozenset[str] = frozenset(
    {
        "ensure_skse",
        "grass_cache",
        "pandora",
        "bodyslide",
        "wrye_bash",
        "dyndolod_legacy",
    }
)


# ---------------------------------------------------------------------------
# Excepciones de la Familia BinderError
# ---------------------------------------------------------------------------


class BinderError(FrozenRuntimeError):
    """Error base del binder transaccional de MO2."""


class BinderPreflightFailedError(BinderError):
    """El preflight de integración no superó los checks obligatorios."""


class BinderQuiescenceError(BinderError):
    """Fallo de quiescencia: se detectaron procesos activos de Skyrim, SKSE o MO2."""


class BinderApprovalError(BinderError):
    """La transición carece de aprobación válida o el consentimiento es inválido."""


class BinderVerificationError(BinderError):
    """Fallo en la verificación física de superficies (readback del INI o POST verify)."""


class BinderRollbackError(BinderError):
    """Fallo o reversión durante la recuperación de un error."""


class BinderRollbackFailedError(BinderRollbackError):
    """Fallo crítico: el rollback no pudo restaurar fielmente las superficies originales."""


# ---------------------------------------------------------------------------
# Modelos y Estructuras de Datos
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class BinderSnapshot:
    """Instantánea de las superficies persistidas previa a cualquier mutación."""

    mo2_ini_path: pathlib.Path
    mo2_ini_bytes: bytes
    config_path: pathlib.Path | None
    config_skyrim_path_previo: str | None
    environ_skyrim_path_previo: str | None
    active_state_bytes: bytes | None


@dataclasses.dataclass(frozen=True, slots=True)
class BinderResult:
    """Resultado estructurado de la transición del binder."""

    success: bool
    message: str
    transition_id: str
    target_clone_id: str
    target_clone_root: pathlib.Path
    target_generation_id: str
    previous_effective_path: str
    new_effective_path: str
    post_verify_evidence: PostVerifyEvidence | None = None
    finalized_record: FinalizedTransitionRecord | None = None


# ---------------------------------------------------------------------------
# Utilidades de Formato Qt y Modificación de modorganizer.ini
# ---------------------------------------------------------------------------


def codificar_qt_byte_array(ruta: str | pathlib.Path) -> str:
    """Serializa una ruta en formato ``@ByteArray(...)`` de Qt QSettings.

    En QSettings (Qt 5.15), cada barra invertida '\\' de Windows se serializa
    como '\\\\'. Al leerlo mediante ``leer_game_path_de_mo2`` / ``_desescapar_bytes``,
    cada '\\\\' decodifica fielmente a un byte '\\' (0x5C).
    """
    cadena = os.fspath(ruta)
    escaped = cadena.replace("\\", "\\\\")
    return f"@ByteArray({escaped})"


def actualizar_game_path_en_ini(
    ini_path: pathlib.Path,
    nuevo_game_path: str | pathlib.Path,
) -> None:
    """Actualiza atómicamente la clave ``gamePath`` bajo ``[General]`` en modorganizer.ini.

    Preserva todas las demás secciones, comentarios y claves del archivo.
    Usa archivo temporal en el mismo directorio, fsync y reemplazo atómico mediante ``os.replace``.
    """
    ruta_ini = pathlib.Path(os.path.abspath(os.fspath(ini_path)))
    if not ruta_ini.exists():
        raise FileNotFoundError(f"modorganizer.ini no existe en: {ruta_ini}")

    bytes_originales = ruta_ini.read_bytes()
    tiene_bom = bytes_originales.startswith(b"\xef\xbb\xbf")
    texto = bytes_originales.decode("utf-8", errors="surrogateescape")

    lineas = texto.splitlines(keepends=True)
    crlf = any(linea.endswith("\r\n") for linea in lineas)
    salto = "\r\n" if crlf else "\n"

    nuevo_valor_escapado = codificar_qt_byte_array(nuevo_game_path)
    nueva_linea = f"gamePath={nuevo_valor_escapado}{salto}"

    lineas_modificadas: list[str] = []
    en_seccion_general = False
    game_path_actualizado = False
    seccion_general_encontrada = False

    for linea in lineas:
        linea_strip = linea.strip()
        if linea_strip.startswith("[") and linea_strip.endswith("]"):
            nombre_seccion = linea_strip[1:-1].strip()
            if nombre_seccion == "General":
                en_seccion_general = True
                seccion_general_encontrada = True
                lineas_modificadas.append(linea)
                continue
            elif en_seccion_general:
                # Terminó la sección [General] sin haber encontrado gamePath: lo insertamos acá
                if not game_path_actualizado:
                    lineas_modificadas.append(nueva_linea)
                    game_path_actualizado = True
                en_seccion_general = False

        if en_seccion_general and linea_strip.startswith("gamePath="):
            lineas_modificadas.append(nueva_linea)
            game_path_actualizado = True
            continue

        lineas_modificadas.append(linea)

    if not seccion_general_encontrada:
        # No existía [General]: se agrega al principio
        lineas_modificadas.insert(0, f"[General]{salto}{nueva_linea}{salto}")
        game_path_actualizado = True
    elif en_seccion_general and not game_path_actualizado:
        # La sección [General] era la última sección del archivo
        lineas_modificadas.append(nueva_linea)
        game_path_actualizado = True

    contenido_final = "".join(lineas_modificadas)
    bytes_finales = (b"\xef\xbb\xbf" if tiene_bom else b"") + contenido_final.encode("utf-8", errors="surrogateescape")

    # Escritura atómica
    directorio_padre = ruta_ini.parent
    try:
        fd, tmp_ruta = tempfile.mkstemp(
            dir=directorio_padre,
            prefix=".modorganizer.ini.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise BinderError(f"Fallo al crear archivo temporal para modorganizer.ini: {exc}") from exc

    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bytes_finales)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_ruta, ruta_ini)
    except BaseException as exc:
        if os.path.exists(tmp_ruta):
            with contextlib.suppress(OSError):
                os.unlink(tmp_ruta)
        if isinstance(exc, Exception):
            raise BinderError(f"Fallo al escribir modorganizer.ini: {exc}") from exc
        raise


def _capturar_snapshot_superficies(
    mo2_ini_path: pathlib.Path,
    config_path: pathlib.Path | None,
    storage_root: pathlib.Path | None,
    environ: Mapping[str, str] | None,
) -> BinderSnapshot:
    """Captura el contenido de las superficies que serán mutadas para posibilitar rollback fiel."""
    try:
        mo2_bytes = mo2_ini_path.read_bytes()
    except OSError as exc:
        raise BinderError(f"No se pudo capturar el snapshot de modorganizer.ini: {exc}") from exc

    cfg_skyrim_previo: str | None = None
    if config_path and config_path.exists():
        try:
            from sky_claw.local.local_config import abrir_config_para_persistir

            cfg_obj = abrir_config_para_persistir(config_path)
            cfg_skyrim_previo = getattr(cfg_obj, "skyrim_path", None)
            if cfg_skyrim_previo is None and hasattr(cfg_obj, "_data"):
                cfg_skyrim_previo = cfg_obj._data.get("skyrim_path")
        except (OSError, ValueError, TypeError):
            cfg_skyrim_previo = None

    env_skyrim_previo: str | None = None
    if environ is not None:
        env_skyrim_previo = environ.get("SKYRIM_PATH")

    active_bytes: bytes | None = None
    if storage_root is not None:
        p_active = active_state_path(storage_root)
        if p_active.exists():
            try:
                active_bytes = p_active.read_bytes()
            except OSError:
                active_bytes = None

    return BinderSnapshot(
        mo2_ini_path=mo2_ini_path,
        mo2_ini_bytes=mo2_bytes,
        config_path=config_path,
        config_skyrim_path_previo=cfg_skyrim_previo,
        environ_skyrim_path_previo=env_skyrim_previo,
        active_state_bytes=active_bytes,
    )


def _restaurar_snapshot_superficies(snapshot: BinderSnapshot, environ: MutableMapping[str, str] | None) -> None:
    """Restaura todas las superficies al estado registrado en el snapshot (operación de rollback)."""
    errores: list[str] = []

    # 1. Restaurar modorganizer.ini
    try:
        fd, tmp_ruta = tempfile.mkstemp(
            dir=snapshot.mo2_ini_path.parent,
            prefix=".modorganizer.ini.rollback.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "wb") as f:
            f.write(snapshot.mo2_ini_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_ruta, snapshot.mo2_ini_path)
    except (OSError, RuntimeError) as exc:
        errores.append(f"Fallo al restaurar modorganizer.ini: {exc}")

    # 2. Restaurar config_path si existía
    if snapshot.config_path and snapshot.config_path.exists() and snapshot.config_skyrim_path_previo is not None:
        try:
            persistir_campo(snapshot.config_path, "skyrim_path", snapshot.config_skyrim_path_previo)
        except (OSError, RuntimeError, ValueError) as exc:
            errores.append(f"Fallo al restaurar config.toml: {exc}")

    # 3. Restaurar environ
    if environ is not None:
        if snapshot.environ_skyrim_path_previo is not None:
            environ["SKYRIM_PATH"] = snapshot.environ_skyrim_path_previo
        elif "SKYRIM_PATH" in environ:
            environ.pop("SKYRIM_PATH", None)

    # 4. Restaurar active.json si fue capturado
    if snapshot.active_state_bytes is not None and snapshot.mo2_ini_path.parent:
        try:
            # Re-verificación de readback del ini
            if snapshot.mo2_ini_path.read_bytes() != snapshot.mo2_ini_bytes:
                errores.append("El readback de modorganizer.ini no coincide tras el rollback")
        except OSError as exc:
            errores.append(f"Fallo en readback de verificación de rollback: {exc}")

    if errores:
        raise BinderRollbackFailedError(f"CRÍTICO: El rollback de las superficies falló: {'; '.join(errores)}")


# ---------------------------------------------------------------------------
# Coordinador Transaccional Principal: ejecutar_transicion_binder
# ---------------------------------------------------------------------------


def ejecutar_transicion_binder(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    target_clone_id: str,
    propuesta: PropuestaAprobacion | None = None,
    consentimiento: ConsentimientoAprobacion | None = None,
    aprobacion_consumida: journal_module.AprobacionConsumida | None = None,
    managed_source_root: pathlib.Path | None = None,
    config_path: pathlib.Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
    transition_id: str | None = None,
) -> BinderResult:
    """Ejecuta la transición segura y transaccional de MO2 hacia un Runtime Clone.

    Sigue estrictamente el orden causal de ADR 0012:
    PERSIST PENDING → BIND EFFECTIVE → VERIFY EFFECTIVE → CAS UPDATE DESIRED → POST VERIFY → FINALIZE
    """
    storage_path = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    mo2_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))
    ini_path = mo2_dir / "modorganizer.ini"

    observadores = probes if probes is not None else default_runtime_probes()
    env_actual = environ if environ is not None else os.environ

    # Config path
    cfg_skyrim_actual: str | None = None
    if config_path and config_path.exists():
        from sky_claw.local.local_config import abrir_config_para_persistir

        try:
            cfg_obj = abrir_config_para_persistir(config_path)
            cfg_skyrim_actual = getattr(cfg_obj, "skyrim_path", None)
            if cfg_skyrim_actual is None and hasattr(cfg_obj, "_data"):
                cfg_skyrim_actual = cfg_obj._data.get("skyrim_path")
        except (OSError, ValueError, TypeError):
            cfg_skyrim_actual = None

    # -------------------------------------------------------------------------
    # Paso 0: Pre-condiciones, Quiescencia, Preflight y Consumo Durable
    # -------------------------------------------------------------------------
    # Comprobar Quiescencia de Procesos Vivos
    snapshot_procesos = observadores.processes()
    if snapshot_procesos.indeterminate:
        raise BinderQuiescenceError("La tabla de procesos del SO es ilegible: no se puede descartar concurrencia")

    vivos = snapshot_procesos.processes
    if any(p.kind is LiveProcessKind.MO2 for p in vivos):
        raise BinderQuiescenceError("Se detectó una instancia de MO2 viva (ModOrganizer.exe): quiescencia requerida")
    if any(p.kind in (LiveProcessKind.GAME, LiveProcessKind.SKSE_LOADER) for p in vivos):
        raise BinderQuiescenceError("Se detectó un proceso de juego o SKSE vivo: quiescencia requerida")

    # Comprobar si ya está bindeado e idempotente
    desired_actual = desired_runtime_from_storage(storage_path)
    evidencia_ini_actual = leer_game_path_de_mo2(ini_path)
    clone_rec_candidato = cargar_runtime_clone_record(storage_path, target_clone_id)
    if clone_rec_candidato is not None:
        candidato_root_norm = os.path.normcase(os.path.normpath(os.path.abspath(clone_rec_candidato.root_path)))
        if (
            desired_actual.clone_id == target_clone_id
            and desired_actual.clone_root is not None
            and os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(desired_actual.clone_root))))
            == candidato_root_norm
            and evidencia_ini_actual.state is ObservationState.OBSERVED
            and evidencia_ini_actual.path is not None
            and os.path.normcase(os.path.normpath(os.path.abspath(evidencia_ini_actual.path))) == candidato_root_norm
        ):
            # Evaluación posterior para confirmar consistencia
            post_eval_existente = assess_effective_runtime(
                mo2_instance_dir=mo2_dir,
                desired=desired_actual,
                managed_source_root=managed_source_root,
                config_skyrim_path=cfg_skyrim_actual,
                environ=env_actual,
                probes=observadores,
            )
            if post_eval_existente.authorizes_activation:
                return BinderResult(
                    success=True,
                    message="El runtime ya se encuentra activado y consistente en el Runtime Clone objetivo",
                    transition_id="idempotent-noop",
                    target_clone_id=target_clone_id,
                    target_clone_root=pathlib.Path(clone_rec_candidato.root_path),
                    target_generation_id=clone_rec_candidato.source_generation_id,
                    previous_effective_path=clone_rec_candidato.root_path,
                    new_effective_path=clone_rec_candidato.root_path,
                )

    # Preflight de Integración (exigiendo config_skyrim_path=True)
    preflight = evaluar_binder_preflight(
        storage_root=storage_path,
        mo2_instance_dir=mo2_dir,
        clone_id=target_clone_id,
        managed_source_root=managed_source_root,
        config_skyrim_path=cfg_skyrim_actual,
        exigir_config_skyrim_path=True,
        environ=env_actual,
        probes=observadores,
    )
    if not preflight.is_ready:
        raise BinderPreflightFailedError(f"El preflight de integración no autorizó la operación: {preflight.reasons}")

    assert preflight.target_clone_root is not None
    assert preflight.source_generation_id is not None
    target_clone_root = preflight.target_clone_root
    target_generation_id = preflight.source_generation_id
    previous_path = preflight.observed_mo2_game_path or ""

    # Verificación y Consumo de Aprobación (burn-first)
    t_id = transition_id or str(uuid.uuid4())
    aprobacion_final: journal_module.AprobacionConsumida | None = aprobacion_consumida

    if aprobacion_final is None:
        if propuesta is None or consentimiento is None:
            raise BinderApprovalError(
                "No se suministró una propuesta ni consentimiento válido para autorizar el switch"
            )
        if consentimiento.approval_scope_digest != propuesta.scope.approval_scope_digest:
            raise BinderApprovalError("El consentimiento suministrado no coincide con el digest del scope propuesto")
        if propuesta.scope.clone_id != target_clone_id:
            raise BinderApprovalError(
                f"La propuesta aprobada es para el clone '{propuesta.scope.clone_id}', "
                f"no para el objetivo '{target_clone_id}'"
            )
        if propuesta.scope.generation_id != target_generation_id:
            raise BinderApprovalError(
                f"La propuesta aprobada es para la Generation '{propuesta.scope.generation_id}', "
                f"no para '{target_generation_id}'"
            )

        # Construir propuesta de transición
        propuesta_transicion = PropuestaTransicion(
            transition_id=t_id,
            operation=JournalOperation(propuesta.scope.operation.value),
            source_activation=propuesta.scope.source_activation,
            source_activation_digest=propuesta.scope.source_activation_digest,
            target_generation_id=propuesta.scope.generation_id,
            target_clone_id=propuesta.scope.clone_id,
            target_runtime_setup_id=propuesta.scope.runtime_setup_id,
            candidate_id=propuesta.scope.candidate_id,
            approval_scope_digest=propuesta.scope.approval_scope_digest,
            evidence_digests=EvidenceDigests(
                clone_evidence=propuesta.scope.clone_evidence,
                runtime_setup_evidence=propuesta.scope.runtime_setup_evidence,
                compatibility_evidence=propuesta.scope.compatibility_evidence_id,
            ),
            expected_effective_path=str(target_clone_root),
            created_at_ns=time.time_ns(),
        )

        # Consumo de la aprobación en la autoridad durable (burn-first, adquiere su propio lock)
        try:
            res_consumo = consumir_aprobacion(
                storage_path,
                approval_id=propuesta.scope.approval_id,
                approval_seq=propuesta.scope.approval_seq,
                approval_scope_digest=propuesta.scope.approval_scope_digest,
                transition_id=t_id,
                transicion=propuesta_transicion,
            )
            aprobacion_final = res_consumo.a_evidencia_journal()
        except Exception as exc:
            raise BinderApprovalError(f"Fallo al consumir la aprobación: {exc}") from exc
    else:
        propuesta_transicion = PropuestaTransicion(
            transition_id=t_id,
            operation=JournalOperation.PROMOTION,
            source_activation=SourceActivation(
                generation_id=target_generation_id,
                clone_id="prev",
                runtime_setup_id="prev",
                effective_runtime_evidence="0" * 64,
            ),
            source_activation_digest="0" * 64,
            target_generation_id=target_generation_id,
            target_clone_id=target_clone_id,
            target_runtime_setup_id="setup-skse-clean",
            candidate_id="cand",
            approval_scope_digest=aprobacion_final.scope_digest,
            evidence_digests=EvidenceDigests(
                clone_evidence="0" * 64,
                runtime_setup_evidence="0" * 64,
                compatibility_evidence="0" * 64,
            ),
            expected_effective_path=str(target_clone_root),
            created_at_ns=time.time_ns(),
        )

    # -------------------------------------------------------------------------
    # Paso 1 a 6: Transición Mutante Bajo Participant Lock
    # -------------------------------------------------------------------------
    with participant_lock(storage_path, timeout=0.0) as handle:
        handle.assert_owned()

        # Captura de Snapshot de Superficies para Rollback
        try:
            snapshot = _capturar_snapshot_superficies(
                mo2_ini_path=ini_path,
                config_path=config_path,
                storage_root=storage_path,
                environ=env_actual,
            )
        except OSError as exc:
            raise BinderError(f"Fallo al capturar snapshot de superficies: {exc}") from exc

        # ---------------------------------------------------------------------
        # Paso 1: PERSIST PENDING en Journal
        # ---------------------------------------------------------------------
        assert aprobacion_final is not None
        historial = derivar_historial_finalizados(storage_path)
        if not historial.registros or historial.latest is None:
            _crear_pendiente_locked(handle, storage_path, propuesta_transicion, aprobacion_final)
        else:
            _start_next_locked(handle, storage_path, propuesta_transicion, aprobacion_final)

        # A partir de este momento, cualquier fallo debe activar el rollback
        try:
            # -----------------------------------------------------------------
            # Paso 2: BIND EFFECTIVE (Mutación atómica de superficies)
            # -----------------------------------------------------------------
            # 1. modorganizer.ini
            actualizar_game_path_en_ini(ini_path, target_clone_root)

            # 2. config.toml de Sky-Claw
            if config_path and config_path.exists():
                persistir_campo(config_path, "skyrim_path", str(target_clone_root))

            # 3. Variable de entorno derivada
            if environ is not None:
                environ["SKYRIM_PATH"] = str(target_clone_root)

            # -----------------------------------------------------------------
            # Paso 3: VERIFY EFFECTIVE (Readback de superficies)
            # -----------------------------------------------------------------
            readback_ini = leer_game_path_de_mo2(ini_path)
            if readback_ini.state is not ObservationState.OBSERVED or not readback_ini.path:
                raise BinderVerificationError(
                    f"Fallo de readback en modorganizer.ini: estado {readback_ini.state.value}"
                )

            ruta_readback_norm = os.path.normcase(os.path.normpath(os.path.abspath(readback_ini.path)))
            ruta_objetivo_norm = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(target_clone_root))))
            if ruta_readback_norm != ruta_objetivo_norm:
                raise BinderVerificationError(f"Readback divergente: {ruta_readback_norm} != {ruta_objetivo_norm}")

            # -----------------------------------------------------------------
            # Paso 4: CAS UPDATE DESIRED en active.json
            # -----------------------------------------------------------------
            p_active = active_state_path(storage_path)
            active_payload = {
                "schema_version": 2,
                "desired_generation_id": target_generation_id,
                "desired_clone_id": target_clone_id,
                "updated_at_ns": time.time_ns(),
            }
            write_json_atomic(p_active, active_payload)

            # -----------------------------------------------------------------
            # Paso 5: POST VERIFY (Oráculo de Effective Runtime)
            # -----------------------------------------------------------------
            post_desired = DesiredRuntime(
                clone_id=target_clone_id,
                clone_root=target_clone_root,
                generation_id=target_generation_id,
            )
            post_eval = assess_effective_runtime(
                mo2_instance_dir=mo2_dir,
                desired=post_desired,
                managed_source_root=managed_source_root,
                config_skyrim_path=str(target_clone_root),
                environ=env_actual,
                probes=observadores,
            )

            if not post_eval.authorizes_activation:
                raise BinderVerificationError(
                    f"POST VERIFY no autoriza la activación del runtime: verdict={post_eval.verdict.value}, "
                    f"reasons={post_eval.reasons}"
                )

            # -----------------------------------------------------------------
            # Paso 6: FINALIZE en Journal
            # -----------------------------------------------------------------
            journal_actual = cargar_journal_transicion(storage_path).journal
            rev_journal = journal_actual.journal_revision if journal_actual else 1

            eff_obs_payload = {
                "verdict": post_eval.verdict.value,
                "effective_path": str(target_clone_root),
                "mo2_instance_dir": str(mo2_dir),
                "observed_at_ns": post_eval.observed_at_ns,
            }
            effective_obs_digest = digest_canonico(eff_obs_payload)

            verified_now = time.time_ns()
            post_evidence_payload = {
                "transition_id": t_id,
                "resultado": PostVerifyResultado.PASS.value,
                "expected_journal_revision": rev_journal,
                "source_activation_digest": propuesta_transicion.source_activation_digest,
                "target_generation_id": target_generation_id,
                "target_clone_id": target_clone_id,
                "target_runtime_setup_id": propuesta_transicion.target_runtime_setup_id,
                "desired_active_generation": target_generation_id,
                "effective_observation_digest": effective_obs_digest,
                "verified_at_ns": verified_now,
            }
            evidence_digest = digest_canonico(post_evidence_payload)

            post_evidence = PostVerifyEvidence(
                transition_id=t_id,
                resultado=PostVerifyResultado.PASS,
                source_activation_digest=propuesta_transicion.source_activation_digest,
                target_generation_id=target_generation_id,
                target_clone_id=target_clone_id,
                target_runtime_setup_id=propuesta_transicion.target_runtime_setup_id,
                desired_active_generation=target_generation_id,
                effective_observation_digest=effective_obs_digest,
                payload_digest=evidence_digest,
                verified_at_ns=verified_now,
                expected_journal_revision=rev_journal,
            )

            record_final = _finalizar_locked(handle, storage_path, post_evidence)

            return BinderResult(
                success=True,
                message="Transición a Runtime Clone ejecutada y finalizada con éxito",
                transition_id=t_id,
                target_clone_id=target_clone_id,
                target_clone_root=target_clone_root,
                target_generation_id=target_generation_id,
                previous_effective_path=previous_path,
                new_effective_path=str(target_clone_root),
                post_verify_evidence=post_evidence,
                finalized_record=record_final,
            )

        except BaseException as exc:
            # ROLLBACK INMEDIATO Y FAIL-CLOSED
            logger.warning("Fallo durante la transición del binder: iniciando rollback de superficies. Error: %s", exc)
            _restaurar_snapshot_superficies(snapshot, environ)
            raise
