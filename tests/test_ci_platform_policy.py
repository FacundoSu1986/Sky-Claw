"""Anclas de la política de plataformas del workflow principal.

La aplicación completa se certifica en Windows. Linux/WSL2 conserva una señal
informativa y acotada para las ramas POSIX del núcleo async documentadas en
``DEPLOYMENT.md``; no vuelve a ejecutar la suite completa.
"""

from __future__ import annotations

import ast
import shlex
from pathlib import Path
from typing import Any, cast

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
    "tests/test_frozen_runtime_p4_root_lock.py",
    "tests/test_etapa6_testigo_durable.py",
]


def _jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW_CI.read_text(encoding="utf-8"))
    return cast(dict[str, Any], workflow["jobs"])


def _paso_por_id(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    coincidencias = [step for step in job["steps"] if step.get("id") == step_id]
    assert len(coincidencias) == 1, f"se esperaba exactamente un step con id={step_id!r}"
    return cast(dict[str, Any], coincidencias[0])


def _paso_por_nombre(job: dict[str, Any], nombre: str) -> dict[str, Any]:
    coincidencias = [step for step in job["steps"] if step.get("name") == nombre]
    assert len(coincidencias) == 1, f"se esperaba exactamente un step llamado {nombre!r}"
    return cast(dict[str, Any], coincidencias[0])


def test_lint_typecheck_y_seguridad_incluyen_scripts_con_comandos_anclados() -> None:
    jobs = _jobs()
    lint = jobs["lint"]
    typecheck = jobs["typecheck"]
    security = jobs["security"]

    assert _paso_por_nombre(lint, "Ruff check")["run"] == (
        "ruff check sky_claw/ scripts/ tests/ --output-format=github"
    )
    assert _paso_por_nombre(lint, "Ruff format check")["run"] == "ruff format --check sky_claw/ scripts/ tests/"
    assert _paso_por_nombre(typecheck, "Mypy check")["run"] == (
        "python -m mypy sky_claw/ scripts/ --ignore-missing-imports --no-error-summary"
    )
    assert _paso_por_nombre(security, "Bandit (SAST)")["run"] == "bandit -r sky_claw/ scripts/ -ll"
    assert _paso_por_nombre(jobs["test"], "Run tests with coverage")["run"] == (
        "pytest --cov=sky_claw --cov=scripts --cov-report=xml --cov-fail-under=60 tests/"
    )


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

    setup_python = _paso_por_id(posix, "setup_python_posix")
    assert setup_python["with"]["python-version"] == "3.11"

    ejecutar = _paso_por_id(posix, "run_posix_tests")
    tokens = shlex.split(ejecutar["run"].replace("\\\n", " "))
    assert tokens[:3] == ["python", "-m", "pytest"]

    objetivos_configurados = sorted(token for token in tokens if token.startswith("tests/"))
    assert objetivos_configurados == sorted(OBJETIVOS_POSIX)


def test_objetivos_posix_apuntan_a_tests_reales_y_cubren_skip_win32_de_etapa6() -> None:
    """Como ``test-posix-core`` lleva ``continue-on-error: true``, la suite obligatoria de
    Windows debe verificar por AST que (a) cada objetivo en ``ci.yml`` existe de verdad y
    (b) todo test de ``test_etapa6_testigo_durable.py`` saltado en ``win32`` (p. ej. defensa
    FIFO ``O_NONBLOCK`` + ``fstat`` y ``fsync`` de directorio padre) se ejecuta en Linux."""
    posix = _jobs()["test-posix-core"]
    ejecutar = _paso_por_id(posix, "run_posix_tests")
    tokens = [t for t in shlex.split(ejecutar["run"].replace("\\\n", " ")) if t.startswith("tests/")]

    modulos_completos: set[str] = set()
    nodos_puntuales: set[tuple[str, str]] = set()
    for objetivo in tokens:
        ruta_rel, sep, nodo = objetivo.partition("::")
        ruta_abs = RAIZ / ruta_rel
        assert ruta_abs.is_file(), f"objetivo POSIX inexistente en ci.yml: {objetivo}"
        if not sep:
            modulos_completos.add(ruta_rel)
            continue
        arbol = ast.parse(ruta_abs.read_text(encoding="utf-8"))
        definidas = {n.name for n in arbol.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert nodo in definidas, f"función POSIX inexistente en {ruta_rel}: {nodo}"
        nodos_puntuales.add((ruta_rel, nodo))

    rel_etapa6 = "tests/test_etapa6_testigo_durable.py"
    arbol_etapa6 = ast.parse((RAIZ / rel_etapa6).read_text(encoding="utf-8"))
    exclusivos_posix = [
        n.name
        for n in arbol_etapa6.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any("win32" in ast.unparse(dec) and "skipif" in ast.unparse(dec) for dec in n.decorator_list)
    ]
    assert "test_el_almacen_rechaza_archivos_especiales_fifo_sin_bloquear_en_posix" in exclusivos_posix
    for nombre_fn in exclusivos_posix:
        cubierto = rel_etapa6 in modulos_completos or (rel_etapa6, nombre_fn) in nodos_puntuales
        assert cubierto, f"{rel_etapa6}::{nombre_fn} se salta en Windows y tampoco corre en test-posix-core"
