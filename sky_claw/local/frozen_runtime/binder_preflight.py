"""Preflight de integración read-only para el binder de MO2 (P5-C0 / P7-A5 PR 1).

Tracker: #867 (histórico: #672)
ADR 0012 §13, §29.4
ADR 0015 §12, §13

Establece las precondiciones verificables para conmutar MO2 hacia un Runtime Clone
independiente de Steam de forma completamente segura y determinista.

No habilita el switch productivo (P5-C1). No muta ninguna superficie ni archivo en disco.
Reutiliza estrictamente los verificadores y oráculos existentes del subsistema:
- discovery.py: discover_managed_source (evidencia de proveedor Steam y manifest)
- clones.py: RuntimeCloneRecord + verificar_runtime_clone + admitir_clone_root_path
- generations.py: verificar_generation (integridad de Generation y no-drift)
- provisioning.py: verificar_procedencia_stock_generacion + cargar_digest_aprobado_stock
- independence.py: verify_clone_physical_integrity (sin enlaces ni hardlinks)
- compatibility.py: assess_runtime_compatibility (SKSE loader y DLL matching build)
- processes.py: probe_live_processes (concurrencia de Skyrim, SKSE y MO2)
- mo2_ini.py: leer_game_path_de_mo2 + leer_ejecutables_de_mo2 + leer_directorios_datos_de_mo2
- mo2_instance.py: probe_mo2_instance_identity (identidad global HKCU vs portable real)
- oracle.py: _primer_reparse, _normalizar, _es_igual_o_hijo
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib
import re
import stat
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum

from sky_claw.app.security.links import link_kind_or_raise
from sky_claw.config import SKYRIM_SE_APPID, STEAM_DEFAULT_PATHS
from sky_claw.local.frozen_runtime.clones import (
    CANONICAL_ADMITTED_ROLE,
    RuntimeCloneLifecycle,
    RuntimeCloneRecord,
    _canonical_long_path_windows,
    admitir_clone_root_path,
    cargar_runtime_clone_record,
    validar_clone_id,
    verificar_runtime_clone,
)
from sky_claw.local.frozen_runtime.discovery import discover_managed_source
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import (
    Mo2Executable,
    leer_directorios_datos_de_mo2,
    leer_ejecutables_de_mo2,
    leer_game_path_de_mo2,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_instance import (
    probe_mo2_instance_identity,
)
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    LiveProcessKind,
    Mo2InstanceIdentityState,
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
    StockProvenanceInvalidError,
    StockReferenceUnapprovedError,
)
from sky_claw.local.frozen_runtime.generations import (
    leer_generation_metadata,
    verificar_generation,
)
from sky_claw.local.frozen_runtime.independence import (
    PhysicalReparseError,
    _archivos_fisicos,
    descripcion_de_enlace,
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
from sky_claw.local.frozen_runtime.provisioning import (
    PROVIDER_STOCK,
    StockReference,
    cargar_digest_aprobado_stock,
    verificar_procedencia_stock_generacion,
)
from sky_claw.local.frozen_runtime.state import load_frozen_runtime_state
from sky_claw.local.frozen_runtime.storage import (
    active_state_path,
    clones_state_dir,
    generation_dir,
)
from sky_claw.local.frozen_runtime.storage_models import (
    GenerationMetadata,
    GenerationVerificationState,
)
from sky_claw.local.runtime_vault.runtime_observation import RuntimeObservationError

_RE_ALIAS_83 = re.compile(r"~[0-9]")


def _contiene_alias_83(ruta: str | pathlib.Path) -> bool:
    """True si algún componente de la ruta contiene el patrón de alias corto de Windows 8.3."""
    partes = pathlib.Path(ruta).parts
    return any(_RE_ALIAS_83.search(p) is not None for p in partes)


def _tiene_segmentos_relativos_crudos(raw_str: str) -> bool:
    """True si la cadena cruda contiene componentes '.' o '..' sin resolver."""
    partes = [p for p in re.split(r"[\\/]", raw_str) if p]
    return "." in partes or ".." in partes


def _dentro_de_area_steamapps(ruta: pathlib.Path) -> bool:
    """True si la ruta o alguno de sus ancestros se denomina 'steamapps'."""
    return any(comp.name.casefold() == "steamapps" for comp in (ruta, *ruta.parents))


def _colision_o_contencion(candidato: pathlib.Path, otra: pathlib.Path) -> PreflightReasonCode | None:
    """Devuelve ``PATHS_COLLIDE`` si coinciden o ``PATH_CONTAINMENT_VIOLATION`` si una contiene a la otra."""
    if _normalizar(candidato) == _normalizar(otra):
        return PreflightReasonCode.PATHS_COLLIDE
    if _es_igual_o_hijo(candidato, otra) or _es_igual_o_hijo(otra, candidato):
        return PreflightReasonCode.PATH_CONTAINMENT_VIOLATION
    return None


def _comparte_inodos_correspondientes(raiz_a: pathlib.Path, raiz_b: pathlib.Path) -> bool:
    """Comprueba en O(N_a) si algún archivo regular en ``raiz_a`` comparte ``(st_dev, st_ino)`` con ``raiz_b``."""
    if not raiz_a.is_dir() or not raiz_b.is_dir():
        return False
    for actual, _dirs, archivos in os.walk(raiz_a):
        actual_path = pathlib.Path(actual)
        for nombre in archivos:
            archivo_a = actual_path / nombre
            rel = archivo_a.relative_to(raiz_a)
            archivo_b = raiz_b / rel
            if not archivo_b.is_file():
                continue
            st_a = os.stat(archivo_a, follow_symlinks=False)
            st_b = os.stat(archivo_b, follow_symlinks=False)
            if st_a.st_dev > 0 and st_a.st_ino > 0 and (st_a.st_dev, st_a.st_ino) == (st_b.st_dev, st_b.st_ino):
                return True
    return False


def _argumentos_dependientes_de_steam(
    arguments: str,
    managed_source_root: pathlib.Path | None,
) -> bool:
    """Verifica si la cadena de argumentos referencia Steam o el directorio de la Managed Source."""
    if not arguments or not arguments.strip():
        return False
    args_limpio = arguments.strip()
    norm_slash = "\\".join(args_limpio.casefold().split("/"))
    if "\\steamapps\\" in norm_slash or norm_slash.endswith("\\steamapps") or "steamapps" in norm_slash:
        return True
    if managed_source_root is not None:
        ms_str = os.path.normpath(os.path.abspath(os.fspath(managed_source_root))).casefold()
        if ms_str in norm_slash:
            return True
        ms_posix = managed_source_root.as_posix().casefold()
        if ms_posix in "/".join(args_limpio.casefold().split("\\")):
            return True
    return False


def _ejecutables_dependientes_de_steam(
    ejecutables: tuple[Mo2Executable, ...],
    managed_source_root: pathlib.Path | None,
) -> list[str]:
    """Identifica entradas en ``[customExecutables]`` cuyo binario, workdir o argumentos caen en Steam."""
    dependientes: list[str] = []
    for exe in ejecutables:
        for etiqueta, valor in (("binary", exe.binary), ("workingDirectory", exe.working_directory)):
            if not valor:
                continue
            limpio = valor.strip().strip('"')
            ruta_exe = pathlib.Path(limpio)
            if not ruta_exe.is_absolute():
                continue
            norm_exe = pathlib.Path(os.path.normpath(os.path.abspath(limpio)))
            en_ms = managed_source_root is not None and _es_igual_o_hijo(norm_exe, managed_source_root)
            if en_ms or _dentro_de_area_steamapps(norm_exe):
                dependientes.append(f"{exe.index}\\{etiqueta} ({exe.title}): '{limpio}'")
        if exe.arguments and _argumentos_dependientes_de_steam(exe.arguments, managed_source_root):
            dependientes.append(f"{exe.index}\\arguments ({exe.title}): '{exe.arguments}'")
    return dependientes


class BinderTransitionPolicy(StrEnum):
    """Política explícita de admisión de señales del proveedor durante el preflight."""

    STRICT_PROVIDER_IDLE = "strict_provider_idle"
    STOCK_INDEPENDENT_TRANSITION = "stock_independent_transition"


class BinderPreflightVerdict(StrEnum):
    """Veredicto consolidado del preflight. Sólo READY autoriza iniciar el switch."""

    READY = "ready"
    BLOCKED = "blocked"


class PreflightCheckCode(StrEnum):
    """Familia cerrada de comprobaciones obligatorias del preflight."""

    PATH_SAFETY = "path_safety"
    MO2_INSTANCE_IDENTITY = "mo2_instance_identity"
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
    STOCK_PROVENANCE_INVALID = "stock_provenance_invalid"
    STOCK_REFERENCE_UNAPPROVED = "stock_reference_unapproved"
    CLONE_PHYSICAL_INTEGRITY_INVALID = "clone_physical_integrity_invalid"
    CLONE_PHYSICAL_INTEGRITY_INDETERMINATE = "clone_physical_integrity_indeterminate"
    OUTGOING_MANUAL_RUNTIME_INVALID = "outgoing_manual_runtime_invalid"
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
    MO2_INSTANCE_INACTIVE = "mo2_instance_inactive"
    MO2_INSTANCE_AMBIGUOUS = "mo2_instance_ambiguous"
    MO2_INSTANCE_IDENTITY_UNKNOWN = "mo2_instance_identity_unknown"
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
    transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STRICT_PROVIDER_IDLE
    outgoing_manual_runtime_root: pathlib.Path | None = None
    stock_reference_digest: str | None = None
    mo2_instance_identity_state: Mo2InstanceIdentityState | None = None
    assessed_at_ns: int = 0
    notes: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        """Invariante: True solo si el veredicto es READY, todos los checks están presentes sin duplicados y todos son PASS."""
        if self.verdict is not BinderPreflightVerdict.READY:
            return False
        codigos = [c.check for c in self.checks]
        if len(codigos) != len(PreflightCheckCode) or set(codigos) != set(PreflightCheckCode):
            return False
        return all(c.status is PreflightCheckStatus.PASS for c in self.checks)


def _deduplicar_motivos(reasons: list[PreflightReasonCode]) -> tuple[PreflightReasonCode, ...]:
    """Elimina motivos duplicados conservando el orden de inserción de los fallos."""
    vistos: set[PreflightReasonCode] = set()
    salida: list[PreflightReasonCode] = []
    for r in reasons:
        if r not in vistos and r is not PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED:
            vistos.add(r)
            salida.append(r)
    return tuple(salida)


def _registrar_fallo_path_safety(
    checks_map: dict[PreflightCheckCode, PreflightCheckResult],
    reasons: list[PreflightReasonCode],
    *,
    status: PreflightCheckStatus,
    reason: PreflightReasonCode,
    detail: str,
) -> None:
    """Registra un fallo en PATH_SAFETY sólo si el check aún no ha fallado."""
    reasons.append(reason)
    if checks_map[PreflightCheckCode.PATH_SAFETY].status is PreflightCheckStatus.PASS:
        checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
            check=PreflightCheckCode.PATH_SAFETY,
            status=status,
            reason=reason,
            detail=detail,
        )


def _raices_otros_clones_registrados(storage: pathlib.Path, target_clone_id: str) -> list[pathlib.Path]:
    """Descubre otros Clones registrados en ``state/clones/*.json`` para verificar disjunción.

    Fail-closed: lanza FrozenRuntimeStorageError si algún registro está redirigido,
    corrupto o ilegible.
    """
    otros: list[pathlib.Path] = []
    carpeta = clones_state_dir(storage)
    if not carpeta.is_dir():
        return otros
    archivos = sorted(carpeta.glob("*.json"))
    for archivo in archivos:
        cid = archivo.stem
        if cid.casefold() == target_clone_id.casefold():
            continue
        tipo_enlace = link_kind_or_raise(archivo)
        if tipo_enlace is not None:
            raise FrozenRuntimeStorageError(f"registro de clone redirigido (reparse/symlink): '{archivo}'")
        rec = cargar_runtime_clone_record(storage, cid)
        if rec is None:
            raise FrozenRuntimeStorageError(f"registro de clone vacío o ininteligible: '{archivo}'")
        otros.append(pathlib.Path(os.path.abspath(os.fspath(rec.root_path))))
    return otros


def _validar_aislamiento_runtime_manual(
    manual_root: pathlib.Path,
    *,
    generation_root: pathlib.Path | None,
    managed_source_root: pathlib.Path | None,
    clone_root: pathlib.Path | None,
    stock_source_root: pathlib.Path | None,
    otros_clones_roots: Sequence[pathlib.Path] = (),
    steam_staging_roots: Sequence[pathlib.Path] = (),
    exigir_stock_source: bool = False,
) -> tuple[PreflightCheckStatus, PreflightReasonCode | None, str]:
    """Valida el aislamiento físico del runtime manual saliente (fail-closed ante symlinks/reparse).

    Admite hardlinks históricos (st_nlink > 1) únicamente si son disjuntos de Steam,
    áreas de staging/downloading de Steam, Clone, Generation, Stock y otros Clones registrados.
    """
    motivo = descripcion_de_enlace(manual_root)
    if motivo is not None:
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.REPARSE_POINT_DETECTED,
            f"root de runtime manual redirigido: {motivo}",
        )
    if not manual_root.is_dir():
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"el runtime manual '{manual_root}' no existe o no es un directorio",
        )

    # Invariante fail-closed: nunca declarar independencia si falta evidencia de raíces protegidas obligatorias
    if generation_root is None or not generation_root.is_dir():
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"evidencia insuficiente para verificar aislamiento: Generation ausente o no es directorio ('{generation_root}')",
        )
    if managed_source_root is None or not managed_source_root.is_dir():
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"evidencia insuficiente para verificar aislamiento: Managed Source (Steam) ausente o no es directorio ('{managed_source_root}')",
        )
    if clone_root is None or not clone_root.is_dir():
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"evidencia insuficiente para verificar aislamiento: Clone destino ausente o no es directorio ('{clone_root}')",
        )
    if exigir_stock_source:
        if stock_source_root is None or not stock_source_root.is_dir():
            return (
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                f"evidencia insuficiente para verificar aislamiento: fuente Stock obligatoria ausente o no es directorio ('{stock_source_root}')",
            )
    elif stock_source_root is not None and not stock_source_root.is_dir():
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"fuente Stock declarada no es un directorio o es inaccesible: '{stock_source_root}'",
        )

    try:
        archivos_manual = _archivos_fisicos(manual_root, etiqueta="outgoing_manual_runtime")
    except PhysicalReparseError as exc:
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.REPARSE_POINT_DETECTED,
            str(exc),
        )
    except FrozenRuntimeStorageError as exc:
        return (
            PreflightCheckStatus.INDETERMINATE,
            PreflightReasonCode.REPARSE_INSPECTION_FAILED,
            str(exc),
        )

    superficies_a_evaluar: list[tuple[str, pathlib.Path]] = [
        ("la Generation", generation_root),
        ("la Managed Source (Steam)", managed_source_root),
        ("el Clone destino", clone_root),
    ]
    if stock_source_root is not None and stock_source_root.is_dir():
        superficies_a_evaluar.append(("la fuente Stock", stock_source_root))
    for otro_c in otros_clones_roots:
        if not otro_c.is_dir():
            return (
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                f"raíz de otro Clone registrado no es directorio o es inaccesible: '{otro_c}'",
            )
        superficies_a_evaluar.append(("otro Clone registrado", otro_c))
    for stage_dir in steam_staging_roots:
        if stage_dir.exists():
            if not stage_dir.is_dir():
                return (
                    PreflightCheckStatus.FAIL,
                    PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                    f"área de staging de Steam existe pero no es un directorio: '{stage_dir}'",
                )
            superficies_a_evaluar.append(("el área de staging de Steam (downloading/temp)", stage_dir))

    for nombre_sup, raiz_sup in superficies_a_evaluar:
        try:
            archivos_sup = _archivos_fisicos(raiz_sup, etiqueta=f"superficie_protegida_{nombre_sup}")
        except PhysicalReparseError as exc:
            return (
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.REPARSE_POINT_DETECTED,
                f"error de reparse al inspeccionar {nombre_sup}: {exc}",
            )
        except FrozenRuntimeStorageError as exc:
            return (
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                f"error al verificar inodos contra {nombre_sup}: {exc}",
            )
        except OSError as exc:
            return (
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                f"error al verificar inodos contra {nombre_sup}: {exc}",
            )

        inodos_sup: dict[tuple[int, int], str] = {}
        for rel_sup, st_sup in archivos_sup:
            if st_sup.st_dev <= 0 or st_sup.st_ino <= 0:
                return (
                    PreflightCheckStatus.FAIL,
                    PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                    f"identidad física no disponible en {nombre_sup} para '{rel_sup}' (fail-closed)",
                )
            inodos_sup[(st_sup.st_dev, st_sup.st_ino)] = rel_sup

        for rel_manual, st_manual in archivos_manual:
            if st_manual.st_dev <= 0 or st_manual.st_ino <= 0:
                return (
                    PreflightCheckStatus.FAIL,
                    PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                    f"identidad física no disponible en runtime manual para '{rel_manual}' (fail-closed)",
                )
            ident_manual = (st_manual.st_dev, st_manual.st_ino)
            if ident_manual in inodos_sup:
                rel_sup = inodos_sup[ident_manual]
                return (
                    PreflightCheckStatus.FAIL,
                    PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
                    f"outgoing_manual_runtime_root comparte objetos de archivo (hardlink) con {nombre_sup}: "
                    f"manual '{rel_manual}' <-> {nombre_sup} '{rel_sup}'",
                )

    multilinks = [rel for rel, st in archivos_manual if st.st_nlink > 1]
    if multilinks:
        detalle = (
            f"runtime manual saliente verificado ({len(multilinks)} hardlink(s) históricos "
            f"disjuntos de Steam/Clone/Generation admitidos en {len(archivos_manual)} archivo(s))"
        )
    else:
        detalle = f"runtime manual saliente verificado (sin reparse ni hardlinks en {len(archivos_manual)} archivo(s))"

    return PreflightCheckStatus.PASS, None, detalle


def _validar_autoridad_fuente_stock(
    stock_root: pathlib.Path,
    *,
    reference: StockReference,
    observadores: RuntimeProbes,
    storage: pathlib.Path,
    ms_root_resolved: pathlib.Path | None,
) -> tuple[PreflightCheckStatus, PreflightReasonCode | None, str]:
    """Valida la autoridad de stock_source_root frente a la referencia Stock aprobada (A1).

    Fail-closed: una ruta arbitraria, sin identidad de Skyrim o no coincidente se rechaza.
    """
    motivo = descripcion_de_enlace(stock_root)
    if motivo is not None:
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.REPARSE_POINT_DETECTED,
            f"la fuente Stock está redirigida por enlace/reparse: {motivo}",
        )
    if not stock_root.is_dir():
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"la fuente Stock no existe o no es un directorio: '{stock_root}'",
        )
    if _dentro_de_area_steamapps(stock_root):
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
            f"la fuente Stock está dentro de un área administrada por Steam ('steamapps'): '{stock_root}'",
        )
    for etiqueta, otra in (("storage_root", storage), ("Managed Source", ms_root_resolved)):
        if otra is not None:
            col = _colision_o_contencion(stock_root, otra)
            if col is not None:
                return (
                    PreflightCheckStatus.FAIL,
                    col,
                    f"la fuente Stock se solapa o colisiona con {etiqueta} ('{otra}')",
                )
    exe_stock = stock_root / "SkyrimSE.exe"
    if not exe_stock.is_file():
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.STOCK_PROVENANCE_INVALID,
            f"la fuente Stock carece de SkyrimSE.exe regular: '{stock_root}'",
        )
    try:
        ident_stock = observadores.identity(stock_root)
    except (RuntimeObservationError, FrozenRuntimeError, OSError) as exc:
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.STOCK_PROVENANCE_INVALID,
            f"no se pudo observar identidad de Skyrim SE en la fuente Stock: {exc}",
        )
    if ident_stock.game_key.casefold() != reference.expected_runtime.game_key.casefold() or (
        reference.expected_runtime.game_version and ident_stock.game_version != reference.expected_runtime.game_version
    ):
        return (
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.STOCK_PROVENANCE_INVALID,
            f"la identidad de la fuente Stock ({ident_stock.game_key} {ident_stock.game_version}) "
            f"no coincide con la referencia Stock aprobada "
            f"({reference.expected_runtime.game_key} {reference.expected_runtime.game_version})",
        )
    for exp in reference.critical_expectations:
        p_crit = stock_root / exp.rel_path
        if not p_crit.is_file():
            return (
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.STOCK_PROVENANCE_INVALID,
                f"la fuente Stock carece del archivo crítico esperado '{exp.rel_path}'",
            )
        try:
            st = os.lstat(p_crit)
            if stat.S_ISLNK(st.st_mode):
                return (
                    PreflightCheckStatus.FAIL,
                    PreflightReasonCode.REPARSE_POINT_DETECTED,
                    f"archivo crítico '{exp.rel_path}' en la fuente Stock es un enlace simbólico",
                )
            if st.st_size != exp.expected_size:
                return (
                    PreflightCheckStatus.FAIL,
                    PreflightReasonCode.STOCK_PROVENANCE_INVALID,
                    f"archivo crítico '{exp.rel_path}' en la fuente Stock tiene tamaño "
                    f"{st.st_size} bytes (esperado {exp.expected_size})",
                )
            digest_real = hashlib.sha256(p_crit.read_bytes()).hexdigest()
            if digest_real != exp.expected_digest:
                return (
                    PreflightCheckStatus.FAIL,
                    PreflightReasonCode.STOCK_PROVENANCE_INVALID,
                    f"archivo crítico '{exp.rel_path}' en la fuente Stock difiere del digest "
                    f"esperado ({digest_real} != {exp.expected_digest})",
                )
        except OSError as exc:
            return (
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.STOCK_PROVENANCE_INVALID,
                f"no se pudo leer el archivo crítico '{exp.rel_path}' en la fuente Stock: {exc}",
            )
    return PreflightCheckStatus.PASS, None, ""


def _validar_runtime_manual_saliente(
    *,
    manual_raw: pathlib.Path,
    storage: pathlib.Path,
    gen_id: str,
    target_clone_id: str,
    clone_root: pathlib.Path,
    ms_root_resolved: pathlib.Path | None,
    stock_source_root: pathlib.Path | None,
    mo2_dir: pathlib.Path,
    mo2_data_dirs: tuple[pathlib.Path, ...],
    observadores: RuntimeProbes,
    steam_staging_roots: Sequence[pathlib.Path] = (),
    transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STRICT_PROVIDER_IDLE,
) -> tuple[pathlib.Path | None, PreflightCheckStatus, PreflightReasonCode | None, str]:
    """Valida de forma estricta y fail-closed ``outgoing_manual_runtime_root`` (Contrato R2)."""
    raw_str = os.fspath(manual_raw)
    if not raw_str or not raw_str.strip():
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            "outgoing_manual_runtime_root vacío",
        )
    if _tiene_segmentos_relativos_crudos(raw_str):
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"outgoing_manual_runtime_root contiene segmentos relativos ('.' o '..'): '{manual_raw}'",
        )
    if _contiene_alias_83(raw_str):
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
            f"outgoing_manual_runtime_root contiene alias 8.3: '{manual_raw}'",
        )
    ruta_declarada = pathlib.Path(manual_raw)
    if not ruta_declarada.is_absolute():
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"outgoing_manual_runtime_root debe ser una ruta absoluta: '{manual_raw}'",
        )

    manual_norm = pathlib.Path(os.path.normpath(os.path.abspath(raw_str)))
    clase_rep, det_rep = _primer_reparse(manual_norm)
    if clase_rep == _REPARSE_HAY:
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.REPARSE_POINT_DETECTED,
            det_rep or f"reparse point detectado en outgoing_manual_runtime_root: '{manual_norm}'",
        )
    if clase_rep == _REPARSE_INDETERMINADO:
        return (
            None,
            PreflightCheckStatus.INDETERMINATE,
            PreflightReasonCode.REPARSE_INSPECTION_FAILED,
            det_rep or f"inspección de reparse indeterminada en outgoing_manual_runtime_root: '{manual_norm}'",
        )

    if manual_norm.exists():
        larga = _canonical_long_path_windows(manual_norm)
        if _normalizar(manual_norm) != _normalizar(larga):
            return (
                None,
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
                f"outgoing_manual_runtime_root usa alias corto 8.3 no canónico: '{manual_norm}' != '{larga}'",
            )

    if _dentro_de_area_steamapps(manual_norm):
        if ms_root_resolved is not None and _normalizar(manual_norm) == _normalizar(ms_root_resolved):
            return (
                None,
                PreflightCheckStatus.FAIL,
                PreflightReasonCode.PATHS_COLLIDE,
                "outgoing_manual_runtime_root coincide con la Managed Source de Steam",
            )
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
            f"outgoing_manual_runtime_root cae dentro de un área steamapps de Steam: '{manual_norm}'",
        )

    protegidas: list[tuple[str, pathlib.Path]] = [
        ("Frozen Runtime Storage", storage),
        ("Clone destino", clone_root),
        ("mo2_instance_dir", mo2_dir),
    ]
    if ms_root_resolved is not None:
        protegidas.append(("Managed Source", ms_root_resolved))
    if stock_source_root is not None:
        protegidas.append(("Stock source", pathlib.Path(os.path.abspath(os.fspath(stock_source_root)))))
    for dir_datos in mo2_data_dirs:
        protegidas.append(("directorio de datos de MO2", dir_datos))
    try:
        otros_clones = _raices_otros_clones_registrados(storage, target_clone_id)
    except (FrozenRuntimeError, OSError) as exc:
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.CLONE_RECORD_CORRUPT,
            f"registro de clone corrupto o ilegible en storage: {exc}",
        )
    for otro_clone in otros_clones:
        protegidas.append(("otro Runtime Clone registrado", otro_clone))

    for etiqueta, otra in protegidas:
        conflicto = _colision_o_contencion(manual_norm, otra)
        if conflicto is not None:
            return (
                None,
                PreflightCheckStatus.FAIL,
                conflicto,
                f"outgoing_manual_runtime_root se solapa o colisiona con {etiqueta} ('{otra}')",
            )

    if not manual_norm.is_dir():
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"outgoing_manual_runtime_root no existe o no es un directorio: '{manual_norm}'",
        )

    exe_skyrim = manual_norm / "SkyrimSE.exe"
    if not exe_skyrim.is_file():
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"outgoing_manual_runtime_root carece de SkyrimSE.exe regular: '{manual_norm}'",
        )

    try:
        ident_manual = observadores.identity(manual_norm)
    except (RuntimeObservationError, FrozenRuntimeError, OSError) as exc:
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"identidad de Skyrim no verificable en outgoing_manual_runtime_root: {exc}",
        )
    if ident_manual.game_key.casefold() != "skyrimse" or not (
        ident_manual.game_version and ident_manual.game_version.strip()
    ):
        return (
            None,
            PreflightCheckStatus.FAIL,
            PreflightReasonCode.OUTGOING_MANUAL_RUNTIME_INVALID,
            f"outgoing_manual_runtime_root no posee identidad válida de Skyrim SE: {ident_manual!r}",
        )

    st_aisl, rz_aisl, det_aisl = _validar_aislamiento_runtime_manual(
        manual_norm,
        generation_root=generation_dir(storage, gen_id),
        managed_source_root=ms_root_resolved,
        clone_root=clone_root,
        stock_source_root=stock_source_root,
        otros_clones_roots=tuple(otros_clones),
        steam_staging_roots=steam_staging_roots,
        exigir_stock_source=(transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION),
    )
    if st_aisl is not PreflightCheckStatus.PASS:
        return None, st_aisl, rz_aisl, det_aisl

    return manual_norm, PreflightCheckStatus.PASS, None, det_aisl


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
    transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STRICT_PROVIDER_IDLE,
    outgoing_manual_runtime_root: pathlib.Path | None = None,
    approved_stock_reference_digest: str | None = None,
    approved_stock_reference_digest_file: pathlib.Path | None = None,
    stock_source_root: pathlib.Path | None = None,
) -> BinderPreflightAssessment:
    """Evalúa exhaustivamente las precondiciones para conmutar MO2 hacia un Runtime Clone.

    Operación 100% READ-ONLY. No escribe en disco, Registro ni altera la configuración de MO2.
    """
    if not isinstance(transition_policy, BinderTransitionPolicy):
        raise ValueError(f"transition_policy inválida: {transition_policy!r}")

    observadores = probes if probes is not None else default_runtime_probes()
    probe_mo2 = observadores.mo2_instance if observadores.mo2_instance is not None else probe_mo2_instance_identity
    sello = assessed_at_ns if assessed_at_ns is not None else time.time_ns()

    checks_map: dict[PreflightCheckCode, PreflightCheckResult] = {}
    reasons: list[PreflightReasonCode] = []
    notes_list: list[str] = []

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
    elif stock_source_root is not None and _contiene_alias_83(stock_source_root):
        checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
            check=PreflightCheckCode.PATH_SAFETY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
            detail=f"stock_source_root contiene alias 8.3: '{stock_source_root}'",
        )
        reasons.append(PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED)
    else:
        clase_mo2, det_mo2 = _primer_reparse(mo2_dir)
        clase_storage, det_storage = _primer_reparse(storage)
        clase_ini, det_ini = _primer_reparse(ini_path)
        clase_stock, det_stock = (
            _primer_reparse(pathlib.Path(os.path.abspath(os.fspath(stock_source_root))))
            if stock_source_root is not None
            else ("none", None)
        )

        if _REPARSE_HAY in (clase_mo2, clase_storage, clase_ini, clase_stock):
            detalle = (
                det_ini
                if clase_ini == _REPARSE_HAY
                else (
                    det_mo2
                    if clase_mo2 == _REPARSE_HAY
                    else (det_storage if clase_storage == _REPARSE_HAY else det_stock)
                )
            )
            checks_map[PreflightCheckCode.PATH_SAFETY] = PreflightCheckResult(
                check=PreflightCheckCode.PATH_SAFETY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.REPARSE_POINT_DETECTED,
                detail=detalle or "reparse point en mo2_instance_dir, storage_root o modorganizer.ini",
            )
            reasons.append(PreflightReasonCode.REPARSE_POINT_DETECTED)
        elif _REPARSE_INDETERMINADO in (clase_mo2, clase_storage, clase_ini, clase_stock):
            detalle = (
                det_ini
                if clase_ini == _REPARSE_INDETERMINADO
                else (
                    det_mo2
                    if clase_mo2 == _REPARSE_INDETERMINADO
                    else (det_storage if clase_storage == _REPARSE_INDETERMINADO else det_stock)
                )
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
    # 2. Identidad de la instancia activa de MO2 (Contrato R3: Global vs Portable)
    # -------------------------------------------------------------------------
    mo2_ident = probe_mo2(mo2_dir)
    if mo2_ident.is_active:
        checks_map[PreflightCheckCode.MO2_INSTANCE_IDENTITY] = PreflightCheckResult(
            check=PreflightCheckCode.MO2_INSTANCE_IDENTITY,
            status=PreflightCheckStatus.PASS,
            reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
            detail=mo2_ident.detail or f"instancia MO2 activa verificada ({mo2_ident.state.value})",
        )
    elif mo2_ident.state is Mo2InstanceIdentityState.INACTIVE:
        checks_map[PreflightCheckCode.MO2_INSTANCE_IDENTITY] = PreflightCheckResult(
            check=PreflightCheckCode.MO2_INSTANCE_IDENTITY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.MO2_INSTANCE_INACTIVE,
            detail=mo2_ident.detail or f"'{mo2_dir}' no corresponde a la instancia activa de MO2",
        )
        reasons.append(PreflightReasonCode.MO2_INSTANCE_INACTIVE)
    elif mo2_ident.state is Mo2InstanceIdentityState.AMBIGUOUS:
        checks_map[PreflightCheckCode.MO2_INSTANCE_IDENTITY] = PreflightCheckResult(
            check=PreflightCheckCode.MO2_INSTANCE_IDENTITY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.MO2_INSTANCE_AMBIGUOUS,
            detail=mo2_ident.detail or f"identidad de instancia MO2 ambigua en '{mo2_dir}'",
        )
        reasons.append(PreflightReasonCode.MO2_INSTANCE_AMBIGUOUS)
    else:
        checks_map[PreflightCheckCode.MO2_INSTANCE_IDENTITY] = PreflightCheckResult(
            check=PreflightCheckCode.MO2_INSTANCE_IDENTITY,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.MO2_INSTANCE_IDENTITY_UNKNOWN,
            detail=mo2_ident.detail or f"no se pudo determinar la identidad de la instancia MO2 en '{mo2_dir}'",
        )
        reasons.append(PreflightReasonCode.MO2_INSTANCE_IDENTITY_UNKNOWN)

    # -------------------------------------------------------------------------
    # 3. Configuración de MO2 (modorganizer.ini legible, formato Qt, directorios y estabilidad)
    # -------------------------------------------------------------------------
    game_path_obs_str: str | None = None
    ejecutables_mo2: tuple[Mo2Executable, ...] = ()
    mo2_data_dirs: tuple[pathlib.Path, ...] = ()
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
            evidencia_datos = leer_directorios_datos_de_mo2(ini_path)
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
                elif (
                    evidencia_datos.state is not ObservationState.OBSERVED
                    or evidencia_datos.base_directory is None
                    or evidencia_datos.mod_directory is None
                ):
                    checks_map[PreflightCheckCode.MO2_CONFIGURATION] = PreflightCheckResult(
                        check=PreflightCheckCode.MO2_CONFIGURATION,
                        status=PreflightCheckStatus.FAIL,
                        reason=PreflightReasonCode.MO2_INI_UNREADABLE,
                        detail=evidencia_datos.detail or "base_directory/mod_directory ilegibles en [Settings]",
                    )
                    reasons.append(PreflightReasonCode.MO2_INI_UNREADABLE)
                else:
                    game_path_obs_str = _normalizar(evidencia_gp.path)
                    base_d = pathlib.Path(os.path.normpath(os.path.abspath(evidencia_datos.base_directory)))
                    mod_d = pathlib.Path(os.path.normpath(os.path.abspath(evidencia_datos.mod_directory)))
                    mo2_data_dirs = (base_d, mod_d)
                    for d_mo2 in mo2_data_dirs:
                        if _contiene_alias_83(d_mo2):
                            _registrar_fallo_path_safety(
                                checks_map,
                                reasons,
                                status=PreflightCheckStatus.FAIL,
                                reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
                                detail=f"directorio de datos de MO2 contiene alias 8.3: '{d_mo2}'",
                            )
                        else:
                            c_rep, det_r = _primer_reparse(d_mo2)
                            if c_rep == _REPARSE_HAY:
                                _registrar_fallo_path_safety(
                                    checks_map,
                                    reasons,
                                    status=PreflightCheckStatus.FAIL,
                                    reason=PreflightReasonCode.REPARSE_POINT_DETECTED,
                                    detail=det_r or f"reparse point en directorio de datos de MO2: '{d_mo2}'",
                                )
                            elif c_rep == _REPARSE_INDETERMINADO:
                                _registrar_fallo_path_safety(
                                    checks_map,
                                    reasons,
                                    status=PreflightCheckStatus.INDETERMINATE,
                                    reason=PreflightReasonCode.REPARSE_INSPECTION_FAILED,
                                    detail=det_r or f"inspección de reparse fallida en datos de MO2: '{d_mo2}'",
                                )
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
    # 4. Concurrencia de procesos (sin Skyrim, sin SKSE, sin MO2)
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
    # 5. Managed Source discovery y señales del proveedor Steam (Contrato R1)
    # -------------------------------------------------------------------------
    ms_root_resolved: pathlib.Path | None = None
    steam_active_pendiente_exencion = False
    steam_obs_buildid = ""
    steam_obs_flags: str | None = None
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
        obs_steam = observe_provider_activity(ms_result.source)
        staging_indeterminado: str | None = None
        for sub_stage in ("downloading", "temp"):
            ruta_stage = ms_result.source.library_steamapps / sub_stage / ms_result.source.appid
            try:
                if ruta_stage.exists():
                    if not ruta_stage.is_dir():
                        staging_indeterminado = f"ruta de staging Steam no es directorio: '{ruta_stage}'"
                        break
                    with os.scandir(ruta_stage) as entradas_stage:
                        for _ in entradas_stage:
                            break
            except OSError as exc:
                staging_indeterminado = f"directorio de staging Steam ilegible ('{ruta_stage}'): {exc}"
                break

        if not obs_steam.manifest_readable:
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail=f"manifest de Steam ilegible o malformado: {obs_steam.manifest_parse_error}",
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
        elif not obs_steam.buildid or not obs_steam.buildid.strip().isdigit():
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail="manifest de Steam sin buildid numérico válido",
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
        elif obs_steam.state_flags is None or not obs_steam.state_flags.strip().isdigit():
            # En P1 (evaluate_provider_observation), un StateFlags no numérico o ausente con
            # staging no vacío se clasifica ACTIVE para no degradar actividad conocida.
            # En P7-A5, bajo STOCK_INDEPENDENT_TRANSITION donde ACTIVE puede eximirse con
            # Stock v2, un StateFlags ausente o corrupto es INDETERMINATE y SIEMPRE bloquea.
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail="manifest de Steam con StateFlags ausente o no numérico (INDETERMINATE siempre bloquea)",
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
        elif staging_indeterminado is not None:
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail=f"{staging_indeterminado} (INDETERMINATE siempre bloquea)",
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
        else:
            estado_prov = evaluate_provider_observation(obs_steam)
            steam_obs_buildid = obs_steam.buildid
            steam_obs_flags = obs_steam.state_flags
            if estado_prov is ProviderObservationState.IDLE:
                checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                    check=PreflightCheckCode.MANAGED_SOURCE,
                    status=PreflightCheckStatus.PASS,
                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                    detail=(
                        f"Managed Source verificada en '{ms_root_resolved}' "
                        f"(buildid {obs_steam.buildid}, state_flags {obs_steam.state_flags})"
                    ),
                )
            elif (
                estado_prov is ProviderObservationState.ACTIVE
                and transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION
            ):
                # No se aprueba de antemano: sólo pasará a PASS si TODAS las garantías
                # de aislamiento Stock v2 + autoridad externa A1 + runtime no-Steam se cumplen.
                steam_active_pendiente_exencion = True
            else:
                det_prov = (
                    "Steam en estado activo (descarga o actualización en curso)"
                    if estado_prov is ProviderObservationState.ACTIVE
                    else "estado de Steam indeterminado (INDETERMINATE siempre bloquea)"
                )
                checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                    check=PreflightCheckCode.MANAGED_SOURCE,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                    detail=det_prov,
                )
                reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)

    def _bloqueo_temprano(cid_actual: str | None = None) -> BinderPreflightAssessment:
        """Construye un BinderPreflightAssessment con veredicto BLOCKED temprano."""
        if steam_active_pendiente_exencion and PreflightCheckCode.MANAGED_SOURCE not in checks_map:
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail="Steam permanece ACTIVE y el preflight abortó antes de verificar aislamiento Stock",
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)
        checks_ord = tuple(checks_map[c] for c in PreflightCheckCode if c in checks_map)
        return BinderPreflightAssessment(
            verdict=BinderPreflightVerdict.BLOCKED,
            reasons=_deduplicar_motivos(reasons),
            checks=checks_ord,
            target_clone_id=cid_actual,
            managed_source_root=ms_root_resolved,
            mo2_instance_dir=mo2_dir,
            observed_mo2_game_path=game_path_obs_str,
            transition_policy=transition_policy,
            mo2_instance_identity_state=mo2_ident.state,
            assessed_at_ns=sello,
            notes=(),
        )

    # -------------------------------------------------------------------------
    # 6. Resolución y carga del RuntimeCloneRecord (P2-D)
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
            return _bloqueo_temprano()
        except StateSchemaError as exc:
            checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID,
                detail=f"active.json con schema inválido: {exc}",
            )
            reasons.append(PreflightReasonCode.CLONE_RECORD_SCHEMA_INVALID)
            return _bloqueo_temprano()
        except (FrozenRuntimeStorageError, OSError) as exc:
            checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
                check=PreflightCheckCode.CLONE_RECORD,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.CLONE_RECORD_CORRUPT,
                detail=f"error al acceder a active.json: {exc}",
            )
            reasons.append(PreflightReasonCode.CLONE_RECORD_CORRUPT)
            return _bloqueo_temprano()

    if not target_clone_id:
        checks_map[PreflightCheckCode.CLONE_RECORD] = PreflightCheckResult(
            check=PreflightCheckCode.CLONE_RECORD,
            status=PreflightCheckStatus.FAIL,
            reason=PreflightReasonCode.CLONE_ID_UNSPECIFIED,
            detail="no se especificó clone_id ni existe desired_clone_id en active.json",
        )
        reasons.append(PreflightReasonCode.CLONE_ID_UNSPECIFIED)
        return _bloqueo_temprano()

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
        return _bloqueo_temprano(target_clone_id)

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
        return _bloqueo_temprano(target_clone_id)

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
    # 7. Ciclo de vida del Clone (debe ser estrictamente PROVISIONED)
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
    gen_id = record.source_generation_id

    # -------------------------------------------------------------------------
    # 8. Admisión física y contención de rutas del Clone + runtime manual saliente (R2)
    # -------------------------------------------------------------------------
    if checks_map[PreflightCheckCode.PATH_SAFETY].status is PreflightCheckStatus.PASS:
        if _contiene_alias_83(clone_root):
            _registrar_fallo_path_safety(
                checks_map,
                reasons,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED,
                detail=f"clone_root contiene alias 8.3: '{clone_root}'",
            )
        else:
            clase_clone, det_clone = _primer_reparse(clone_root)
            if clase_clone == _REPARSE_HAY:
                _registrar_fallo_path_safety(
                    checks_map,
                    reasons,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.REPARSE_POINT_DETECTED,
                    detail=det_clone or "reparse point en clone_root",
                )
            elif clase_clone == _REPARSE_INDETERMINADO:
                _registrar_fallo_path_safety(
                    checks_map,
                    reasons,
                    status=PreflightCheckStatus.INDETERMINATE,
                    reason=PreflightReasonCode.REPARSE_INSPECTION_FAILED,
                    detail=det_clone or "inspección de reparse en clone_root indeterminada",
                )
            elif ms_root_resolved is not None and _normalizar(clone_root) == _normalizar(ms_root_resolved):
                _registrar_fallo_path_safety(
                    checks_map,
                    reasons,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATHS_COLLIDE,
                    detail="clone_root coincide con la Managed Source",
                )
            elif ms_root_resolved is not None and (
                _es_igual_o_hijo(clone_root, ms_root_resolved) or _es_igual_o_hijo(ms_root_resolved, clone_root)
            ):
                _registrar_fallo_path_safety(
                    checks_map,
                    reasons,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
                    detail="clone_root se solapa o está contenido dentro de la Managed Source",
                )
            elif _es_igual_o_hijo(clone_root, storage):
                _registrar_fallo_path_safety(
                    checks_map,
                    reasons,
                    status=PreflightCheckStatus.FAIL,
                    reason=PreflightReasonCode.PATH_CONTAINMENT_VIOLATION,
                    detail="clone_root está contenido dentro del Frozen Runtime Storage (versions/state)",
                )
            elif _colision_o_contencion(clone_root, mo2_dir) is not None:
                _registrar_fallo_path_safety(
                    checks_map,
                    reasons,
                    status=PreflightCheckStatus.FAIL,
                    reason=_colision_o_contencion(clone_root, mo2_dir) or PreflightReasonCode.PATHS_COLLIDE,
                    detail="clone_root coincide o se solapa con mo2_instance_dir",
                )
            else:
                for dir_datos in mo2_data_dirs:
                    conflicto_mo2 = _colision_o_contencion(clone_root, dir_datos)
                    if conflicto_mo2 is not None:
                        _registrar_fallo_path_safety(
                            checks_map,
                            reasons,
                            status=PreflightCheckStatus.FAIL,
                            reason=conflicto_mo2,
                            detail=f"clone_root se solapa con el directorio de datos de MO2 ('{dir_datos}')",
                        )
                        break

                if checks_map[PreflightCheckCode.PATH_SAFETY].status is PreflightCheckStatus.PASS:
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
                        _registrar_fallo_path_safety(
                            checks_map,
                            reasons,
                            status=PreflightCheckStatus.FAIL,
                            reason=motivo_refinado,
                            detail=f"admitir_clone_root_path rechazó la ruta: {exc}",
                        )

    manual_resolved: pathlib.Path | None = None
    if outgoing_manual_runtime_root is not None:
        steam_staging: list[pathlib.Path] = []
        if ms_result.source is not None:
            steam_staging.append(ms_result.source.library_steamapps / "downloading" / ms_result.source.appid)
            steam_staging.append(ms_result.source.library_steamapps / "temp" / ms_result.source.appid)

        manual_res, est_manual, motivo_manual, det_manual = _validar_runtime_manual_saliente(
            manual_raw=outgoing_manual_runtime_root,
            storage=storage,
            gen_id=gen_id,
            target_clone_id=target_clone_id,
            clone_root=clone_root,
            ms_root_resolved=ms_root_resolved,
            stock_source_root=stock_source_root,
            mo2_dir=mo2_dir,
            mo2_data_dirs=mo2_data_dirs,
            observadores=observadores,
            steam_staging_roots=tuple(steam_staging),
            transition_policy=transition_policy,
        )
        if motivo_manual is not None:
            _registrar_fallo_path_safety(
                checks_map,
                reasons,
                status=est_manual,
                reason=motivo_manual,
                detail=det_manual,
            )
        else:
            manual_resolved = manual_res
            if det_manual:
                notes_list.append(det_manual)

    # -------------------------------------------------------------------------
    # 9. Integridad de la Generation y procedencia Stock v2 + autoridad A1 (R1)
    # -------------------------------------------------------------------------
    gen_meta: GenerationMetadata | None = None
    stock_digest_verificado: str | None = None
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
            gen_meta = leer_generation_metadata(storage, gen_id)
        except (FrozenRuntimeError, OSError) as exc:
            checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                check=PreflightCheckCode.GENERATION_INTEGRITY,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.GENERATION_NOT_FOUND,
                detail=f"metadata de generation '{gen_id}' no legible: {exc}",
            )
            reasons.append(PreflightReasonCode.GENERATION_NOT_FOUND)

    if gen_meta is not None:
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
                exige_stock = (
                    transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION
                    or gen_meta.provider == PROVIDER_STOCK
                    or approved_stock_reference_digest is not None
                    or approved_stock_reference_digest_file is not None
                )
                if exige_stock:
                    try:
                        digest_archivo: str | None = None
                        if approved_stock_reference_digest_file is not None:
                            digest_archivo = cargar_digest_aprobado_stock(approved_stock_reference_digest_file)
                        if (
                            approved_stock_reference_digest is not None
                            and digest_archivo is not None
                            and approved_stock_reference_digest != digest_archivo
                        ):
                            raise StockReferenceUnapprovedError(
                                "approved_stock_reference_digest y approved_stock_reference_digest_file difieren"
                            )
                        digest_efectivo = (
                            approved_stock_reference_digest
                            if approved_stock_reference_digest is not None
                            else digest_archivo
                        )
                        proc_stock = verificar_procedencia_stock_generacion(
                            storage,
                            gen_id,
                            approved_reference_digest=digest_efectivo,
                            require_approved_digest=(
                                transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION
                            ),
                        )
                        stock_digest_verificado = proc_stock.reference.reference_digest
                        if stock_source_root is not None:
                            st_aut, rz_aut, det_aut = _validar_autoridad_fuente_stock(
                                pathlib.Path(os.path.abspath(os.fspath(stock_source_root))),
                                reference=proc_stock.reference,
                                observadores=observadores,
                                storage=storage,
                                ms_root_resolved=ms_root_resolved,
                            )
                            if st_aut is not PreflightCheckStatus.PASS:
                                checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                                    check=PreflightCheckCode.GENERATION_INTEGRITY,
                                    status=st_aut,
                                    reason=rz_aut or PreflightReasonCode.STOCK_PROVENANCE_INVALID,
                                    detail=det_aut,
                                )
                                if rz_aut:
                                    reasons.append(rz_aut)
                            else:
                                checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                                    check=PreflightCheckCode.GENERATION_INTEGRITY,
                                    status=PreflightCheckStatus.PASS,
                                    reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                                    detail=(
                                        f"generation '{gen_id}' y fuente Stock verificadas como VALID con procedencia "
                                        f"Stock v2 íntegra (reference_digest {stock_digest_verificado})"
                                    ),
                                )
                        else:
                            checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                                check=PreflightCheckCode.GENERATION_INTEGRITY,
                                status=PreflightCheckStatus.PASS,
                                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                                detail=(
                                    f"generation '{gen_id}' verificada como VALID con procedencia Stock v2 íntegra "
                                    f"(reference_digest {stock_digest_verificado})"
                                ),
                            )
                    except StockProvenanceInvalidError as exc:
                        checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                            check=PreflightCheckCode.GENERATION_INTEGRITY,
                            status=PreflightCheckStatus.FAIL,
                            reason=PreflightReasonCode.STOCK_PROVENANCE_INVALID,
                            detail=str(exc),
                        )
                        reasons.append(PreflightReasonCode.STOCK_PROVENANCE_INVALID)
                    except StockReferenceUnapprovedError as exc:
                        checks_map[PreflightCheckCode.GENERATION_INTEGRITY] = PreflightCheckResult(
                            check=PreflightCheckCode.GENERATION_INTEGRITY,
                            status=PreflightCheckStatus.FAIL,
                            reason=PreflightReasonCode.STOCK_REFERENCE_UNAPPROVED,
                            detail=str(exc),
                        )
                        reasons.append(PreflightReasonCode.STOCK_REFERENCE_UNAPPROVED)
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
    # 10. Integridad física y linaje del Runtime Clone
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
    # 11. Compatibilidad de Runtime y SKSE en el clone
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
    # 12. Coherencia de configuración multi-superficie y exclusión de Steam
    # -------------------------------------------------------------------------
    ms_norm = _normalizar(ms_root_resolved) if ms_root_resolved is not None else None
    clone_norm = _normalizar(clone_root)
    manual_norm_str = _normalizar(manual_resolved) if manual_resolved is not None else None

    raices_admitidas: set[str] = {clone_norm}
    if outgoing_manual_runtime_root is not None:
        if manual_norm_str is not None:
            raices_admitidas.add(manual_norm_str)
    elif transition_policy is BinderTransitionPolicy.STRICT_PROVIDER_IDLE and ms_norm is not None:
        raices_admitidas.add(ms_norm)

    coherencia_fail: PreflightReasonCode | None = None
    coherencia_detail: str = ""

    cfg_norm: str | None = None
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
            if cfg_norm not in raices_admitidas:
                coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
                coherencia_detail = (
                    f"config_skyrim_path ('{config_skyrim_path}') apunta fuera de los runtimes admitidos "
                    f"para la política '{transition_policy.value}'"
                )

    gp_norm: str | None = None
    if coherencia_fail is None and game_path_obs_str is not None:
        gp_norm = _normalizar(game_path_obs_str)
        if gp_norm not in raices_admitidas:
            coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
            coherencia_detail = (
                f"MO2 gamePath ('{game_path_obs_str}') apunta a un directorio no admitido "
                f"para la política '{transition_policy.value}'"
            )

    if coherencia_fail is None and cfg_norm is not None and gp_norm is not None and cfg_norm != gp_norm:
        coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
        coherencia_detail = (
            f"divergencia cruzada entre superficies: Config.skyrim_path ('{config_skyrim_path}') "
            f"difiere de MO2 gamePath ('{game_path_obs_str}')"
        )

    entorno = os.environ if environ is None else environ
    env_skyrim_raw = (entorno.get("SKYRIM_PATH") or "").strip()
    if coherencia_fail is None and env_skyrim_raw:
        env_path = pathlib.Path(os.path.abspath(os.fspath(env_skyrim_raw)))
        if _contiene_alias_83(env_path):
            coherencia_fail = PreflightReasonCode.SHORT_NAME_ALIAS_DETECTED
            coherencia_detail = f"SKYRIM_PATH en el entorno contiene alias 8.3: '{env_skyrim_raw}'"
        else:
            env_norm = _normalizar(env_skyrim_raw)
            if env_norm not in raices_admitidas:
                coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
                coherencia_detail = (
                    f"variable de entorno SKYRIM_PATH ('{env_skyrim_raw}') apunta fuera de los runtimes admitidos"
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

    if coherencia_fail is None and game_path_obs_str is not None and ejecutables_mo2:
        lanzadores_fuera = _lanzadores_del_juego_fuera_de(game_path_obs_str, ejecutables_mo2)
        if lanzadores_fuera:
            coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
            coherencia_detail = (
                f"lanzadores de juego en [customExecutables] registrados fuera de gamePath ('{game_path_obs_str}'): "
                + "; ".join(lanzadores_fuera)
            )
        elif (
            transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION
            or outgoing_manual_runtime_root is not None
        ):
            exes_steam = _ejecutables_dependientes_de_steam(ejecutables_mo2, ms_root_resolved)
            if exes_steam:
                coherencia_fail = PreflightReasonCode.CONFIG_DIVERGENCE_DETECTED
                coherencia_detail = (
                    "entradas en [customExecutables] siguen dependiendo de rutas administradas por Steam: "
                    + "; ".join(exes_steam)
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
    # 13. Resolución diferida de Steam ACTIVE bajo STOCK_INDEPENDENT_TRANSITION (R1)
    # -------------------------------------------------------------------------
    if steam_active_pendiente_exencion:
        otros_codigos = [c for c in PreflightCheckCode if c is not PreflightCheckCode.MANAGED_SOURCE]
        otros_todos_pass = (
            all(c in checks_map and checks_map[c].status is PreflightCheckStatus.PASS for c in otros_codigos)
            and stock_digest_verificado is not None
            and not reasons
        )
        if otros_todos_pass:
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.PASS,
                reason=PreflightReasonCode.PREFLIGHT_ALL_CHECKS_PASSED,
                detail=(
                    f"Steam ACTIVE admitido bajo STOCK_INDEPENDENT_TRANSITION: procedencia Stock v2 + "
                    f"digest A1 aprobado ({stock_digest_verificado}) y cero superficies/ejecutables en Steam "
                    f"(buildid {steam_obs_buildid}, state_flags {steam_obs_flags})"
                ),
            )
        else:
            checks_map[PreflightCheckCode.MANAGED_SOURCE] = PreflightCheckResult(
                check=PreflightCheckCode.MANAGED_SOURCE,
                status=PreflightCheckStatus.FAIL,
                reason=PreflightReasonCode.MANAGED_SOURCE_INVALID,
                detail=(
                    "Steam en estado ACTIVE y no se satisfacen todas las garantías de aislamiento Stock independiente"
                ),
            )
            reasons.append(PreflightReasonCode.MANAGED_SOURCE_INVALID)

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
        final_reasons = _deduplicar_motivos(reasons)

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
        transition_policy=transition_policy,
        outgoing_manual_runtime_root=manual_resolved,
        stock_reference_digest=stock_digest_verificado,
        mo2_instance_identity_state=mo2_ident.state,
        assessed_at_ns=sello,
        notes=tuple(notes_list),
    )


__all__ = [
    "BinderPreflightAssessment",
    "BinderPreflightVerdict",
    "BinderTransitionPolicy",
    "PreflightCheckCode",
    "PreflightCheckResult",
    "PreflightCheckStatus",
    "PreflightReasonCode",
    "evaluar_binder_preflight",
]
