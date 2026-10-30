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
    _normalizar,
    _primer_reparse,
    default_runtime_probes,
)
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeError,
    InvalidCloneIdError,
    RuntimeCloneAdmissionError,
    RuntimeCloneCorruptError,
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
)
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import active_state_path
from sky_claw.local.frozen_runtime.storage_models import (
    GenerationVerificationState,
)

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
        """Invariante: True solo si el veredicto es READY y TODOS los checks son PASS."""
        return self.verdict is BinderPreflightVerdict.READY and all(
            c.status is PreflightCheckStatus.PASS for c in self.checks
        )


def evaluar_binder_preflight(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    clone_id: str | None = None,
    managed_source_root: pathlib.Path | None = None,
    steam_roots: tuple[str, ...] = STEAM_DEFAULT_PATHS,
    config_skyrim_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
    assessed_at_ns: int | None = None,
) -> BinderPreflightAssessment:
    """Evalúa exhaustivamente las precondiciones para conmutar MO2 hacia un Runtime Clone.

    Operación 100% READ-ONLY. No escribe en disco ni altera la configuración de MO2.
    """
    observadores = probes if probes is not None else default_runtime_probes()
    sello = assessed_at_ns if assessed_at_ns is not None else time.time_ns()

    checks: list[PreflightCheckResult] = []
    reasons: list[PreflightReasonCode] = []
    notas: list[str] = []

    storage = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    mo2_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))

    # -------------------------------------------------------------------------
    # 1. Comprobación de seguridad en rutas base: mo2_instance_dir y storage_root
    # -------------------------------------------------------------------------
    if _contiene_alias_83(storage) or _contiene_alias_83(mo2_dir):
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
                detail="mo2_instance_dir o storage_root contienen componentes con alias 8.3",
            )
        )
        reasons.append(PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED)
    else:
        clase_mo2, det_mo2 = _primer_reparse(mo2_dir)
        clase_storage, det_storage = _primer_reparse(storage)

        if clase_mo2 == _REPARSE_HAY or clase_storage == _REPARSE_HAY:
            detalle = det_mo2 if clase_mo2 == _REPARSE_HAY else det_storage
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.REPARSE_POINT_DETECTED,
                    detail=detalle or "reparse point en mo2_instance_dir o storage_root",
                )
            )
            reasons.append(PreflightReasonCode.REPARSE_POINT_DETECTED)
        elif clase_mo2 == _REPARSE_INDETERMINADO or clase_storage == _REPARSE_INDETERMINADO:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.INDETERMINATE,
                    reason=PreflightReasonCode.REPARSE_INSPECTION_FAILED,
                    detail="fallo al inspeccionar reparse points en rutas base",
                )
            )
            reasons.append(PreflightReasonCode.REPARSE_INSPECTION_FAILED)
        else:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail="rutas base libres de reparse points y alias",
                )
            )

    # -------------------------------------------------------------------------
    # 2. Configuración de MO2 (modorganizer.ini legible y formato Qt)
    # -------------------------------------------------------------------------
    ini_path = mo2_dir / "modorganizer.ini"
    game_path_obs_str: str | None = None
    if not ini_path.is_file():
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.MO2_CONFIGURATION,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MO2_INI_MISSING,
                detail=f"no se encontró modorganizer.ini en '{mo2_dir}'",
            )
        )
        reasons.append(PreflightReasonCode.MO2_INI_MISSING)
    else:
        evidencia_gp = leer_game_path_de_mo2(ini_path)
        if evidencia_gp.state is not ObservationState.OBSERVED or evidencia_gp.path is None:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.MO2_CONFIGURATION,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.MO2_INI_UNREADABLE,
                    detail=evidencia_gp.detail or "gamePath no decodificable en [General]",
                )
            )
            reasons.append(PreflightReasonCode.MO2_INI_UNREADABLE)
        else:
            game_path_obs_str = _normalizar(evidencia_gp.path)
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.MO2_CONFIGURATION,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail=f"gamePath decodificado correctamente: '{evidencia_gp.path}'",
                )
            )

    # -------------------------------------------------------------------------
    # 3. Concurrencia de procesos (sin Skyrim, sin SKSE, sin MO2)
    # -------------------------------------------------------------------------
    procesos_snap = observadores.processes()
    if procesos_snap.indeterminate:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PROCESS_CONCURRENCY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.PROCESS_TABLE_UNREADABLE,
                detail=procesos_snap.detail or "tabla de procesos del sistema ilegible",
            )
        )
        reasons.append(PreflightReasonCode.PROCESS_TABLE_UNREADABLE)
    else:
        juegos_vivos = [
            p for p in procesos_snap.processes if p.kind in (LiveProcessKind.GAME, LiveProcessKind.SKSE_LOADER)
        ]
        mo2_vivos = [p for p in procesos_snap.processes if p.kind is LiveProcessKind.MO2]

        if juegos_vivos:
            pids = ", ".join(str(p.pid) for p in juegos_vivos)
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PROCESS_CONCURRENCY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.LIVE_GAME_RUNNING,
                    detail=f"procesos de juego o loader activos (pids: {pids})",
                )
            )
            reasons.append(PreflightReasonCode.LIVE_GAME_RUNNING)
        elif mo2_vivos:
            pids = ", ".join(str(p.pid) for p in mo2_vivos)
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PROCESS_CONCURRENCY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.LIVE_MO2_RUNNING,
                    detail=f"ModOrganizer.exe activo en memoria (pids: {pids})",
                )
            )
            reasons.append(PreflightReasonCode.LIVE_MO2_RUNNING)
        else:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PROCESS_CONCURRENCY,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail="sin procesos de Skyrim, SKSE ni MO2 en ejecución",
                )
            )

    # -------------------------------------------------------------------------
    # 4. Managed Source discovery (evidencia confiable de Steam)
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
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND,
                detail=ms_result.message or "Managed Source no encontrada",
            )
        )
        reasons.append(PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND)
    elif ms_result.state is DiscoveryState.AMBIGUOUS:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_AMBIGUOUS,
                detail=ms_result.message or "Múltiples Managed Sources ambiguas",
            )
        )
        reasons.append(PreflightReasonCode.MANAGED_SOURCE_AMBIGUOUS)
    elif ms_result.state is DiscoveryState.INVALID or ms_result.source is None:
        es_ausente = managed_source_root is not None and not pathlib.Path(managed_source_root).exists()
        codigo_ms = (
            PreflightReasonCode.MANAGED_SOURCE_NOT_FOUND if es_ausente else PreflightReasonCode.MANAGED_SOURCE_INVALID
        )
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=codigo_ms,
                detail=ms_result.message or "Managed Source inválida o ausente",
            )
        )
        reasons.append(codigo_ms)
    else:
        ms_root_resolved = ms_result.source.root
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail=f"Managed Source verificada en '{ms_root_resolved}'",
            )
        )

    # -------------------------------------------------------------------------
    # 5. Resolución y carga del RuntimeCloneRecord
    # -------------------------------------------------------------------------
    target_clone_id = clone_id
    if target_clone_id is None:
        carga_active = load_frozen_runtime_state(active_state_path(storage))
        if carga_active.found and carga_active.state is not None:
            target_clone_id = carga_active.state.desired_clone_id

    if not target_clone_id:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_ID_UNSPECIFIED,
                detail="no se especificó clone_id ni existe desired_clone_id en active.json",
            )
        )
        reasons.append(PreflightReasonCode.CLONE_ID_UNSPECIFIED)
        return BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=tuple(reasons),
            checks=tuple(checks),
            managed_source_root=ms_root_resolved,
            mo2_instance_dir=mo2_dir,
            observed_mo2_game_path=game_path_obs_str,
            assessed_at_ns=sello,
            notes=tuple(notas),
        )

    try:
        validar_clone_id(target_clone_id)
    except InvalidCloneIdError as exc:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_ID_INVALID,
                detail=str(exc),
            )
        )
        reasons.append(PreflightReasonCode.CLONE_ID_INVALID)
        return BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=tuple(reasons),
            checks=tuple(checks),
            target_clone_id=target_clone_id,
            managed_source_root=ms_root_resolved,
            mo2_instance_dir=mo2_dir,
            observed_mo2_game_path=game_path_obs_str,
            assessed_at_ns=sello,
            notes=tuple(notas),
        )

    record: RuntimeCloneRecord | None = None
    try:
        record = cargar_runtime_clone_record(storage, target_clone_id)
    except (RuntimeCloneCorruptError, StateCorruptError) as exc:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_CORRUPT,
                detail=str(exc),
            )
        )
        reasons.append(PreflightReasonCode.CLONE_RECORD_CORRUPT)
    except (RuntimeCloneSchemaError, StateSchemaError) as exc:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID,
                detail=str(exc),
            )
        )
        reasons.append(PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID)
    except (FrozenRuntimeError, OSError) as exc:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_CORRUPT,
                detail=f"error de lectura de clone record: {exc}",
            )
        )
        reasons.append(PreflightReasonCode.CLONE_RECORD_CORRUPT)

    if record is None and not any(c.check is PreflightCheckCode.CLONE_RECORD for c in checks):
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_MISSING,
                detail=f"no existe registro para clone_id '{target_clone_id}'",
            )
        )
        reasons.append(PreflightReasonCode.CLONE_RECORD_MISSING)

    if record is None:
        return BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=tuple(reasons),
            checks=tuple(checks),
            target_clone_id=target_clone_id,
            managed_source_root=ms_root_resolved,
            mo2_instance_dir=mo2_dir,
            observed_mo2_game_path=game_path_obs_str,
            assessed_at_ns=sello,
            notes=tuple(notas),
        )

    # Validar admitted role
    if record.admitted_role != CANONICAL_ADMITTED_ROLE:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_ROLE_INVALID,
                detail=f"admitted_role '{record.admitted_role}' != '{CANONICAL_ADMITTED_ROLE}'",
            )
        )
        reasons.append(PreflightReasonCode.CLONE_ROLE_INVALID)
    else:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail=f"clone record cargado válidamente para '{target_clone_id}'",
            )
        )

    # -------------------------------------------------------------------------
    # 6. Ciclo de vida del Clone (debe ser estrictamente PROVISIONED)
    # -------------------------------------------------------------------------
    if record.lifecycle is not RuntimeCloneLifecycle.PROVISIONED:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_LIFECYCLE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_LIFECYCLE_NOT_PROVISIONED,
                detail=f"lifecycle es '{record.lifecycle}' (exige PROVISIONED)",
            )
        )
        reasons.append(PreflightReasonCode.CLONE_LIFECYCLE_NOT_PROVISIONED)
    else:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_LIFECYCLE,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail="clone en ciclo de vida PROVISIONED",
            )
        )

    clone_root = pathlib.Path(os.path.abspath(os.fspath(record.root_path)))

    # -------------------------------------------------------------------------
    # 7. Admisión física y contención de rutas del clone
    # -------------------------------------------------------------------------
    if _contiene_alias_83(clone_root):
        reasons.append(PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED)
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
                detail=f"clone_root contiene alias 8.3: '{clone_root}'",
            )
        )

    clase_clone, det_clone = _primer_reparse(clone_root)
    if clase_clone == _REPARSE_HAY:
        reasons.append(PreflightReasonCode.REPARSE_POINT_DETECTED)
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.REPARSE_POINT_DETECTED,
                detail=det_clone or "reparse point en clone_root",
            )
        )
    elif clase_clone == _REPARSE_INDETERMINADO:
        reasons.append(PreflightReasonCode.REPARSE_INSPECTION_FAILED)
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.INDETERMINATE,
                reason=PreflightReasonCode.REPARSE_INSPECTION_FAILED,
                detail=det_clone or "inspección de reparse en clone_root indeterminada",
            )
        )

    # Colisiones de rutas
    if ms_root_resolved is not None:
        if _normalizar(clone_root) == _normalizar(ms_root_resolved):
            reasons.append(PreflightReasonCode.PATHS_COLLIDE)
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATHS_COLLIDE,
                    detail="clone_root coincide con la Managed Source",
                )
            )
        elif _es_igual_o_hijo(clone_root, ms_root_resolved) or _es_igual_o_hijo(ms_root_resolved, clone_root):
            reasons.append(PreflightReasonCode.PATH_CONTAINMENT_VIOLATION)
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.PATH_SAFETY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
                    detail="clone_root se solapa o está contenido dentro de la Managed Source",
                )
            )

    if _es_igual_o_hijo(clone_root, storage):
        reasons.append(PreflightReasonCode.PATH_CONTAINMENT_VIOLATION)
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
                detail="clone_root está contenido dentro del Frozen Runtime Storage (versions/state)",
            )
        )

    if _normalizar(clone_root) == _normalizar(mo2_dir) or _es_igual_o_hijo(clone_root, mo2_dir):
        reasons.append(PreflightReasonCode.PATHS_COLLIDE)
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.PATHS_COLLIDE,
                detail="clone_root coincide o está contenido dentro de mo2_instance_dir",
            )
        )

    # Validar contra admitir_clone_root_path
    try:
        admitir_clone_root_path(
            clone_root,
            storage_root=storage,
            managed_source_root=ms_root_resolved,
        )
    except RuntimeCloneAdmissionError as exc:
        msg = str(exc).lower()
        if "8.3" in msg or "~" in msg:
            if PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED not in reasons:
                reasons.append(PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED)
        elif "steam" in msg or "storage" in msg or "versions" in msg or "common" in msg:
            if PreflightReasonCode.PATH_CONTAINMENT_VIOLATION not in reasons:
                reasons.append(PreflightReasonCode.PATH_CONTAINMENT_VIOLATION)
        elif PreflightReasonCode.CLONE_ADMISSION_REJECTED not in reasons:
            reasons.append(PreflightReasonCode.CLONE_ADMISSION_REJECTED)

        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_ADMISSION_REJECTED,
                detail=f"admitir_clone_root_path rechazó la ruta: {exc}",
            )
        )

    # -------------------------------------------------------------------------
    # 8. Integridad de la Generation de origen (debe ser estrictamente VALID)
    # -------------------------------------------------------------------------
    gen_id = record.source_generation_id
    gen_meta_ok = False
    try:
        leer_generation_metadata(storage, gen_id)
        gen_meta_ok = True
    except (FrozenRuntimeError, OSError) as exc:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.GENERATION_INTEGRITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.GENERATION_NOT_FOUND,
                detail=f"metadata de generation '{gen_id}' no legible: {exc}",
            )
        )
        reasons.append(PreflightReasonCode.GENERATION_NOT_FOUND)

    if gen_meta_ok:
        try:
            gen_verif = verificar_generation(storage, gen_id)
            if gen_verif.state is GenerationVerificationState.DRIFTED:
                checks.append(
                    PreflightCheckResult(
                        check=PreflightCheckCode.GENERATION_INTEGRITY,
                        status=PreflightCheckStatus.FAIL,
                        reason=PreflightReasonCode.GENERATION_DRIFTED,
                        detail=f"generation '{gen_id}' sufrió drift respecto a su digest registrado",
                    )
                )
                reasons.append(PreflightReasonCode.GENERATION_DRIFTED)
            elif gen_verif.state is not GenerationVerificationState.VALID:
                checks.append(
                    PreflightCheckResult(
                        check=PreflightCheckCode.GENERATION_INTEGRITY,
                        status=PreflightCheckStatus.FAIL,
                        reason=PreflightReasonCode.GENERATION_INVALID,
                        detail=f"generation '{gen_id}' no es válida (estado: {gen_verif.state})",
                    )
                )
                reasons.append(PreflightReasonCode.GENERATION_INVALID)
            else:
                checks.append(
                    PreflightCheckResult(
                        check=PreflightCheckCode.GENERATION_INTEGRITY,
                        status=PreflightCheckStatus.PASS,
                        reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                        detail=f"generation '{gen_id}' verificada estrictamente como VALID",
                    )
                )
        except (FrozenRuntimeError, OSError) as exc:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.GENERATION_INTEGRITY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.GENERATION_INVALID,
                    detail=f"error al verificar generation '{gen_id}': {exc}",
                )
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
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail="clone físicamente independiente, hashes críticos coincidentes y st_nlink==1",
                )
            )
        elif clone_verif.state is GenerationVerificationState.INDETERMINATE:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
                    status=PreflightCheckStatus.INDETERMINATE,
                    reason=PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INDETERMINATE,
                    detail="integridad física del clone indeterminada (filesystem no reporta inodos)",
                )
            )
            reasons.append(PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INDETERMINATE)
        else:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID,
                    detail=f"verificar_runtime_clone retornó estado '{clone_verif.state}'",
                )
            )
            reasons.append(PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID)
    except (FrozenRuntimeError, OSError) as exc:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CLONE_PHYSICAL_INTEGRITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID,
                detail=f"error al verificar integridad del clone: {exc}",
            )
        )
        reasons.append(PreflightReasonCode.CLONE_PHYSICAL_INTEGRITY_INVALID)

    # -------------------------------------------------------------------------
    # 10. Compatibilidad de Runtime y SKSE en el clone
    # -------------------------------------------------------------------------
    try:
        identidad = observadores.identity(clone_root)
        if not identidad.game_version:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.IDENTITY_UNREADABLE,
                    detail="ejecutable SkyrimSE.exe en el clone no declara versión PE observable",
                )
            )
            reasons.append(PreflightReasonCode.IDENTITY_UNREADABLE)
        else:
            compat = observadores.compatibility(clone_root)
            if compat.state is RuntimeCompatibility.INCOMPATIBLE:
                checks.append(
                    PreflightCheckResult(
                        check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                        status=PreflightCheckStatus.FAIL,
                        reason=PreflightReasonCode.SKSE_INCOMPATIBLE,
                        detail=compat.reason or "SKSE incompatible con SkyrimSE.exe",
                    )
                )
                reasons.append(PreflightReasonCode.SKSE_INCOMPATIBLE)
            elif compat.state is not RuntimeCompatibility.COMPATIBLE:
                checks.append(
                    PreflightCheckResult(
                        check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                        status=PreflightCheckStatus.FAIL,
                        reason=PreflightReasonCode.COMPATIBILITY_UNKNOWN,
                        detail=compat.reason or "compatibilidad SKSE desconocida (UNKNOWN != COMPATIBLE)",
                    )
                )
                reasons.append(PreflightReasonCode.COMPATIBILITY_UNKNOWN)
            else:
                checks.append(
                    PreflightCheckResult(
                        check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                        status=PreflightCheckStatus.PASS,
                        reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                        detail=f"runtime y SKSE compatibles ({compat.game_version})",
                    )
                )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.RUNTIME_COMPATIBILITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.COMPATIBILITY_UNKNOWN,
                detail=f"error al evaluar compatibilidad de runtime: {exc}",
            )
        )
        reasons.append(PreflightReasonCode.COMPATIBILITY_UNKNOWN)

    # -------------------------------------------------------------------------
    # 11. Coherencia de configuración de Sky-Claw
    # -------------------------------------------------------------------------
    if config_skyrim_path:
        cfg_path = pathlib.Path(os.path.abspath(os.fspath(config_skyrim_path)))
        if _contiene_alias_83(cfg_path):
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.CONFIG_COHERENCE,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
                    detail=f"config_skyrim_path contiene alias 8.3: '{config_skyrim_path}'",
                )
            )
            reasons.append(PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED)
        else:
            checks.append(
                PreflightCheckResult(
                    check=PreflightCheckCode.CONFIG_COHERENCE,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail=f"config_skyrim_path evaluado: '{config_skyrim_path}'",
                )
            )
    else:
        checks.append(
            PreflightCheckResult(
                check=PreflightCheckCode.CONFIG_COHERENCE,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail="config_skyrim_path no provisto (advisory)",
            )
        )

    # -------------------------------------------------------------------------
    # Veredicto final
    # -------------------------------------------------------------------------
    todos_pass = all(c.status is PreflightCheckStatus.PASS for c in checks)
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
        checks=tuple(checks),
        target_clone_id=target_clone_id,
        target_clone_root=clone_root,
        source_generation_id=gen_id,
        managed_source_root=ms_root_resolved,
        mo2_instance_dir=mo2_dir,
        observed_mo2_game_path=game_path_obs_str,
        assessed_at_ns=sello,
        notes=tuple(notas),
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
