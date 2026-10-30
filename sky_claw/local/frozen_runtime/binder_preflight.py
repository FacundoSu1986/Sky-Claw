"""Preflight de integración read-only para el binder de MO2 (P5-C0).

Tracker: #672
ADR 0012 §13, §29.4
ADR 0015 §12, §13

Establece las precondiciones verificables para conmutar MO2 hacia un Runtime Clone
independiente de Steam de forma completamente segura y determinista.

No habilita el switch productivo (P5-C1). No muta ninguna superficie ni archivo en disco.
Reutiliza estrictamente los verificadores y oráculos existentes del subsistema:
- discovery.py: discover_managed_source (evidencia de proveedor Steam y manifest)
- clones.py: RuntimeCloneRecord + verificar_runtime_clone + admitir_clone_root_path
- generations.py: verificar_generation (integridad de Generation y no-drift)
- compatibility.py: assess_runtime_compatibility (SKSE loader y DLL matching build)
- processes.py: probe_live_processes (concurrencia de Skyrim, SKSE y MO2)
- mo2_ini.py: leer_game_path_de_mo2 + leer_ejecutables_de_mo2 (fidelidad Qt en modorganizer.ini)
- oracle.py: _primer_reparse, _normalizar, _es_igual_o_hijo
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import time
from collections.abc import Mapping
from enum import StrEnum

from sky_claw.config import SKYRIM_SE_APPID, STEAM_DEFAULT_PATHS
from sky_claw.local.frozen_runtime.clones import (
    CANONICAL_ADMITTED_ROLE,
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
    admitir_clone_root_path,
    cargar_runtime_clone_record,
    validar_clone_id,
    verificar_runtime_clone,
)
from sky_claw.local.frozen_runtime.discovery import discover_managed_source
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    Mo2Executable,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    LiveProcessKind,
    ObservationState,
    RuntimeCompatibility,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.effective_runtime.oracle import (
    _REPARSE_HAY,
    _REPARSE_INDETERMINADO,
    _es_igual_o_hijo,
    _lanzadores_del_juego_fuera_de,
    _normalizar,
    _primer_reparse,
    default_runtime_probes,
)
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeError,
    FrozenRuntimeStorageError,
    InvalidCloneIdError,
    RuntimeCloneAdmissionError,
    RuntimeCloneCorruptError,
    RuntimeCloneNotFoundError,
    RuntimeCloneSchemaError,
    StateCorruptError,
    StateSchemaError,
)
from sky_claw.local.frozen_runtime.generations import (
    leer_generation_metadata,
    verificar_generation,
)
from sky_claw.local.frozen_runtime.models import (
    DiscoveryState,
    ManagedSourceDiscoveryResult,
    ManagedSourceProvider,
    ProviderObservationState,
)
from sky_claw.local.frozen_runtime.provider_signals import (
    evaluate_provider_observation,
    observe_provider_activity,
)
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import active_state_path
from sky_claw.local.frozen_runtime.storage_models import (
    GenerationVerificationState,
)
from sky_claw.local.runtime_vault.runtime_observation import RuntimeObservationError

_RE_ALIAS_83 = re.compile(r"~[0-9]")


def _contiene_alias_83(ruta: str | pathlib.Path) -> bool:
    """True si algún componente de la ruta contiene el patrón de alias corto de Windows 8.3."""
    partes = pathlib.Path(ruta).parts
    return any(_RE_ALIAS_83.search(p) is not None for p in partes)


class BinderPreflightVerdict(StrEnum):
    """Veredicto consolidado del preflight. Sólo READY autoriza iniciar el switch."""

    READY = "ready"
    BLOCKED = "blocked"


class PreflightCheckCode(StrEnum):
    """Familia cerrada de comprobaciones obligatorias del preflight."""

    PATH_SAFETY = "path_safety"
    MANAGED_SOURCE = "managed_source"
    CLONE_RECORD = "clone_record"
    CLONE_LIFECYCLE = "clone_lifecycle"
    GENERATION_INTEGRITY = "generation_integrity"
    CLONE_PHYSICAL_INTEGRITY = "clone_physical_integrity"
    RUNTIME_COMPATIBILITY = "runtime_compatibility"
    PROCESS_CONCURRENCY = "process_concurrency"
    MO2_CONFIGURATION = "mo2_configuration"
    CONFIG_COHERENCE = "config_coherence"


class PreflightCheckStatus(StrEnum):
    """Estado de evaluación de cada comprobación."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class PreflightReasonCode(StrEnum):
    """Motivos tipados de rechazo o aprobación del preflight."""

    PREFLIGHT_ALL_CHECKS_PASSED = "preflight_all_checks_passed"
    MANAGED_SOURCE_NOT_FOUND = "managed_source_not_found"
    MANAGED_SOURCE_INVALID = "managed_source_invalid"
    MANAGED_SOURCE_AMBIGUOUS = "managed_source_ambiguous"
    CLONE_ID_UNSPECIFIED = "clone_id_unspecified"
    CLONE_ID_INVALID = "clone_id_invalid"
    CLONE_RECORD_MISSING = "clone_record_missing"
    CLONE_RECORD_CORRUPT = "clone_record_corrupt"
    CLONE_RECORD_SCHEMA_INVALID = "clone_record_schema_invalid"
    CLONE_LIFECYCLE_NOT_PROVISIONED = "clone_lifecycle_not_provisioned"
    CLONE_ROLE_INVALID = "clone_role_invalid"
    CLONE_ADMISSION_REJECTED = "clone_admission_rejected"
    GENERATION_NOT_FOUND = "generation_not_found"
    GENERATION_DRIFTED = "generation_drifted"
    GENERATION_INVALID = "generation_invalid"
    CLONE_PHYSICAL_INTEGRITY_INVALID = "clone_physical_integrity_invalid"
    CLONE_PHYSICAL_INTEGRITY_INDETERMINATE = "clone_physical_integrity_indeterminate"
    REPARSE_POINT_DETECTED = "reparse_point_detected"
    REPARSE_INSPECTION_FAILED = "reparse_inspection_failed"
    SHORT_NAME_ALIAS_DETECTED = "short_name_alias_detected"
    PATH_CONTAINMENT_VIOLATION = "path_containment_violation"
    PATHS_COLLIDE = "paths_collide"
    SKSE_INCOMPATIBLE = "skse_incompatible"
    COMPATIBILITY_UNKNOWN = "compatibility_unknown"
    IDENTITY_UNREADABLE = "identity_unreadable"
    PROCESS_TABLE_UNREADABLE = "process_table_unreadable"
    LIVE_GAME_RUNNING = "live_game_running"
    LIVE_MO2_RUNNING = "live_mo2_running"
    MO2_INI_MISSING = "mo2_ini_missing"
    MO2_INI_UNREADABLE = "mo2_ini_unreadable"
    MO2_GAME_PATH_UNREADABLE = "mo2_game_path_unreadable"
    CONFIG_DIVERGENCE_DETECTED = "config_divergence_detected"


@dataclasses.dataclass(frozen=True, slots=True)
class PreflightCheckResult:
    """Resultado individual de una comprobación del preflight."""

    check: PreflightCheckCode
    status: PreflightCheckStatus
    reason: PreflightReasonCode
    detail: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class BinderPreflightAssessment:
    """Evaluación consolidada del preflight de integración para el switch de MO2."""

    verdict: BinderPreflightVerdict
    reasons: tuple[PreflightReasonCode, ...]
    checks: tuple[PreflightCheckResult, ...]
    target_clone_id: str | None = None
    target_clone_root: pathlib.Path | None = None
    source_generation_id: str | None = None
    managed_source_root: pathlib.Path | None = None
    mo2_instance_dir: pathlib.Path | None = None
    observed_mo2_game_path: str | None = None
    assessed_at_ns: int = 0
    notes: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        """Invariante: True solo si el veredicto es READY, todos los 10 checks están presentes sin duplicados y todos son PASS."""
        if self.verdict is not BinderPreflightVerdict.READY:
            return False
        codigos = [c.check for c in self.checks]
        if len(codigos) != len(PreflightCheckCode) or set(codigos) != set(PreflightCheckCode):
            return False
        return all(c.status is PreflightCheckStatus.PASS for c in self.checks)


def evaluar_binder_preflight(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    clone_id: str | None = None,
    managed_source_root: pathlib.Path | None = None,
    steam_roots: tuple[str, ...] = STEAM_DEFAULT_PATHS,
    config_skyrim_path: str | None = None,
    exigir_config_skyrim_path: bool = False,
    environ: Mapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
    assessed_at_ns: int | None = None,
) -> BinderPreflightAssessment:
    """Evalúa exhaustivamente las precondiciones para conmutar MO2 hacia un Runtime Clone.

    Operación 100% READ-ONLY. No escribe en disco ni altera la configuración de MO2.
    """
    observadores = probes if probes is not None else default_runtime_probes()
    sello = assessed_at_ns if assessed_at_ns is not None else time.time_ns()

    checks_map: dict[PreflightCheckCode, PreflightCheckResult] = {}
    reasons: list[PreflightReasonCode] = []

    storage = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    mo2_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))
    ini_path = mo2_dir / "modorganizer.ini"

    # -------------------------------------------------------------------------
    # 1. Comprobación de seguridad en rutas base: mo2_instance_dir, storage_root e ini
    # -------------------------------------------------------------------------
    if _contiene_alias_83(storage) or _contiene_alias_83(mo2_dir) or _contiene_alias_83(ini_path):
        checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
            check=PreflightCheckCode.PATH_SAFETY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
            detail="mo2_instance_dir, storage_root o modorganizer.ini contienen componentes con alias 8.3",
        )
        reasons.append(PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED)
    else:
        clase_mo2, det_mo2 = _primer_reparse(mo2_dir)
        clase_storage, det_storage = _primer_reparse(storage)
        clase_ini, det_ini = _primer_reparse(ini_path)

        if _REPARSE_HAY in (clase_mo2, clase_storage, clase_ini):
            detalle = det_ini if clase_ini == _REPARSE_HAY else (det_mo2 if clase_mo2 == _REPARSE_HAY else det_storage)
            checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.REPARSE_POINT_DETECTED,
                detail=detalle or "reparse point en mo2_instance_dir, storage_root o modorganizer.ini",
            )
            reasons.append(PreflightReasonCode.REPARSE_POINT_DETECTED)
        elif _REPARSE_INDETERMINADO in (clase_mo2, clase_storage, clase_ini):
            detalle = (
                det_ini
                if clase_ini == _REPARSE_INDETERMINADO
                else (det_mo2 if clase_mo2 == _REPARSE_INDETERMINADO else det_storage)
            )
            checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.INDETERMINATE,
                reason=PreflightReasonCode.REPARSE_INSPECTION_FAILED,
                detail=detalle or "fallo al inspeccionar reparse points en rutas base o modorganizer.ini",
            )
            reasons.append(PreflightReasonCode.REPARSE_INSPECTION_FAILED)
        else:
            checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail="rutas base y modorganizer.ini libres de reparse points y alias",
            )

    # -------------------------------------------------------------------------
    # 2. Configuración de MO2 (modorganizer.ini legible, formato Qt y estabilidad)
    # -------------------------------------------------------------------------
    game_path_obs_str: str | None = None
    ejecutables_mo2: tuple[Mo2Executable, ...] = ()
    stat_pre = None
    es_archivo = False
    try:
        es_archivo = ini_path.is_file()
    except OSError as exc:
        checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
            check=PreflightCheckCode.MO2_CONFIGURATION,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.MO2_INI_UNREADABLE,
            detail=f"error al acceder a modorganizer.ini: {exc}",
        )
        reasons.append(PreflightReasonCode.MO2_INI_UNREADABLE)

    if PreflightCheckCode.MO2_CONFIGURATION not in checks_map:
        if not es_archivo:
            checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
                check=PreflightCheckCode.MO2_CONFIGURATION,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MO2_INI_MISSING,
                detail=f"no se encontró modorganizer.ini en '{mo2_dir}'",
            )
            reasons.append(PreflightReasonCode.MO2_INI_MISSING)
        else:
            try:
                stat_pre = ini_path.stat()
            except OSError as exc:
                checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
                    check=PreflightCheckCode.MO2_CONFIGURATION,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.MO2_INI_UNREADABLE,
                    detail=f"error al acceder a modorganizer.ini: {exc}",
                )
                reasons.append(PreflightReasonCode.MO2_INI_UNREADABLE)

        if stat_pre is not None:
            evidencia_gp = leer_game_path_de_mo2(ini_path)
            ejecutables_mo2 = leer_ejecutables_de_mo2(ini_path)
            try:
                stat_post = ini_path.stat()
                if (stat_pre.st_ino, stat_pre.st_dev, stat_pre.st_mtime_ns) != (
                    stat_post.st_ino,
                    stat_post.st_dev,
                    stat_post.st_mtime_ns,
                ):
                    checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
                        check=PreflightCheckCode.MO2_CONFIGURATION,
                        status=PreflightCheckStatus.FAIL,
                        reason=PreflightReasonCode.MO2_INI_UNREADABLE,
                        detail="modorganizer.ini modificado concurrentemente durante la lectura",
                    )
                    reasons.append(PreflightReasonCode.MO2_INI_UNREADABLE)
                elif evidencia_gp.state is not ObservationState.OBSERVED or evidencia_gp.path is None:
                    checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
                        check=PreflightCheckCode.MO2_CONFIGURATION,
                        status=PreflightCheckStatus.FAIL,
                        reason=PreflightReasonCode.MO2_INI_UNREADABLE,
                        detail=evidencia_gp.detail or "gamePath no decodificable en [General]",
                    )
                    reasons.append(PreflightReasonCode.MO2_INI_UNREADABLE)
                else:
                    game_path_obs_str = _normalizar(evidencia_gp.path)
                    checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
                        check=PreflightCheckCode.MO2_CONFIGURATION,
                        status=PreflightCheckStatus.PASS,
                        reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                        detail=f"gamePath decodificado correctamente: '{evidencia_gp.path}'",
                    )
            except OSError as exc:
                checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
                    check=PreflightCheckCode.MO2_CONFIGURATION,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.MO2_INI_UNREADABLE,
                    detail=f"error al verificar modorganizer.ini: {exc}",
                )
                reasons.append(PreflightReasonCode.MO2_INI_UNREADABLE)

    # -------------------------------------------------------------------------
    # 3. Concurrencia de procesos (sin Skyrim, sin SKSE, sin MO2)
    # -------------------------------------------------------------------------
    procesos_snap = observadores.processes()
    if procesos_snap.indeterminate:
        checks_map[PreflightCheckCode.PROCESS_CONCURRENCY] = PreflightCheckResult(
            check=PreflightCheckCode.PROCESS_CONCURRENCY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.PROCESS_TABLE_UNREADABLE,
            detail=procesos_snap.detail or "tabla de procesos del sistema ilegible",
        )
        reasons.append(PreflightReasonCode.PROCESS_TABLE_UNREADABLE)
    else:
        juegos_vivos = [
            p for p in procesos_snap.processes if p.kind in (LiveProcessKind.GAME, LiveProcessKind.SKSE_LOADER)
        ]
        mo2_vivos = [p for p in procesos_snap.processes if p.kind is LiveProcessKind.MO2]

        if juegos_vivos:
            pids = ", ".join(str(p.pid) for p in juegos_vivos)
            checks_map[PreflightCheckCode.PROCESS_CONCURRENCY] = PreflightCheckResult(
                check=PreflightCheckCode.PROCESS_CONCURRENCY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.LIVE_GAME_RUNNING,
                detail=f"procesos de juego o loader activos (pids: {pids})",
            )
            reasons.append(PreflightReasonCode.LIVE_GAME_RUNNING)
        elif mo2_vivos:
            pids = ", ".join(str(p.pid) for p in mo2_vivos)
            checks_map[PreflightCheckCode.PROCESS_CONCURRENCY] = PreflightCheckResult(
                check=PreflightCheckCode.PROCESS_CONCURRENCY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.LIVE_MO2_RUNNING,
                detail=f"ModOrganizer.exe activo en memoria (pids: {pids})",
            )
            reasons.append(PreflightReasonCode.LIVE_MO2_RUNNING)
        else:
            checks_map[PreflightCheckCode.PROCESS_CONCURRENCY] = PreflightCheckResult(
                check=PreflightCheckCode.PROCESS_CONCURRENCY,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail="sin procesos de Skyrim, SKSE ni MO2 en ejecución",
            )

    # -------------------------------------------------------------------------
    # 4. Managed Source discovery y verificación de señales del proveedor Steam
    # -------------------------------------------------------------------------
    ms_root_resolved: pathlib.Path | None = None
    try:
        ms_result = discover_managed_source(
            provider=ManagedSourceProvider.STEAM,
            game_key="skyrimse",
            appid=SKYRIM_SE_APPID,
            explicit_root=managed_source_root,
            steam_roots=steam_roots,
        )
    except FrozenRuntimeError as exc:
        ms_result = ManagedSourceDiscoveryResult(
            state=DiscoveryState.INVALID,
            message=str(exc),
        )

    if ms_result.state is DiscoveryState.NOT_FOUND:
        checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
            check=PreflightCheckCode.MANAGED_SOURCE,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND,
            detail=ms_result.message or "Managed Source no encontrada",
        )
        reasons.append(PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND)
    elif ms_result.state is DiscoveryState.AMBIGUOUS:
        checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
            check=PreflightCheckCode.MANAGED_SOURCE,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.MANAGED_SOURCE_AMBIGUOUS,
            detail=ms_result.message or "Múltiples Managed Sources ambiguas",
        )
        reasons.append(PreflightReasonCode.MANAGED_SOURCE_AMBIGUOUS)
    elif ms_result.state is DiscoveryState.INVALID or ms_result.source is None:
        es_ausente = managed_source_root is not None and not pathlib.Path(managed_source_root).exists()
        codigo_ms = (
            PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND if es_ausente else PreflightReasonCode.MANAGED_SOURCE_INVALID
        )
        checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
            check=PreflightCheckCode.MANAGED_SOURCE,
            status=PreflightCheckStatus.FAIL,
            reason=codigo_ms,
            detail=ms_result.message or "Managed Source inválida o ausente",
        )
        reasons.append(codigo_ms)
    else:
        ms_root_resolved = ms_result.source.root
        # P2-A & Final Coherence Gate C: Auditoría real de reposo y actividad en manifest y directorios
        obs_steam = observe_provider_activity(ms_result.source)
        if not obs_steam.manifest_readable:
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail=f"manifest de Steam ilegible o malformado: {obs_steam.manifest_parse_error}",
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
        elif not obs_steam.buildid or not obs_steam.buildid.strip():
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail="manifest de Steam sin buildid válido",
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
        else:
            estado_prov = evaluate_provider_observation(obs_steam)
            if estado_prov is not ProviderObservationState.IDLE:
                det_prov = (
                    "Steam en estado activo (descarga o actualización en curso)"
                    if estado_prov is ProviderObservationState.ACTIVE
                    else "estado de Steam indeterminado"
                )
                checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                    check=PreflightCheckCode.MANAGED_SOURCE,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                    detail=det_prov,
                )
                reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
            else:
                checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                    check=PreflightCheckCode.MANAGED_SOURCE,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail=f"Managed Source verificada en '{ms_root_resolved}' (buildid {obs_steam.buildid}, state_flags {obs_steam.state_flags})",
                )

    # -------------------------------------------------------------------------
    # 5. Resolución y carga del RuntimeCloneRecord (P2-D)
    # -------------------------------------------------------------------------
    target_clone_id = clone_id
    persisted_desired_gen_id: str | None = None
    if target_clone_id is None:
        try:
            carga_active = load_frozen_runtime_state(active_state_path(storage))
            if carga_active.found and carga_active.state is not None:
                target_clone_id = carga_active.state.desired_clone_id
                persisted_desired_gen_id = carga_active.state.desired_active_generation
        except StateCorruptError as exc:
            checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_CORRUPT,
                detail=f"active.json corrupto o malformado: {exc}",
            )
            reasons.append(PreflightReasonCode.CLONE_RECORD_CORRUPT)
            checks_ord = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
            return BinderPreflightAssessment(
                verdict=BinderPreflightVerdict.BLOCKED,
                reasons=tuple(reasons),
                checks=checks_ord,
                managed_source_root=ms_root_resolved,
                mo2_instance_dir=mo2_dir,
                observed_mo2_game_path=game_path_obs_str,
                assessed_at_ns=sello,
                notes=(),
            )
        except StateSchemaError as exc:
            checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID,
                detail=f"active.json con schema inválido: {exc}",
            )
            reasons.append(PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID)
            checks_ord = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
            return BinderPreflightAssessment(
                verdict=BinderPreflightVerdict.BLOCKED,
                reasons=tuple(reasons),
                checks=checks_ord,
                managed_source_root=ms_root_resolved,
                mo2_instance_dir=mo2_dir,
                observed_mo2_game_path=game_path_obs_str,
                assessed_at_ns=sello,
                notes=(),
            )
        except (FrozenRuntimeStorageError, OSError) as exc:
            checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_CORRUPT,
                detail=f"error al acceder a active.json: {exc}",
            )
            reasons.append(PreflightReasonCode.CLONE_RECORD_CORRUPT)
            checks_ord = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
            return BinderPreflightAssessment(
                verdict=BinderPreflightVerdict.BLOCKED,
                reasons=tuple(reasons),
                checks=checks_ord,
                managed_source_root=ms_root_resolved,
                mo2_instance_dir=mo2_dir,
                observed_mo2_game_path=game_path_obs_str,
                assessed_at_ns=sello,
                notes=(),
            )

    if not target_clone_id:
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_ID_UNSPECIFIED,
            detail="no se especificó clone_id ni existe desired_clone_id en active.json",
        )
        reasons.append(PreflightReasonCode.CLONE_ID_UNSPECIFIED)
        checks_ord = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
        return BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=tuple(reasons),
            checks=checks_ord,
            managed_source_root=ms_root_resolved,
            mo2_instance_dir=mo2_dir,
            observed_mo2_game_path=game_path_obs_str,
            assessed_at_ns=sello,
            notes=(),
        )

    try:
        validar_clone_id(target_clone_id)
    except InvalidCloneIdError as exc:
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_ID_INVALID,
            detail=str(exc),
        )
        reasons.append(PreflightReasonCode.CLONE_ID_INVALID)
        checks_ord = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
        return BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=tuple(reasons),
            checks=checks_ord,
            target_clone_id=target_clone_id,
            managed_source_root=ms_root_resolved,
            mo2_instance_dir=mo2_dir,
            observed_mo2_game_path=game_path_obs_str,
            assessed_at_ns=sello,
            notes=(),
        )

    record: RuntimeCloneRecord | None = None
    try:
        record = cargar_runtime_clone_record(storage, target_clone_id)
    except (RuntimeCloneCorruptError, StateCorruptError) as exc:
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_RECORD_CORRUPT,
            detail=str(exc),
        )
        reasons.append(PreflightReasonCode.CLONE_RECORD_CORRUPT)
    except (RuntimeCloneSchemaError, StateSchemaError) as exc:
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID,
            detail=str(exc),
        )
        reasons.append(PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID)
    except (RuntimeCloneNotFoundError, FrozenRuntimeError, OSError) as exc:
        reason_clone = (
            PreflightReasonCode.CLONE_RECORD_MISSING
            if isinstance(exc, RuntimeCloneNotFoundError)
            else PreflightReasonCode.CLONE_RECORD_CORRUPT
        )
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.FAIL,
            reason=reason_clone,
            detail=f"error de lectura de clone record: {exc}",
        )
        reasons.append(reason_clone)

    if record is None:
        if PreflightCheckCode.CLONE_RECORD not in checks_map:
            checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_MISSING,
                detail=f"no existe registro para clone_id '{target_clone_id}'",
            )
            reasons.append(PreflightReasonCode.CLONE_RECORD_MISSING)
        checks_ord = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
        return BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=tuple(reasons),
            checks=checks_ord,
            target_clone_id=target_clone_id,
            managed_source_root=ms_root_resolved,
            mo2_instance_dir=mo2_dir,
            observed_mo2_game_path=game_path_obs_str,
            assessed_at_ns=sello,
            notes=(),
        )

    # Validar admitted role
    if record.admitted_role != CANONICAL_ADMITTED_ROLE:
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_ROLE_INVALID,
            detail=f"admitted_role '{record.admitted_role}' != '{CANONICAL_ADMITTED_ROLE}'",
        )
        reasons.append(PreflightReasonCode.CLONE_ROLE_INVALID)
    else:
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.PASS,
            reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
            detail=f"clone record cargado válidamente para '{target_clone_id}'",
        )

    # -------------------------------------------------------------------------
    # 6. Ciclo de vida del Clone (debe ser estrictamente PROVISIONED)
    # -------------------------------------------------------------------------
    if record.lifecycle is not RuntimeCloneLifecycle.PROVISIONED:
        checks_map[PreflightCheckCode.CLONE_LIFECYCLE] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_LIFECYCLE,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_LIFECYCLE_NOT_PROVISIONED,
            detail=f"lifecycle es '{record.lifecycle}' (exige PROVISIONED)",
        )
        reasons.append(PreflightReasonCode.CLONE_LIFECYCLE_NOT_PROVISIONED)
    else:
        checks_map[PreflightCheckCode.CLONE_LIFECYCLE] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_LIFECYCLE,
            status=PreflightCheckStatus.PASS,
            reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
            detail="clone en ciclo de vida PROVISIONED",
        )

    clone_root = pathlib.Path(os.path.abspath(os.fspath(record.root_path)))

    # -------------------------------------------------------------------------
    # 7. Admisión física y contención de rutas del clone (actualiza PATH_SAFETY)
    # -------------------------------------------------------------------------
    path_safety_cur = checks_map[PreflightCheckCode.PATH_SAFETY]
    if path_safety_cur.status is PreflightCheckStatus.PASS:
        if _contiene_alias_83(clone_root):
            reasons.append(PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED)
            checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
                detail=f"clone_root contiene alias 8.3: '{clone_root}'",
            )
        else:
            clase_clone, det_clone = _primer_reparse(clone_root)
            if clase_clone == _REPARSE_HAY:
                reasons.append(PreflightReasonCode.REPARSE_POINT_DETECTED)
                checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.REPARSE_POINT_DETECTED,
                    detail=det_clone or "reparse point en clone_root",
                )
            elif clase_clone == _REPARSE_INDETERMINADO:
                reasons.append(PreflightReasonCode.REPARSE_INSPECTION_FAILED)
                checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.INDETERMINATE,
                    reason=PreflightReasonCode.REPARSE_INSPECTION_FAILED,
                    detail=det_clone or "inspección de reparse en clone_root indeterminada",
                )
            elif ms_root_resolved is not None and _normalizar(clone_root) == _normalizar(ms_root_resolved):
                reasons.append(PreflightReasonCode.PATHS_COLLIDE)
                checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATHS_COLLIDE,
                    detail="clone_root coincide con la Managed Source",
                )
            elif ms_root_resolved is not None and (
                _es_igual_o_hijo(clone_root, ms_root_resolved) or _es_igual_o_hijo(ms_root_resolved, clone_root)
            ):
                reasons.append(PreflightReasonCode.PATH_CONTAINMENT_VIOLATION)
                checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
                    detail="clone_root se solapa o está contenido dentro de la Managed Source",
                )
            elif _es_igual_o_hijo(clone_root, storage):
                reasons.append(PreflightReasonCode.PATH_CONTAINMENT_VIOLATION)
                checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
                    detail="clone_root está contenido dentro del Frozen Runtime Storage (versions/state)",
                )
            elif _normalizar(clone_root) == _normalizar(mo2_dir) or _es_igual_o_hijo(clone_root, mo2_dir):
                reasons.append(PreflightReasonCode.PATHS_COLLIDE)
                checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATHS_COLLIDE,
                    detail="clone_root coincide o está contenido dentro de mo2_instance_dir",
                )
            else:
                try:
                    admitir_clone_root_path(
                        clone_root,
                        storage_root=storage,
                        managed_source_root=ms_root_resolved,
                    )
                except RuntimeCloneAdmissionError as exc:
                    msg = str(exc).lower()
                    if "8.3" in msg or "~" in msg:
                        motivo_refinado = PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED
                    elif any(k in msg for k in ("steam", "storage", "versions", "common")):
                        motivo_refinado = PreflightReasonCode.PATH_CONTAINMENT_VIOLATION
                    else:
                        motivo_refinado = PreflightReasonCode.CLONE_ADMISSION_REJECTED

                    if motivo_refinado not in reasons:
                        reasons.append(motivo_refinado)

                    checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                        check=PreflightCheckCode.PATH_SAFETY,
                        status=PreflightCheckStatus.FAIL,
                        reason=motivo_refinado,
                        detail=f"admitir_clone_root_path rechazó la ruta: {exc}",
                    )

    # -------------------------------------------------------------------------
    # 8. Integridad de la Generation de origen (debe ser estrictamente VALID)
    # -------------------------------------------------------------------------
    gen_id = record.source_generation_id
    gen_meta_ok = False
    if persisted_desired_gen_id is not None and gen_id != persisted_desired_gen_id:
        checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
            check=PreflightCheckCode.GENERATION_INTEGRITY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.GENERATION_INVALID,
            detail=(
                f"linaje incoherente: la Generation de origen del clone '{gen_id}' "
                f"difiere de la deseada en active.json ('{persisted_desired_gen_id}')"
            ),
        )
        reasons.append(PreflightReasonCode.GENERATION_INVALID)
    else:
        try:
            leer_generation_metadata(storage, gen_id)
            gen_meta_ok = True
        except (FrozenRuntimeError, OSError) as exc:
            checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                check=PreflightCheckCode.GENERATION_INTEGRITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.GENERATION_NOT_FOUND,
                detail=f"metadata de generation '{gen_id}' no legible: {exc}",
            )
            reasons.append(PreflightReasonCode.GENERATION_NOT_FOUND)

    if gen_meta_ok:
        try:
            gen_verif = verificar_generation(storage, gen_id)
            if gen_verif.state is GenerationVerificationState.DRIFTED:
                checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                    check=PreflightCheckCode.GENERATION_INTEGRITY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.GENERATION_DRIFTED,
                    detail=f"generation '{gen_id}' sufrió drift respecto a su digest registrado",
                )
                reasons.append(PreflightReasonCode.GENERATION_DRIFTED)
            elif gen_verif.state is not GenerationVerificationState.VALID:
                checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                    check=PreflightCheckCode.GENERATION_INTEGRITY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.GENERATION_INVALID,
                    detail=f"generation '{gen_id}' no es válida (estado: {gen_verif.state})",
                )
                reasons.append(PreflightReasonCode.GENERATION_INVALID)
            else:
                checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                    check=PreflightCheckCode.GENERATION_INTEGRITY,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail=f"generation '{gen_id}' verificada estrictamente como VALID",
                )
        except (FrozenRuntimeError, OSError) as exc:
            checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                check=PreflightCheckCode.GENERATION_INTEGRITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.GENERATION_INVALID,
                detail=f"error al verificar generation '{gen_id}': {exc}",
            )
            reasons.append(PreflightReasonCode.GENERATION_INVALID)

    # -------------------------------------------------------------------------
    # 9. Integridad física y linaje del Runtime Clone
    # -------------------------------------------------------------------------
    try:
        clone_verif = verificar_runtime_clone(
            storage,
            target_clone_id,
            managed_source_root=ms_root_resolved,
        )
        if clone_verif.state is GenerationVerificationState.VALID:
            checks_map[PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail="clone físicamente independiente, hashes críticos coincidentes y st_nlink==1",
            )
        elif clone_verif.state is GenerationVerificationState.INDETERMINATE:
            checks_map[PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
                status=PreflightCheckStatus.INDETERMINATE,
                reason=PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INDETERMINATE,
                detail=f"integridad física del clone indeterminada: {clone_verif.message}",
            )
            reasons.append(PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INDETERMINATE)
        else:
            checks_map[PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID,
                detail=f"verificar_runtime_clone retornó '{clone_verif.state}': {clone_verif.message}",
            )
            reasons.append(PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID)
    except (FrozenRuntimeError, OSError) as exc:
        checks_map[PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID,
            detail=f"error al verificar integridad del clone: {exc}",
        )
        reasons.append(PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID)

    # -------------------------------------------------------------------------
    # 10. Compatibilidad de Runtime y SKSE en el clone
    # -------------------------------------------------------------------------
    try:
        identidad = observadores.identity(clone_root)
        if not identidad.game_version:
            checks_map[PreflightCheckCode.RUNTIME_COMPATIBILITY] = PreflightCheckResult(
                check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.IDENTITY_UNREADABLE,
                detail="ejecutable SkyrimSE.exe en el clone no declara versión PE observable",
            )
            reasons.append(PreflightReasonCode.IDENTITY_UNREADABLE)
        else:
            compat = observadores.compatibility(clone_root)
            if compat.state is RuntimeCompatibility.INCOMPATIBLE:
                checks_map[PreflightCheckCode.RUNTIME_COMPATIBILITY] = PreflightCheckResult(
                    check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.SKSE_INCOMPATIBLE,
                    detail=compat.reason or "SKSE incompatible con SkyrimSE.exe",
                )
                reasons.append(PreflightReasonCode.SKSE_INCOMPATIBLE)
            elif compat.state is not RuntimeCompatibility.COMPATIBLE:
                checks_map[PreflightCheckCode.RUNTIME_COMPATIBILITY] = PreflightCheckResult(
                    check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.COMPATIBILITY_UNKNOWN,
                    detail=compat.reason or "compatibilidad SKSE desconocida (UNKNOWN != COMPATIBLE)",
                )
                reasons.append(PreflightReasonCode.COMPATIBILITY_UNKNOWN)
            else:
                checks_map[PreflightCheckCode.RUNTIME_COMPATIBILITY] = PreflightCheckResult(
                    check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail=f"runtime y SKSE compatibles ({compat.game_version})",
                )
    except (RuntimeObservationError, FrozenRuntimeError, OSError) as exc:
        checks_map[PreflightCheckCode.RUNTIME_COMPATIBILITY] = PreflightCheckResult(
            check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.COMPATIBILITY_UNKNOWN,
            detail=f"error al evaluar compatibilidad de runtime: {exc}",
        )
        reasons.append(PreflightReasonCode.COMPATIBILITY_UNKNOWN)

    # -------------------------------------------------------------------------
    # 11. Coherencia de configuración multi-superficie (P1-A, P1-B, Final Coherence A & B)
    # -------------------------------------------------------------------------
    ms_norm = _normalizar(ms_root_resolved) if ms_root_resolved is not None else None
    clone_norm = _normalizar(clone_root) if clone_root is not None else None

    coherencia_fail: PreflightReasonCode | None = None
    coherencia_detail: str = ""

    cfg_norm: str | None = None
    # 11.1 Config.skyrim_path
    if exigir_config_skyrim_path and not config_skyrim_path:
        coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
        coherencia_detail = "config_skyrim_path es obligatoria para el switch pero no fue provista"
    elif config_skyrim_path:
        cfg_path = pathlib.Path(os.path.abspath(os.fspath(config_skyrim_path)))
        if _contiene_alias_83(cfg_path):
            coherencia_fail = PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED
            coherencia_detail = f"config_skyrim_path contiene alias 8.3: '{config_skyrim_path}'"
        else:
            cfg_norm = _normalizar(config_skyrim_path)
            # Debe coincidir o con la Managed Source (pre-switch) o con el Clone (post-switch)
            if cfg_norm != ms_norm and cfg_norm != clone_norm:
                coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
                coherencia_detail = (
                    f"config_skyrim_path ('{config_skyrim_path}') apunta a un tercer árbol "
                    f"distinto de Managed Source ('{ms_root_resolved}') y Clone ('{clone_root}')"
                )

    # 11.2 MO2 gamePath (debe ser Managed Source en pre-switch o Clone en post-switch)
    gp_norm: str | None = None
    if coherencia_fail is None and game_path_obs_str is not None:
        gp_norm = _normalizar(game_path_obs_str)
        if gp_norm != ms_norm and gp_norm != clone_norm:
            coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
            coherencia_detail = (
                f"MO2 gamePath ('{game_path_obs_str}') apunta a un tercer directorio "
                f"distinto de Managed Source ('{ms_root_resolved}') y Clone ('{clone_root}')"
            )

    # 11.3 Coherencia cruzada entre Config.skyrim_path y MO2 gamePath (Gate A)
    if coherencia_fail is None and cfg_norm is not None and gp_norm is not None and cfg_norm != gp_norm:
        coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
        coherencia_detail = (
            f"divergencia cruzada entre superficies: Config.skyrim_path ('{config_skyrim_path}') "
            f"difiere de MO2 gamePath ('{game_path_obs_str}')"
        )

    # 11.4 Variable de entorno SKYRIM_PATH (Gate B, reutilizando reglas P5-A)
    entorno = os.environ if environ is None else environ
    env_skyrim_raw = (entorno.get("SKYRIM_PATH") or "").strip()
    if coherencia_fail is None and env_skyrim_raw:
        env_path = pathlib.Path(os.path.abspath(os.fspath(env_skyrim_raw)))
        if _contiene_alias_83(env_path):
            coherencia_fail = PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED
            coherencia_detail = f"SKYRIM_PATH en el entorno contiene alias 8.3: '{env_skyrim_raw}'"
        else:
            env_norm = _normalizar(env_skyrim_raw)
            if env_norm != ms_norm and env_norm != clone_norm:
                coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
                coherencia_detail = (
                    f"variable de entorno SKYRIM_PATH ('{env_skyrim_raw}') apunta a un tercer árbol "
                    f"distinto de Managed Source ('{ms_root_resolved}') y Clone ('{clone_root}')"
                )
            elif gp_norm is not None and env_norm != gp_norm:
                coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
                coherencia_detail = (
                    f"variable de entorno SKYRIM_PATH ('{env_skyrim_raw}') difiere de "
                    f"MO2 gamePath ('{game_path_obs_str}')"
                )
            elif cfg_norm is not None and env_norm != cfg_norm:
                coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
                coherencia_detail = (
                    f"variable de entorno SKYRIM_PATH ('{env_skyrim_raw}') difiere de "
                    f"Config.skyrim_path ('{config_skyrim_path}')"
                )

    # 11.5 customExecutables de MO2 (P1-B)
    if coherencia_fail is None and game_path_obs_str is not None and ejecutables_mo2:
        lanzadores_fuera = _lanzadores_del_juego_fuera_de(game_path_obs_str, ejecutables_mo2)
        if lanzadores_fuera:
            coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
            coherencia_detail = (
                f"lanzadores de juego en [customExecutables] registrados fuera de gamePath ('{game_path_obs_str}'): "
                + "; ".join(lanzadores_fuera)
            )

    if coherencia_fail is not None:
        checks_map[PreflightCheckCode.CONFIG_COHERENCE] = PreflightCheckResult(
            check=PreflightCheckCode.CONFIG_COHERENCE,
            status=PreflightCheckStatus.FAIL,
            reason=coherencia_fail,
            detail=coherencia_detail,
        )
        reasons.append(coherencia_fail)
    else:
        checks_map[PreflightCheckCode.CONFIG_COHERENCE] = PreflightCheckResult(
            check=PreflightCheckCode.CONFIG_COHERENCE,
            status=PreflightCheckStatus.PASS,
            reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
            detail="configuración de Sky-Claw, MO2 y ejecutables coherente con el runtime admitido",
        )

    # -------------------------------------------------------------------------
    # Veredicto final
    # -------------------------------------------------------------------------
    checks_ordenados = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
    todos_pass = len(checks_ordenados) == len(PreflightCheckCode) and all(
        c.status is PreflightCheckStatus.PASS for c in checks_ordenados
    )
    final_reasons: tuple[PreflightReasonCode, ...]
    if todos_pass and not reasons:
        verdict = BinderPreflightVerdict.READY
        final_reasons = (PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,)
    else:
        verdict = BinderPreflightVerdict.BLOCKED
        # Conservar orden único de motivos de fallo
        vistos: set[PreflightReasonCode] = set()
        final_reasons_list: list[PreflightReasonCode] = []
        for r in reasons:
            if r not in vistos and r is not PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED:
                vistos.add(r)
                final_reasons_list.append(r)
        final_reasons = tuple(final_reasons_list)

    return BinderPreflightAssessment(
        verdict=verdict,
        reasons=final_reasons,
        checks=checks_ordenados,
        target_clone_id=target_clone_id,
        target_clone_root=clone_root,
        source_generation_id=gen_id,
        managed_source_root=ms_root_resolved,
        mo2_instance_dir=mo2_dir,
        observed_mo2_game_path=game_path_obs_str,
        assessed_at_ns=sello,
        notes=(),
    )


__all__ = [
    "BinderPreflightAssessment",
    "BinderPreflightVerdict",
    "PreflightCheckCode",
    "PreflightCheckResult",
    "PreflightCheckStatus",
    "PreflightReasonCode",
    "evaluar_binder_preflight",
]
