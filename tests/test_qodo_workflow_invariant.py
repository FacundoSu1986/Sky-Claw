"""Ancla de invariante de workflows de Qodo / PR-Agent.

Por qué existe:
Garantiza que todas las invocaciones de Codium-ai/pr-agent dentro de
.github/workflows/*.yml estén explícitamente inventariadas y congeladas
en sus recetas de routing OpenRouter, pinning de Action, bloqueo de
repositorio privado y correspondencia exacta con la lista de modelos
aprobados en la política de gobernanza.

Cualquier invocación nueva (incluso con variantes de mayúsculas/minúsculas),
cualquier step duplicado dentro del mismo job, cualquier omisión del gate
de repositorio público o cualquier divergencia en claves/modelos romperá
este test de forma determinista.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = RAIZ / ".github" / "workflows"
POLICY_FILE = RAIZ / ".github" / "AI_REVIEW_DATA_POLICY.md"
PR_AGENT_TOML = RAIZ / ".pr_agent.toml"

# SHA del tag v0.50.0 de Codium-ai/pr-agent (el repo upstream ahora se llama
# The-PR-Agent/pr-agent y GitHub redirige). Verificado con
# `gh api repos/Codium-ai/pr-agent/git/ref/tags/v0.50.0`: el objeto del tag es
# este commit (`e673a02076ce09a4726c3d0c3cc85780e59ca52b`).
#
# Este congelamiento es deliberado y es el motivo por el que un bump de
# Dependabot NO puede aterrizar solo: mover el pin exige una decisión humana que
# audite el release contra `.pr_agent.toml` y contra las recetas de routing de
# los workflows. Al subir la versión, actualizar este SHA junto con los tres
# `uses:` de .github/workflows/qodo-*.yml.
PINNED_ACTION_REF_ESPERADA = "e673a02076ce09a4726c3d0c3cc85780e59ca52b"
CANONICAL_ACTION_REPO = "Codium-ai/pr-agent"

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
    "CONFIG.MODEL": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "CONFIG.CUSTOM_MODEL_MAX_TOKENS": "200000",
    "CONFIG.FALLBACK_MODELS": '["openrouter/nvidia/nemotron-3.5-lightning:free"]',
    "LITELLM.DROP_PARAMS": "true",
}

RECETA_ORACLE: dict[str, str] = {
    "OPENROUTER_API_KEY": "${{ secrets.OPENROUTER_API_KEY }}",
    "OPENROUTER__KEY": "${{ secrets.OPENROUTER_API_KEY }}",
    "CONFIG.MODEL": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "CONFIG.CUSTOM_MODEL_MAX_TOKENS": "200000",
    "CONFIG.FALLBACK_MODELS": '["openrouter/nvidia/nemotron-3.5-lightning:free"]',
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

    Normaliza el nombre de la Action de forma case-insensitive (owner/repo en GitHub no distinguen mayúsculas).
    Retorna un diccionario mapeando (workflow_filename, job_id) -> lista de steps encontrados con sus metadatos.
    """
    directorio = workflows_dir or WORKFLOWS_DIR
    invocaciones: dict[tuple[str, str], list[dict[str, Any]]] = {}

    archivos_workflow = sorted(list(directorio.glob("*.yml")) + list(directorio.glob("*.yaml")))

    for archivo in archivos_workflow:
        contenido = archivo.read_text(encoding="utf-8")
        data = yaml.safe_load(contenido)
        if not isinstance(data, dict):
            continue

        triggers_raw = data.get("on") if "on" in data else data.get(True, {})
        if isinstance(triggers_raw, str):
            triggers = {triggers_raw}
        elif isinstance(triggers_raw, (list, dict)):
            triggers = {str(t) for t in triggers_raw}
        else:
            triggers = set()

        jobs = data.get("jobs", {})
        if not isinstance(jobs, dict):
            continue

        for job_id, job_data in jobs.items():
            if not isinstance(job_data, dict):
                continue
            steps = job_data.get("steps", [])
            if not isinstance(steps, list):
                continue

            job_if = str(job_data.get("if", "")).strip()

            for step in steps:
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses", "")).strip()
                action_repo, _, action_ref = uses.partition("@")
                if action_repo.strip().casefold() == CANONICAL_ACTION_REPO.casefold():
                    clave = (archivo.name, job_id)
                    invocaciones.setdefault(clave, []).append(
                        {
                            "uses": uses,
                            "action_repo": action_repo.strip(),
                            "action_ref": action_ref.strip(),
                            "job_if": job_if,
                            "workflow_triggers": triggers,
                            "env": step.get("env", {}) or {},
                        }
                    )

    return invocaciones


def obtener_modelos_aprobados_politica(policy_path: Path | None = None) -> set[str]:
    """Extrae el conjunto de modelos aprobados delimitados en AI_REVIEW_DATA_POLICY.md."""
    archivo = policy_path or POLICY_FILE
    contenido = archivo.read_text(encoding="utf-8")

    patron_seccion = r"<!--\s*approved-models:start\s*-->(.*?)<!--\s*approved-models:end\s*-->"
    coincidencia = re.search(patron_seccion, contenido, re.DOTALL)
    assert coincidencia is not None, (
        f"No se encontró el bloque delimitado <!-- approved-models:start --> ... "
        f"<!-- approved-models:end --> en {archivo.name}"
    )

    bloque = coincidencia.group(1)
    # Extraer todos los identificadores de modelos encerrados en backticks con prefijo openrouter/
    modelos = set(re.findall(r"`(openrouter/[^`]+)`", bloque))
    assert len(modelos) > 0, f"No se extrajo ningún modelo aprobado del bloque en {archivo.name}"
    return modelos


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


def test_gate_repositorio_publico_en_todos_los_jobs() -> None:
    """Verifica que cada job que invoque Qodo tenga el gate explícito fail-closed github.event.repository.private == false."""
    descubiertas = descubrir_invocaciones_qodo()

    for (archivo, job_id), steps in descubiertas.items():
        for idx, step_metadata in enumerate(steps):
            job_if = step_metadata.get("job_if", "")
            assert "github.event.repository.private == false" in job_if, (
                f"El job {archivo} / {job_id} (step {idx}) no incluye la condición fail-closed "
                f"'github.event.repository.private == false' en su cláusula if.\n"
                f"Cláusula if actual: {job_if!r}"
            )


def test_pinning_de_accion_qodo_es_exacto() -> None:
    """Verifica que cada invocación use exactamente el SHA fijado y el repo canónico de Codium-ai/pr-agent."""
    descubiertas = descubrir_invocaciones_qodo()
    for (archivo, job_id), steps in descubiertas.items():
        for idx, step_metadata in enumerate(steps):
            action_ref = step_metadata.get("action_ref", "")
            action_repo = step_metadata.get("action_repo", "")
            assert action_repo.casefold() == CANONICAL_ACTION_REPO.casefold(), (
                f"Repo inesperado en {archivo} / {job_id} (step {idx}): {action_repo!r}"
            )
            assert action_ref == PINNED_ACTION_REF_ESPERADA, (
                f"Pinning inesperado en {archivo} / {job_id} (step {idx}): {action_ref!r} != {PINNED_ACTION_REF_ESPERADA!r}"
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


def test_modelos_configurados_coinciden_con_allowlist_de_politica() -> None:
    """Verifica que el conjunto de todos los modelos (primarios y fallbacks) coincida exactamente con la allowlist de la política."""
    descubiertas = descubrir_invocaciones_qodo()
    modelos_aprobados = obtener_modelos_aprobados_politica()

    modelos_configurados: set[str] = set()
    for (archivo, job_id), steps in descubiertas.items():
        for idx, step_metadata in enumerate(steps):
            env = step_metadata.get("env", {})
            modelo_primario = env.get("CONFIG.MODEL")
            if modelo_primario:
                modelos_configurados.add(str(modelo_primario))

            fallbacks_raw = env.get("CONFIG.FALLBACK_MODELS")
            if fallbacks_raw:
                try:
                    fallbacks = json.loads(str(fallbacks_raw))
                    if isinstance(fallbacks, list):
                        for fb in fallbacks:
                            modelos_configurados.add(str(fb))
                except Exception as exc:
                    raise AssertionError(
                        f"Error al parsear CONFIG.FALLBACK_MODELS en {archivo} / {job_id} (step {idx}): {fallbacks_raw}"
                    ) from exc

    assert modelos_configurados == modelos_aprobados, (
        f"Discrepancia entre modelos configurados en workflows y allowlist en AI_REVIEW_DATA_POLICY.md.\n"
        f"No aprobados en política pero configurados en workflows: {modelos_configurados - modelos_aprobados}\n"
        f"Aprobados en política pero no configurados en workflows:  {modelos_aprobados - modelos_configurados}"
    )


def test_gate_dependabot_en_jobs_automaticos_pull_request() -> None:
    """Verifica que TODA invocación automática de PR-Agent en pull_request excluya a dependabot[bot].

    Deriva dinámicamente todas las invocaciones descubiertas: si el workflow se activa
    por pull_request y el job no es un handler exclusivo de issue_comment (como comment-command),
    debe incluir explícitamente 'github.event.pull_request.user.login != \\'dependabot[bot]\\''
    en su cláusula if.
    """
    descubiertas = descubrir_invocaciones_qodo()
    assert len(descubiertas) > 0, "No se descubrió ninguna invocación de Qodo/PR-Agent"

    jobs_automaticos_evaluados = 0
    for (archivo_nombre, job_id), steps in descubiertas.items():
        for idx, step_metadata in enumerate(steps):
            triggers = step_metadata.get("workflow_triggers", set())
            job_if = step_metadata.get("job_if", "")

            # Si el workflow no se activa por pull_request, no es un flujo de PR automático
            if "pull_request" not in triggers and "pull_request_target" not in triggers:
                continue

            # comment-command es un disparador manual interactivo vía issue_comment
            # (requiere comentario explícito de OWNER/MEMBER/COLLABORATOR y no sufre de falta de secrets de Dependabot)
            if (
                "issue_comment" in job_if
                or "github.event.comment" in job_if
                or "github.event_name == 'issue_comment'" in job_if
            ):
                continue

            # Cualquier otro job que se ejecute bajo pull_request es automático y requiere el gate de Dependabot
            jobs_automaticos_evaluados += 1
            assert "github.event.pull_request.user.login != 'dependabot[bot]'" in job_if, (
                f"El job {archivo_nombre} / {job_id} (step {idx}) es una invocación automática bajo pull_request "
                f"pero no incluye 'github.event.pull_request.user.login != \\'dependabot[bot]\\'' en su cláusula if.\n"
                f"Cláusula if actual: {job_if!r}"
            )

    # Ancla de integridad: asegurar que se evaluaron al menos las invocaciones automáticas esperadas
    assert jobs_automaticos_evaluados >= 2, (
        f"Se esperaban al menos 2 jobs automáticos bajo pull_request; se evaluaron {jobs_automaticos_evaluados}."
    )


# Claves/secciones que pr-agent v0.50.0 convirtió en host-only o retiró en `.pr_agent.toml` raíz
# (la Action las descarta con warning o son no-op). Si se necesitan, van en `env:` del workflow.
CLAVES_PROHIBIDAS_EN_PR_AGENT_TOML: dict[str, set[str]] = {
    "github": {
        "ratelimit_retries",
        "api_retries",
        "seconds_between_requests",
        "seconds_between_writes",
        "agent_login",
        "deployment_type",
        "polling_repositories",
    },
    "config": {
        "num_retries",
        "enable_custom_labels",
        "enable_auto_approval",
        "extra_config_url",
        "system_instructions",
        "global_settings_repo",
    },
    "pr_code_suggestions": {
        "max_number_of_calls",
        "approve_pr_on_self_review",
        "fold_suggestions_on_self_review",
    },
}
SECCIONES_PROHIBIDAS_EN_PR_AGENT_TOML: set[str] = {"custom_labels"}


def _auditar_pr_agent_toml(toml_path: Path) -> None:
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    claves_raiz = {k.lower(): k for k in data}

    secciones_invalidas = SECCIONES_PROHIBIDAS_EN_PR_AGENT_TOML & set(claves_raiz)
    assert not secciones_invalidas, (
        f"{toml_path.name} contiene secciones host-only en pr-agent v0.50.0: {sorted(secciones_invalidas)}"
    )

    for seccion, claves_prohibidas in CLAVES_PROHIBIDAS_EN_PR_AGENT_TOML.items():
        clave_real = claves_raiz.get(seccion)
        if clave_real is None:
            continue
        tabla = data.get(clave_real, {})
        if not isinstance(tabla, dict):
            continue
        presentes = claves_prohibidas & {k.lower() for k in tabla}
        assert not presentes, (
            f"{toml_path.name} [{clave_real}] declara claves host-only o removidas en pr-agent v0.50.0: "
            f"{sorted(presentes)}. Moverlas al host (env del workflow) si se requiere override."
        )


def test_pr_agent_toml_sin_claves_host_only_o_removidas() -> None:
    """Verifica que `.pr_agent.toml` sea TOML válido y no declare claves host-only/removidas en v0.50.0."""
    _auditar_pr_agent_toml(PR_AGENT_TOML)


def test_auditar_pr_agent_toml_rechaza_claves_host_only(tmp_path: Path) -> None:
    """Verifica que el oráculo detecte claves host-only (como github.ratelimit_retries en v0.50.0)."""
    falso_toml = tmp_path / ".pr_agent.toml"
    falso_toml.write_text("[github]\nratelimit_retries = 5\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="ratelimit_retries"):
        _auditar_pr_agent_toml(falso_toml)
