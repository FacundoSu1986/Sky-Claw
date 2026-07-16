"""Advisor LLM para conflictos críticos de xEdit (Fase 1 — Recomendador).

El LLM se inyecta como un callable ``(system_prompt, user_prompt) -> str``
(:data:`LLMCallable`), desacoplado de los providers concretos del repo: quien
cablea (``AppContext``) decide cómo resolver router/sesión; los tests inyectan
un stub. Decisión documentada en la auditoría — el advisor no importa
``agent/providers.py``.

Fail-closed en todos los bordes: timeout, excepción del callable, respuesta
sin JSON, JSON fuera del esquema → :meth:`PatchRecommendation.manual_only`
con el porqué en ``summary``. La única excepción que propaga es
``CancelledError`` (convención del repo: la cancelación nunca se traga).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Protocol

from sky_claw.local.ai.conflict_prompt import SYSTEM_PROMPT, build_prompt
from sky_claw.local.ai.recommendation import PatchRecommendation

if TYPE_CHECKING:
    from sky_claw.local.ai.recommendation import ForumReference
    from sky_claw.local.xedit.conflict_analyzer import RecordConflict
    from sky_claw.local.xedit.record_dump_parser import RecordDump

logger = logging.getLogger(__name__)

#: Presupuesto por llamada al LLM. Un conflicto es un prompt corto (~500
#: tokens); si el provider tarda más que esto, algo está mal y el advisor
#: degrada a manual_only en vez de colgar el Ritual.
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0


class LLMCallable(Protocol):
    """Contrato del callable LLM inyectado: ``(system, user) -> respuesta``."""

    async def __call__(self, system_prompt: str, user_prompt: str) -> str: ...


def _extract_json_object(text: str) -> dict[str, object]:
    """Extrae el primer objeto JSON del texto del LLM.

    El SYSTEM_PROMPT pide JSON puro, pero los modelos a veces lo envuelven en
    fences o prosa. Se intenta el texto completo primero y después el bloque
    delimitado por la primera ``{`` y la última ``}``. Cualquier otro desvío
    es un ``ValueError`` — el caller degrada a manual_only.
    """
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("La respuesta del LLM no contiene un objeto JSON") from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON inválido en la respuesta del LLM: {exc}") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"La respuesta del LLM es {type(parsed).__name__}, se esperaba un objeto")
    return parsed


class PatchAdvisorLLM:
    """Produce :class:`PatchRecommendation` advisory para conflictos críticos.

    No muta plugins ni toca disco: entrada (conflicto + dump opcional) →
    prompt → LLM → parseo estricto → recomendación. El operador decide.
    """

    def __init__(
        self,
        llm: LLMCallable,
        *,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    ) -> None:
        self._llm = llm
        self._timeout_seconds = timeout_seconds

    async def advise(
        self,
        conflict: RecordConflict,
        dump: RecordDump | None = None,
        forum_references: tuple[ForumReference, ...] = (),
    ) -> PatchRecommendation:
        """Recomendación advisory para UN conflicto crítico (fail-closed).

        Nunca lanza (salvo ``CancelledError``): cualquier fallo del LLM o del
        parseo devuelve ``manual_only`` con el motivo en ``summary``.
        """
        user_prompt = build_prompt(conflict, dump, forum_references)
        try:
            raw = await asyncio.wait_for(
                self._llm(SYSTEM_PROMPT, user_prompt),
                timeout=self._timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning(
                "Advisor LLM: timeout de %.0fs para %s %s — manual_only.",
                self._timeout_seconds,
                conflict.record_type,
                conflict.form_id,
            )
            return PatchRecommendation.manual_only(
                conflict.form_id,
                conflict.record_type,
                f"El LLM no respondió en {self._timeout_seconds:.0f}s — resolver manualmente en xEdit.",
            )
        except Exception as exc:  # noqa: BLE001 — boundary: el advisor jamás tumba el pipeline
            logger.warning(
                "Advisor LLM: fallo llamando al LLM para %s %s — manual_only.",
                conflict.record_type,
                conflict.form_id,
                exc_info=True,
            )
            return PatchRecommendation.manual_only(
                conflict.form_id,
                conflict.record_type,
                f"El LLM no está disponible ({exc}) — resolver manualmente en xEdit.",
            )

        try:
            data = _extract_json_object(raw)
            recommendation = PatchRecommendation.from_dict(data)
        except ValueError as exc:
            logger.warning(
                "Advisor LLM: respuesta inválida para %s %s (%s) — manual_only.",
                conflict.record_type,
                conflict.form_id,
                exc,
            )
            return PatchRecommendation.manual_only(
                conflict.form_id,
                conflict.record_type,
                f"La respuesta del LLM no respetó el esquema ({exc}) — resolver manualmente en xEdit.",
            )

        # El LLM no es autoridad sobre QUÉ conflicto respondió: se ancla al
        # form_id/record_type del conflicto real (un mismatch delata respuesta
        # cruzada en batch o alucinación — fail-closed).
        from sky_claw.local.xedit.record_dump_parser import normalize_form_id

        if normalize_form_id(recommendation.form_id) != normalize_form_id(conflict.form_id):
            logger.warning(
                "Advisor LLM: la respuesta refiere a %s pero el conflicto es %s — manual_only.",
                recommendation.form_id,
                conflict.form_id,
            )
            return PatchRecommendation.manual_only(
                conflict.form_id,
                conflict.record_type,
                "La respuesta del LLM refiere a otro record — resolver manualmente en xEdit.",
            )

        return recommendation


__all__ = ["DEFAULT_LLM_TIMEOUT_SECONDS", "LLMCallable", "PatchAdvisorLLM"]
