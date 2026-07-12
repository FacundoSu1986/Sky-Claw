"""Asistente de estrategia de parcheo (T-20, ADR 0002).

La capa **advisory con trazabilidad**: dado un conjunto de conflictos, no elige
una estrategia ejecutable (eso es :class:`PatchOrchestrator`), sino que le
explica al operador *qué* enfoque conviene por grupo de conflictos y *por qué*
— el germen del boundary ``PatchPlanner`` de ADR 0002 y el "panel de cirugía"
de la visión (review §5.5: decisión informada, no conteo).

Diseño declarativo (mismo patrón que ``flag_rules.py``): una regla es un dato
(:class:`StrategyRule`), no código — sumar un tipo a un enfoque es agregar una
firma a un set, sin tocar :func:`recommend`.

Invariante duro (ADR 0001, T-04(a) no completada): el asistente **nunca**
recomienda el merged patch propio. Las listas niveladas se delegan al Bashed
Patch (unión + Relev/Delev); ningún enfoque de la tabla es un merge propio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sky_claw.local.xedit.patch_orchestrator import LEVELED_LIST_TYPES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sky_claw.local.xedit.conflict_analyzer import RecordConflict
    from sky_claw.local.xedit.flag_rules import FlagAlert

# ---------------------------------------------------------------------------
# Enfoques (approaches) — el vocabulario advisory. Deliberadamente NO existe un
# enfoque "merged patch propio": ADR 0001 lo rechazó (T-04(a) no completada).
# ---------------------------------------------------------------------------

#: Delegar a la generación del Bashed Patch (Wrye Bash): listas niveladas.
BASHED_PATCH = "bashed_patch"
#: Aplicar un patcher de Synthesis: reaplicable de forma reproducible.
SYNTHESIS = "synthesis"
#: Forwardeo manual en xEdit: alto riesgo, ninguna herramienta lo automatiza.
XEDIT_MANUAL = "xedit_manual"
#: Fallback honesto: sin regla que lo cubra, requiere revisión humana.
REVIEW = "review"

# ---------------------------------------------------------------------------
# Sets declarativos de tipos → enfoque. Conservadores y documentados como
# extensibles: sumar un tipo validado es editar el set (dato, no código).
# ---------------------------------------------------------------------------

#: Narrativa / IA de alto riesgo (subconjunto de los críticos): xEdit manual.
_MANUAL_TYPES: frozenset[str] = frozenset({"QUST", "SCEN", "NPC_", "INFO", "DIAL", "PACK", "FACT"})

#: Stats / keywords con patcher de Synthesis reproducible. NO incluye listas
#: niveladas (van a Bashed Patch, ADR 0001). Extensible a medida que se validan
#: patchers de dominio.
_SYNTHESIS_TYPES: frozenset[str] = frozenset({"KYWD", "WEAP", "ARMO", "AMMO"})


@dataclass(frozen=True)
class StrategyRule:
    """Una regla declarativa: qué tipos mapean a qué enfoque, y su porqué.

    Attributes:
        record_types: Firmas de record a las que aplica (normalizadas en mayúsculas).
        approach: Uno de :data:`BASHED_PATCH` / :data:`SYNTHESIS` /
            :data:`XEDIT_MANUAL` / :data:`REVIEW`.
        rationale: El "por qué" que ve el operador.
    """

    record_types: frozenset[str]
    approach: str
    rationale: str


#: Reglas activas por defecto. El orden fija la precedencia si dos reglas
#: cubrieran el mismo tipo (no ocurre hoy: los sets son disjuntos).
DEFAULT_STRATEGY_RULES: tuple[StrategyRule, ...] = (
    StrategyRule(
        record_types=LEVELED_LIST_TYPES,
        approach=BASHED_PATCH,
        rationale=(
            "unión de entradas + Relev/Delev es la especialidad del Bashed Patch (ADR 0001); "
            "xEdit manual perdería entradas y un merge propio quedó descartado"
        ),
    ),
    StrategyRule(
        record_types=_SYNTHESIS_TYPES,
        approach=SYNTHESIS,
        rationale=(
            "un patcher de Synthesis (stats/keywords) reaplica el cambio de forma "
            "reproducible tras cada reorden del load order"
        ),
    ),
    StrategyRule(
        record_types=_MANUAL_TYPES,
        approach=XEDIT_MANUAL,
        rationale=(
            "conflicto de alto riesgo (narrativa/IA): forwardeo manual en xEdit; "
            "ninguna herramienta automática lo resuelve con seguridad"
        ),
    ),
)

#: Recomendación de fallback cuando ningún set cubre el tipo.
_REVIEW_RATIONALE = "sin regla que lo cubra: revisar manualmente antes de parchear"

#: Orden de severidad para presentar (lo más accionable primero).
_SEVERITY_RANK: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class PatchRecommendation:
    """Una recomendación advisory por grupo de conflictos, con su trazabilidad.

    Attributes:
        approach: Enfoque recomendado (constante de este módulo).
        record_type: Firma del grupo de conflictos.
        rationale: El "por qué" de la recomendación.
        severity: Severidad más alta del grupo (``critical`` / ``warning`` / ``info``).
        conflict_count: Cuántos conflictos de este tipo cubre.
        form_ids: FormIDs de los conflictos (trazabilidad — el operador los abre en xEdit).
        flag_alerts: Alertas de flags críticos (T-19b) de los conflictos del grupo.
    """

    approach: str
    record_type: str
    rationale: str
    severity: str
    conflict_count: int
    form_ids: tuple[str, ...]
    flag_alerts: tuple[FlagAlert, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serializa para el manifiesto / GUI (JSON-serializable)."""
        return {
            "approach": self.approach,
            "record_type": self.record_type,
            "rationale": self.rationale,
            "severity": self.severity,
            "conflict_count": self.conflict_count,
            "form_ids": list(self.form_ids),
            "flag_alerts": [
                {
                    "flag": a.flag,
                    "winner": a.winner,
                    "defined_by": list(a.defined_by),
                    "severity": a.severity,
                    "explanation": a.explanation,
                }
                for a in self.flag_alerts
            ],
        }


def _match_rule(record_type: str, rules: Sequence[StrategyRule]) -> StrategyRule | None:
    """Primera regla cuyo set incluye la firma (normalizada), o ``None``."""
    sig = record_type.upper().strip()
    for rule in rules:
        if sig in rule.record_types:
            return rule
    return None


def _most_severe(severities: Sequence[str]) -> str:
    """La severidad más alta del grupo (default ``info`` si desconocida)."""
    return min(severities, key=lambda s: _SEVERITY_RANK.get(s, len(_SEVERITY_RANK)))


def recommend(
    conflicts: Sequence[RecordConflict],
    rules: Sequence[StrategyRule] = DEFAULT_STRATEGY_RULES,
) -> tuple[PatchRecommendation, ...]:
    """Agrupa los conflictos por tipo y recomienda un enfoque por grupo.

    Cada recomendación lleva su porqué (trazabilidad, aceptación T-20). El
    orden es por severidad (crítico primero) y, a igual severidad, alfabético
    por tipo para ser determinista.

    Args:
        conflicts: Conflictos a analizar (de :class:`ConflictAnalyzer`).
        rules: Reglas de estrategia (default: :data:`DEFAULT_STRATEGY_RULES`).

    Returns:
        Una tupla de :class:`PatchRecommendation`, una por tipo de record.
        Nunca contiene un enfoque de merged patch propio (ADR 0001).
    """
    # Agrupar por firma normalizada, preservando el orden de aparición.
    grupos: dict[str, list[RecordConflict]] = {}
    for conflict in conflicts:
        sig = conflict.record_type.upper().strip()
        grupos.setdefault(sig, []).append(conflict)

    recomendaciones: list[PatchRecommendation] = []
    for sig, grupo in grupos.items():
        rule = _match_rule(sig, rules)
        approach = rule.approach if rule is not None else REVIEW
        rationale = rule.rationale if rule is not None else _REVIEW_RATIONALE
        severity = _most_severe([c.severity for c in grupo])
        form_ids = tuple(c.form_id for c in grupo)
        flag_alerts = tuple(alert for c in grupo for alert in c.flag_alerts)
        recomendaciones.append(
            PatchRecommendation(
                approach=approach,
                record_type=sig,
                rationale=rationale,
                severity=severity,
                conflict_count=len(grupo),
                form_ids=form_ids,
                flag_alerts=flag_alerts,
            )
        )

    recomendaciones.sort(key=lambda r: (_SEVERITY_RANK.get(r.severity, len(_SEVERITY_RANK)), r.record_type))
    return tuple(recomendaciones)
