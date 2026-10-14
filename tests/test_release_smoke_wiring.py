"""Ancla los smokes a cada workflow que produce o publica el ejecutable."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[1]
DIRECTORIO_WORKFLOWS = RAIZ / ".github" / "workflows"
COMANDO_SMOKE = "python scripts/smoke_frozen_executable.py dist/SkyClawApp.exe"
PRODUCTORES_ESPERADOS = {
    ("ci.yml", "build"),
    ("release.yml", "release"),
}


def _cargar_workflows() -> dict[str, dict[str, Any]]:
    rutas = sorted(ruta for patron in ("*.yml", "*.yaml") for ruta in DIRECTORIO_WORKFLOWS.glob(patron))
    return {ruta.name: yaml.safe_load(ruta.read_text(encoding="utf-8")) for ruta in rutas}


def _productores_de_ejecutable(workflows: dict[str, dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    productores: dict[tuple[str, str], list[dict[str, Any]]] = {}
    patron_build = re.compile(r"(?:^|\s)pyinstaller\s+sky_claw\.spec(?:\s|$)", re.IGNORECASE)
    for nombre_workflow, workflow in workflows.items():
        for nombre_job, job in workflow.get("jobs", {}).items():
            steps = job.get("steps", [])
            if any(patron_build.search(step.get("run", "")) for step in steps):
                productores[(nombre_workflow, nombre_job)] = steps
    return productores


def _indice_unico(steps: list[dict[str, Any]], predicado: Any, descripcion: str) -> int:
    indices = [indice for indice, step in enumerate(steps) if predicado(step)]
    assert len(indices) == 1, f"se esperaba exactamente un step {descripcion}; encontrados: {indices}"
    return indices[0]


def test_todos_y_solo_los_workflows_productores_smokean_antes_de_publicar() -> None:
    workflows = _cargar_workflows()
    productores = _productores_de_ejecutable(workflows)

    assert set(productores) == PRODUCTORES_ESPERADOS
    for nombre_workflow, nombre_job in sorted(PRODUCTORES_ESPERADOS):
        steps = productores[(nombre_workflow, nombre_job)]
        build_index = _indice_unico(
            steps,
            lambda step: "pyinstaller sky_claw.spec" in step.get("run", "").lower(),
            "de construcción PyInstaller",
        )
        smoke_index = _indice_unico(
            steps,
            lambda step: step.get("run") == COMANDO_SMOKE,
            "de smoke del ejecutable congelado",
        )
        assert build_index < smoke_index, f"{nombre_workflow} debe smoke-testear después de construir"

        if nombre_workflow == "ci.yml":
            assert workflows[nombre_workflow]["jobs"][nombre_job]["runs-on"] == "windows-latest"
            publish_index = _indice_unico(
                steps,
                lambda step: step.get("uses", "").startswith("actions/upload-artifact@"),
                "de publicación del artefacto de CI",
            )
            assert steps[build_index].get("id") == "build_executable"
            assert steps[smoke_index].get("id") == "smoke_frozen_executable"
        else:
            assert workflows[nombre_workflow]["jobs"][nombre_job]["runs-on"] == "windows-latest"
            sbom_index = _indice_unico(steps, lambda step: step.get("name") == "Generate SBOM", "de SBOM")
            signing_index = _indice_unico(
                steps,
                lambda step: step.get("name") == "Sign executable",
                "de firma",
            )
            publish_index = _indice_unico(
                steps,
                lambda step: step.get("name") == "Create GitHub Release",
                "de publicación de release",
            )
            assert smoke_index < sbom_index < signing_index < publish_index
            assert steps[smoke_index].get("id") == "smoke_frozen_executable"

        assert smoke_index < publish_index, f"{nombre_workflow} publica antes de validar el ejecutable"


def test_release_conserva_el_job_pausado_y_no_cambia_la_receta_de_firma() -> None:
    release = _cargar_workflows()["release.yml"]
    release_job = release["jobs"]["release"]
    steps = release_job["steps"]

    assert release_job["if"] == "${{ false }}"
    signing_step = _indice_unico(steps, lambda step: step.get("name") == "Sign executable", "de firma")
    assert steps[signing_step]["run"] == (
        "cosign sign-blob dist/SkyClawApp.exe `\n  --yes `\n  --bundle dist/SkyClawApp.exe.bundle.json\n"
    )
