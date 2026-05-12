"""P0.1 / P0.2 contract tests: launcher mode coherence and build fail-fast.

These tests are intentionally static (no process spawning) — they parse the
bat files and __main__.py source to assert invariants that CI must enforce.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _argparse_mode_choices() -> set[str]:
    """Extract --mode choices from sky_claw/__main__.py via AST (no import side-effects)."""
    source = (REPO_ROOT / "sky_claw" / "__main__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "--mode":
            continue
        for kw in node.keywords:
            if kw.arg == "choices" and isinstance(kw.value, ast.List):
                return {elt.value for elt in kw.value.elts if isinstance(elt, ast.Constant)}
    return set()


# ---------------------------------------------------------------------------
# P0.1 — Launcher coherence
# ---------------------------------------------------------------------------


def test_skyclaw_bat_uses_valid_mode() -> None:
    """All --mode X invocations in SkyClawApp.bat must be valid argparse choices.

    RED path: SkyClawApp.bat:52 uses ``--mode web`` which is absent from
    choices=["cli", "telegram", "oneshot", "gui", "security"].
    """
    bat_content = (REPO_ROOT / "SkyClawApp.bat").read_text(encoding="utf-8")
    modes_in_bat = re.findall(r"--mode\s+(\w+)", bat_content)

    assert modes_in_bat, "No --mode invocations found in SkyClawApp.bat"

    valid_choices = _argparse_mode_choices()
    assert valid_choices, "Could not extract argparse choices from sky_claw/__main__.py"

    invalid_modes = [m for m in modes_in_bat if m not in valid_choices]
    assert not invalid_modes, (
        f"SkyClawApp.bat uses invalid --mode value(s): {invalid_modes}. "
        f"Valid argparse choices are: {sorted(valid_choices)}"
    )


def test_argparse_choices_matches_documented_modes() -> None:
    """The docstring examples in __main__.py must only reference valid choices."""
    source = (REPO_ROOT / "sky_claw" / "__main__.py").read_text(encoding="utf-8")
    valid_choices = _argparse_mode_choices()
    assert valid_choices, "Could not extract choices"

    # Extract modes mentioned in the module docstring (``--mode <word>``)
    tree = ast.parse(source)
    docstring = ast.get_docstring(tree) or ""
    docstring_modes = re.findall(r"--mode\s+(\w+)", docstring)
    invalid = [m for m in docstring_modes if m not in valid_choices]
    assert not invalid, f"__main__.py docstring references undeclared modes: {invalid}"


# ---------------------------------------------------------------------------
# P0.2 — Build fail-fast
# ---------------------------------------------------------------------------


def test_build_bat_exits_on_test_failure() -> None:
    """build.bat must exit /b 1 when pytest reports failures — not just warn.

    RED path: build.bat:58 says "Proceeding with build anyway" instead of
    exiting, allowing a broken binary to be produced.
    """
    bat_content = (REPO_ROOT / "build.bat").read_text(encoding="utf-8")

    # Isolate the pytest block (between "[3/4] Running tests" and the next step).
    pytest_block_match = re.search(
        r"Running tests.*?(?=\[\d+/\d+\]|\Z)",
        bat_content,
        re.DOTALL | re.IGNORECASE,
    )
    assert pytest_block_match, "Could not locate pytest block in build.bat"
    block = pytest_block_match.group(0)

    assert "Proceeding with build anyway" not in block, (
        "build.bat must NOT continue after pytest failures — remove the 'Proceeding with build anyway' path"
    )
    assert re.search(r"exit\s*/b\s*1", block, re.IGNORECASE), "build.bat must call 'exit /b 1' when pytest errorlevel 1"


def test_build_bat_no_artifact_on_failure_ordering() -> None:
    """The actual build step (pyinstaller sky_claw.spec) must come AFTER pytest."""
    bat_content = (REPO_ROOT / "build.bat").read_text(encoding="utf-8")

    # Use the spec invocation, not `pip install pyinstaller` which appears earlier
    pytest_pos = bat_content.lower().find("pytest tests")
    pyinstaller_pos = bat_content.lower().find("pyinstaller sky_claw.spec")

    assert pytest_pos != -1, "pytest tests/ invocation not found in build.bat"
    assert pyinstaller_pos != -1, "pyinstaller sky_claw.spec invocation not found in build.bat"
    assert pytest_pos < pyinstaller_pos, "pytest tests/ must appear before pyinstaller sky_claw.spec in build.bat"
