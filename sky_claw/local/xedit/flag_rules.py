"""Motor declarativo de reglas de flags críticos (T-19b, ADR 0002).

La semilla del boundary ``ConflictRuleEngine``: convierte el dato por-versión
de T-19a (``RecordConflict.flag_states``) en **alertas explicadas** — la
diferencia entre "hay 12 conflictos críticos" y una decisión informada
(review §5.5).

Diseño declarativo: una regla es un dato (:class:`FlagRule`), no código —
agregar PERK/MGEF/Relev/Delev es sumar una entrada a
:data:`DEFAULT_FLAG_RULES` (con su export correspondiente en
``CRITICAL_FLAGS``; el test de anclaje exige esa correspondencia — una regla
sin datos nunca dispararía).

Semántica de disparo (honesta): la regla alerta **solo** cuando el estado del
ganador es False *confirmado* y algún otro plugin define el flag en True. Un
ganador sin estado (SPIT ilegible/ausente → el export no emitió su línea
FLAG) NO alerta: no se afirma una pérdida sin dato.

La primera regla: ``Manual Cost Calc`` en SPEL. Sin ese flag, el motor del
juego recalcula el coste del hechizo por magnitud/duración — coste
astronómico en mods de magia sostenida que definen coste manual.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sky_claw.local.xedit.conflict_analyzer import RecordConflict


@dataclass(frozen=True)
class FlagRule:
    """Una regla declarativa: firma + flag + por qué importa perderlo.

    Attributes:
        signature: Firma del record a la que aplica (p. ej. ``"SPEL"``).
        flag: Nombre canónico del flag (igual al export de T-19a).
        explanation: El "por qué" que ve el operador cuando la regla dispara.
    """

    signature: str
    flag: str
    explanation: str


@dataclass(frozen=True)
class FlagAlert:
    """Una regla que disparó sobre un conflicto concreto, lista para mostrar.

    Attributes:
        form_id: FormID del record en disputa.
        editor_id: EditorID (legible) del record.
        record_type: Firma del record.
        flag: Flag que el ganador no preserva.
        winner: Plugin que gana el conflicto (y pierde el flag).
        defined_by: Plugins que SÍ definen el flag (los que se pierden).
        explanation: El "por qué" de la regla.
        severity: Siempre ``"critical"`` hoy (las reglas default son críticas).
    """

    form_id: str
    editor_id: str
    record_type: str
    flag: str
    winner: str
    defined_by: tuple[str, ...]
    explanation: str
    severity: str = "critical"


#: La primera regla del motor (T-19b): texto explicativo del backlog, literal.
MANUAL_COST_CALC_RULE = FlagRule(
    signature="SPEL",
    flag="Manual Cost Calc",
    explanation=(
        "sin este flag el motor recalcula el coste por duración → coste astronómico en mods de magia sostenida"
    ),
)

#: Reglas activas por defecto. Extensible: PERK/MGEF/Relev/Delev se suman acá
#: (con su export en CRITICAL_FLAGS — anclado por test).
DEFAULT_FLAG_RULES: tuple[FlagRule, ...] = (MANUAL_COST_CALC_RULE,)


def evaluate_flag_rules(
    conflict: RecordConflict,
    rules: Sequence[FlagRule] = DEFAULT_FLAG_RULES,
) -> tuple[FlagAlert, ...]:
    """Evalúa las reglas contra un conflicto y devuelve las alertas disparadas.

    Args:
        conflict: El conflicto (con ``flag_states`` de T-19a).
        rules: Reglas a evaluar (default: :data:`DEFAULT_FLAG_RULES`).

    Returns:
        Tupla de :class:`FlagAlert` (vacía si nada disparó o faltan datos).
    """
    alerts: list[FlagAlert] = []
    for rule in rules:
        if rule.signature != conflict.record_type:
            continue
        winner_state = next(
            (f.value for f in conflict.flag_states if f.plugin == conflict.winner and f.flag == rule.flag),
            None,
        )
        if winner_state is not False:
            # True = el ganador preserva; None = desconocido (no afirmar
            # pérdida sin dato). En ambos casos no hay alerta.
            continue
        defined_by = tuple(
            f.plugin for f in conflict.flag_states if f.flag == rule.flag and f.value and f.plugin != conflict.winner
        )
        if not defined_by:
            continue
        alerts.append(
            FlagAlert(
                form_id=conflict.form_id,
                editor_id=conflict.editor_id,
                record_type=conflict.record_type,
                flag=rule.flag,
                winner=conflict.winner,
                defined_by=defined_by,
                explanation=rule.explanation,
            )
        )
    return tuple(alerts)
