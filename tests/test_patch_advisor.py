"""Tests del asistente de estrategia de parcheo (T-20 de TECHNICAL_REVIEW_TASKS.md).

La capa advisory con trazabilidad: por cada grupo de conflictos, recomienda un
enfoque (Bashed Patch / Synthesis / xEdit manual / revisar) **y su porqué**. Es
el germen del boundary ``PatchPlanner`` de ADR 0002 y consume directo el
``RecordConflict`` que T-19a/b enriquecieron (``flag_alerts``).

Invariante duro (ADR 0001, T-04(a) no completada): el asistente **nunca**
recomienda el merged patch propio — las listas niveladas se delegan al Bashed
Patch.
"""

from __future__ import annotations

import json

from sky_claw.local.xedit.conflict_analyzer import RecordConflict
from sky_claw.local.xedit.flag_rules import FlagAlert
from sky_claw.local.xedit.patch_advisor import (
    BASHED_PATCH,
    REVIEW,
    SYNTHESIS,
    XEDIT_MANUAL,
    PatchRecommendation,
    StrategyRule,
    recommend,
)

#: Enfoques válidos conocidos por el asistente (para el test "nunca merged").
_APPROACHES_CONOCIDOS = {BASHED_PATCH, SYNTHESIS, XEDIT_MANUAL, REVIEW}


def _conflicto(
    *,
    record_type: str,
    form_id: str = "000AB123",
    editor_id: str = "SomeRecord",
    winner: str = "Overhaul.esp",
    severity: str = "warning",
    flag_alerts: tuple[FlagAlert, ...] = (),
) -> RecordConflict:
    return RecordConflict(
        form_id=form_id,
        editor_id=editor_id,
        record_type=record_type,
        winner=winner,
        losers=["Skyrim.esm"],
        severity=severity,
        flag_alerts=flag_alerts,
    )


class TestRecomendacionesPorTipo:
    def test_lvli_recomienda_bashed_patch(self) -> None:
        """El caso rojo del backlog: listas niveladas → Bashed Patch, con porqué."""
        recs = recommend([_conflicto(record_type="LVLI")])

        assert len(recs) == 1
        rec = recs[0]
        assert isinstance(rec, PatchRecommendation)
        assert rec.approach == BASHED_PATCH
        assert rec.record_type == "LVLI"
        # La justificación menciona el ADR que fija la decisión.
        assert "ADR 0001" in rec.rationale

    def test_kywd_recomienda_synthesis(self) -> None:
        """Tipo con patcher conocido → Synthesis (reaplicable tras cada reorden)."""
        recs = recommend([_conflicto(record_type="KYWD")])

        assert len(recs) == 1
        assert recs[0].approach == SYNTHESIS

    def test_qust_recomienda_xedit_manual(self) -> None:
        """Crítico de alto riesgo (quest) → forwardeo manual en xEdit."""
        recs = recommend([_conflicto(record_type="QUST", severity="critical")])

        assert len(recs) == 1
        assert recs[0].approach == XEDIT_MANUAL

    def test_tipo_desconocido_es_review(self) -> None:
        """Sin regla que lo cubra → REVIEW (fallback honesto, no inventa enfoque)."""
        recs = recommend([_conflicto(record_type="TXST")])

        assert len(recs) == 1
        assert recs[0].approach == REVIEW

    def test_nunca_recomienda_merged_patch(self) -> None:
        """Invariante ADR 0001: ni siquiera para LVLI se emite un merge propio."""
        recs = recommend(
            [
                _conflicto(record_type="LVLI"),
                _conflicto(record_type="LVLN"),
                _conflicto(record_type="LVSP"),
            ]
        )

        for rec in recs:
            # Ningún enfoque de la tabla es un merge propio: las listas
            # niveladas se delegan al Bashed Patch (ADR 0001).
            assert rec.approach in _APPROACHES_CONOCIDOS
            assert rec.approach == BASHED_PATCH
            assert "merged" not in rec.approach.lower()


class TestTrazabilidad:
    def test_agrupa_por_tipo_con_conteo_y_form_ids(self) -> None:
        """Cada recomendación lleva sus FormIDs y el conteo del grupo (aceptación)."""
        recs = recommend(
            [
                _conflicto(record_type="LVLI", form_id="00000001"),
                _conflicto(record_type="LVLI", form_id="00000002"),
                _conflicto(record_type="KYWD", form_id="00000003"),
            ]
        )

        por_tipo = {rec.record_type: rec for rec in recs}
        assert por_tipo["LVLI"].conflict_count == 2
        assert set(por_tipo["LVLI"].form_ids) == {"00000001", "00000002"}
        assert por_tipo["KYWD"].conflict_count == 1
        assert por_tipo["KYWD"].form_ids == ("00000003",)

    def test_spel_adjunta_flag_alerts(self) -> None:
        """Sinergia T-19b: las alertas de flags viajan en la recomendación."""
        alerta = FlagAlert(
            form_id="000AB123",
            editor_id="HealSpell",
            record_type="SPEL",
            flag="Manual Cost Calc",
            winner="Overhaul.esp",
            defined_by=("Skyrim.esm",),
            explanation="coste astronómico",
        )
        recs = recommend([_conflicto(record_type="SPEL", severity="critical", flag_alerts=(alerta,))])

        assert len(recs) == 1
        assert recs[0].flag_alerts == (alerta,)


class TestOrdenYExtensibilidad:
    def test_orden_por_severidad_critico_primero(self) -> None:
        """Lo más accionable arriba: crítico antes que warning/info."""
        recs = recommend(
            [
                _conflicto(record_type="LVLI", severity="warning"),
                _conflicto(record_type="QUST", severity="critical"),
                _conflicto(record_type="TXST", severity="info"),
            ]
        )

        severidades = [rec.severity for rec in recs]
        assert severidades == ["critical", "warning", "info"]

    def test_regla_custom_mapea_tipo_nuevo(self) -> None:
        """Extensibilidad: una regla es un dato, no código (mismo motor)."""
        regla = StrategyRule(
            record_types=frozenset({"CELL"}),
            approach=SYNTHESIS,
            rationale="un patcher de celdas reaplica el cambio",
        )
        recs = recommend([_conflicto(record_type="CELL")], rules=(regla,))

        assert len(recs) == 1
        assert recs[0].approach == SYNTHESIS
        assert "celdas" in recs[0].rationale


class TestSerializacion:
    def test_to_dict_es_json_serializable(self) -> None:
        alerta = FlagAlert(
            form_id="000AB123",
            editor_id="HealSpell",
            record_type="SPEL",
            flag="Manual Cost Calc",
            winner="Overhaul.esp",
            defined_by=("Skyrim.esm",),
            explanation="coste astronómico",
        )
        recs = recommend(
            [
                _conflicto(record_type="LVLI"),
                _conflicto(record_type="SPEL", severity="critical", flag_alerts=(alerta,)),
            ]
        )

        payload = [rec.to_dict() for rec in recs]
        # No debe lanzar: todo el shape es JSON-serializable.
        texto = json.dumps(payload)
        assert "LVLI" in texto
        assert "Manual Cost Calc" in texto
        # El dict expone los campos de trazabilidad.
        primero = payload[0]
        assert set(primero) >= {
            "approach",
            "record_type",
            "rationale",
            "severity",
            "conflict_count",
            "form_ids",
            "flag_alerts",
        }
