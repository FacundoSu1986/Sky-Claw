"""Document-only contract checks for the GP2 P0.1 Golden Admission design.

These assertions check that the ADR keeps the authority flow and its limits
explicit. They are not evidence that a verifier child, IPC channel, or TGR
writer enforces those guarantees in production.
"""

from __future__ import annotations

import re
from pathlib import Path

ADR_PATH = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0010-runtime-vault-golden-protection-apply.md"
ADR = ADR_PATH.read_text(encoding="utf-8")


def section(start: str, end: str) -> str:
    start_index = ADR.index(start)
    end_index = ADR.index(end, start_index + len(start))
    return ADR[start_index:end_index]


def assert_in_order(text: str, *markers: str) -> None:
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_admission_sources_are_closed_and_distinct_from_gp2_fsm() -> None:
    axioms = section(
        "### 11.0 Axioma de Autoridad Única",
        "### 11.1 El Problema de la Autorización",
    )
    assert "GOLDEN_ADMISSION_SOURCES = {INDEPENDENT_PROVENANCE, OPERATOR_TOFU}" in axioms
    assert "STAGING_IS_AUTHORITY      = NO" in axioms
    assert "OBSERVATION_IS_AUTHORITY  = NO" in axioms
    assert "ADMISSION_IS_VERIFICATION = NO" in axioms
    assert "GP2_APPLY_WRITES_TGR           = NO" in axioms
    assert "estados al FSM GP2" in axioms


def test_independent_provenance_flow_admits_full_expectation_before_verifying() -> None:
    flow = section(
        "**INDEPENDENT_PROVENANCE — alta inicial:**",
        "**INDEPENDENT_PROVENANCE — refresh A→B:**",
    )
    assert_in_order(
        flow,
        "verified provenance bundle provides expectation B",
        "validate source, full-tree coverage, runtime and physical-root binding",
        "ADMITTED expectation + helper-issued receipt B",
        "fresh RV-2",
        "require VERIFIED",
        "privileged plan-specific confirmation",
        "TGR B",
    )
    assert "expected_runtime" in section(
        "#### Fuentes cerradas de expectativa",
        "#### Modelos de admission",
    )


def test_tofu_requires_observation_confirmation_receipt_and_fresh_rerun() -> None:
    flow = section(
        "**OPERATOR_TOFU — alta inicial:**",
        "**OPERATOR_TOFU — refresh A→B:**",
    )
    assert_in_order(
        flow,
        "UNTRUSTED CANDIDATE",
        "fresh read-only observation",
        "OBSERVED B",
        "privileged confirmation: admit exactly B",
        "helper-issued ADMITTED EXPECTATION / one-use receipt B",
        "fresh RV-2 rerun from zero",
        "B2 == B",
        "VERIFIED",
        "TGR B",
    )
    contract = section(
        "### 11.4 Golden Admission",
        "## 12. Privileged Helper Boundary",
    )
    assert "no reutiliza `FileIdentity`, bytes, resultado ni digest del primer observation pass" in contract
    assert "cero TGR write" in contract


def test_receipt_fields_and_untrusted_staging_exclusions_are_explicit() -> None:
    contract = section(
        "### 11.4 Golden Admission",
        "## 12. Privileged Helper Boundary",
    )
    required_fields = (
        "operation_id",
        "canonical_root",
        "VolumeSerialNumber",
        "root_file_id",
        "tree_digest",
        "expected_runtime",
        "policy_version",
        "admission_source",
        "operator_sid",
        "admitted_at",
        "critical_expectations_digest",
        "source_provenance_digest",
        "nonce",
    )
    receipt = section(
        "GoldenAdmissionReceipt` es creado",
        "Bindings normativos del receipt:",
    )
    assert all(field in receipt for field in required_fields)
    assert "nunca se deserializa desde `UNTRUSTED_STAGING`" in contract
    assert "TokenUser` del `OperatorPrimaryToken` original" in contract
    assert "critical_expectations=()` sigue siendo válido" in contract
    assert "source_provenance_digest = null" in contract


def test_tgr_and_tofu_claims_include_their_security_limits() -> None:
    assert "OPERATOR_TOFU DOES NOT DETECT PRE-EXISTING COMPROMISE" in ADR
    assert "TGR_SEMANTICS = LAST_EXPLICITLY_AUTHORIZED_AND_SUBSEQUENTLY_VERIFIED_SNAPSHOT" in ADR
    assert "TGR_ASSERTS_CURRENT_FILESYSTEM = NO" in ADR
    assert "ni snapshot atómico" in ADR
    assert "REFUSE_TO_PLAN" in section(
        "## 28. Limitaciones de Seguridad Declaradas",
        "## 29. Referencias Primarias Microsoft",
    )


def test_actor_d_mutations_and_all_future_rvo_oracles_are_classified() -> None:
    admission = section(
        "#### Clasificación de amenazas Actor D para admission",
        "#### Steam-managed mirror",
    )
    for marker in (
        "pre-admission",
        "después de `OBSERVED`",
        "después de `ADMITTED`",
        "durante el fresh rerun",
        "post-rerun",
        "Replay de un receipt",
        "Runtime sustituto",
        "SID o timestamp falsificados en staging",
    ):
        assert marker in admission

    oracle_ids = re.findall(r"^- \*\*(RVO-\d{2}):\*\*", ADR, flags=re.MULTILINE)
    assert oracle_ids == [f"RVO-{number:02d}" for number in range(1, 13)]
    assert "**no** se consideran tests ejecutados" in ADR
    assert "no puede pasar estos oráculos sólo fabricando callbacks/mocks" in ADR


def test_steam_and_component_provenance_are_not_overclaimed() -> None:
    context = section(
        "#### Steam-managed mirror, base runtime y mods",
        "## 12. Privileged Helper Boundary",
    )
    assert "no demuestra provenance criptográfica" in context
    assert "No se asume que toda instalación actual ya esté separada" in context
    assert "base runtime no autentica MO2 mods" in context
    assert "appmanifest_489830.acf" in context
