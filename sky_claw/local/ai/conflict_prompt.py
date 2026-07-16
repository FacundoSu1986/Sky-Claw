"""Builder del prompt del advisor de IA (Fase 1).

Produce un resumen ESTRUCTURADO y compacto del conflicto (~500 tokens), no el
record entero: un NPC_ con 200 subrecords son 5-10 KB y un QUST con aliases
20 KB+ — caro e innecesario. Con dump disponible se mandan solo los elementos
en disputa (:meth:`RecordDump.differing_elements`); sin dump, el LLM recomienda
con menos contexto (plugins + tipo de record) y lo declara.

El SYSTEM_PROMPT exige responder SOLO con JSON del esquema de
:class:`~sky_claw.local.ai.recommendation.PatchRecommendation` — el parseo es
estricto y cualquier desvío degrada a ``manual_only`` (fail-closed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sky_claw.local.ai.recommendation import ForumReference
    from sky_claw.local.xedit.conflict_analyzer import RecordConflict
    from sky_claw.local.xedit.record_dump_parser import RecordDump

#: Máximo de elementos en disputa que viajan al prompt (los records CELL/QUST
#: patológicos pueden diferir en cientos de paths; el resto se declara).
MAX_DIFFERING_ELEMENTS = 30

#: Truncado por valor de elemento — un solo VMAD/CTDA gigante no debe comerse
#: el presupuesto del prompt.
MAX_VALUE_CHARS = 200

SYSTEM_PROMPT = """\
You are an expert Skyrim SE/AE mod-conflict analyst embedded in Sky-Claw, \
a Mod Organizer 2 automation tool. You analyze record-level conflicts that \
xEdit detected between plugins and produce an ADVISORY recommendation: the \
human operator reads it and applies it manually in xEdit. You never mutate \
plugins yourself.

Rules:
- Respond with a SINGLE JSON object and nothing else (no markdown fences, \
no prose outside the JSON).
- Schema: {"form_id": str, "record_type": str, "severity": "safe_auto" | \
"needs_review" | "manual_only", "summary": str, "subrecords": [{"subrecord": \
str, "action": "forward" | "merge" | "skip" | "manual", "source_plugin": str, \
"reasoning": str}], "confidence": float between 0.0 and 1.0}
- "forward" = copy the losing plugin's value into a patch override; "merge" = \
combine values from several plugins; "skip" = the winning value is already \
correct; "manual" = requires human judgment in xEdit.
- Facegen/head-part conflicts (PNAM/FaceGen), Papyrus quest fragments and \
scene timing are NEVER safe to auto-resolve: mark those subrecords "manual" \
and the overall severity "manual_only" or "needs_review".
- If you lack enough context to recommend anything concrete, use severity \
"manual_only" and explain why in "summary".
- Reply in the operator's language (Spanish) for "summary" and "reasoning".
"""


def _truncate(value: str, limit: int = MAX_VALUE_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def build_prompt(
    conflict: RecordConflict,
    dump: RecordDump | None = None,
    forum_references: tuple[ForumReference, ...] = (),
) -> str:
    """Arma el user-prompt para UN conflicto crítico.

    Args:
        conflict: El conflicto detectado por ``ConflictAnalyzer``.
        dump: Dump de ``dump_record_detail.pas`` para ese FormID, o ``None``
            si el script falló/no corrió (el prompt lo declara — el LLM debe
            saber que está recomendando con contexto parcial).
        forum_references: Soluciones conocidas de la comunidad (Fase 2; en
            Fase 1 siempre vacío).

    Returns:
        Prompt compacto en texto plano (secciones delimitadas, sin markdown).
    """
    lines: list[str] = [
        f'CONFLICT: {conflict.record_type} {conflict.form_id} "{conflict.editor_id}"',
        f"WINNER (by load order): {conflict.winner}",
        f"LOSERS: {', '.join(conflict.losers) if conflict.losers else '(none reported)'}",
        f"SEVERITY (analyzer): {conflict.severity}",
    ]

    if dump is not None and dump.versions:
        differing = dump.differing_elements()
        lines.append("")
        lines.append(f"DIFFERING SUBRECORDS ({len(differing)} total):")
        for path, values in differing[:MAX_DIFFERING_ELEMENTS]:
            rendered = "; ".join(f"{plugin}={_truncate(value)}" for plugin, value in sorted(values.items()))
            lines.append(f"- {path}: {rendered}")
        if len(differing) > MAX_DIFFERING_ELEMENTS:
            lines.append(f"- (…{len(differing) - MAX_DIFFERING_ELEMENTS} more differing subrecords omitted)")
    else:
        lines.append("")
        lines.append(
            "NO RECORD DUMP AVAILABLE: recommend based only on the plugins and "
            "record type above, and lower your confidence accordingly."
        )

    if forum_references:
        lines.append("")
        lines.append("KNOWN COMMUNITY SOLUTIONS:")
        for ref in forum_references:
            lines.append(f"- [{ref.source}] {ref.title} — {ref.url}")
            if ref.snippet:
                lines.append(f"  {_truncate(ref.snippet)}")

    lines.append("")
    lines.append(
        "QUESTION: The user wants all these plugins working together. Which "
        "subrecords should be forwarded/merged into a compatibility patch, and "
        "which require manual work? Answer with the JSON schema only."
    )
    return "\n".join(lines)


__all__ = ["MAX_DIFFERING_ELEMENTS", "MAX_VALUE_CHARS", "SYSTEM_PROMPT", "build_prompt"]
