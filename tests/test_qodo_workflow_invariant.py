"""Ancla de invariante de workflows de Qodo / PR-Agent.

Por qué existe:
Garantiza que todas las invocaciones de Codium-ai/pr-agent dentro de
.github/workflows/*.yml estén explícitamente inventariadas y congeladas
en su receta de routing OpenRouter.

Cualquier invocación nueva que se agregue, cualquier step duplicado dentro
del mismo job, o cualquier divergencia en claves de credenciales, modelos,
límites de tokens o fallbacks romperá este test de forma determinista
hasta que se declare su receta.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = RAIZ / ".github" / "workflows"

PINNED_ACTION_ESPERADA = "Codium-ai/pr-agent@570f67ed5fc8db5be74c18df070bc20079b64b0d"

CLAVES_ROUTING = (
    "OPENROUTER_API_KEY",
    "OPENROUTER__KEY",
    "CONFIG.MODEL",
    "CONFIG.CUSTOM_MODEL_MAX_TOKENS",
    "CONFIG.FALLBACK_MODELS",
    "LITELLM.DROP_PARAMS",
)

RECETA_ADVERSARIAL: dict[str, str] = {
    "OPENROUTER_API_KEY": "${{ secrets.OPENROUTER_API_KEY }}",
    "OPENROUTER__KEY": "${{ secrets.OPENROUTER_API_KEY }}",
    "CONFIG.MODEL": "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "CONFIG.CUSTOM_MODEL_MAX_TOKENS": "200000",
    "CONFIG.FALLBACK_MODELS": '["openrouter/minimax/minimax-m3:free", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"]',
    "LITELLM.DROP_PARAMS": "true",
}

RECETA_ORACLE: dict[str, str] = {
    "OPENROUTER_API_KEY": "${{ secrets.OPENROUTER_API_KEY }}",
    "OPENROUTER__KEY": "${{ secrets.OPENROUTER_API_KEY }}",
    "CONFIG.MODEL": "openrouter/minimax/minimax-m3:free",
    "CONFIG.CUSTOM_MODEL_MAX_TOKENS": "200000",
    "CONFIG.FALLBACK_MODELS": '["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter/nvidia/nemotron-3-super-120b-a12b:free"]',
    "LITELLM.DROP_PARAMS": "true",
}

RECETAS_ESPERADAS: dict[tuple[str, str], dict[str, str]] = {
    ("qodo-merge-adversarial.yml", "auto-review"): RECETA_ADVERSARIAL,
    ("qodo-merge-adversarial.yml", "comment-command"): RECETA_ADVERSARIAL,
    ("qodo-regression-test-oracle.yml", "regression-test-oracle"): RECETA_ORACLE,
}


def descubrir_invocaciones_qodo(
    workflows_dir: Path | None = None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Descubre dinámicamente todas las invocaciones de Codium-ai/pr-agent en .github/workflows/*.

    Retorna un diccionario mapeando (workflow_filename, job_id) -> lista de steps encontrados.
    Garantiza que múltiples invocaciones dentro del mismo job no se sobrescriban.
    """
    directorio = workflows_dir or WORKFLOWS_DIR
    invocaciones: dict[tuple[str, str], list[dict[str, Any]]] = {}

    archivos_workflow = sorted(list(directorio.glob("*.yml")) + list(directorio.glob("*.yaml")))

    for archivo in archivos_workflow:
        contenido = archivo.read_text(encoding="utf-8")
        data = yaml.safe_load(contenido)
        if not isinstance(data, dict):
            continue

        jobs = data.get("jobs", {})
        if not isinstance(jobs, dict):
            continue

        for job_id, job_data in jobs.items():
            if not isinstance(job_data, dict):
                continue
            steps = job_data.get("steps", [])
            if not isinstance(steps, list):
                continue

            for step in steps:
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses", "")).strip()
                if uses.startswith("Codium-ai/pr-agent"):
                    clave = (archivo.name, job_id)
                    invocaciones.setdefault(clave, []).append(
                        {
                            "uses": uses,
                            "env": step.get("env", {}) or {},
                        }
                    )

    return invocaciones


def test_conjunto_de_invocaciones_qodo_es_exacto() -> None:
    """Verifica que el conjunto de jobs coincida exactamente y que cada uno tenga exactamente una invocación."""
    descubiertas = descubrir_invocaciones_qodo()
    conjunto_descubierto = set(descubiertas.keys())
    conjunto_esperado = set(RECETAS_ESPERADAS.keys())

    assert conjunto_descubierto == conjunto_esperado, (
        f"Invocaciones de Qodo divergentes. "
        f"Faltantes: {conjunto_esperado - conjunto_descubierto}, "
        f"Inesperadas: {conjunto_descubierto - conjunto_esperado}"
    )

    # Verificar que CADA job contenga EXACTAMENTE UNA invocación (evita duplicados o sobrescritura silenciosa)
    for (archivo, job_id), steps in descubiertas.items():
        assert len(steps) == 1, (
            f"El job {archivo} / {job_id} contiene {len(steps)} invocaciones de PR-Agent; se esperaba exactamente 1."
        )


def test_pinning_de_accion_qodo_es_exacto() -> None:
    """Verifica que cada invocación use exactamente el SHA fijado de Codium-ai/pr-agent."""
    descubiertas = descubrir_invocaciones_qodo()
    for (archivo, job_id), steps in descubiertas.items():
        for idx, step_metadata in enumerate(steps):
            uses = step_metadata.get("uses", "")
            assert uses == PINNED_ACTION_ESPERADA, (
                f"Pinning inesperado en {archivo} / {job_id} (step {idx}): {uses!r} != {PINNED_ACTION_ESPERADA!r}"
            )


def test_receta_routing_openrouter_por_invocacion() -> None:
    """Verifica que cada invocación congele exactamente su receta de routing OpenRouter."""
    descubiertas = descubrir_invocaciones_qodo()

    for (archivo, job_id), receta_esperada in RECETAS_ESPERADAS.items():
        assert (archivo, job_id) in descubiertas, f"Falta invocación {archivo} / {job_id}"
        steps = descubiertas[(archivo, job_id)]
        assert len(steps) == 1, f"Múltiples steps en {archivo} / {job_id}"
        env = steps[0]["env"]

        # Extraer únicamente las claves de routing relevantes para la aserción
        routing_actual = {k: str(env.get(k, "")) for k in CLAVES_ROUTING}

        assert routing_actual == receta_esperada, (
            f"Receta de routing divergente en {archivo} / {job_id}.\n"
            f"Actual:   {routing_actual}\n"
            f"Esperada: {receta_esperada}"
        )
