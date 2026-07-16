"""Tests del PatchAdvisorLLM (Fase 1) — fail-closed en todos los bordes.

El contrato: ``advise`` NUNCA lanza (salvo ``CancelledError``) — timeout,
excepción del callable, JSON inválido, esquema violado o respuesta cruzada
degradan a ``severity="manual_only"`` con el motivo en ``summary``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from sky_claw.local.ai.patch_advisor_llm import PatchAdvisorLLM
from sky_claw.local.ai.recommendation import PatchRecommendation
from sky_claw.local.xedit.conflict_analyzer import RecordConflict

RESPUESTA_VALIDA = {
    "form_id": "00012EB7",
    "record_type": "NPC_",
    "severity": "needs_review",
    "summary": "Forwardear las factions de Ordinator al patch.",
    "subrecords": [
        {
            "subrecord": "SNAM - Factions",
            "action": "forward",
            "source_plugin": "Ordinator.esp",
            "reasoning": "Ordinator añade ThievesGuild y Requiem la pisa.",
        }
    ],
    "confidence": 0.8,
}


def _conflicto() -> RecordConflict:
    return RecordConflict(
        form_id="00012EB7",
        editor_id="BanditThief",
        record_type="NPC_",
        winner="Requiem.esp",
        losers=["Ordinator.esp"],
        severity="critical",
    )


async def test_respuesta_json_valida_produce_recomendacion() -> None:
    async def llm(system: str, user: str) -> str:
        return json.dumps(RESPUESTA_VALIDA)

    rec = await PatchAdvisorLLM(llm=llm).advise(_conflicto())

    assert isinstance(rec, PatchRecommendation)
    assert rec.severity == "needs_review"
    assert rec.form_id == "00012eb7"  # normalizado
    assert rec.subrecords[0].action == "forward"
    assert rec.confidence == 0.8


async def test_json_envuelto_en_fences_se_extrae() -> None:
    """Los modelos a veces desobedecen el 'JSON puro' y envuelven en fences."""

    async def llm(system: str, user: str) -> str:
        return "```json\n" + json.dumps(RESPUESTA_VALIDA) + "\n```"

    rec = await PatchAdvisorLLM(llm=llm).advise(_conflicto())

    assert rec.severity == "needs_review"


async def test_timeout_degrada_a_manual_only() -> None:
    async def llm_colgado(system: str, user: str) -> str:
        await asyncio.sleep(30)
        return "{}"

    rec = await PatchAdvisorLLM(llm=llm_colgado, timeout_seconds=0.05).advise(_conflicto())

    assert rec.severity == "manual_only"
    assert "no respondió" in rec.summary
    assert rec.subrecords == ()


async def test_excepcion_del_llm_degrada_a_manual_only() -> None:
    async def llm_roto(system: str, user: str) -> str:
        raise RuntimeError("No hay proveedor LLM activo")

    rec = await PatchAdvisorLLM(llm=llm_roto).advise(_conflicto())

    assert rec.severity == "manual_only"
    assert "No hay proveedor LLM activo" in rec.summary


async def test_respuesta_sin_json_degrada_a_manual_only() -> None:
    async def llm_prosa(system: str, user: str) -> str:
        return "Mirá, yo forwardearía las factions y listo."

    rec = await PatchAdvisorLLM(llm=llm_prosa).advise(_conflicto())

    assert rec.severity == "manual_only"


async def test_accion_fuera_de_whitelist_degrada_a_manual_only() -> None:
    """Whitelist cerrada: una acción inventada invalida TODA la recomendación."""
    data = dict(RESPUESTA_VALIDA)
    data["subrecords"] = [{"subrecord": "SNAM", "action": "delete_plugin", "source_plugin": "", "reasoning": ""}]

    async def llm(system: str, user: str) -> str:
        return json.dumps(data)

    rec = await PatchAdvisorLLM(llm=llm).advise(_conflicto())

    assert rec.severity == "manual_only"
    assert rec.subrecords == ()


async def test_severity_fuera_de_whitelist_degrada_a_manual_only() -> None:
    data = dict(RESPUESTA_VALIDA)
    data["severity"] = "auto_apply_now"

    async def llm(system: str, user: str) -> str:
        return json.dumps(data)

    rec = await PatchAdvisorLLM(llm=llm).advise(_conflicto())

    assert rec.severity == "manual_only"


async def test_form_id_cruzado_degrada_a_manual_only() -> None:
    """El LLM no es autoridad sobre QUÉ conflicto respondió."""
    data = dict(RESPUESTA_VALIDA)
    data["form_id"] = "DEADBEEF"

    async def llm(system: str, user: str) -> str:
        return json.dumps(data)

    rec = await PatchAdvisorLLM(llm=llm).advise(_conflicto())

    assert rec.severity == "manual_only"
    assert rec.form_id == "00012eb7"  # el del conflicto real, no el del LLM


async def test_confidence_fuera_de_rango_degrada_a_manual_only() -> None:
    data = dict(RESPUESTA_VALIDA)
    data["confidence"] = 7.5

    async def llm(system: str, user: str) -> str:
        return json.dumps(data)

    rec = await PatchAdvisorLLM(llm=llm).advise(_conflicto())

    assert rec.severity == "manual_only"


async def test_cancelled_error_propaga_sin_tragarse() -> None:
    """Convención del repo: la cancelación jamás se convierte en resultado."""

    async def llm_cancelado(system: str, user: str) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await PatchAdvisorLLM(llm=llm_cancelado).advise(_conflicto())


async def test_el_prompt_recibido_declara_ausencia_de_dump() -> None:
    """advise(dump=None) debe avisarle al LLM que recomienda a ciegas."""
    prompts: list[str] = []

    async def llm(system: str, user: str) -> str:
        prompts.append(user)
        return json.dumps(RESPUESTA_VALIDA)

    await PatchAdvisorLLM(llm=llm).advise(_conflicto(), dump=None)

    assert "NO RECORD DUMP AVAILABLE" in prompts[0]
