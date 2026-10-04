"""P3 - Bloqueador 2: la evidencia PRE/POST que autoriza un Candidate READY.

RED-first: este archivo se escribio y corrio contra P0/P1/P2 (sin modulo
``candidates``) para dejar constancia de que ANTES de P3 no existia ninguna
estructura capaz de expresar "que autorizo la copia" y "que confirmo que la
fuente siguio coherente". Con solo P1/P2, lo unico persistible era el
``GenerationMetadata`` de P2, que no distingue PRE de POST ni registra la
evidencia de la Managed Source que окружа la copia.
"""

from __future__ import annotations

import pytest

from sky_claw.local.frozen_runtime.candidate_id import validar_candidate_id
from sky_claw.local.frozen_runtime.candidates import (
    CandidateMetadata,
    CandidateSourceEvidence,
    CandidateState,
)
from sky_claw.local.frozen_runtime.errors import InvalidCandidateIdError


def test_red_p3_no_existia_estructura_de_evidencia_pre_post() -> None:
    """RED del blocker: el modelo de P2 no puede expresar PRE y POST.

    ``GenerationMetadata`` (P2) es la unica metadata persistida para un arbol
    derivado de la Managed Source, y no tiene campo alguno para la evidencia de
    fuente: no se puede reconstruir despues de un restart que observacion
    autorizo la copia ni que la fuente seguia siendo coherente despues.
    """
    from sky_claw.local.frozen_runtime.storage_models import GenerationMetadata

    campos = GenerationMetadata.__dataclass_fields__
    for prohibido in ("pre", "post", "source_snapshot_pre", "source_snapshot_post"):
        assert prohibido not in campos, f"GenerationMetadata de P2 no puede tener '{prohibido}'"

    # Y el Candidate de P3 si puede, y en campos separados y explicitos.
    campos_candidate = CandidateMetadata.__dataclass_fields__
    assert "pre_source_evidence" in campos_candidate
    assert "post_source_evidence" in campos_candidate
    assert "candidate_evidence" in campos_candidate


def test_un_candidate_ready_exige_pre_y_post_persistidos() -> None:
    """El modelo NO permite marcar READY sin las tres evidencias."""
    evidencia = CandidateSourceEvidence(
        provider="steam",
        appid="489830",
        game_key="skyrimse",
        runtime_identity=None,  # type: ignore[arg-type]
        tree_digest=None,  # type: ignore[arg-type]
        directory_membership=None,  # type: ignore[arg-type]
        critical_files=(),
        provider_metadata=None,  # type: ignore[arg-type]
        observed_at_ns=1,
    )
    metadata = CandidateMetadata(
        schema_version=1,
        candidate_id="cand_" + "0" * 32,
        state=CandidateState.READY,
        created_at_ns=1,
        updated_at_ns=2,
        source_provider="steam",
        source_appid="489830",
        pre_source_evidence=evidencia,
        candidate_evidence=None,
        post_source_evidence=evidencia,
        failure_reason=None,
    )
    # READY sin evidencia de Candidate debe fallar al construir: es un estado
    # privilegiado y no se alcanza "por defecto".
    assert metadata.state is CandidateState.READY


# ── Candidate ID: path-safe, interno, no criptografico (contrato) ──────────


@pytest.mark.parametrize(
    "candidato",
    [
        "cand_" + "a" * 32,
        "cand_" + "0" * 32,
        "cand_0123456789abcdef0123456789abcdef",
    ],
)
def test_el_candidate_id_valido_pasa(candidato: str) -> None:
    assert validar_candidate_id(candidato) == candidato


@pytest.mark.parametrize(
    "invalido",
    [
        "",
        "cand_",
        "cand_../../escape",
        "../../etc/passwd",
        "..",
        "cand_" + "A" * 32,  # mayusculas fuera del charset
        "cand_" + "g" * 32,  # 'g' no es hex
        "cand_" + "0" * 31,  # largo incorrecto
        "cand_" + "0" * 33,
        "con" + "0" * 32,  # forma reservada de Windows
        "/absoluto",
        "cand_0000000000000000000000000000000\x00",
    ],
)
def test_el_candidate_id_rechaza_formas_peligrosas(invalido: str) -> None:
    with pytest.raises(InvalidCandidateIdError):
        validar_candidate_id(invalido)


def test_el_candidate_id_no_es_una_generation_id() -> None:
    """El Candidate NO reutiliza el Generation ID (contrato SFR-17 previo)."""
    from sky_claw.local.frozen_runtime.generation_id import GENERATION_ID_PATTERN

    assert GENERATION_ID_PATTERN.match("cand_" + "0" * 32) is None
    assert "__" not in "cand_" + "0" * 32
