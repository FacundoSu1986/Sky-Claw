# -*- coding: utf-8 -*-
"""
Validador Anti-Path Traversal para prevenir ataques de recorrido de directorios.

Este módulo proporciona validación de rutas de archivos para proteger el sistema
contra ataques de path traversal que podrían permitir acceso a archivos sensibles.
"""

import logging
import pathlib
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Set

logger = logging.getLogger("SkyClaw.validators.path")

# Patrones de path traversal
PATH_TRAVERSAL_PATTERNS = [
    re.compile(r"\.\./", re.IGNORECASE),  # ../
    re.compile(r"\.\.\\", re.IGNORECASE),  # ..\
    re.compile(r"\.\.%2[fF]"),  # ..%2f (URL encoded /)
    re.compile(r"\.\.%5[cC]"),  # ..%5c (URL encoded \)
    re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)"),  # .. as a standalone path component
    re.compile(r"%2[eE]%2[eE]", re.IGNORECASE),  # %2e%2e (URL encoded ..)
    re.compile(r"%2[eE]\.", re.IGNORECASE),  # %2e. (half-encoded ..)
    re.compile(r"\.%2[eE]", re.IGNORECASE),  # .%2e (half-encoded ..)
    re.compile(r"%252[eE]", re.IGNORECASE),  # %252e (double URL encoded .)
    re.compile(r"%[cC]0%[aA][fF]"),  # %c0%af (overlong UTF-8 for /)
    re.compile(r"%[cC]1%9[cC]"),  # %c1%9c (overlong UTF-8 for \)
]

# Patrones de bytes nulos
NULL_BYTE_PATTERNS = [
    re.compile(r"%00"),  # URL encoded null
    re.compile(r"\\x00"),  # Escape sequence null
    re.compile(r"\0"),  # Literal null
]

# Prefijos peligrosos (rutas absolutas/UNC)
DANGEROUS_PREFIXES = [
    "/",  # Unix absolute
    "\\\\",  # UNC path
]

# Patrones peligrosos de Windows
DANGEROUS_WINDOWS_PATTERNS = [
    re.compile(r"^[a-zA-Z]:"),  # Drive letter
    re.compile(r":.*:"),  # ADS stream (multiple colons)
]


@dataclass
class PathValidationResult:
    """Resultado de validación de path."""

    is_valid: bool
    normalized_path: Optional[str]
    error_message: Optional[str]
    is_absolute: bool


class PathTraversalValidator:
    """
    Validador de paths para prevenir path traversal.

    Este validador verifica que las rutas no contengan:
    - Secuencias de path traversal (../, ..\\)
    - Bytes nulos (%00, \\x00, \\0)
    - Rutas UNC peligrosas (\\server\\share)
    - Rutas absolutas no autorizadas

    Attributes:
        allowed_roots: Conjunto de directorios permitidos para rutas absolutas
        allow_absolute: Si se permiten rutas absolutas (con validación)
    """

    def __init__(
        self,
        allowed_roots: Optional[Set[pathlib.Path]] = None,
        allow_absolute: bool = False,
    ):
        """
        Inicializa el validador de path traversal.

        Args:
            allowed_roots: Directorios permitidos para rutas absolutas.
                          Si se especifica, las rutas absolutas deben estar
                          dentro de uno de estos directorios.
            allow_absolute: Si se permiten rutas absolutas (requiere allowed_roots
                           o validación adicional)
        """
        self.allowed_roots = allowed_roots or set()
        self.allow_absolute = allow_absolute

    def validate(self, path: str) -> PathValidationResult:
        """
        Valida un path contra ataques de path traversal.

        Pasos de validación:
        1. Verificar que el path no esté vacío
        2. Verificar bytes nulos
        3. Verificar patrones de traversal (raw y URL-encoded/double-encoded)
        4. Verificar prefijos peligrosos (rutas absolutas)
        5. Normalizar path
        6. Verificar contra roots permitidos (si aplica)

        Args:
            path: Ruta a validar

        Returns:
            PathValidationResult con el resultado de la validación
        """
        if not path or not path.strip():
            return PathValidationResult(
                is_valid=False,
                normalized_path=None,
                error_message="Path vacío",
                is_absolute=False,
            )

        original_path = path

        # Paso 1: Verificar bytes nulos (patrones URL-encoded y escape sequences)
        for pattern in NULL_BYTE_PATTERNS:
            if pattern.search(path):
                logger.warning(f"Path traversal blocked: null byte en {original_path}")
                return PathValidationResult(
                    is_valid=False,
                    normalized_path=None,
                    error_message="Byte nulo detectado en path",
                    is_absolute=False,
                )

        # Verificar byte nulo literal
        if "\x00" in path:
            logger.warning(
                f"Path traversal blocked: null byte literal en {original_path}"
            )
            return PathValidationResult(
                is_valid=False,
                normalized_path=None,
                error_message="Byte nulo literal detectado en path",
                is_absolute=False,
            )

        # Paso 2: Verificar patrones de traversal en el path original
        for pattern in PATH_TRAVERSAL_PATTERNS:
            if pattern.search(path):
                logger.warning(
                    f"Path traversal blocked: patrón {pattern.pattern} en {original_path}"
                )
                return PathValidationResult(
                    is_valid=False,
                    normalized_path=None,
                    error_message="Secuencia de path traversal detectada",
                    is_absolute=False,
                )

        # Paso 2b: Verificar con URL-decode para detectar ataques de codificación
        # Se decodifica hasta dos veces para cubrir doble codificación (%252e → %2e → .)
        try:
            decoded_once = urllib.parse.unquote(path)
            decoded_twice = urllib.parse.unquote(decoded_once)
            # Only check each decoded form that differs from the previous step
            candidates = []
            if decoded_once != path:
                candidates.append(decoded_once)
            if decoded_twice != decoded_once:
                candidates.append(decoded_twice)
            for decoded_path in candidates:
                for pattern in PATH_TRAVERSAL_PATTERNS:
                    if pattern.search(decoded_path):
                        logger.warning(
                            f"Path traversal blocked: patrón codificado {pattern.pattern} en {original_path}"
                        )
                        return PathValidationResult(
                            is_valid=False,
                            normalized_path=None,
                            error_message="Secuencia de path traversal detectada (codificada)",
                            is_absolute=False,
                        )
                if any(decoded_path.startswith(p) for p in DANGEROUS_PREFIXES):
                    logger.warning(
                        f"Path traversal blocked: path absoluto codificado - {original_path}"
                    )
                    return PathValidationResult(
                        is_valid=False,
                        normalized_path=None,
                        error_message="Ruta absoluta detectada en path codificado",
                        is_absolute=True,
                    )
                for wp in DANGEROUS_WINDOWS_PATTERNS:
                    if wp.search(decoded_path):
                        logger.warning(
                            f"Path traversal blocked: patrón Windows codificado en {original_path}"
                        )
                        return PathValidationResult(
                            is_valid=False,
                            normalized_path=None,
                            error_message="Patrón de path Windows detectado en path codificado",
                            is_absolute=True,
                        )
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning(
                f"Path traversal: decode falló para {original_path!r}: {exc}"
            )

        # Paso 3: Verificar si es ruta absoluta
        is_absolute = any(path.startswith(p) for p in DANGEROUS_PREFIXES)
        is_windows_absolute = any(p.search(path) for p in DANGEROUS_WINDOWS_PATTERNS)

        if (is_absolute or is_windows_absolute) and not self.allow_absolute:
            logger.warning(
                f"Path traversal blocked: path absoluto no permitido - {original_path}"
            )
            return PathValidationResult(
                is_valid=False,
                normalized_path=None,
                error_message="Rutas absolutas no permitidas",
                is_absolute=True,
            )

        # Paso 4: Normalizar path
        try:
            # Usar pathlib para normalización cross-platform
            normalized = pathlib.Path(path)
            normalized_str = str(normalized).replace("\\", "/")
        except Exception as e:
            return PathValidationResult(
                is_valid=False,
                normalized_path=None,
                error_message=f"Error normalizando path: {e}",
                is_absolute=False,
            )

        # Paso 5: Si es absoluto y hay roots, verificar contra ellos
        if (is_absolute or is_windows_absolute) and self.allowed_roots:
            try:
                resolved = normalized.resolve()
                in_allowed_root = False
                for root in self.allowed_roots:
                    try:
                        resolved.relative_to(root.resolve())
                        in_allowed_root = True
                        break
                    except ValueError:
                        continue

                if not in_allowed_root:
                    logger.warning(
                        f"Path traversal blocked: path fuera de roots - {original_path}"
                    )
                    return PathValidationResult(
                        is_valid=False,
                        normalized_path=None,
                        error_message="Path absoluto fuera de directorios permitidos",
                        is_absolute=True,
                    )
            except Exception as e:
                return PathValidationResult(
                    is_valid=False,
                    normalized_path=None,
                    error_message=f"Error resolviendo path: {e}",
                    is_absolute=True,
                )

        return PathValidationResult(
            is_valid=True,
            normalized_path=normalized_str,
            error_message=None,
            is_absolute=is_absolute or is_windows_absolute,
        )


def validate_path_traversal(
    path: str, allowed_roots: Optional[Set[pathlib.Path]] = None
) -> str:
    """
    Función de conveniencia para usar como field_validator.

    Valida un path contra ataques de path traversal usando
    configuración por defecto (sin rutas absolutas permitidas).

    Args:
        path: Path a validar
        allowed_roots: Roots permitidos para paths absolutos (opcional)

    Returns:
        str: Path normalizado si es válido

    Raises:
        ValueError: Si el path es inválido o representa riesgo de traversal
    """
    validator = PathTraversalValidator(
        allowed_roots=allowed_roots, allow_absolute=allowed_roots is not None
    )
    result = validator.validate(path)

    if not result.is_valid:
        raise ValueError(f"Validación de path fallida: {result.error_message}")

    return result.normalized_path


def validate_path_strict(path: str) -> str:
    """
    Función de conveniencia para validación estricta de paths.

    Esta función rechaza rutas absolutas y aplica todas las
    validaciones de path traversal. Es la función más restrictiva.

    Args:
        path: Path a validar

    Returns:
        str: Path normalizado si es válido

    Raises:
        ValueError: Si el path es inválido o representa riesgo de traversal
    """
    validator = PathTraversalValidator(allow_absolute=False)
    result = validator.validate(path)

    if not result.is_valid:
        raise ValueError(f"Validación de path fallida: {result.error_message}")

    return result.normalized_path
