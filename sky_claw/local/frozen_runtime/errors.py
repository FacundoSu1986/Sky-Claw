"""Excepciones tipadas del subsistema Frozen Runtime (P1: Managed Source).

P1 es READ-ONLY sobre la Managed Source y fail-closed: toda condición
inobservable o ambigua se materializa como excepción tipada o estado
explícito, nunca como un valor por defecto silencioso.
"""

from __future__ import annotations


class FrozenRuntimeError(Exception):
    """Base de errores de Frozen Runtime."""


class MalformedVdfError(FrozenRuntimeError):
    """Entrada VDF/ACF sintácticamente inválida o con codificación ilegible."""


class DuplicateKeyVdfError(MalformedVdfError):
    """Clave duplicada en el mismo scope VDF/ACF (política explícita: fail-closed)."""


class ProviderEvidenceError(FrozenRuntimeError):
    """Sin evidencia del proveedor para etiquetar la ruta como Managed Source.

    Una copia externa con ``SkyrimSE.exe`` NO es una Managed Source de Steam:
    la identidad de proveedor requiere evidencia del proveedor (library de
    Steam + ``appmanifest_<appid>.acf``).
    """


class AmbiguousManagedSourceError(FrozenRuntimeError):
    """Múltiples candidatos válidos y distintos de Managed Source (sin desempate)."""


class FrozenRuntimeObservationError(FrozenRuntimeError):
    """No se pudo observar (identidad/inventario/metadata) la Managed Source."""


# ============================================================================
# Storage (P2)
# ============================================================================


class FrozenRuntimeStorageError(FrozenRuntimeError):
    """Base de errores de storage de Frozen Runtime (P2)."""


class StateCorruptError(FrozenRuntimeStorageError):
    """Estado persistente malformado, truncado o ilegible (fail-closed).

    Distingue ``state absent`` (arranque limpio) de ``state present but
    corrupt`` (jamás se cae silenciosamente a "sin active generation").
    """


class StateSchemaError(FrozenRuntimeStorageError):
    """Estado con schema desconocido o campos inválidos (fail-closed)."""


class InvalidGenerationIdError(FrozenRuntimeStorageError):
    """generation-id con caracteres, formato, traversal o forma reservada inválida."""


class GenerationCollisionError(FrozenRuntimeStorageError):
    """generation-id existente cuya identidad completa difiere (fail-closed).

    La idempotencia exige que full tree digest + runtime identity + critical
    evidence coincidan; si no, nunca se sobreescribe en silencio.
    """


# ============================================================================
# Candidates (P3)
# ============================================================================


class InvalidCandidateIdError(FrozenRuntimeStorageError):
    """candidate-id con formato, traversal o forma reservada invalido."""


class CandidateError(FrozenRuntimeError):
    """Base de fallos del ciclo de vida de un Candidate (P3).

    Se materializan como excepcion o como estado INVALID persistido con su
    motivo: nunca como un ``False`` ambiguo que el caller pueda interpretar
    como "copia ok".
    """


class CandidateCopyError(CandidateError):
    """La copia fisica independiente fallo (no se produjo un Candidate usable)."""


class CandidateVerificationError(CandidateError):
    """Un Candidate no supera la verificacion fresca contra PRE/POST."""


class CandidateIdCollisionError(CandidateError):
    """El ``candidate_id`` ya esta reservado por un Candidate existente.

    Se lanza desde la RESERVA del id, que ocurre ANTES de cualquier escritura de
    metadata, para que una colision de ids no pueda pisar un Candidate ya
    construido. Es un `CandidateError` y no un error de verificacion: el
    candidato que existia sigue siendo valido.
    """


class CandidateCorruptMetadataError(FrozenRuntimeStorageError):
    """Metadata de Candidate ausente en el payload, corrupta o de schema desconocido.

    Fail-closed en el sentido fuerte: nunca se interpreta como "sin Candidate"
    ni como "Candidate valido". Un Candidate sin metadata legible es UNKNOWN y
    jamas READY.
    """
