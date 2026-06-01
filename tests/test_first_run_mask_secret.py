"""Tests for the secret-masking helper in the first-run wizard (M-1).

Validates that ``_mask_secret`` never echoes an API key in clear text:
- a populated key shows only its last 4 characters (``****abcd``)
- short keys collapse to ``****`` (no leak of the full value)
- empty keys render as an empty string
- the original secret value is never returned verbatim
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

# first_run.py lives under local_scripts/scripts (not an importable package),
# so load it by path the same way the wizard bootstraps itself.
_FIRST_RUN_PATH = pathlib.Path(__file__).parent.parent / "local_scripts" / "scripts" / "first_run.py"
_spec = importlib.util.spec_from_file_location("first_run", _FIRST_RUN_PATH)
assert _spec is not None and _spec.loader is not None
_first_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_first_run)
_mask_secret = _first_run._mask_secret


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("sk-1234567890abcd", "****abcd"),  # long key → last 4 only
        ("ab", "****"),  # short key → fully masked
        ("abcd", "****"),  # exactly 4 → fully masked (no leak)
        ("", ""),  # empty → empty preview
    ],
)
def test_mask_secret_previews(secret: str, expected: str) -> None:
    assert _mask_secret(secret) == expected


def test_mask_secret_never_returns_full_secret() -> None:
    """A realistic key must not appear verbatim in the masked preview."""
    secret = "sk-proj-SUPERSECRETvalue1234"
    masked = _mask_secret(secret)
    assert secret not in masked
    assert masked == "****1234"
    # Only the trailing 4 chars are exposed; the sensitive prefix is gone.
    assert "SUPERSECRET" not in masked
