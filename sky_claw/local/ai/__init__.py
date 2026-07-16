"""Package del advisor de IA para resolución de conflictos de xEdit.

Fase 1 (Recomendador): el LLM analiza conflictos que los scripts ``.pas`` no
pueden resolver automáticamente y produce recomendaciones advisory. El
operador decide si sigue el consejo manualmente en xEdit.
"""

from sky_claw.local.ai.conflict_prompt import SYSTEM_PROMPT, build_prompt
from sky_claw.local.ai.forum_search import search_forums
from sky_claw.local.ai.patch_advisor_llm import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    LLMCallable,
    PatchAdvisorLLM,
)
from sky_claw.local.ai.recommendation import (
    ForumReference,
    PatchRecommendation,
    SubrecordRecommendation,
)

__all__ = [
    "SYSTEM_PROMPT",
    "build_prompt",
    "search_forums",
    "PatchAdvisorLLM",
    "LLMCallable",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "PatchRecommendation",
    "SubrecordRecommendation",
    "ForumReference",
]
