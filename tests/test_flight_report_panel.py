"""Tests del constructor de view-model del panel de informe de vuelo (T-28 GUI).

El panel NiceGUI es glue fino de browser; la lógica de display vive en el helper
puro ``build_flight_report_view_model``, que es lo que fijamos acá. La entrada es
el contrato estable de :class:`~sky_claw.antigravity.orchestrator.preview.manifest.FlightReport`
(el mismo modelo que los 6 rituales persisten en el journal, T-26/T-28), así que
los tests construyen informes de dominio REALES y serializan — no un dict inventado.
"""

from __future__ import annotations

from sky_claw.antigravity.gui.views.sections.flight_report_panel import build_flight_report_view_model
from sky_claw.antigravity.orchestrator.preview.manifest import (
    ConflictPair,
    FlightReport,
    LoadOrderDiff,
    RollbackStep,
)


def _committed_report() -> dict:
    """Vuelo aplicado: tocó un archivo, resolvió un conflicto y trae plan de rollback."""
    return FlightReport(
        ritual_id="wrye-bash-42",
        tool="Wrye Bash",
        transaction_status="committed",
        files_touched=["Bashed Patch, 0.esp"],
        summary="Bashed Patch regenerado desde 254 plugins activos.",
        conflicts_resolved=[
            ConflictPair(winner="Bashed Patch, 0.esp", losers=["A.esp", "B.esp"], record_type="LVLI", form_id="0x00A1"),
        ],
        rollback_plan=[
            RollbackStep(
                original_path="overwrite/Bashed Patch, 0.esp", snapshot_path="snaps/bp.bak", snapshot_id="snap-1"
            ),
        ],
    ).model_dump(mode="json")


def _rolled_back_report() -> dict:
    """Vuelo revertido: la TX terminó rolled_back (el ritual falló y se restauró)."""
    return FlightReport(
        ritual_id="xedit-7",
        tool="SSEEdit",
        transaction_status="rolled_back",
        files_touched=["Update.esm"],
        summary="QuickAutoClean abortado; se restauró el master.",
    ).model_dump(mode="json")


def _degraded_report() -> dict:
    """Informe degradado: sin manifiesto persistido (nunca vacío silencioso)."""
    return FlightReport(
        transaction_status="desconocido",
        degraded=True,
        degraded_reason="No se encontró el ActionManifest de la transacción 99.",
    ).model_dump(mode="json")


def test_committed_report_populates_all_sections() -> None:
    vm = build_flight_report_view_model(_committed_report())

    assert vm["header"]["tool"] == "Wrye Bash"
    assert vm["header"]["ritual_id"] == "wrye-bash-42"
    assert vm["header"]["status"] == "committed"
    assert "aplica" in vm["header"]["status_label"].lower()
    assert vm["header"]["degraded"] is False

    # "qué cambió"
    assert vm["changed"]["files_touched"] == ["Bashed Patch, 0.esp"]
    assert vm["changed"]["has_changes"] is True

    # "por qué"
    assert "Bashed Patch" in vm["summary"]

    # "quién ganó cada conflicto"
    assert len(vm["conflicts_resolved"]) == 1
    conflict = vm["conflicts_resolved"][0]
    assert conflict["winner"] == "Bashed Patch, 0.esp"
    assert conflict["losers"] == ["A.esp", "B.esp"]
    assert conflict["record_type"] == "LVLI"

    # "cómo revertir"
    assert len(vm["rollback"]) == 1
    assert vm["rollback"][0]["snapshot_id"] == "snap-1"
    assert "overwrite/Bashed Patch, 0.esp" in vm["rollback"][0]["original_path"]


def test_rolled_back_report_status_is_red() -> None:
    vm = build_flight_report_view_model(_rolled_back_report())

    assert vm["header"]["status"] == "rolled_back"
    assert "revert" in vm["header"]["status_label"].lower()
    # Sin conflictos ni rollback plan → secciones vacías, sin romper.
    assert vm["conflicts_resolved"] == []
    assert vm["rollback"] == []


def test_degraded_report_surfaces_reason() -> None:
    vm = build_flight_report_view_model(_degraded_report())

    assert vm["header"]["degraded"] is True
    assert "ActionManifest" in vm["header"]["degraded_reason"]
    # tool/ritual ausentes no rompen: caen a etiquetas por defecto no vacías.
    assert vm["header"]["tool"]
    assert vm["header"]["status"] == "desconocido"


def test_load_order_diff_moves_surface() -> None:
    report = FlightReport(
        ritual_id="loot-1",
        tool="LOOT",
        transaction_status="committed",
        load_order_diff=LoadOrderDiff.from_orders(["A.esp", "B.esp"], ["B.esp", "A.esp"]),
    ).model_dump(mode="json")

    vm = build_flight_report_view_model(report)

    assert vm["changed"]["has_changes"] is True
    plugins_movidos = {m["plugin"] for m in vm["changed"]["moves"]}
    assert plugins_movidos == {"A.esp", "B.esp"}


def test_post_run_validation_none_declared_unavailable() -> None:
    """Sin post-run validator (T-21), el slot queda ``None`` — el renderer lo
    declara 'no disponible' en vez de omitirlo (honestidad, no vacío silencioso)."""
    vm = build_flight_report_view_model(_committed_report())
    assert vm["post_run_validation"] is None

    con_validacion = FlightReport(
        ritual_id="loot-2",
        tool="LOOT",
        transaction_status="committed",
        post_run_validation={"status": "green", "checks": []},
    ).model_dump(mode="json")
    vm2 = build_flight_report_view_model(con_validacion)
    assert vm2["post_run_validation"] == {"status": "green", "checks": []}


def test_committed_sin_cambios_reporta_has_changes_false() -> None:
    """Un vuelo committed que no tocó archivos ni movió el orden → has_changes False."""
    report = FlightReport(ritual_id="x", tool="LOOT", transaction_status="committed").model_dump(mode="json")
    vm = build_flight_report_view_model(report)
    assert vm["changed"]["files_touched"] == []
    assert vm["changed"]["moves"] == []
    assert vm["changed"]["has_changes"] is False


def test_partial_dict_does_not_crash() -> None:
    """Un dict parcial (solo el estado de la TX) degrada a secciones vacías, nunca
    rompe — mismo criterio defensivo que ``build_preflight_view_model``."""
    vm = build_flight_report_view_model({"transaction_status": "committed"})

    assert vm["header"]["status"] == "committed"
    assert vm["header"]["tool"]  # etiqueta por defecto, no cadena vacía
    assert vm["changed"]["files_touched"] == []
    assert vm["conflicts_resolved"] == []
    assert vm["rollback"] == []
    assert vm["summary"] == ""


def test_unknown_status_falls_back_without_crashing() -> None:
    vm = build_flight_report_view_model({"transaction_status": "pending"})
    assert vm["header"]["status"] == "pending"
    assert vm["header"]["status_label"]  # hay etiqueta, no vacío
