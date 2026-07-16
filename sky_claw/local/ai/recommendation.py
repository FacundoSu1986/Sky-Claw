"""Modelos de datos del advisor de IA para conflictos de xEdit (Fase 1).

Todo es advisory: ninguna de estas estructuras muta plugins. El operador lee
la recomendación (GUI/Telegram), decide, y ejecuta manualmente en xEdit.

Fail-closed: cualquier borde raro (JSON inválido del LLM, campos faltantes,
acción desconocida) degrada a ``severity="manual_only"`` vía
:meth:`PatchRecommendation.manual_only` — nunca a una recomendación inventada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Acciones que el LLM puede recomendar para un subrecord. Whitelist cerrada:
#: una acción fuera de este set invalida la recomendación completa (fail-closed).
VALID_ACTIONS: frozenset[str] = frozenset({"forward", "merge", "skip", "manual"})

#: Severidades de la recomendación completa. ``manual_only`` es el fail-closed
#: canónico: el LLM no pudo (o no debió) recomendar nada accionable.
VALID_SEVERITIES: frozenset[str] = frozenset({"safe_auto", "needs_review", "manual_only"})


@dataclass(frozen=True, slots=True)
class ForumReference:
    """Referencia a una solución conocida de la comunidad (Fase 2 la puebla).

    Attributes:
        title: Título del hilo/post.
        url: URL de la fuente.
        source: Identificador de la fuente (``nexus``/``reddit``/``afkmods``).
        snippet: Extracto relevante (puede ser vacío).
    """

    title: str
    url: str
    source: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class SubrecordRecommendation:
    """Recomendación por subrecord (la unidad accionable en xEdit).

    Attributes:
        subrecord: Firma/path del subrecord (ej. ``"SNAM (factions)"``).
        action: Una de :data:`VALID_ACTIONS`.
        source_plugin: Plugin desde el que forwardear/mergear (vacío en skip).
        reasoning: Por qué — el operador decide con esto a la vista.
    """

    subrecord: str
    action: str
    source_plugin: str
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "subrecord": self.subrecord,
            "action": self.action,
            "source_plugin": self.source_plugin,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True, slots=True)
class PatchRecommendation:
    """Recomendación advisory completa para UN conflicto crítico.

    Attributes:
        form_id: FormID del record conflictivo (hex, normalizado lowercase).
        record_type: Firma del record (``NPC_``, ``QUST``…).
        severity: Una de :data:`VALID_SEVERITIES`.
        summary: Resumen en una frase para la GUI/Telegram.
        subrecords: Recomendaciones por subrecord (vacío en manual_only).
        forum_references: Soluciones conocidas de la comunidad (Fase 2).
        confidence: Confianza declarada por el LLM en [0.0, 1.0].
    """

    form_id: str
    record_type: str
    severity: str
    summary: str
    subrecords: tuple[SubrecordRecommendation, ...] = field(default_factory=tuple)
    forum_references: tuple[ForumReference, ...] = field(default_factory=tuple)
    confidence: float = 0.0

    @classmethod
    def manual_only(cls, form_id: str, record_type: str, reason: str) -> PatchRecommendation:
        """Fail-closed canónico: sin recomendación accionable, solo el porqué."""
        return cls(
            form_id=form_id.lower(),
            record_type=record_type,
            severity="manual_only",
            summary=reason,
            subrecords=(),
            forum_references=(),
            confidence=0.0,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchRecommendation:
        """Construye desde el JSON del LLM con validación estricta.

        Raises:
            ValueError: Cualquier campo faltante, tipo incorrecto, acción o
                severidad fuera de la whitelist. El caller (PatchAdvisorLLM)
                degrada a :meth:`manual_only` — nunca se "adivina" un campo.
        """
        if not isinstance(data, dict):
            raise ValueError(f"La recomendación debe ser un objeto JSON, no {type(data).__name__}")

        form_id = data.get("form_id")
        record_type = data.get("record_type")
        severity = data.get("severity")
        summary = data.get("summary")
        if not isinstance(form_id, str) or not form_id:
            raise ValueError("form_id faltante o inválido en la recomendación del LLM")
        if not isinstance(record_type, str) or not record_type:
            raise ValueError("record_type faltante o inválido en la recomendación del LLM")
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"severity {severity!r} fuera de la whitelist {sorted(VALID_SEVERITIES)}")
        if not isinstance(summary, str) or not summary:
            raise ValueError("summary faltante o inválido en la recomendación del LLM")

        raw_confidence = data.get("confidence", 0.0)
        if not isinstance(raw_confidence, (int, float)) or not 0.0 <= float(raw_confidence) <= 1.0:
            raise ValueError(f"confidence {raw_confidence!r} fuera de [0.0, 1.0]")

        subrecords: list[SubrecordRecommendation] = []
        for raw in data.get("subrecords", []):
            if not isinstance(raw, dict):
                raise ValueError("Cada subrecord debe ser un objeto JSON")
            action = raw.get("action")
            if action not in VALID_ACTIONS:
                raise ValueError(f"action {action!r} fuera de la whitelist {sorted(VALID_ACTIONS)}")
            subrecord = raw.get("subrecord")
            if not isinstance(subrecord, str) or not subrecord:
                raise ValueError("subrecord faltante o inválido")
            subrecords.append(
                SubrecordRecommendation(
                    subrecord=subrecord,
                    action=action,
                    source_plugin=str(raw.get("source_plugin", "")),
                    reasoning=str(raw.get("reasoning", "")),
                )
            )

        references: list[ForumReference] = []
        for raw in data.get("forum_references", []):
            if not isinstance(raw, dict):
                raise ValueError("Cada forum_reference debe ser un objeto JSON")
            references.append(
                ForumReference(
                    title=str(raw.get("title", "")),
                    url=str(raw.get("url", "")),
                    source=str(raw.get("source", "")),
                    snippet=str(raw.get("snippet", "")),
                )
            )

        return cls(
            form_id=form_id.lower(),
            record_type=record_type,
            severity=severity,
            summary=summary,
            subrecords=tuple(subrecords),
            forum_references=tuple(references),
            confidence=float(raw_confidence),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serializable para el contrato dict del dispatcher (GUI/Telegram)."""
        return {
            "form_id": self.form_id,
            "record_type": self.record_type,
            "severity": self.severity,
            "summary": self.summary,
            "subrecords": [s.to_dict() for s in self.subrecords],
            "forum_references": [r.to_dict() for r in self.forum_references],
            "confidence": self.confidence,
        }


__all__ = [
    "VALID_ACTIONS",
    "VALID_SEVERITIES",
    "ForumReference",
    "PatchRecommendation",
    "SubrecordRecommendation",
]
