"""Tests for sky_claw.local.validators.safe_save_validator.

All file I/O uses pytest's tmp_path fixture — no real game files are touched.
Target: 100% branch coverage.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.validators.safe_save_validator import (
    _DANGER_MESSAGE,
    _DANGEROUS_KEYS,
    SafeSaveValidationError,
    SafeSaveValidationResult,
    SafeSaveValidator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ini(
    tmp_path: pathlib.Path,
    section: str,
    kv: dict[str, str],
    *,
    encoding: str = "utf-8",
    filename: str = "Skyrim.ini",
    extra_body: str = "",
) -> pathlib.Path:
    ini = tmp_path / filename
    lines = [f"[{section}]"]
    for k, v in kv.items():
        lines.append(f"{k}={v}")
    body = "\n".join(lines) + "\n" + extra_body
    ini.write_text(body, encoding=encoding)
    return ini


def _real_validator(tmp_path: pathlib.Path) -> SafeSaveValidator:
    return SafeSaveValidator(path_validator=PathValidator(roots=[tmp_path]))


class _FakeViolatingValidator:
    """Minimal PathValidator stand-in that always rejects."""

    def validate(self, path: str | pathlib.Path, *, strict_symlink: bool = True) -> pathlib.Path:  # noqa: ARG002
        raise PathViolationError(f"forced violation for {path}")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_all_keys_off_passes(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {k: "0" for k in _DANGEROUS_KEYS})
        result = _real_validator(tmp_path).validate(ini)
        assert result == SafeSaveValidationResult(is_valid=True, ini_path=ini.resolve())

    def test_absent_section_is_not_a_failure(self, tmp_path: pathlib.Path) -> None:
        ini = tmp_path / "Skyrim.ini"
        ini.write_text("[Display]\niSize=1080\n", encoding="utf-8")
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is True
        assert result.offending_keys == ()

    def test_absent_keys_are_not_a_failure(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {"bOtherFlag": "1"})
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is True

    def test_non_one_values_pass(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(
            tmp_path,
            "SaveGame",
            {
                "bSaveOnTravel": "true",
                "bSaveOnWait": "2",
                "bSaveOnRest": "yes",
                "bSaveOnCharacterMenu": "",
            },
        )
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is True
        assert result.offending_keys == ()


# ---------------------------------------------------------------------------
# Failure detection
# ---------------------------------------------------------------------------


class TestFailureDetection:
    def test_single_key_on_fails_with_exact_message(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {"bSaveOnWait": "1"})
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.error_message == _DANGER_MESSAGE
        assert result.offending_keys == ("bSaveOnWait",)
        assert result.ini_path == ini.resolve()

    def test_all_keys_on_fails_with_ordered_offenders(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {k: "1" for k in _DANGEROUS_KEYS})
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.offending_keys == _DANGEROUS_KEYS

    def test_keys_in_main_section_also_detected(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "Main", {"bSaveOnRest": "1"})
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.offending_keys == ("bSaveOnRest",)

    def test_keys_in_general_section_also_detected(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "General", {"bSaveOnTravel": "1"})
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.offending_keys == ("bSaveOnTravel",)

    def test_case_insensitive_key_matching(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {"bsaveontravel": "1"})
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.offending_keys == ("bSaveOnTravel",)

    def test_value_with_whitespace_is_stripped(self, tmp_path: pathlib.Path) -> None:
        ini = tmp_path / "Skyrim.ini"
        ini.write_text("[SaveGame]\nbSaveOnTravel =   1   \n", encoding="utf-8")
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.offending_keys == ("bSaveOnTravel",)


# ---------------------------------------------------------------------------
# File-level edge cases
# ---------------------------------------------------------------------------


class TestFileEdgeCases:
    def test_path_violation_returns_invalid_result(self, tmp_path: pathlib.Path) -> None:
        validator = SafeSaveValidator(path_validator=_FakeViolatingValidator())  # type: ignore[arg-type]
        result = validator.validate(tmp_path / "Skyrim.ini")
        assert result.is_valid is False
        assert "fuera del root permitido" in (result.error_message or "")
        assert result.offending_keys == ()
        assert result.ini_path is None

    def test_missing_file_returns_invalid_result(self, tmp_path: pathlib.Path) -> None:
        missing = tmp_path / "DoesNotExist.ini"
        result = _real_validator(tmp_path).validate(missing)
        assert result.is_valid is False
        assert "No se encontró" in (result.error_message or "")

    def test_corrupt_ini_returns_parse_error(self, tmp_path: pathlib.Path) -> None:
        ini = tmp_path / "Skyrim.ini"
        ini.write_text("[unclosed_section\nbSaveOnTravel=1\n", encoding="utf-8")
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert "No se pudo parsear" in (result.error_message or "")
        assert result.ini_path == ini.resolve()

    def test_utf8_with_bom_is_parsed(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {"bSaveOnTravel": "1"}, encoding="utf-8-sig")
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.offending_keys == ("bSaveOnTravel",)

    def test_duplicate_sections_tolerated(self, tmp_path: pathlib.Path) -> None:
        ini = tmp_path / "Skyrim.ini"
        ini.write_text(
            "[SaveGame]\nbSaveOnWait=0\n[SaveGame]\nbSaveOnRest=0\n",
            encoding="utf-8",
        )
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is True

    def test_duplicate_keys_with_second_one_detects_failure(self, tmp_path: pathlib.Path) -> None:
        ini = tmp_path / "Skyrim.ini"
        ini.write_text(
            "[SaveGame]\nbSaveOnTravel=0\nbSaveOnTravel=1\n",
            encoding="utf-8",
        )
        result = _real_validator(tmp_path).validate(ini)
        assert result.is_valid is False
        assert result.offending_keys == ("bSaveOnTravel",)


# ---------------------------------------------------------------------------
# validate_or_raise + logging
# ---------------------------------------------------------------------------


class TestValidateOrRaise:
    def test_raises_on_failure(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {"bSaveOnTravel": "1"})
        validator = _real_validator(tmp_path)
        with pytest.raises(SafeSaveValidationError, match="Peligro de Corrupción") as ei:
            validator.validate_or_raise(ini)
        assert ei.value.offending_keys == ("bSaveOnTravel",)

    def test_returns_path_on_success(self, tmp_path: pathlib.Path) -> None:
        ini = _make_ini(tmp_path, "SaveGame", {k: "0" for k in _DANGEROUS_KEYS})
        validator = _real_validator(tmp_path)
        returned = validator.validate_or_raise(ini)
        assert returned == ini.resolve()


class TestLogging:
    def test_logger_emits_expected_events(self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
        ok_ini = _make_ini(tmp_path, "SaveGame", {k: "0" for k in _DANGEROUS_KEYS})
        bad_ini = _make_ini(
            tmp_path,
            "SaveGame",
            {"bSaveOnTravel": "1"},
            filename="BadSkyrim.ini",
        )
        validator = _real_validator(tmp_path)

        with caplog.at_level(logging.INFO, logger="SkyClaw.Validators.SafeSave"):
            validator.validate(ok_ini)
            validator.validate(bad_ini)

        messages = {rec.message for rec in caplog.records}
        assert "safe_save.validation_passed" in messages
        assert "safe_save.validation_failed" in messages
