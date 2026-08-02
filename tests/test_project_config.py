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

    addopts: str = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
    assert "--basetemp" in addopts, (
        "[tool.pytest.ini_options] must include addopts with '--basetemp=.pytest-tmp' "
        "to avoid PermissionError on Windows (AppData ACL residues). "
        f"Current addopts: {addopts!r}"
    )
    assert ".pytest-tmp" in addopts, (
        f"basetemp must point to '.pytest-tmp' (workspace-local, gitignored). Current addopts: {addopts!r}"
    )


def test_pytest_trata_warnings_de_lifecycle_como_errores() -> None:
    """El gate debe fallar ante fugas async sin endurecer warnings ajenos."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    filterwarnings: list[str] = pyproject["tool"]["pytest"]["ini_options"].get("filterwarnings", [])
    assert set(filterwarnings) == {
        "error::RuntimeWarning",
        "error::ResourceWarning",
        "error::pytest.PytestUnraisableExceptionWarning",
    }


def test_pytest_asyncio_no_puede_bajar_del_piso_que_fuga_el_loop() -> None:
    """El piso de ``pytest-asyncio`` es la corrección real de la fuga de #414.

    Hasta 1.3.0 inclusive, ``_temporary_event_loop_policy`` abría con
    ``old_loop = _get_event_loop_no_warn()``, que llama a
    ``asyncio.get_event_loop()`` con el ``DeprecationWarning`` silenciado — y esa
    función **crea** un loop cuando la sesión no tiene uno actual. El huérfano
    nunca se corre ni se cierra: queda de "actual" hasta que alguien ponga el
    loop en ``None``, y ahí el GC lo finaliza con un ``unclosed event loop`` más
    los dos sockets de su self-pipe. Con ``error::ResourceWarning`` (#409), eso
    es la suite en rojo, en un test al azar.

    Se ancla el piso y no la ausencia del síntoma porque **bajar la versión es
    la única forma de traer el bug de vuelta**, y un rango sin piso lo permite en
    silencio: el ``>=1.0`` anterior resolvía a 1.3.0 vía ``uv.lock`` mientras CI
    —que instala con ``pip install -e ".[dev]"``, sin lock— tomaba 1.4.0. Esa
    divergencia es la razón de que #413 y #414 vieran un flake local que CI no
    reproducía.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    dev: list[str] = pyproject["project"]["optional-dependencies"]["dev"]
    candidatos = [d for d in dev if d.replace(" ", "").startswith("pytest-asyncio")]

    # Aserción explícita en vez de `next(...)`: si la dependencia se renombra o
    # desaparece, un StopIteration deja un traceback sin decir qué propiedad se
    # rompió, y este ancla existe justamente para nombrarla.
    assert len(candidatos) == 1, f"se esperaba exactamente un pytest-asyncio en [dev], hay {candidatos}"

    especificador = candidatos[0]
    piso = re.search(r">=\s*(\d+)\.(\d+)", especificador)

    assert piso is not None, f"pytest-asyncio tiene que declarar un piso `>=X.Y`, dice: {especificador}"
    assert (int(piso.group(1)), int(piso.group(2))) >= (1, 4), (
        f"el piso de pytest-asyncio no puede bajar de 1.4 (fuga de #414), dice: {especificador}"
    )


def test_pytest_tmp_dir_is_gitignored() -> None:
    """.pytest-tmp/ must be listed in .gitignore to avoid committing temp artifacts.

    RED path: .gitignore currently has no entry for .pytest-tmp.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.pytest-tmp", gitignore, re.MULTILINE), (
        ".gitignore must contain an entry matching '.pytest-tmp' so pytest basetemp artefacts are never committed."
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


def test_locks_no_conservan_dependencias_del_stategraph_retirado() -> None:
    """Los dos caminos de instalación deben retirar la familia LangGraph."""
    dependencias_retiradas = {
        "langchain-core",
        "langgraph",
        "langgraph-checkpoint",
        "langgraph-sdk",
        "langsmith",
    }
    requirements_lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    uv_lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")

    for dependencia in dependencias_retiradas:
        assert not re.search(rf"(?m)^{re.escape(dependencia)}==", requirements_lock), (
            f"requirements.lock todavía instala {dependencia}"
        )
        assert f'name = "{dependencia}"' not in uv_lock, f"uv.lock todavía instala {dependencia}"


def test_modulos_de_logging_tienen_override_mypy_strict() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)
    target_modules = {
        "sky_claw._logging_runtime",
        "sky_claw.logging_config",
        "sky_claw.__main__",
    }
    strict_modules: set[str] = set()
    for override in pyproject["tool"]["mypy"]["overrides"]:
        if override.get("strict") is True and override.get("ignore_errors") is False:
            strict_modules.update(override["module"])
        if override.get("ignore_errors") is True:
            assert target_modules.isdisjoint(override["module"])
    assert target_modules <= strict_modules
