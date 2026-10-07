"""Hardening POST-MERGE de Frozen Runtime P3 — frontera de metadata (findings de PR #682).

Dos findings levantados por los reviewers sobre el HEAD mergeado de #682, mas el
hermano que cada uno deja al descubierto. Se adjudica cada uno por separado; los
CONFIRMADOS traen su test RED, escrito y visto fallar contra `97dcc7ab` (merge de
#682 + #696) antes del fix.

F2 · `candidates.py::_evidencia_desde_dict` exigia `isinstance(int)` y excluia
     `bool`, pero NO exigia `>= 0`: un `observed_at_ns: -1` persistido se
     reconstruia como evidencia VALIDA. El hermano `_entero_json_no_negativo`
     (usado por `created_at_ns`/`updated_at_ns`) si lo exige.

F4 · `candidates.py::leer_metadata_candidate` promete UNA sola frontera de
     corrupcion ("incluidos los ids invalidos"), y traduce el `candidate_id`
     EMBEBIDO (`_candidate_id_desde_datos`), pero el id derivado del NOMBRE del
     archivo (`ruta.stem`) escapaba crudo como `InvalidCandidateIdError`. Los
     callers (`verificar_candidate`, `descubrir_candidates`) solo capturan
     `CandidateCorruptMetadataError`.

RED-first: estos tests se escribieron y se vieron FALLAR contra `97dcc7ab`
(`P3_POSTMERGE_PRE_FIX_RED`).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from sky_claw.local.frozen_runtime import candidates as candidates_module
from sky_claw.local.frozen_runtime.candidates import (
    candidate_metadata_path,
    candidates_state_dir,
    leer_metadata_candidate,
)
from sky_claw.local.frozen_runtime.errors import CandidateCorruptMetadataError
from sky_claw.local.frozen_runtime.storage_models import GenerationVerificationState
from tests._p3_rig import crear as _crear
from tests._p3_rig import parche_identidad, rig  # noqa: F401

# ===========================================================================
# F2 · `observed_at_ns` negativo en evidencia PERSISTIDA
# ===========================================================================


def _meta_ready(rig) -> pathlib.Path:  # noqa: ANN001, F811
    """Candidate READY real, con sus tres evidencias persistidas."""
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID
    assert resultado.candidate_id
    return candidate_metadata_path(root, resultado.candidate_id)


def _leer(ruta: pathlib.Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def _escribir(ruta: pathlib.Path, datos: dict) -> None:
    ruta.write_text(json.dumps(datos, indent=2), encoding="utf-8")


def test_f2_observed_at_ns_negativo_en_metadata_persistida_es_corrupcion(rig) -> None:  # noqa: F811
    """`-1` no puede reconstruirse como evidencia valida: es corrupcion tipada."""
    meta = _meta_ready(rig)
    datos = _leer(meta)
    datos["pre_source_evidence"]["observed_at_ns"] = -1
    _escribir(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError, match="observed_at_ns"):
        leer_metadata_candidate(meta)


def test_f2_observed_at_ns_no_negativo_se_acepta(rig) -> None:  # noqa: F811
    """El hermano positivo: `0` y un entero positivo siguen siendo validos."""
    meta = _meta_ready(rig)
    for valor in (0, 1_700_000_000_000_000_000):
        datos = _leer(meta)
        datos["pre_source_evidence"]["observed_at_ns"] = valor
        _escribir(meta, datos)

        releida = leer_metadata_candidate(meta)
        assert releida.pre_source_evidence is not None
        assert releida.pre_source_evidence.observed_at_ns == valor


def test_f2_el_contrato_de_tipos_no_se_relaja(rig) -> None:  # noqa: F811
    """Los controles que YA rechazaba siguen rechazados, y con la MISMA familia.

    `bool` es subclase de `int`; `1.5` y `"123"` no son enteros JSON. Ninguno
    puede colarse por el camino nuevo (que reusa `_entero_json_no_negativo`).
    """
    meta = _meta_ready(rig)
    for valor in (True, False, 1.5, "123", None, [1]):
        datos = _leer(meta)
        datos["pre_source_evidence"]["observed_at_ns"] = valor
        _escribir(meta, datos)

        with pytest.raises(CandidateCorruptMetadataError):
            leer_metadata_candidate(meta)


def test_f2_la_evidencia_corrupta_no_se_normaliza(rig) -> None:  # noqa: F811
    """Nada de `abs()`/`max(0)`/`int()`: el valor negativo no reaparece corregido."""
    meta = _meta_ready(rig)
    datos = _leer(meta)
    datos["pre_source_evidence"]["observed_at_ns"] = -999999
    _escribir(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)

    # El archivo sigue tal cual: el loader no reescribio la evidencia.
    assert _leer(meta)["pre_source_evidence"]["observed_at_ns"] == -999999


# ===========================================================================
# F4 · id invalido derivado del NOMBRE del archivo
# ===========================================================================


def _metadata_valida(candidate_id: str) -> dict:
    """Metadata schema-v1 completa y coherente (BUILDING, sin evidencias)."""
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "state": "building",
        "created_at_ns": 1,
        "updated_at_ns": 2,
        "source_provider": "steam",
        "source_appid": "489830",
        "pre_source_evidence": None,
        "candidate_evidence": None,
        "post_source_evidence": None,
        "failure_reason": None,
    }


def test_f4_nombre_de_archivo_invalido_no_escapa_como_id_invalido(tmp_path: pathlib.Path) -> None:
    """Un `cand_bad.json` es metadata corrupta, no un `InvalidCandidateIdError`.

    El contrato publico del loader promete UNA sola excepcion de corrupcion para
    que el caller la convierta en UNKNOWN. Un id invalido derivado del NOMBRE es
    la misma clase de hecho que uno derivado del CONTENIDO, que ya se traduce.
    """
    meta = tmp_path / "cand_bad.json"
    meta.write_text(json.dumps(_metadata_valida("cand_" + "0" * 32)), encoding="utf-8")

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)


def test_f4_el_hermano_del_contenido_sigue_traduciendo(tmp_path: pathlib.Path) -> None:
    """El id EMBEBIDO invalido ya se traducia; el fix no puede debilitar ese lado."""
    meta = tmp_path / ("cand_" + "1" * 32 + ".json")
    meta.write_text(json.dumps(_metadata_valida("cand_no-es-hex")), encoding="utf-8")

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)


def test_f4_un_nombre_que_no_pretende_ser_candidate_id_no_se_valida(tmp_path: pathlib.Path) -> None:
    """La frontera es angosta: solo los stems que AFIRMAN ser `cand_*` se validan.

    Un `notes.json` suelto no es una identidad de Candidate, asi que el loader no
    inventa una validacion de id: lo registra `descubrir_candidates` como UNKNOWN.
    El fix de F4 no puede ensanchar esa frontera y rechazar nombres ajenos.
    """
    meta = tmp_path / "notes.json"
    meta.write_text(json.dumps(_metadata_valida("cand_" + "2" * 32)), encoding="utf-8")

    assert leer_metadata_candidate(meta).candidate_id == "cand_" + "2" * 32


def test_f4_el_directorio_de_metadata_es_el_del_schema(tmp_path: pathlib.Path) -> None:
    """Ancla de ruta: el namespace auditado es `state/candidates/`, no el payload."""
    assert candidates_state_dir(tmp_path) == tmp_path / "state" / "candidates"


def test_f4_la_metadata_sigue_siendo_la_unica_frontera_publica() -> None:
    """El loader no captura `Exception`: la familia de corrupcion es explicita.

    Si el fix hubiera usado `except Exception`, un fallo de IO se disfrazaria de
    metadata corrupta. Se congela la familia declarada en el modulo.
    """
    fuente = pathlib.Path(candidates_module.__file__ or "").read_text(encoding="utf-8")
    inicio = fuente.index("def leer_metadata_candidate")
    fin = fuente.index("\ndef ", inicio + 1)
    cuerpo = fuente[inicio:fin]
    assert "except Exception" not in cuerpo
    assert "except InvalidCandidateIdError as exc:" in cuerpo
