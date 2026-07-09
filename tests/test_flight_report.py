"""Tests del Informe final de vuelo (T-28 de TECHNICAL_REVIEW_TASKS.md, ADR 0002).

La contraparte post-vuelo de la caja negra (T-26): al terminar un Ritual, un
informe legible que LEE el ``ActionManifest`` persistido — no inventa datos —
con las cuatro secciones del test rojo de T-28: qué cambió / por qué / quién
ganó cada conflicto / cómo revertir. Un run sin manifiesto produce un informe
degradado explícito, nunca vacío silencioso ("el manifiesto y el informe final
no se revierten: son el producto").

Estos tests anclan: el productor puro desde el manifiesto, el render Markdown
(aceptación: "exportable como Markdown"), el round-trip de persistencia tras un
"reinicio" (nueva instancia de journal sobre la misma DB), y el composer que
lee la caja negra sin confundir el op del informe con el del manifiesto.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sky_claw.antigravity.orchestrator.preview.flight_report import (
    FlightReport,
    build_flight_report,
    compose_flight_report_from_journal,
    render_flight_report_markdown,
)
from sky_claw.antigravity.orchestrator.preview.manifest import (
    ActionManifest,
    ConflictPair,
    LoadOrderDiff,
    RollbackStep,
)

if TYPE_CHECKING:
    import pathlib

    from sky_claw.antigravity.db.journal import OperationJournal


def _manifiesto() -> ActionManifest:
    """Un ActionManifest con datos en las cuatro fuentes del informe."""
    return ActionManifest(
        ritual_id="loot-sort-42",
        tool="LOOT",
        tool_version="0.28.0",
        files_touched=["plugins.txt", "loadorder.txt"],
        records_forwarded=[
            ConflictPair(winner="USSEP.esp", losers=["OtroMod.esp"], record_type="SPEL", form_id="0001A4F2"),
        ],
        load_order_diff=LoadOrderDiff.from_orders(
            before=["Skyrim.esm", "B.esp", "A.esp"],
            after=["Skyrim.esm", "A.esp", "B.esp"],
        ),
        rollback_plan=[
            RollbackStep(original_path="plugins.txt", snapshot_path="/snap/p.bak", snapshot_id="s1"),
        ],
        summary="Ordenar orden de carga con LOOT.",
    )


async def _journal_abierto(tmp_path: pathlib.Path) -> OperationJournal:
    from sky_claw.antigravity.db.journal import OperationJournal

    journal = OperationJournal(tmp_path / "journal.db")
    await journal.open()
    return journal


class TestProductor:
    def test_informe_contiene_las_cuatro_secciones(self) -> None:
        """Cambios / razones / ganadores / rollback salen del manifiesto real."""
        manifest = _manifiesto()

        report = build_flight_report(manifest=manifest, transaction_status="committed")

        # Identidad del Ritual (no se re-deriva nada).
        assert report.ritual_id == "loot-sort-42"
        assert report.tool == "LOOT"
        assert report.tool_version == "0.28.0"
        assert report.transaction_status == "committed"
        assert report.degraded is False
        # Sección cambios.
        assert report.files_touched == ["plugins.txt", "loadorder.txt"]
        assert report.load_order_diff is not None
        assert report.load_order_diff.changed
        # Sección razones.
        assert report.summary == "Ordenar orden de carga con LOOT."
        # Sección ganadores.
        assert [c.winner for c in report.conflicts_resolved] == ["USSEP.esp"]
        assert report.conflicts_resolved[0].losers == ["OtroMod.esp"]
        # Sección rollback (apunta a snapshots reales).
        assert report.rollback_plan[0].snapshot_id == "s1"
        assert report.rollback_plan[0].original_path == "plugins.txt"

    def test_sin_manifiesto_informe_degradado_explicito(self) -> None:
        """Sin caja negra el informe existe igual, degradado y con razón."""
        report = build_flight_report(manifest=None, transaction_status="rolled_back")

        assert report.degraded is True
        assert report.degraded_reason
        assert report.transaction_status == "rolled_back"
        assert report.files_touched == []
        assert report.rollback_plan == []

    def test_slot_de_validacion_post_run(self) -> None:
        """El slot de T-21 viaja cuando existe; None mientras T-21 no aterrice."""
        con = build_flight_report(
            manifest=_manifiesto(),
            transaction_status="committed",
            post_run_validation={"status": "green", "checks": 3},
        )
        sin = build_flight_report(manifest=_manifiesto(), transaction_status="committed")

        assert con.post_run_validation == {"status": "green", "checks": 3}
        assert sin.post_run_validation is None

    def test_round_trip_de_serializacion(self) -> None:
        report = build_flight_report(manifest=_manifiesto(), transaction_status="committed")

        recuperado = FlightReport.model_validate_json(report.model_dump_json())

        assert recuperado == report
        assert recuperado.kind == "flight_report"
        assert recuperado.created_at.tzinfo is not None  # aware UTC


class TestMarkdown:
    def test_contiene_los_cuatro_encabezados_y_los_datos(self) -> None:
        report = build_flight_report(manifest=_manifiesto(), transaction_status="committed")

        md = render_flight_report_markdown(report)

        for encabezado in ("## Qué cambió", "## Por qué", "## Quién ganó cada conflicto", "## Cómo revertir"):
            assert encabezado in md
        assert "plugins.txt" in md
        assert "USSEP.esp" in md and "OtroMod.esp" in md
        assert "s1" in md and "/snap/p.bak" in md
        assert "committed" in md

    def test_degradado_muestra_la_razon_nunca_vacio(self) -> None:
        report = build_flight_report(manifest=None, transaction_status="pending")

        md = render_flight_report_markdown(report)

        assert md.strip()
        assert report.degraded_reason is not None
        assert report.degraded_reason in md

    def test_declara_la_validacion_post_run_pendiente(self) -> None:
        """El slot vacío se declara explícito (T-21 pendiente), no se omite."""
        sin = render_flight_report_markdown(build_flight_report(manifest=_manifiesto(), transaction_status="committed"))
        con = render_flight_report_markdown(
            build_flight_report(
                manifest=_manifiesto(),
                transaction_status="committed",
                post_run_validation={"status": "green"},
            )
        )

        assert "## Validación post-run" in sin
        assert "T-21" in sin  # degradado explícito del slot, nunca omitido
        assert "green" in con


class TestPersistencia:
    async def test_round_trip_sobre_reinicio_del_journal(self, tmp_path: pathlib.Path) -> None:
        """Persistir → recuperar tras 'reinicio' (nueva instancia de journal
        sobre la misma DB) → el informe se reconstruye idéntico."""
        from sky_claw.antigravity.db.journal import OperationJournal

        db_path = tmp_path / "journal.db"
        report = build_flight_report(manifest=_manifiesto(), transaction_status="committed")

        journal = OperationJournal(db_path)
        await journal.open()
        tx_id = await journal.begin_transaction(description="loot_sort", agent_id="loot-sorting-service")
        op_id = await journal.persist_flight_report(report, agent_id="loot-sorting-service", transaction_id=tx_id)
        await journal.commit_transaction(tx_id)
        await journal.close()

        assert op_id > 0

        # "Reinicio": nueva instancia de journal sobre la misma DB.
        journal2 = OperationJournal(db_path)
        await journal2.open()
        entries = await journal2.get_operations_by_transaction(tx_id)
        await journal2.close()

        informes = [
            FlightReport.model_validate(e.metadata)
            for e in entries
            if e.metadata and e.metadata.get("kind") == "flight_report"
        ]
        assert len(informes) == 1
        assert informes[0] == report


class TestComposer:
    async def test_compone_desde_la_caja_negra_persistida(self, tmp_path: pathlib.Path) -> None:
        manifest = _manifiesto()
        journal = await _journal_abierto(tmp_path)
        tx_id = await journal.begin_transaction(description="loot_sort", agent_id="loot-sorting-service")
        await journal.persist_action_manifest(manifest, agent_id="loot-sorting-service", transaction_id=tx_id)
        await journal.commit_transaction(tx_id)

        report = await compose_flight_report_from_journal(journal, transaction_id=tx_id)
        await journal.close()

        assert report.degraded is False
        assert report.transaction_status == "committed"
        assert report.ritual_id == manifest.ritual_id
        assert report.files_touched == manifest.files_touched
        assert report.conflicts_resolved == manifest.records_forwarded
        assert report.rollback_plan == manifest.rollback_plan

    async def test_no_confunde_el_op_del_informe_con_el_manifiesto(self, tmp_path: pathlib.Path) -> None:
        """Releer una TX que ya tiene manifiesto + informe persistidos debe
        componer desde el MANIFIESTO (el informe también trae ritual_id)."""
        manifest = _manifiesto()
        journal = await _journal_abierto(tmp_path)
        tx_id = await journal.begin_transaction(description="loot_sort", agent_id="loot-sorting-service")
        await journal.persist_action_manifest(manifest, agent_id="loot-sorting-service", transaction_id=tx_id)
        await journal.commit_transaction(tx_id)
        # Un informe previo con datos distintos (files_touched vacío): si el
        # composer lo tomara como manifiesto, la re-composición saldría vacía.
        report_previo = build_flight_report(manifest=None, transaction_status="committed")
        await journal.persist_flight_report(report_previo, agent_id="loot-sorting-service", transaction_id=tx_id)

        recompuesto = await compose_flight_report_from_journal(journal, transaction_id=tx_id)
        await journal.close()

        assert recompuesto.degraded is False
        assert recompuesto.files_touched == manifest.files_touched

    async def test_tx_sin_manifiesto_produce_informe_degradado(self, tmp_path: pathlib.Path) -> None:
        journal = await _journal_abierto(tmp_path)
        tx_id = await journal.begin_transaction(description="loot_sort", agent_id="loot-sorting-service")
        await journal.commit_transaction(tx_id)

        report = await compose_flight_report_from_journal(journal, transaction_id=tx_id)
        await journal.close()

        assert report.degraded is True
        assert report.degraded_reason
        assert report.transaction_status == "committed"
        # Nunca vacío silencioso: el Markdown degradado muestra la razón.
        assert report.degraded_reason in render_flight_report_markdown(report)

    async def test_tx_inexistente_estado_desconocido_y_degradado(self, tmp_path: pathlib.Path) -> None:
        journal = await _journal_abierto(tmp_path)

        report = await compose_flight_report_from_journal(journal, transaction_id=99999)
        await journal.close()

        assert report.degraded is True
        assert report.transaction_status == "desconocido"
