"""Modelos inmutables de storage de Frozen Runtime — P2 (Generation model).

Contratos ADR 0012:

- ``FrozenRuntimeState`` registra la **Desired Active Generation** (SFR-16);
  NO registra Effective Runtime: que MO2/SKSE ejecuten esa generación se
  demuestra en P5, y este archivo jamás afirma por sí solo promoción exitosa.
- Una Generation publicada es lógicamente inmutable (SFR-17); el estado se
  deriva de evidencia fresca, no de flags persistentes que envejecen.
- SFR-18: la independencia física Generation↔Managed Source es contrato
  verificable (symlink/junction/reparse/hardlink/alias/contención).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from enum import StrEnum

from sky_claw.local.frozen_runtime.errors import CandidateVerificationError
from sky_claw.local.frozen_runtime.membership import DirectoryMembershipEvidence
from sky_claw.local.frozen_runtime.models import ProviderMetadataObservation
from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest


class IndependenceState(StrEnum):
    """Veredictos de independencia física (SFR-18)."""

    INDEPENDENT = "independent"
    VIOLATED = "violated"
    INDETERMINATE = "indeterminate"


class GenerationVerificationState(StrEnum):
    """Clasificación tipada de una Generation conocida.

    Se deriva de evidencia fresca (inventario sellado + identidad), no de
    flags persistentes: ``DRIFTED != VALID`` y jamás es target de rollback sin
    re-verificación exitosa.
    """

    VALID = "valid"
    DRIFTED = "drifted"
    INVALID = "invalid"
    UNKNOWN = "unknown"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class SharedObjectEvidence:
    """Evidencia de un objeto NTFS compartido entre Generation y Managed Source."""

    rel_path_generation: str
    rel_path_source: str
    volume_serial: int
    file_index: int


@dataclass(frozen=True, slots=True)
class PhysicalIndependenceResult:
    """Resultado tipado de la verificación de independencia física (SFR-18)."""

    state: IndependenceState
    message: str = ""
    shared_objects: tuple[SharedObjectEvidence, ...] = ()

    @property
    def success(self) -> bool:
        return self.state is IndependenceState.INDEPENDENT


@dataclass(frozen=True, slots=True)
class GenerationMetadata:
    """Metadata inmutable de una Generation publicada (``state/generations/<id>.json``).

    Vive FUERA del árbol de la Generation a propósito: así el árbol de la
    generación permanece byte-idéntico al snapshot del que nació (el digest no
    cambia al publicar) y la verificación no necesita filtrados especiales.
    ``provider_metadata`` es auxiliar (nunca autoridad; el buildid no define
    la identidad de la Generation).
    """

    schema_version: int
    generation_id: str
    display_version: str
    runtime_identity: RuntimeIdentity
    tree_digest: TreeDigest
    critical_files: tuple[FileIdentity, ...]
    provider: str
    provider_appid: str | None = None
    provider_buildid: str | None = None
    created_at_ns: int = 0


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    """Una Generation descubierta bajo ``versions/``: clasificada, no activada."""

    generation_id: str | None
    directory: pathlib.Path
    metadata: GenerationMetadata | None
    state: GenerationVerificationState
    message: str = ""


@dataclass(frozen=True, slots=True)
class GenerationInventory:
    """Listado tipado de ``versions/`` (sin activar nada)."""

    root: pathlib.Path
    records: tuple[GenerationRecord, ...]


@dataclass(frozen=True, slots=True)
class GenerationVerificationResult:
    """Resultado de la verificación on-demand de una Generation (P2 hook)."""

    state: GenerationVerificationState
    message: str = ""
    recorded: GenerationMetadata | None = None
    observed_digest: TreeDigest | None = None
    observed_identity: RuntimeIdentity | None = None

    @property
    def success(self) -> bool:
        return self.state is GenerationVerificationState.VALID


class StorageAdmissionState(StrEnum):
    """Desenlace de la admisión del root de storage (fail-closed)."""

    ADMITTED = "admitted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class StorageAdmissionResult:
    """Resultado tipado de la admisión de rutas del storage."""

    state: StorageAdmissionState
    message: str = ""
    root: pathlib.Path | None = None

    @property
    def success(self) -> bool:
        return self.state is StorageAdmissionState.ADMITTED


@dataclass(frozen=True, slots=True)
class StorageInitResult:
    """Resultado de la inicialización (idempotente, no destructiva)."""

    admission: StorageAdmissionResult
    root: pathlib.Path
    created_dirs: tuple[pathlib.Path, ...] = ()
    state_initialized: bool = False

    @property
    def success(self) -> bool:
        return self.admission.success


@dataclass(frozen=True, slots=True)
class FrozenRuntimeState:
    """Intención persistente de Sky-Claw (schema v1).

    ``desired_active_generation=None`` = arranque limpio (sin Generation
    activa todavía). Es el Desired de SFR-16: NO registra Effective Runtime.
    """

    schema_version: int
    desired_active_generation: str | None
    updated_at_ns: int


@dataclass(frozen=True, slots=True)
class FrozenRuntimeStateLoadResult:
    """Carga tipada del estado: distingue ausente de corrupto.

    ``found=False, state=None`` = estado ausente (pre-inicialización o
    arranque limpio). Un estado presente pero corrupto LANZA (fail-closed);
    nunca se interpreta como ausente.
    """

    found: bool
    state: FrozenRuntimeState | None
    message: str = ""


# ============================================================================
# Candidates (P3)
# ============================================================================


class CandidateState(StrEnum):
    """Estados de un Candidate (P3).

    ``READY`` es un estado PRIVILEGIADO: se alcanza solo despues de que la copia
    quedo verificada contra evidencia independiente de la Managed Source en las
    tres posiciones. Un Candidate recien creado es ``BUILDING`` y nunca ``READY``
    por defecto.
    """

    BUILDING = "building"
    READY = "ready"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class CandidateSourceEvidence:
    """Evidencia de la Managed Source en UNA posicion (PRE o POST).

    Es la unidad de autoridad (SFR-15). Se produce unicamente de una observacion
    fresca de la Managed Source, jamas de un Candidate. Lleva la membership de
    directorios porque el ``TreeDigest`` es ciego a los vacios.

    ``buildid`` se conserva como AUXILIAR dentro de ``provider_metadata``: nunca
    define identidad primaria, y su ausencia (``None``) no genera falso positivo.
    """

    provider: str
    appid: str
    game_key: str
    runtime_identity: RuntimeIdentity
    tree_digest: TreeDigest
    directory_membership: DirectoryMembershipEvidence
    critical_files: tuple[FileIdentity, ...]
    provider_metadata: ProviderMetadataObservation
    observed_at_ns: int
    files: tuple[FileIdentity, ...] = ()

    @property
    def buildid(self) -> str | None:
        """buildid observado (auxiliar); ``None`` si el manifest no lo expone."""
        return self.provider_metadata.buildid

    @property
    def archivos(self) -> tuple[FileIdentity, ...]:
        """Enumeracion SELLADA completa; es lo que la copia debe transferir."""
        return self.files


@dataclass(frozen=True, slots=True)
class CandidateMetadata:
    """Metadata persistente de un Candidate (``state/candidates/<id>.json``).

    Vive FUERA de ``payload/`` a proposito: dentro contaminaria el
    ``TreeDigest`` y la ``DirectoryMembership`` que P3 acaba de sellar.

    La invariante que hace ``READY`` alcanzable solo con evidencia completa:
    :meth:`exigir_listo_para_persistencia` lanza si se intenta persistir READY
    sin ``pre_source_evidence``, ``candidate_evidence`` ni
    ``post_source_evidence``.
    """

    schema_version: int
    candidate_id: str
    state: CandidateState
    created_at_ns: int
    updated_at_ns: int
    source_provider: str
    source_appid: str
    pre_source_evidence: CandidateSourceEvidence | None
    candidate_evidence: CandidateSourceEvidence | None
    post_source_evidence: CandidateSourceEvidence | None
    failure_reason: str | None = None

    def exigir_listo_para_persistencia(self) -> None:
        """Fail-closed: READY exige las TRES evidencias (SFR-15)."""
        if self.state is not CandidateState.READY:
            return
        faltantes = [
            nombre
            for nombre, valor in (
                ("pre_source_evidence", self.pre_source_evidence),
                ("candidate_evidence", self.candidate_evidence),
                ("post_source_evidence", self.post_source_evidence),
            )
            if valor is None
        ]
        if faltantes:
            raise CandidateVerificationError(
                "no se puede persistir un Candidate READY sin evidencia independiente completa: "
                f"faltan {', '.join(faltantes)} (SFR-15: el Candidate no puede autorizarse a si mismo)"
            )


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """Un Candidate descubierto bajo ``candidates/``: clasificado, no promovido."""

    candidate_id: str | None
    directory: pathlib.Path
    metadata: CandidateMetadata | None
    state: GenerationVerificationState
    message: str = ""


@dataclass(frozen=True, slots=True)
class CandidateInventory:
    """Listado tipado de ``candidates/`` (sin promover nada)."""

    root: pathlib.Path
    records: tuple[CandidateRecord, ...]


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Resultado tipado de crear/verificar un Candidate (nunca un bool opaco).

    ``state`` distingue VALID (READY) de INVALID de UNKNOWN de INDETERMINATE; el
    ``message`` lleva el motivo y la evidencia queda en los campos.
    """

    state: GenerationVerificationState
    message: str = ""
    candidate_id: str | None = None
    metadata: CandidateMetadata | None = None
    pre_source_evidence: CandidateSourceEvidence | None = None
    candidate_evidence: CandidateSourceEvidence | None = None
    post_source_evidence: CandidateSourceEvidence | None = None
    observed_digest: TreeDigest | None = None
    expected_digest: TreeDigest | None = None

    @property
    def success(self) -> bool:
        return self.state is GenerationVerificationState.VALID
