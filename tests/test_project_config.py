"""P0.3 contract tests: pytest reproducibility and project config invariants.

These tests assert static properties of configuration files so CI catches
regressions in project setup without spawning subprocesses.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# P0.3 — Pytest reproducible on Windows (no PermissionError on AppData\Temp)
# ---------------------------------------------------------------------------


def test_pytest_addopts_sets_basetemp() -> None:
    """pyproject.toml must set addopts with --basetemp=.pytest-tmp.

    RED path: [tool.pytest.ini_options] currently has no addopts key, so
    pytest uses AppData\\Local\\Temp\\pytest-of-User which raises PermissionError
    on Windows due to inherited ACLs from previous runs.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)

    addopts: str = (
        pyproject.get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("addopts", "")
    )
    assert "--basetemp" in addopts, (
        "[tool.pytest.ini_options] must include addopts with '--basetemp=.pytest-tmp' "
        "to avoid PermissionError on Windows (AppData ACL residues). "
        f"Current addopts: {addopts!r}"
    )
    assert ".pytest-tmp" in addopts, (
        "basetemp must point to '.pytest-tmp' (workspace-local, gitignored). "
        f"Current addopts: {addopts!r}"
    )


def test_pytest_tmp_dir_is_gitignored() -> None:
    """.pytest-tmp/ must be listed in .gitignore to avoid committing temp artifacts.

    RED path: .gitignore currently has no entry for .pytest-tmp.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.pytest-tmp", gitignore, re.MULTILINE), (
        ".gitignore must contain an entry matching '.pytest-tmp' "
        "so pytest basetemp artefacts are never committed."
    )


def test_conftest_has_session_finish_cleanup() -> None:
    """tests/conftest.py must define a pytest_sessionfinish hook for basetemp cleanup.

    RED path: conftest.py ends at line 84 with no pytest_sessionfinish hook,
    leaving .pytest-tmp with Windows ACL locks between runs.
    """
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "pytest_sessionfinish" in conftest, (
        "tests/conftest.py must define a 'pytest_sessionfinish' hook "
        "that cleans up .pytest-tmp with ACL-tolerant shutil.rmtree "
        "(onerror handler) to prevent PermissionError on consecutive Windows runs."
    )
