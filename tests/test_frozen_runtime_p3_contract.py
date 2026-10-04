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
    serializar_metadata_candidate,
)
from sky_claw.local.frozen_runtime.errors import (
    CandidateCorruptMetadataError,
    CandidateVerificationError,
    InvalidCandidateIdError,
)


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
    """READY sin evidencia de Candidate NO puede persistirse (SFR-15).

    El guard vive en ``exigir_listo_para_persistencia``, que es lo que invocan
    serializar y leer. Por eso el test tiene que LLAMARLO: si solo afirmara que
    el estado es READY, pasaria igual con el guard eliminado.
    """
    evidencia = _evidencia_minima()
    sin_candidato = CandidateMetadata(
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
    with pytest.raises(CandidateVerificationError, match="candidate_evidence"):
        sin_candidato.exigir_listo_para_persistencia()
    with pytest.raises(CandidateVerificationError):
        serializar_metadata_candidate(sin_candidato)

    completo = CandidateMetadata(
        schema_version=1,
        candidate_id="cand_" + "0" * 32,
        state=CandidateState.READY,
        created_at_ns=1,
        updated_at_ns=2,
        source_provider="steam",
        source_appid="489830",
        pre_source_evidence=evidencia,
        candidate_evidence=evidencia,
        post_source_evidence=evidencia,
        failure_reason=None,
    )
    completo.exigir_listo_para_persistencia()
    assert "pre_source_evidence" in serializar_metadata_candidate(completo)


def _evidencia_minima() -> CandidateSourceEvidence:
    """Evidencia de fuente minima pero bien formada para los tests de contrato."""
    from sky_claw.local.frozen_runtime.membership import construir_evidencia_membership
    from sky_claw.local.frozen_runtime.models import ManagedSourceProvider, ProviderMetadataObservation
    from sky_claw.local.runtime_vault.models import FileIdentity, RuntimeIdentity, TreeDigest

    critico = (FileIdentity(rel_path="SkyrimSE.exe", size=1, digest="a" * 64),)
    return CandidateSourceEvidence(
        provider="steam",
        appid="489830",
        game_key="skyrimse",
        runtime_identity=RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
        tree_digest=TreeDigest(digest="b" * 64, files=1, bytes=1),
        directory_membership=construir_evidencia_membership(("Data",)),
        critical_files=critico,
        provider_metadata=ProviderMetadataObservation(provider=ManagedSourceProvider.STEAM, appid="489830"),
        observed_at_ns=1,
        files=critico,
    )


def test_la_evidencia_persistida_conserva_la_enumeracion_sellada() -> None:
    """La enumeracion sellada PRE sobrevive al round-trip (CodeRabbit #1).

    Sin esto, tras un restart ``archivos`` volveria vacio y la evidencia
    persistida prometeria una cobertura que ya no tiene.
    """
    from sky_claw.local.frozen_runtime.candidates import _evidencia_desde_dict

    original = _evidencia_minima()
    recargada = _evidencia_desde_dict(
        serializar_metadata_candidate(
            CandidateMetadata(
                schema_version=1,
                candidate_id="cand_" + "0" * 32,
                state=CandidateState.BUILDING,
                created_at_ns=1,
                updated_at_ns=1,
                source_provider="steam",
                source_appid="489830",
                pre_source_evidence=original,
                candidate_evidence=None,
                post_source_evidence=None,
            )
        )["pre_source_evidence"],
        etiqueta="test",
    )

    assert recargada.archivos == original.archivos
    assert recargada.critical_files == original.critical_files
    assert recargada.directory_membership == original.directory_membership


def test_una_entrada_malformada_en_la_evidencia_falla_cerrado() -> None:
    """Una identidad de archivo corrupta LANZA: no se filtra en silencio."""
    from sky_claw.local.frozen_runtime.candidates import _evidencia_desde_dict

    payload = dict(
        serializar_metadata_candidate(
            CandidateMetadata(
                schema_version=1,
                candidate_id="cand_" + "0" * 32,
                state=CandidateState.BUILDING,
                created_at_ns=1,
                updated_at_ns=1,
                source_provider="steam",
                source_appid="489830",
                pre_source_evidence=_evidencia_minima(),
                candidate_evidence=None,
                post_source_evidence=None,
            )
        )["pre_source_evidence"]
    )
    payload["critical_files"] = [{"rel_path": "roto"}]

    with pytest.raises(CandidateCorruptMetadataError):
        _evidencia_desde_dict(payload, etiqueta="test")


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
