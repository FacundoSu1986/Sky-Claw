"""Anclas de la política de plataformas del workflow principal.

La aplicación completa se certifica en Windows. Linux/WSL2 conserva una señal
informativa y acotada para las ramas POSIX del núcleo async documentadas en
``DEPLOYMENT.md``; no vuelve a ejecutar la suite completa.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOW_CI = RAIZ / ".github" / "workflows" / "ci.yml"

OBJETIVOS_POSIX = [
    "tests/test_path_validator.py",
    "tests/test_preflight_wiring.py",
    "tests/test_dyndolod_uia_preflight.py::test_el_observador_por_defecto_falla_cerrado_en_vez_de_adivinar",
    "tests/test_dyndolod_uia_preflight.py::test_el_pipeline_con_el_observador_por_defecto_da_unknown_no_error",
    "tests/test_dyndolod_uia_preflight.py::test_uia_no_disponible_es_un_error_uia",
    "tests/test_dyndolod_uia_preflight.py::test_el_modulo_no_importa_nada_de_windows",
]


def _jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW_CI.read_text(encoding="utf-8"))
    return workflow["jobs"]


def test_la_suite_completa_solo_certifica_windows() -> None:
    jobs = _jobs()
    test = jobs["test"]

    assert test["strategy"]["matrix"] == {
        "os": ["windows-latest"],
        "python-version": ["3.11", "3.12"],
    }
    assert "continue-on-error" not in test
    assert jobs["test-summary"]["name"] == "🧪 Tests (Pytest)"
    assert jobs["test-summary"]["needs"] == ["test"]


def test_posix_conserva_una_senal_informativa_acotada() -> None:
    posix = _jobs()["test-posix-core"]

    assert posix["runs-on"] == "ubuntu-latest"
    assert posix["continue-on-error"] is True

    setup_python = next(step for step in posix["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
    assert setup_python["with"]["python-version"] == "3.11"

    ejecutar = next(step for step in posix["steps"] if step.get("name") == "Run POSIX core tests")
    tokens = [token for token in ejecutar["run"].split() if token != "\\"]
    assert tokens == ["python", "-m", "pytest", "-q", *OBJETIVOS_POSIX]
