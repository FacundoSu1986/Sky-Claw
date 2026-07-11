"""Tests del motor declarativo de reglas de flags (T-19b de TECHNICAL_REVIEW_TASKS.md).

La primera regla del ConflictRuleEngine (ADR 0002): un override define
`Manual Cost Calc` en un SPEL y el ganador del conflicto no lo preserva →
alerta crítica con texto explicativo. Sin este flag, el motor del juego
recalcula el coste del hechizo por duración — coste astronómico en mods de
magia sostenida. El diseño es declarativo: una regla nueva (PERK/MGEF/
Relev/Delev) es un dato (`FlagRule`), no código.
"""

from __future__ import annotations

from sky_claw.local.xedit.conflict_analyzer import (
    CRITICAL_FLAGS,
    OverrideFlagState,
    RecordConflict,
)
from sky_claw.local.xedit.flag_rules import (
    DEFAULT_FLAG_RULES,
    FlagAlert,
    FlagRule,
    evaluate_flag_rules,
)


def _conflicto(
    *,
    record_type: str = "SPEL",
    winner: str = "Overhaul.esp",
    flag_states: tuple[OverrideFlagState, ...] = (),
) -> RecordConflict:
    return RecordConflict(
        form_id="000AB123",
        editor_id="HealSpell",
        record_type=record_type,
        winner=winner,
        losers=["Skyrim.esm", "MagicFix.esp"],
        severity="critical",
        flag_states=flag_states,
    )


def _estado(plugin: str, value: bool, flag: str = "Manual Cost Calc") -> OverrideFlagState:
    return OverrideFlagState(plugin=plugin, flag=flag, value=value)


class TestReglaManualCostCalc:
    def test_ganador_que_no_preserva_dispara_alerta_critica(self) -> None:
        """El caso canónico del backlog: master define el flag, el ganador no."""
        conflicto = _conflicto(
            flag_states=(
                _estado("Skyrim.esm", True),
                _estado("MagicFix.esp", True),
                _estado("Overhaul.esp", False),  # el ganador lo pierde
            )
        )

        alertas = evaluate_flag_rules(conflicto)

        assert len(alertas) == 1
        alerta = alertas[0]
        assert isinstance(alerta, FlagAlert)
        assert alerta.severity == "critical"
        assert alerta.flag == "Manual Cost Calc"
        assert alerta.winner == "Overhaul.esp"
        assert set(alerta.defined_by) == {"Skyrim.esm", "MagicFix.esp"}
        # El texto explicativo del backlog, literal.
        assert "coste astronómico" in alerta.explanation

    def test_ganador_que_preserva_no_alerta(self) -> None:
        conflicto = _conflicto(
            flag_states=(
                _estado("Skyrim.esm", True),
                _estado("Overhaul.esp", True),  # el ganador lo preserva
            )
        )

        assert evaluate_flag_rules(conflicto) == ()

    def test_ganador_con_estado_desconocido_no_alerta(self) -> None:
        """Honestidad: sin línea FLAG del ganador no se afirma la pérdida."""
        conflicto = _conflicto(
            flag_states=(
                _estado("Skyrim.esm", True),
                # Overhaul.esp (ganador) sin estado — SPIT ilegible/ausente.
            )
        )

        assert evaluate_flag_rules(conflicto) == ()

    def test_nadie_define_el_flag_no_alerta(self) -> None:
        conflicto = _conflicto(
            flag_states=(
                _estado("Skyrim.esm", False),
                _estado("Overhaul.esp", False),
            )
        )

        assert evaluate_flag_rules(conflicto) == ()

    def test_sin_flag_states_no_alerta(self) -> None:
        """Compat: un conflicto sin datos por-versión (salida vieja) no dispara."""
        assert evaluate_flag_rules(_conflicto()) == ()

    def test_firma_distinta_no_alerta(self) -> None:
        conflicto = _conflicto(
            record_type="NPC_",
            flag_states=(
                _estado("Skyrim.esm", True),
                _estado("Overhaul.esp", False),
            ),
        )

        assert evaluate_flag_rules(conflicto) == ()


class TestMotorDeclarativo:
    def test_regla_custom_dispara_con_el_mismo_motor(self) -> None:
        """Extensibilidad (aceptación T-19b): una regla nueva es un dato."""
        regla_perk = FlagRule(signature="PERK", flag="Playable", explanation="el perk deja de aparecer en el árbol")
        conflicto = _conflicto(
            record_type="PERK",
            flag_states=(
                _estado("Skyrim.esm", True, flag="Playable"),
                _estado("Overhaul.esp", False, flag="Playable"),
            ),
        )

        alertas = evaluate_flag_rules(conflicto, rules=(regla_perk,))

        assert len(alertas) == 1
        assert alertas[0].flag == "Playable"
        assert "árbol" in alertas[0].explanation

    def test_toda_regla_default_tiene_su_export(self) -> None:
        """Una regla sin datos nunca dispara (verde mentiroso, lección #250):
        cada FlagRule default debe tener su flag exportado en CRITICAL_FLAGS."""
        for regla in DEFAULT_FLAG_RULES:
            assert regla.flag in CRITICAL_FLAGS.get(regla.signature, ()), (
                f"La regla {regla.signature}/{regla.flag} no tiene export en CRITICAL_FLAGS: "
                "el script de xEdit nunca emitiría su dato y la regla jamás dispararía."
            )
