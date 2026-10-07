"""P3 ronda 5 — P3-U (colision por metadata) y P3-V (coercion de tipos).

Findings de Codex sobre `77bb1476`:

* P3-U (`candidates.py:865`): `_reservar_candidate_id` solo reserva
  `candidates/<id>/` con `mkdir(exist_ok=False)`. La identidad de un Candidate
  tiene DOS representaciones persistentes, y el id esta ocupado si existe
  CUALQUIERA. Con el directorio borrado pero `state/candidates/<id>.json`
  presente, la reserva tenia exito y la escritura inicial REEMPLAZABA la
  evidencia historica READY con un BUILDING.
* P3-V (`candidates.py:374`): `str(valor)` como validacion convierte metadata
  corrupta en un string artificial (`null -> "None"`, `{} -> "{}"`, `123 ->
  "123"`), asi que el registro se reconstruye como valido.

RED-first: cada test se escribio y se vio fallar contra `77bb1476`.
"""

from __future__ import annotations

import json
import pathlib
import shutil

import pytest

from sky_claw.local.frozen_runtime import candidates as candidates_module
from sky_claw.local.frozen_runtime.candidates import (
    candidate_dir,
    candidate_metadata_path,
    candidates_dir,
    descubrir_candidates,
    leer_metadata_candidate,
    verificar_candidate,
)
from sky_claw.local.frozen_runtime.errors import CandidateCorruptMetadataError
from sky_claw.local.frozen_runtime.storage_models import CandidateState, GenerationVerificationState
from tests._p3_rig import crear as _crear
from tests._p3_rig import parche_identidad, rig  # noqa: F401


def _ready_y_meta(rig):  # noqa: ANN001, ANN202, F811
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID
    return root, resultado.candidate_id, candidate_metadata_path(root, resultado.candidate_id or "")


def _leer_json(ruta: pathlib.Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def _escribir_json(ruta: pathlib.Path, datos: dict) -> None:
    ruta.write_text(json.dumps(datos, indent=2), encoding="utf-8")


def _fijar(datos: dict, camino: tuple, valor: object) -> None:
    puntero = datos
    for parte in camino[:-1]:
        puntero = puntero[parte]
    puntero[camino[-1]] = valor


# ── P3-U · un id ocupado por METADATA no se puede reutilizar ───────────────


def test_p3u_metadata_sin_directorio_bloquea_la_reutilizacion(rig) -> None:  # noqa: F811
    """Borrar `candidates/<id>/` no libera el id: el JSON historico sigue siendo evidencia."""
    source, root = rig
    cid = "cand_" + "5" * 32
    assert _crear(source, root, cid=cid).state is GenerationVerificationState.VALID

    meta = candidate_metadata_path(root, cid)
    bytes_antes = meta.read_bytes()
    assert leer_metadata_candidate(meta).state is CandidateState.READY

    # El ARBOL del Candidate desaparece; la evidencia historica queda.
    shutil.rmtree(candidate_dir(root, cid))

    reintento = _crear(source, root, cid=cid)

    assert reintento.state is not GenerationVerificationState.VALID
    assert meta.read_bytes() == bytes_antes, "la evidencia historica fue REPLACE-ada por un BUILDING"
    assert leer_metadata_candidate(meta).state is CandidateState.READY, "el estado historico se perdio"


def test_p3u_directorio_sin_metadata_bloquea_la_reutilizacion(rig) -> None:  # noqa: F811
    """El hermano: una reserva sin metadata tampoco libera el id."""
    source, root = rig
    cid = "cand_" + "6" * 32
    (candidates_dir(root) / cid).mkdir(parents=True)

    resultado = _crear(source, root, cid=cid)

    assert resultado.state is not GenerationVerificationState.VALID
    assert not candidate_metadata_path(root, cid).exists(), "se escribio metadata sobre una reserva ajena"


def test_p3u_la_carrera_no_pisa_la_metadata_existente(rig, monkeypatch) -> None:  # noqa: F811
    """Sin el chequeo previo, la escritura EXCLUSIVA inicial es la ultima barrera.

    Se monkeypatchea el probe previo para simular "el id parecia libre y la
    metadata aparecio despues": si la primera escritura usara el camino normal
    (`os.replace`), la evidencia historica se perderia.
    """
    source, root = rig
    cid = "cand_" + "7" * 32
    assert _crear(source, root, cid=cid).state is GenerationVerificationState.VALID

    meta = candidate_metadata_path(root, cid)
    bytes_antes = meta.read_bytes()
    shutil.rmtree(candidate_dir(root, cid))

    monkeypatch.setattr(candidates_module, "_exigir_metadata_libre", lambda *a, **k: None)
    resultado = _crear(source, root, cid=cid)

    assert resultado.state is not GenerationVerificationState.VALID
    assert meta.read_bytes() == bytes_antes, "la carrera reemplazo la evidencia historica"


# ── P3-V · coercion de tipos en la metadata persistida ────────────────────


@pytest.mark.parametrize(
    ("camino", "valor"),
    [
        (("pre_source_evidence", "files", 0, "rel_path"), None),
        (("pre_source_evidence", "files", 0, "rel_path"), {}),
        (("pre_source_evidence", "files", 0, "rel_path"), 123),
        (("pre_source_evidence", "files", 0, "digest"), None),
        (("pre_source_evidence", "files", 0, "digest"), []),
        (("pre_source_evidence", "files", 0, "digest"), 123),
        (("pre_source_evidence", "appid"), 123),
        (("pre_source_evidence", "appid"), {}),
        (("pre_source_evidence", "game_key"), {}),
        (("pre_source_evidence", "runtime_identity", "game_key"), 123),
        (("pre_source_evidence", "runtime_identity", "game_version"), None),
        (("pre_source_evidence", "tree_digest", "digest"), 123),
        (("source_provider",), None),
        (("source_provider",), {}),
        (("source_provider",), 5),
        (("source_appid",), None),
        (("source_appid",), []),
        (("pre_source_evidence", "provider_metadata", "manifest_readable"), "false"),
        (("pre_source_evidence", "provider_metadata", "manifest_readable"), 1),
        (("pre_source_evidence", "provider_metadata", "manifest_readable"), {}),
    ],
)
def test_p3v_tipo_coercionado_es_corrupcion(rig, camino, valor) -> None:  # noqa: F811
    """`str(...)`/`bool(...)` no pueden fabricar un valor valido desde JSON corrupto."""
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    _fijar(datos, camino, valor)
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN
    assert descubrir_candidates(root).records[0].state is GenerationVerificationState.UNKNOWN


@pytest.mark.parametrize("valor", [True, False])
def test_p3v_manifest_readable_bool_real_se_acepta(rig, valor) -> None:  # noqa: F811
    """El rechazo no puede llevarse puestos los booleanos JSON legitimos."""
    _root, _cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    _fijar(datos, ("pre_source_evidence", "provider_metadata", "manifest_readable"), valor)
    _escribir_json(meta, datos)

    evidencia = leer_metadata_candidate(meta).pre_source_evidence
    assert evidencia is not None
    assert evidencia.provider_metadata.manifest_readable is valor


def test_p3v_strings_reales_siguen_parseando(rig) -> None:  # noqa: F811
    """El endurecimiento no puede romper la deserializacion legitima."""
    root, cid, meta = _ready_y_meta(rig)
    evidencia = leer_metadata_candidate(meta).pre_source_evidence
    assert evidencia is not None
    assert isinstance(evidencia.appid, str) and evidencia.appid
    assert isinstance(evidencia.game_key, str) and evidencia.game_key
    assert isinstance(evidencia.tree_digest.digest, str)
    assert isinstance(evidencia.runtime_identity.game_key, str)
    assert isinstance(evidencia.files[0].rel_path, str)
    assert isinstance(evidencia.files[0].digest, str)
    assert isinstance(leer_metadata_candidate(meta).source_provider, str)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.VALID
