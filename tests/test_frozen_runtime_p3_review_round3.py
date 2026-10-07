"""P3 ronda 3 — findings válidos sobre el HEAD `d742ab9c` (Codex).

Cada test se escribió y se vio fallar (RED) contra `d742ab9c` antes del fix.
Cierra: P3-P (padres destino anidados), P3-Q (tipado de la inspección del
payload), P3-R (ancestros redirigidos del source) y P3-S (coerción numérica
lossy en la evidencia persistida).

Garantía declarada: `HANDLE_GRADE_*_TRAVERSAL = NO` — no hay handles relativos
a directorio, así que la revalidación es best-effort fail-closed. P4 (lock
cross-process) sigue abierto.
"""

from __future__ import annotations

import json
import pathlib
import shutil

import pytest

from sky_claw.local.frozen_runtime import candidates as candidates_module
from sky_claw.local.frozen_runtime import copying as copying_module
from sky_claw.local.frozen_runtime.candidates import (
    candidate_dir,
    candidate_metadata_path,
    descubrir_candidates,
    leer_metadata_candidate,
    payload_dir,
    verificar_candidate,
)
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.errors import CandidateCopyError, CandidateCorruptMetadataError
from sky_claw.local.frozen_runtime.storage_models import GenerationVerificationState
from sky_claw.local.runtime_vault.models import FileIdentity
from tests._p3_rig import crear as _crear
from tests._p3_rig import parche_identidad, rig  # noqa: F401
from tests._symlink_guard import crear_junction, junction_guard


def _crear_ready(rig):  # noqa: ANN001, ANN202, F811
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID
    return source, root, resultado.candidate_id


def _armar_contenedor(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    contenedor = tmp_path / "frozen"
    (contenedor / "candidates" / "cand_x").mkdir(parents=True)
    return contenedor, contenedor / "candidates" / "cand_x" / "payload"


def _leer_json(ruta: pathlib.Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def _escribir_json(ruta: pathlib.Path, datos: dict) -> None:
    ruta.write_text(json.dumps(datos, indent=2), encoding="utf-8")


# ── P3-P · los padres DESTINO anidados se revalidan antes de escribir ──────


@junction_guard
def test_p3p_swap_de_padre_nido_entre_archivos_no_escribe_fuera(tmp_path, monkeypatch) -> None:
    """Un junction en `payload/Data` no puede desviar la escritura de un archivo.

    La contencion se verificaba UNA vez en la raiz del payload. Si `payload/Data`
    se reemplaza por un junction DESPUES de ese chequeo y ANTES de que el loop
    llegue al siguiente archivo, `mkdir(exist_ok=True)` acepta el directorio
    redirigido y el `open(..., 'xb')` escribe en el destino externo.
    """
    raiz_origen = tmp_path / "source"
    (raiz_origen / "Data").mkdir(parents=True)
    (raiz_origen / "Data" / "a.bin").write_bytes(b"AAA")
    (raiz_origen / "Data" / "b.bin").write_bytes(b"BBB")

    contenedor, destino = _armar_contenedor(tmp_path)
    externo = tmp_path / "externo"
    externo.mkdir()

    archivos = (
        FileIdentity(rel_path="Data/a.bin", size=3, digest="a" * 64),
        FileIdentity(rel_path="Data/b.bin", size=3, digest="b" * 64),
    )
    original = copying_module.copiar_archivo
    estado = {"cambiado": False}

    def copiar_con_swap(origen, dest):  # noqa: ANN001, ANN202
        resultado = original(origen, dest)
        if not estado["cambiado"]:
            estado["cambiado"] = True
            # El directorio nido era LEGITIMO; ahora lo reemplaza un junction.
            shutil.rmtree(pathlib.Path(dest).parent)
            if (motivo := crear_junction(pathlib.Path(dest).parent, externo)) is not None:
                pytest.skip(f"no se pudo crear junction: {motivo}")
        return resultado

    monkeypatch.setattr(copying_module, "copiar_archivo", copiar_con_swap)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(raiz_origen, destino, archivos, ("Data",), contenedor=contenedor)

    assert list(externo.iterdir()) == [], "se escribio contenido FUERA del FrozenRuntimeRoot"


# ── P3-Q · la inspección del payload no filtra fallos ni afirma pérdida ────


def test_p3q_inspeccion_imposible_es_indeterminate(rig, monkeypatch) -> None:  # noqa: F811
    """Un fallo transitorio de inspección no es una afirmación de pérdida."""
    _source, root, cid = _crear_ready(rig)

    def probe_que_falla(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError("simulado: ACL/sharing/volumen desconectado")

    monkeypatch.setattr(candidates_module, "link_kind_and_identity_or_raise", probe_que_falla)

    veredicto = verificar_candidate(root, cid)
    assert veredicto.state is GenerationVerificationState.INDETERMINATE, veredicto.message


def test_p3q_payload_ausente_sigue_siendo_invalid(rig) -> None:  # noqa: F811
    """La ausencia DEFINIDA sigue siendo INVALID: no se degrada a ambiguo."""
    _source, root, cid = _crear_ready(rig)
    shutil.rmtree(payload_dir(candidate_dir(root, cid)))

    assert verificar_candidate(root, cid).state is GenerationVerificationState.INVALID


def test_p3q_payload_que_es_archivo_regular_es_invalid(rig) -> None:  # noqa: F811
    """Un payload que existe pero no es directorio no puede pasar el preflight."""
    _source, root, cid = _crear_ready(rig)
    payload = payload_dir(candidate_dir(root, cid))
    shutil.rmtree(payload)
    payload.write_bytes(b"no-soy-un-arbol")

    assert verificar_candidate(root, cid).state is GenerationVerificationState.INVALID


def test_p3q_indeterminate_fisico_se_sigue_preservando(rig, monkeypatch) -> None:  # noqa: F811
    """No se regresa el fix de P3-E: el veredicto fisico ambiguo sigue ambiguo."""
    from sky_claw.local.frozen_runtime.storage_models import PhysicalIndependenceResult

    _source, root, cid = _crear_ready(rig)
    monkeypatch.setattr(
        candidates_module,
        "verify_generation_physical_integrity",
        lambda _p: PhysicalIndependenceResult(
            state=candidates_module.IndependenceState.INDETERMINATE, message="no se pudo inspeccionar"
        ),
    )
    assert verificar_candidate(root, cid).state is GenerationVerificationState.INDETERMINATE


# ── P3-R · ancestros redirigidos del SOURCE no aportan bytes externos ──────


@junction_guard
def test_p3r_ancestro_de_source_redirigido_no_copia_bytes_externos(tmp_path) -> None:
    """Un junction en un ancestro del source no puede inyectar bytes externos.

    El probe de fuente miraba SOLO el ultimo componente: ``lstat`` no sigue al
    leaf, pero SI sigue a los ancestros, asi que un `Source/Data` redirigido
    hacia `External/` hacia pasar `External/a.bin` por un archivo regular de la
    Managed Source.
    """
    raiz_origen = tmp_path / "source"
    (raiz_origen / "Data").mkdir(parents=True)
    (raiz_origen / "Data" / "a.bin").write_bytes(b"AAA")

    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "a.bin").write_bytes(b"BYTES-EXTERNOS!!")

    shutil.rmtree(raiz_origen / "Data")
    if (motivo := crear_junction(raiz_origen / "Data", externo)) is not None:
        pytest.skip(f"no se pudo crear junction: {motivo}")

    contenedor, destino = _armar_contenedor(tmp_path)
    archivos = (FileIdentity(rel_path="Data/a.bin", size=16, digest="a" * 64),)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(raiz_origen, destino, archivos, ("Data",), contenedor=contenedor)

    copiado = destino / "Data" / "a.bin"
    assert not copiado.exists() or copiado.read_bytes() != b"BYTES-EXTERNOS!!"


# ── P3-S · coerción numérica lossy en la evidencia persistida ─────────────


def _ready_y_meta(rig):  # noqa: ANN001, ANN202, F811
    _source, root, cid = _crear_ready(rig)
    return root, cid, candidate_metadata_path(root, cid)


@pytest.mark.parametrize(
    ("camino", "valor"),
    [
        (("pre_source_evidence", "tree_digest", "files"), 3.9),
        (("pre_source_evidence", "tree_digest", "files"), True),
        (("pre_source_evidence", "tree_digest", "bytes"), 3.9),
        (("pre_source_evidence", "tree_digest", "bytes"), True),
        (("pre_source_evidence", "files", 0, "size"), 3.7),
        (("pre_source_evidence", "files", 0, "size"), True),
        (("pre_source_evidence", "critical_files", 0, "size"), 3.7),
        (("created_at_ns",), 1.5),
        (("updated_at_ns",), True),
    ],
)
def test_p3s_entero_con_coercion_lossy_es_corrupcion(rig, camino, valor) -> None:  # noqa: F811
    """`1.9 -> 1` y `true -> 1` no pueden reconstruir una evidencia valida."""
    root, cid, meta = _ready_y_meta(rig)
    datos = _leer_json(meta)
    puntero = datos
    for parte in camino[:-1]:
        puntero = puntero[parte]
    puntero[camino[-1]] = valor
    _escribir_json(meta, datos)

    with pytest.raises(CandidateCorruptMetadataError):
        leer_metadata_candidate(meta)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.UNKNOWN
    assert descubrir_candidates(root).records[0].state is GenerationVerificationState.UNKNOWN


def test_p3s_enteros_validos_siguen_aceptandose(rig) -> None:  # noqa: F811
    """El rechazo no puede llevarse puestos los enteros JSON legitimos."""
    root, cid, meta = _ready_y_meta(rig)
    metadata = leer_metadata_candidate(meta)
    assert isinstance(metadata.pre_source_evidence.tree_digest.files, int)
    assert verificar_candidate(root, cid).state is GenerationVerificationState.VALID
