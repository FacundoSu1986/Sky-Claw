from __future__ import annotations

import logging
from pathlib import Path

from sky_claw._logging_runtime import LoggingHealth
from sky_claw.logging_config import _QUEUE_CAPACITY

ROOT = Path(__file__).parent.parent


def _leer_documento(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _crear_registro(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.observability",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def _assert_contrato_saturacion(documentation: str) -> None:
    assert str(_QUEUE_CAPACITY) in documentation
    assert "WARNING" in documentation
    assert "pueden perderse" in documentation
    assert "256" in documentation
    assert "ERROR/CRITICAL" in documentation
    assert "más antigu" in documentation
    assert "fallback síncrono" in documentation
    assert "fuera del event loop" in documentation


def _assert_contrato_release(documentation: str) -> None:
    assert "v0.2.4" in documentation
    assert "Cosign no equivale a Authenticode" in documentation
    assert "`signtool`" in documentation
    assert "[Unreleased]" in documentation
    assert "no verificó criptográficamente" in documentation
    assert "no hizo" in documentation or "ni hizo" in documentation
    assert "cold boot" in documentation


def test_runtime_expulsa_el_error_mas_antiguo_al_superar_256() -> None:
    health = LoggingHealth()
    records = [_crear_registro(f"error-{index}") for index in range(257)]

    for record in records:
        health.record_drop(record)

    assert health.snapshot(listener_alive=False).emergency_records == 256
    assert health.pop_emergency() is records[1]


def test_deployment_declara_saturacion_acotada() -> None:
    _assert_contrato_saturacion(_leer_documento("DEPLOYMENT.md"))


def test_observabilidad_declara_saturacion_acotada() -> None:
    _assert_contrato_saturacion(_leer_documento("docs/operations/observability.md"))


def test_deployment_delimita_evidencia_de_release() -> None:
    _assert_contrato_release(_leer_documento("DEPLOYMENT.md"))


def test_release_delimita_evidencia_de_release() -> None:
    _assert_contrato_release(_leer_documento("docs/operations/release.md"))


def test_diseno_delimita_evidencia_de_release() -> None:
    _assert_contrato_release(
        _leer_documento("docs/superpowers/specs/2026-07-26-crash-logging-review-hardening-design.md")
    )
