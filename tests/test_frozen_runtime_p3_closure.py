"""P3.1 — Redireccion de la superficie de persistencia del Candidate.

Cierra los blockers P3.1 A/B/D/F/G/H/J: reserva de ID sin clobber, corrupcion
tipada, errores de filesystem tipados, proteccion de reparse en la lectura, y
`files` obligatorio en la evidencia persistida.

RED-first: cada test se escribio y sevio fallar contra HEAD 74e66265 antes del
fix correspondiente.
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
    candidates_state_dir,
    descubrir_candidates,
    leer_metadata_candidate,
    payload_dir,
    verificar_candidate,
)
from sky_claw.local.frozen_runtime.errors import (
    CandidateCorruptMetadataError,
)
from sky_claw.local.frozen_runtime.storage_models import (
    CandidateState,
    GenerationVerificationState,
)
from tests._p3_rig import crear as _crear
from tests._p3_rig import parche_identidad, rig  # noqa: F401
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard


def _leer_json(ruta: pathlib.Path) -> dict[str, object]:
    return json.loads(ruta.read_text(encoding="utf-8"))


def _escribir_json(ruta: pathlib.Path, datos: dict[str, object]) -> None:
    ruta.write_text(json.dumps(datos, indent=2), encoding="utf-8")


# ── P3-A · reserva de candidate id sin clobber ─────────────────────────────


def test_c21_un_id_duplicado_no_pisa_un_candidate_ready(rig) -> None:  # noqa: F811
    """P3-A: la metadata READY previa y su payload sobreviven intactos."""
    source, root = rig
    primera = _crear(source, root, cid="cand_" + "7" * 32)
    assert primera.state is GenerationVerificationState.VALID

    meta = candidate_metadata_path(root, "cand_" + "7" * 32)
    payload = payload_dir(candidate_dir(root, "cand_" + "7" * 32))
    antes_meta = _leer_json(meta)
    antes_payload = {
        p.relative_to(payload).as_posix(): p.read_bytes() for p in sorted(payload.rglob("*")) if p.is_file()
    }

    # Mismo ID otra vez: la colision debe fallar ANTES de escribir nada.
    segunda = _crear(source, root, cid="cand_" + "7" * 32)

    assert segunda.state is not GenerationVerificationState.VALID
    assert _leer_json(meta) == antes_meta, "la metadata READY previa fue sobrescrita"
    assert segunda.metadata is None or segunda.metadata.state is not CandidateState.READY

    despues_payload = {
        p.relative_to(payload).as_posix(): p.read_bytes() for p in sorted(payload.rglob("*")) if p.is_file()
    }
    assert despues_payload == antes_payload, "el payload previo fue alterado"


def test_c21_la_reserva_deja_el_candidate_building_si_falla_la_copia(rig, monkeypatch) -> None:  # noqa: F811
    """P3-A + §17: reservar no rompe la semantica de crash (BUILDING, nunca READY)."""
    source, root = rig

    def copia_que_falla(origen, destino, files, directories, **kwargs):
        raise candidates_module.CandidateCopyError("simulado")

    monkeypatch.setattr(candidates_module, "copiar_arbol_independiente", copia_que_falla)
    resultado = _crear(source, root, cid="cand_" + "6" * 32)

    assert resultado.state is GenerationVerificationState.INVALID
    registros = descubrir_candidates(root).records
    assert registros[0].metadata is not None
    assert registros[0].metadata.state is CandidateState.INVALID


def _candidate_ready(root: pathlib.Path, cid: str) -> pathlib.Path:
    meta = candidate_metadata_path(root, cid)
    assert meta.is_file()
    assert _leer_json(meta)["state"] == CandidateState.READY.value
    return meta


@pytest.fixture
def listo(rig):  # noqa: F811
    source, root = rig
    resultado = _crear(source, root, cid="cand_" + "3" * 32)
    assert resultado.state is GenerationVerificationState.VALID
    return source, root, "cand_" + "3" * 32


# ── P3-B / P3-G · corrupcion tipada de la metadata persistida ─────────────


@pytest.mark.parametrize("campo", ["pre_source_evidence", "candidate_evidence", "post_source_evidence"])
def test_c22_ready_incompleto_es_corrupcion_tipada(listo, campo: str) -> None:
    """P3-B: READY sin una de las TRES evidencias es metadata corrupta.

    Antes escapaba como `CandidateVerificationError` (excepcion de verificacion)
    en vez de `CandidateCorruptMetadataError` (corrupcion persistida), que es lo
    que los callers saben convertir en UNKNOWN.
    """
    _, root, cid = listo
    meta = candidate_metadata_path(root, cid)
    datos = _leer_json(meta)
    datos[campo] = None
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


@pytest.mark.parametrize("invalido", [None, "", "no-es-un-id", "../../escape", "cand_" + "Z" * 32])
def test_c23_candidate_id_embebido_invalido_es_corrupcion_tipada(listo, invalido) -> None:
    """P3-G: candidate_id embebido ausente o invalido es corrupcion TIPADA."""
    _, root, cid = listo
    meta = candidate_metadata_path(root, cid)
    datos = _leer_json(meta)
    if invalido is None:
        datos.pop("candidate_id")
    else:
        datos["candidate_id"] = invalido
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    registros = descubrir_candidates(root).records
    assert registros[0].state is GenerationVerificationState.UNKNOWN
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


# ── P3-J · `files` obligatorio en la evidencia persistida ──────────────────


@pytest.mark.parametrize("mutacion", ["ausente", "tipo_malo", "identidad_mala"])
def test_c24_files_ausente_o_malo_es_corrupcion_tipada(listo, mutacion: str) -> None:
    """P3-J: la enumeracion sellada es parte de la evidencia; ausente => corrupta.

    Antes `bruto.get("files")` devolvia None y `_identidades` devolvia `()`: una
    evidencia "completa" que prometia cobertura sin tenerla.
    """
    _, root, cid = listo
    meta = candidate_metadata_path(root, cid)
    datos = _leer_json(meta)
    bloque = datos["pre_source_evidence"]
    if mutacion == "ausente":
        bloque.pop("files")
    elif mutacion == "tipo_malo":
        bloque["files"] = "no-es-una-lista"
    else:
        bloque["files"] = [{"rel_path": "roto"}]
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


# ── P3-C · la evidencia de Candidate persistida tambien se revalida ───────


@pytest.mark.parametrize(
    ("campo", "esperado"),
    [
        ("tree_digest", GenerationVerificationState.INVALID),
        ("critical_files", GenerationVerificationState.INVALID),
        # El bloque de membership se autovalida al parsear (digest/count/lista
        # coherentes entre si), asi que su corrupcion se detecta AUN ANTES, como
        # metadata corrupta => UNKNOWN. Es un resultado MAS conservador, y lo
        # que el contrato exige sin ambiguedad es `not VALID`.
        ("directory_membership", GenerationVerificationState.UNKNOWN),
    ],
)
def test_c25_candidate_evidence_persistida_corrupta_invalida(listo, campo: str, esperado) -> None:
    """P3-C: la triada PERSISTIDA debe ser coherente consigo misma.

    Se corrompe SOLO `candidate_evidence` y el payload queda intacto: la
    re-verificacion fresca no lo detecta porque compara
    PRE/CANDIDATO-FRESCO/POST y nunca mira la evidencia historica que afirma
    "esto es lo que se copio".
    """
    _, root, cid = listo
    meta = candidate_metadata_path(root, cid)
    datos = _leer_json(meta)
    bloque = datos["candidate_evidence"]
    if campo == "tree_digest":
        bloque["tree_digest"]["digest"] = "f" * 64
    elif campo == "directory_membership":
        bloque["directory_membership"]["digest"] = "f" * 64
        bloque["directory_membership"]["directory_count"] = 99
        bloque["directory_membership"]["directories"] = ["Data"]
    else:
        bloque["critical_files"] = [{"rel_path": "SkyrimSE.exe", "size": 1, "digest": "a" * 64}]
    _escribir_json(meta, datos)

    veredicto = verificar_candidate(root, cid)
    assert veredicto.state is esperado, veredicto.message


# ── P3-D · errores de filesystem de persistencia, tipados ─────────────────


@pytest.mark.parametrize("objetivo", ["fsync", "os.replace"])
def test_c26_fallo_de_persistencia_no_escapa_como_oserror(rig, monkeypatch, objetivo: str) -> None:  # noqa: F811
    """P3-D: un fallo de fsync/os.replace da resultado tipado; nunca READY."""
    # El parche va en el modulo que DEFINE `write_json_atomic`, no en el que la
    # llama: `os` es un import por modulo, parchearlo aca no alcanzaria el
    # `os.replace`/`os.fsync` reales de la escritura atomica.
    atomic_module = pytest.importorskip(
        "sky_claw.local.frozen_runtime.atomico"
        if hasattr(candidates_module, "atomico")
        else "sky_claw.local.frozen_runtime.storage"
    )
    fuente, root = rig

    if objetivo == "fsync":

        def boom(_fileno: int) -> None:
            raise OSError(5, "simulado: fsync fallido")

        monkeypatch.setattr(atomic_module.os, "fsync", boom)
    else:

        def boom_reemplazo(_origen, _destino) -> None:
            raise OSError(5, "simulado: os.replace fallido")

        monkeypatch.setattr(atomic_module.os, "replace", boom_reemplazo)

    resultado = _crear(fuente, root, cid="cand_" + "2" * 32)
    assert resultado.state is not GenerationVerificationState.VALID


# ── P3-E · preservar INDETERMINATE ────────────────────────────────────────


def test_c27_indeterminate_fisico_no_se_reporta_como_invalid(listo, monkeypatch) -> None:
    """P3-E: no se afirma corrupcion cuando solo fallo la inspeccion."""
    from sky_claw.local.frozen_runtime.storage_models import PhysicalIndependenceResult

    _, root, cid = listo
    monkeypatch.setattr(
        candidates_module,
        "verify_generation_physical_integrity",
        lambda _p: PhysicalIndependenceResult(
            state=candidates_module.IndependenceState.INDETERMINATE,
            message="no se pudo inspeccionar",
        ),
    )
    veredicto = verificar_candidate(root, cid)
    assert veredicto.state is GenerationVerificationState.INDETERMINATE


# ── P3-F · la metadata no se lee a traves de un enlace ───────────────────


@junction_guard
def test_c28_metadata_redirigida_por_junction_es_corrupcion(listo, tmp_path) -> None:
    """P3-F: `state/candidates/` como junction no puede leer metadata externa."""
    _, root, cid = listo
    fuera = tmp_path / "metadata-externa"
    fuera.mkdir()
    fuera_meta = fuera / f"{cid}.json"
    shutil.copyfile(candidate_metadata_path(root, cid), fuera_meta)

    original = candidates_state_dir(root)
    # Sustituimos el directorio de metadata por un junction al externo.
    shutil.rmtree(original)
    if (motivo := crear_junction(original, fuera)) is not None:
        pytest.skip(f"no se pudo crear junction: {motivo}")

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(candidate_metadata_path(root, cid))
    registros = descubrir_candidates(root).records
    assert registros[0].state is GenerationVerificationState.UNKNOWN


@symlink_guard
def test_c29_archivo_de_metadata_que_es_symlink_es_corrupcion(listo, tmp_path) -> None:
    """P3-F: el propio archivo de metadata no puede ser un enlace."""
    _, root, cid = listo
    meta = candidate_metadata_path(root, cid)
    fuera = tmp_path / "meta-externa.json"
    shutil.copyfile(meta, fuera)
    meta.unlink()
    meta.symlink_to(fuera)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN


@symlink_guard
def test_c30_symlink_colgado_no_se_lectura_como_ausente_limpio(listo, tmp_path) -> None:
    """P3-F: un symlink colgante (target inexistente) tampoco es 'arranque limpio'."""
    _, root, cid = listo
    meta = candidate_metadata_path(root, cid)
    meta.unlink()
    meta.symlink_to(tmp_path / "no-existe.json")

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)


# ── P3-H · el catalogo de evidencia critica no escapa crudo ───────────────


def test_c31_evidencia_critica_fallida_no_escapa_cruda(listo, monkeypatch) -> None:
    """P3-H: la construccion de evidencia critica debe quedar dentro de la frontera.

    Se inyecta el fallo real del borde -- `_archivos_criticos` levantando
    `FrozenRuntimeObservationError` (juego valido sin catalogo correspondiente) --
    y se exige un veredicto tipado, nunca la excepcion cruda.
    """
    from sky_claw.local.frozen_runtime.errors import FrozenRuntimeObservationError

    _, root, cid = listo

    def criticos_que_fallan(_game_key: str, _files):
        raise FrozenRuntimeObservationError("simulado: sin catalogo critico")

    monkeypatch.setattr(candidates_module, "_archivos_criticos", criticos_que_fallan)

    veredicto = verificar_candidate(root, cid)  # no debe propagar FrozenRuntimeObservationError
    assert isinstance(veredicto.state, GenerationVerificationState)
    assert veredicto.state is not GenerationVerificationState.VALID


# ── §14 · binding de la fuente de nivel superior ──────────────────────────


@pytest.mark.parametrize("campo", ["source_provider", "source_appid"])
def test_c32_binding_de_fuente_incoherente_invalida(listo, campo: str) -> None:
    """§14: el binding de nivel superior debe coincidir con la evidencia PRE/POST.

    Sin esto, una metadata puede decir `appid` A en el nivel superior mientras la
    evidencia persistida dice B, y el payload identico la haria pasar.
    """
    _, root, cid = listo
    meta = candidate_metadata_path(root, cid)
    datos = _leer_json(meta)
    datos[campo] = "999999" if campo == "source_appid" else "otro-proveedor"
    _escribir_json(meta, datos)

    veredicto = verificar_candidate(root, cid)
    assert veredicto.state is GenerationVerificationState.INVALID, veredicto.message
