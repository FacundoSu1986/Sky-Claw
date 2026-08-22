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
    "FileIdentity",
    "InventoryError",
    "InventoryLinkError",
    "RuntimeIdentity",
    "RuntimeVaultError",
    "RuntimeVerificationResult",
    "TreeDigest",
    "TreeVerificationResult",
    "VerificationState",
]
