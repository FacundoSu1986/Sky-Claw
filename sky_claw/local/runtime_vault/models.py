"""Modelos inmutables del Runtime Vault (RV-1): identidad de runtime y de árbol.

Runtime Identity + Tree Verification Core. La identidad que se afirma es, por
archivo, la tripla (relpath canónico posix, size, sha256) y, por árbol, el
digest agregado más sus dimensiones (files, bytes). Lo que NO forma parte de la
identidad: el mtime (excluido por contrato) y el root físico (los relpaths son
relativos a la raíz del árbol y el digest no conoce dónde vive el árbol).

``RuntimeIdentity`` es un DTO de DATOS DE ENTRADA: el vault no escanea el host;
quien observa el runtime (EnvironmentScanner u otro) entrega lo que vio. Una
``game_version`` vacía significa "no observado" — y sin versión no hay VERIFIED
posible (UNKNOWN != VERIFIED).
"""

from __future__ import annotations

import pathlib
import string
from dataclasses import dataclass
from enum import StrEnum


class VerificationState(StrEnum):
    """Estado de una verificación. UNKNOWN != VERIFIED: sin identidad que afirmar, no hay verde."""

    VERIFIED = "verified"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Identidad observada del runtime, recibida como datos de entrada.

    Args:
        game_key: clave canónica del juego (p. ej. ``"skyrimse"``).
        game_version: versión observada (p. ej. ``"1.6.1170.0"``); ``""``
            significa "no observado" y hace imposible el estado VERIFIED.
    """

    game_key: str
    game_version: str


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Identidad de un archivo: relpath canónico (posix) + tamaño + sha256 hex.

    El tamaño sale de los bytes leídos junto al digest, para que tamaño y
    contenido no puedan descalzarse. El mtime NO existe como campo.
    """

    rel_path: str
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class TreeDigest:
    """Identidad de un árbol: digest agregado + dimensiones.

    La tupla (digest, files, bytes) ES la identidad: un digest solo no
    distingue un árbol vacío de un árbol con un archivo vacío, y no detecta
    archivos de más o de menos. Nada acá depende del root físico.
    """

    digest: str
    files: int
    bytes: int


@dataclass(frozen=True, slots=True)
class TreeVerificationResult:
    """Resultado de verificar un árbol observado contra un ``TreeDigest`` esperado."""

    state: VerificationState
    message: str = ""
    expected: TreeDigest | None = None
    observed: TreeDigest | None = None

    @property
    def success(self) -> bool:
        """Contrato ``success``/``message`` del repo: True SOLO en VERIFIED.

        Es una propiedad derivada a propósito: no puede existir un resultado
        con ``success=True`` y estado distinto de VERIFIED (UNKNOWN != VERIFIED
        queda garantizado por estructura, no por disciplina del caller).
        """
        return self.state is VerificationState.VERIFIED


@dataclass(frozen=True, slots=True)
class RuntimeVerificationResult:
    """Resultado de comparar identidades de runtime (esperada vs observada)."""

    state: VerificationState
    message: str = ""
    expected: RuntimeIdentity | None = None
    observed: RuntimeIdentity | None = None

    @property
    def success(self) -> bool:
        return self.state is VerificationState.VERIFIED


def _validar_y_normalizar_relpath(rel_path: str) -> str:
    """Valida y normaliza un relpath de archivo crítico para evitar path traversal o absolute path."""
    if not isinstance(rel_path, str):
        raise ValueError("rel_path debe ser un string")
    rel_limpio = rel_path.strip()
    if not rel_limpio:
        raise ValueError("El path crítico no puede ser vacío ni whitespace-only")
    if "\x00" in rel_limpio:
        raise ValueError("El path crítico no puede contener bytes NUL")
    if ":" in rel_limpio:
        raise ValueError("El path crítico no puede contener dos puntos ni letras de unidad de Windows")
    if rel_limpio.startswith(("\\\\", "//")):
        raise ValueError("El path crítico no puede ser una ruta UNC")
    if rel_limpio.startswith(("/", "\\")):
        raise ValueError("El path crítico no puede ser absoluto")

    partes = [p for p in rel_limpio.replace("\\", "/").split("/") if p and p != "."]
    if not partes:
        raise ValueError("El path crítico no contiene segmentos válidos")
    if any(p == ".." for p in partes):
        raise ValueError("El path crítico no puede contener '..' (escape de directorio / traversal)")

    return "/".join(partes)


def _validar_y_normalizar_sha256(digest: str) -> str:
    """Valida y normaliza un digest sha256 a 64 caracteres hex en minúsculas."""
    if not isinstance(digest, str):
        raise ValueError("El digest esperado debe ser un string")
    digest_limpio = digest.strip().lower()
    if len(digest_limpio) != 64 or not all(c in string.hexdigits for c in digest_limpio):
        raise ValueError(f"El digest esperado '{digest}' no es un sha256 hexadecimal válido de 64 caracteres")
    return digest_limpio


@dataclass(frozen=True, slots=True)
class CriticalFileExpectation:
    """Expectativa sobre un archivo crítico del árbol verificado.

    Args:
        rel_path: path relativo canónico dentro del árbol (anti-traversal).
        expected_digest: hash sha256 esperado (hex 64 chars).
        expected_size: tamaño esperado en bytes (opcional, >= 0).
    """

    rel_path: str
    expected_digest: str
    expected_size: int | None = None

    def __post_init__(self) -> None:
        rel_norm = _validar_y_normalizar_relpath(self.rel_path)
        digest_norm = _validar_y_normalizar_sha256(self.expected_digest)
        if self.expected_size is not None and self.expected_size < 0:
            raise ValueError("expected_size no puede ser negativo")
        object.__setattr__(self, "rel_path", rel_norm)
        object.__setattr__(self, "expected_digest", digest_norm)


@dataclass(frozen=True, slots=True)
class CriticalFileEvidence:
    """Resultado de contrastar la evidencia observada de un archivo crítico contra su expectativa."""

    rel_path: str
    state: VerificationState
    message: str = ""
    expected_digest: str | None = None
    observed_digest: str | None = None
    expected_size: int | None = None
    observed_size: int | None = None

    def __post_init__(self) -> None:
        if self.state is VerificationState.VERIFIED:
            if self.expected_digest is None or self.observed_digest is None:
                raise ValueError("CriticalFileEvidence VERIFIED requiere expected_digest y observed_digest no nulos")
            if self.expected_digest.lower() != self.observed_digest.lower():
                raise ValueError(
                    "CriticalFileEvidence VERIFIED requiere que expected_digest coincida con observed_digest"
                )
            if self.expected_size is not None and (
                self.observed_size is None or self.observed_size != self.expected_size
            ):
                raise ValueError("CriticalFileEvidence VERIFIED requiere que observed_size coincida con expected_size")

    @property
    def success(self) -> bool:
        return self.state is VerificationState.VERIFIED


@dataclass(frozen=True, slots=True)
class GoldenMasterCandidate:
    """Candidato a Golden Master: ubicación física + identidad de runtime observada."""

    location: pathlib.Path
    observed_runtime: RuntimeIdentity | None = None


@dataclass(frozen=True, slots=True)
class GoldenMasterDescriptor:
    """Descriptor inmutable de un Golden Master verificado en memoria (rol REFERENCE_ONLY)."""

    location: pathlib.Path
    runtime_identity: RuntimeIdentity
    tree_digest: TreeDigest
    role: str = "reference_only"

    def __post_init__(self) -> None:
        if self.role != "reference_only":
            raise ValueError("GoldenMasterDescriptor solo admite role='reference_only'")


@dataclass(frozen=True, slots=True)
class GoldenMasterVerificationResult:
    """Resultado de la verificación completa de un candidato a Golden Master.

    Solo alcanza VERIFIED si el árbol, la identidad de runtime y todos los
    archivos críticos requeridos están en estado VERIFIED.
    """

    state: VerificationState
    message: str = ""
    tree_result: TreeVerificationResult | None = None
    runtime_result: RuntimeVerificationResult | None = None
    critical_results: tuple[CriticalFileEvidence, ...] = ()
    descriptor: GoldenMasterDescriptor | None = None

    def __post_init__(self) -> None:
        if self.state is VerificationState.VERIFIED:
            if self.tree_result is None or self.tree_result.state is not VerificationState.VERIFIED:
                raise ValueError("GoldenMasterVerificationResult VERIFIED requiere tree_result en estado VERIFIED")
            if (
                self.tree_result.expected is None
                or self.tree_result.observed is None
                or self.tree_result.expected != self.tree_result.observed
            ):
                raise ValueError(
                    "GoldenMasterVerificationResult VERIFIED requiere tree_result con expected == observed"
                )
            if self.runtime_result is None or self.runtime_result.state is not VerificationState.VERIFIED:
                raise ValueError("GoldenMasterVerificationResult VERIFIED requiere runtime_result en estado VERIFIED")
            if (
                self.runtime_result.expected is None
                or self.runtime_result.observed is None
                or self.runtime_result.expected != self.runtime_result.observed
            ):
                raise ValueError(
                    "GoldenMasterVerificationResult VERIFIED requiere runtime_result con expected == observed"
                )
            if not all(c.state is VerificationState.VERIFIED for c in self.critical_results):
                raise ValueError(
                    "GoldenMasterVerificationResult VERIFIED requiere que toda evidencia crítica esté en estado VERIFIED"
                )
            if self.descriptor is None:
                raise ValueError("GoldenMasterVerificationResult VERIFIED requiere descriptor no nulo")
            if self.descriptor.tree_digest != self.tree_result.observed:
                raise ValueError("El descriptor debe coincidir con el árbol observado")
            if self.descriptor.runtime_identity != self.runtime_result.observed:
                raise ValueError("El descriptor debe coincidir con el runtime observado")
        else:
            if self.descriptor is not None:
                raise ValueError(
                    f"GoldenMasterVerificationResult en estado '{self.state.value}' no puede tener descriptor"
                )

    @property
    def success(self) -> bool:
        return self.state is VerificationState.VERIFIED


class RuntimeVaultError(Exception):
    """Base de las excepciones de dominio del Runtime Vault."""


class InventoryError(RuntimeVaultError):
    """El árbol no es inspeccionable: no hay inventario que afirmar.

    Cubre: raíz ausente o no-directorio, árbol sin archivos propios, entradas
    ilegibles o inesperadas, y mutaciones concurrentes del árbol durante el
    recorrido. Nunca un inventario vacío que parezca válido.
    """


class InventoryLinkError(InventoryError):
    """Hay un enlace (symlink/junction/reparse point) dentro del scope: FAIL CLOSED.

    Un enlace inesperado no se sigue NI se ignora: el árbol deja de ser
    autorizable y el inventario se rehúsa.
    """


__all__ = [
    "CriticalFileEvidence",
    "CriticalFileExpectation",
    "FileIdentity",
    "GoldenMasterCandidate",
    "GoldenMasterDescriptor",
    "GoldenMasterVerificationResult",
    "InventoryError",
    "InventoryLinkError",
    "RuntimeIdentity",
    "RuntimeVaultError",
    "RuntimeVerificationResult",
    "TreeDigest",
    "TreeVerificationResult",
    "VerificationState",
]
