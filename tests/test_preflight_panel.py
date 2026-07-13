"""Tests del constructor de view-model del panel de preflight (T-16).

El panel NiceGUI es glue fino de browser; la lógica de display vive en el
helper puro ``build_preflight_view_model``, que es lo que fijamos acá. La
entrada es el contrato estable ``PreflightReport.to_dict()`` (el mismo dict que
consume el journal), así que los tests construyen reportes de dominio REALES y
serializan — no un dict inventado.
"""

from __future__ import annotations

from sky_claw.antigravity.gui.views.sections.preflight_panel import build_preflight_view_model
from sky_claw.local.validators.preflight import (
    PreflightCheck,
    PreflightReport,
    PreflightStatus,
)


def _green_report() -> dict:
    """Semáforo verde: todos los sensores en verde, sin details."""
    return PreflightReport(
        status=PreflightStatus.GREEN,
        checks=(
            PreflightCheck(
                name="vfs", status=PreflightStatus.GREEN, summary="Sin symlinks/junctions en rutas críticas."
            ),
            PreflightCheck(
                name="loot_version",
                status=PreflightStatus.GREEN,
                summary="LOOT 0.29.0 (≥0.29: libloot no resuelve symlinks).",
            ),
        ),
    ).to_dict()


def _red_report() -> dict:
    """Rojo por composición: vfs crítico + LOOT viejo + el check de composición."""
    return PreflightReport(
        status=PreflightStatus.RED,
        checks=(
            PreflightCheck(
                name="vfs",
                status=PreflightStatus.RED,
                summary="1 enlace(s) detectado(s) en la infraestructura.",
                details=("junction: C:/Games/Skyrim — eliminá el junction antes de ordenar",),
            ),
            PreflightCheck(
                name="loot_version",
                status=PreflightStatus.YELLOW,
                summary="LOOT 0.28.1 (<0.29): libloot resuelve symlinks.",
            ),
            PreflightCheck(
                name="composition",
                status=PreflightStatus.RED,
                summary="Enlaces presentes + LOOT 0.28.1 (<0.29): libloot queda ciego ante el VFS de MO2.",
            ),
        ),
    ).to_dict()


def _yellow_report() -> dict:
    """Amarillo: un sensor degradado con details accionables (masters faltantes)."""
    return PreflightReport(
        status=PreflightStatus.YELLOW,
        checks=(
            PreflightCheck(
                name="masters",
                status=PreflightStatus.YELLOW,
                summary="2 plugin(s) con masters faltantes.",
                details=(
                    "Foo.esp requiere Bar.esm (ausente)",
                    "Baz.esp requiere Qux.esm (ausente)",
                ),
            ),
        ),
    ).to_dict()


def test_view_model_green_is_ready_and_unblocked() -> None:
    vm = build_preflight_view_model(_green_report())

    assert vm["status"] == "green"
    assert vm["blocks_mutations"] is False
    assert vm["banner"]["status"] == "green"
    assert vm["banner"]["blocks"] is False
    # El banner comunica que se puede lanzar.
    assert "listo" in vm["banner"]["label"].lower()
    # Los checks se surface sin details cuando no los hay.
    assert [c["name"] for c in vm["checks"]] == ["vfs", "loot_version"]
    assert all(c["details"] == [] for c in vm["checks"])


def test_view_model_red_blocks_mutations() -> None:
    vm = build_preflight_view_model(_red_report())

    assert vm["status"] == "red"
    assert vm["blocks_mutations"] is True
    assert vm["banner"]["status"] == "red"
    assert vm["banner"]["blocks"] is True
    # El banner comunica el bloqueo del Ritual.
    assert "bloque" in vm["banner"]["label"].lower()


def test_view_model_preserves_check_order_and_details() -> None:
    """El orden de los checks (vfs → loot → composición) se preserva y los
    ``details`` accionables se listan tal cual llegan del agregador."""
    vm = build_preflight_view_model(_red_report())

    assert [c["name"] for c in vm["checks"]] == ["vfs", "loot_version", "composition"]
    assert [c["status"] for c in vm["checks"]] == ["red", "yellow", "red"]
    vfs = vm["checks"][0]
    assert vfs["details"] == ["junction: C:/Games/Skyrim — eliminá el junction antes de ordenar"]
    assert "1 enlace" in vfs["summary"]


def test_view_model_yellow_surfaces_remediation_details() -> None:
    vm = build_preflight_view_model(_yellow_report())

    assert vm["status"] == "yellow"
    assert vm["blocks_mutations"] is False
    assert vm["banner"]["status"] == "yellow"
    # Amarillo advierte sin bloquear.
    assert vm["banner"]["blocks"] is False
    masters = vm["checks"][0]
    assert masters["details"] == [
        "Foo.esp requiere Bar.esm (ausente)",
        "Baz.esp requiere Qux.esm (ausente)",
    ]


def test_view_model_tolerates_check_without_details() -> None:
    """Un check parcial (sin la clave ``details``) degrada a lista vacía, nunca
    rompe — mismo criterio defensivo que ``build_preview_view_model``."""
    report = {
        "status": "yellow",
        "blocks_mutations": False,
        "checks": [{"name": "loot_version", "status": "yellow", "summary": "versión indetectable"}],
    }

    vm = build_preflight_view_model(report)  # no debe romper

    assert vm["checks"][0]["details"] == []
    assert vm["checks"][0]["summary"] == "versión indetectable"


def test_view_model_handles_empty_report() -> None:
    """Un reporte sin checks igual produce banner (nunca vacío silencioso)."""
    vm = build_preflight_view_model({"status": "green", "blocks_mutations": False, "checks": []})

    assert vm["status"] == "green"
    assert vm["checks"] == []
    assert vm["banner"]["status"] == "green"
    assert vm["banner"]["label"]  # hay una etiqueta, no cadena vacía
