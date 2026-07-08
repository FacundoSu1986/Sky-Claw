"""Tests del ActionManifest (T-26 de TECHNICAL_REVIEW_TASKS.md, ADR 0002).

El norte "caja negra de vuelo" (ADR 0002) exige que todo Ritual mutante emita,
ANTES de ejecutar, un manifiesto inspeccionable — archivos que tocará,
herramienta + versión, y el plan de rollback (qué snapshot restaura qué) — y que
se persista en el journal para poder auditarlo después ("el manifiesto no se
revierte: es el producto").

Contrato serialization-first, extendiendo los modelos del preview existente
(no un contrato paralelo). Estos tests anclan: el modelo, el productor puro
desde los snapshots del lock, y el round-trip de persistencia recuperable tras
un "reinicio" (nueva instancia de journal sobre la misma DB).
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

from sky_claw.antigravity.db.snapshot_manager import SnapshotInfo
from sky_claw.antigravity.orchestrator.preview.action_manifest import (
    ActionManifest,
    RollbackStep,
    build_action_manifest,
)

if TYPE_CHECKING:
    import pathlib


def _snapshot(original: str, snap: str, sid: str) -> SnapshotInfo:
    return SnapshotInfo(
        snapshot_id=sid,
        original_path=original,
        snapshot_path=snap,
        checksum="deadbeef",
        size_bytes=123,
        created_at=_dt.datetime(2026, 7, 8, tzinfo=_dt.UTC),
    )


class TestModelo:
    def test_construye_y_serializa_round_trip(self) -> None:
        manifest = ActionManifest(
            ritual_id="loot-sort-42",
            tool="LOOT",
            tool_version="0.28.0",
            files_touched=["plugins.txt", "loadorder.txt"],
            rollback_plan=[
                RollbackStep(original_path="plugins.txt", snapshot_path="/snap/p.bak", snapshot_id="s1"),
            ],
        )

        recuperado = ActionManifest.model_validate_json(manifest.model_dump_json())

        assert recuperado == manifest
        assert recuperado.rollback_plan[0].snapshot_id == "s1"
        assert recuperado.created_at.tzinfo is not None  # aware UTC

    def test_manifiesto_minimo_sin_rollback(self) -> None:
        manifest = ActionManifest(ritual_id="x", tool="LOOT", tool_version=None)

        assert manifest.files_touched == []
        assert manifest.rollback_plan == []


class TestProductor:
    def test_build_desde_snapshots_del_lock(self) -> None:
        snapshots = [
            _snapshot("plugins.txt", "/snap/p.bak", "s1"),
            _snapshot("loadorder.txt", "/snap/l.bak", "s2"),
        ]

        manifest = build_action_manifest(
            ritual_id="loot-sort-42",
            tool="LOOT",
            tool_version="0.28.0",
            target_files=["plugins.txt", "loadorder.txt"],
            snapshots=snapshots,
        )

        assert manifest.tool == "LOOT"
        assert manifest.files_touched == ["plugins.txt", "loadorder.txt"]
        assert [s.snapshot_id for s in manifest.rollback_plan] == ["s1", "s2"]
        assert manifest.rollback_plan[0].original_path == "plugins.txt"

    def test_sin_snapshots_rollback_vacio_pero_manifiesto_valido(self) -> None:
        """Un ritual sin snapshot (entorno no resoluble) igual emite manifiesto,
        con rollback_plan vacío — el manifiesto nunca es un vacío silencioso."""
        manifest = build_action_manifest(
            ritual_id="loot-sort-1",
            tool="LOOT",
            tool_version=None,
            target_files=[],
            snapshots=[],
        )

        assert manifest.rollback_plan == []
        assert manifest.ritual_id == "loot-sort-1"


class TestPersistencia:
    async def test_round_trip_sobre_reinicio_del_journal(self, tmp_path: pathlib.Path) -> None:
        """Persistir → recuperar tras 'reinicio' (nueva instancia de journal
        sobre la misma DB) → el manifiesto se reconstruye idéntico."""
        from sky_claw.antigravity.db.journal import OperationJournal

        db_path = tmp_path / "journal.db"
        manifest = build_action_manifest(
            ritual_id="loot-sort-42",
            tool="LOOT",
            tool_version="0.28.0",
            target_files=["plugins.txt"],
            snapshots=[_snapshot("plugins.txt", "/snap/p.bak", "s1")],
        )

        journal = OperationJournal(db_path)
        await journal.open()
        tx_id = await journal.begin_transaction(description="loot_sort", agent_id="loot-sorting-service")
        op_id = await journal.persist_action_manifest(manifest, agent_id="loot-sorting-service", transaction_id=tx_id)
        await journal.commit_transaction(tx_id)
        await journal.close()

        assert op_id > 0

        # "Reinicio": nueva instancia de journal sobre la misma DB.
        journal2 = OperationJournal(db_path)
        await journal2.open()
        entries = await journal2.get_operations_by_transaction(tx_id)
        await journal2.close()

        manifiestos = [
            ActionManifest.model_validate(e.metadata) for e in entries if e.metadata and e.metadata.get("ritual_id")
        ]
        assert len(manifiestos) == 1
        assert manifiestos[0] == manifest
        assert manifiestos[0].rollback_plan[0].snapshot_id == "s1"
