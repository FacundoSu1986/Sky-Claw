"""Pre-flight validator: blocks the launch if base autosaves are enabled.

Zero-Trust: the .ini path is validated through PathValidator before any I/O.
The validator never mutates the file; it only reads.
"""

from __future__ import annotations

import configparser
import logging
import pathlib
from dataclasses import dataclass

from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError

logger = logging.getLogger("SkyClaw.Validators.SafeSave")

_DANGEROUS_KEYS: tuple[str, ...] = (
    "bSaveOnTravel",
    "bSaveOnWait",
    "bSaveOnRest",
    "bSaveOnCharacterMenu",
)
_INSPECTED_SECTIONS: tuple[str, ...] = ("SaveGame", "General", "Main")
_DANGER_MESSAGE = (
    "Peligro de Corrupción: Los autoguardados base están activados. "
    "Desactívalos en tu skyrim.ini para proteger tu partida."
)


class SafeSaveValidationError(Exception):
    """Raised by validate_or_raise() when the pre-flight check fails."""

    def __init__(self, message: str, offending_keys: tuple[str, ...]) -> None:
        super().__init__(message)
        self.offending_keys = offending_keys


@dataclass(frozen=True)
class SafeSaveValidationResult:
    is_valid: bool
    error_message: str | None = None
    offending_keys: tuple[str, ...] = ()
    ini_path: pathlib.Path | None = None


class SafeSaveValidator:
    def __init__(self, path_validator: PathValidator) -> None:
        self._path_validator = path_validator

    def validate(self, ini_path: str | pathlib.Path) -> SafeSaveValidationResult:
        try:
            resolved = self._path_validator.validate(ini_path, strict_symlink=True)
        except PathViolationError as exc:
            logger.warning(
                "safe_save.path_violation",
                extra={"path": str(ini_path), "error": repr(exc)},
            )
            return SafeSaveValidationResult(
                is_valid=False,
                error_message=f"Ruta .ini inválida o fuera del root permitido: {exc}",
            )

        if not resolved.is_file():
            return SafeSaveValidationResult(
                is_valid=False,
                error_message=f"No se encontró el archivo de configuración: {resolved}",
                ini_path=resolved,
            )

        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read(resolved, encoding="utf-8-sig")
        except (configparser.Error, OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "safe_save.parse_error",
                extra={"path": str(resolved), "error": repr(exc)},
            )
            return SafeSaveValidationResult(
                is_valid=False,
                error_message=f"No se pudo parsear {resolved.name}: {exc}",
                ini_path=resolved,
            )

        offenders: set[str] = set()
        for section in _INSPECTED_SECTIONS:
            if not parser.has_section(section):
                continue
            for key in _DANGEROUS_KEYS:
                if not parser.has_option(section, key):
                    continue
                raw = parser.get(section, key, fallback="").strip()
                if raw == "1":
                    offenders.add(key)

        offending_keys = tuple(k for k in _DANGEROUS_KEYS if k in offenders)

        if offending_keys:
            logger.info(
                "safe_save.validation_failed",
                extra={"offenders": offending_keys, "path": str(resolved)},
            )
            return SafeSaveValidationResult(
                is_valid=False,
                error_message=_DANGER_MESSAGE,
                offending_keys=offending_keys,
                ini_path=resolved,
            )

        logger.info("safe_save.validation_passed", extra={"path": str(resolved)})
        return SafeSaveValidationResult(is_valid=True, ini_path=resolved)

    def validate_or_raise(self, ini_path: str | pathlib.Path) -> pathlib.Path:
        result = self.validate(ini_path)
        if not result.is_valid:
            raise SafeSaveValidationError(
                result.error_message or "validation failed",
                result.offending_keys,
            )
        assert result.ini_path is not None
        return result.ini_path
