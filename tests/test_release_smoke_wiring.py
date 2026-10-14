"""El artefacto de CI debe arrancar antes de publicarse como descarga."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[1]


def test_ci_arranca_el_ejecutable_congelado_antes_de_subirlo() -> None:
    workflow_path = RAIZ / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    build_job: dict[str, Any] = workflow["jobs"]["build"]
    steps = build_job["steps"]

    build_index = next(index for index, step in enumerate(steps) if step.get("id") == "build_executable")
    smoke_index = next(index for index, step in enumerate(steps) if step.get("id") == "smoke_frozen_executable")
    upload_index = next(
        index for index, step in enumerate(steps) if step.get("uses", "").startswith("actions/upload-artifact@")
    )

    assert build_index < smoke_index < upload_index
    assert steps[smoke_index]["run"] == "python scripts/smoke_frozen_executable.py dist/SkyClawApp.exe"
    assert build_job["runs-on"] == "windows-latest"
