"""P3 ronda 2 — findings válidos sobre el HEAD de la ronda anterior.

Cada test se escribió y se vio fallar (RED) contra el commit previo al fix. Los
findings de la ronda anterior que ya estaban cerrados en ese HEAD NO se cubren
acá: `thread sin resolver != bug existente`.

Clase que cubre: el namespace de payload tenía el mismo hueco que el de metadata
(se arregló el hermano y no el gemelo), el inventario no veía reservas huérfanas,
un fallo de observación fresca se afirmaba como corrupción, y el probe de enlaces
dejaba escapar un `OSError` crudo en la copia y en la membership.
"""

from __future__ import annotations

import shutil

import pytest

from sky_claw.local.frozen_runtime import candidates as candidates_module
from sky_claw.local.frozen_runtime import copying as copying_module
from sky_claw.local.frozen_runtime import membership as membership_module
from sky_claw.local.frozen_runtime.candidates import (
    candidates_dir,
    descubrir_candidates,
    verificar_candidate,
)
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.errors import CandidateCopyError
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    canonicalizar_archivo,
    capturar_membership_directorios,
)
from sky_claw.local.frozen_runtime.storage_models import GenerationVerificationState
from tests._p3_rig import crear as _crear
from tests._p3_rig import parche_identidad, rig  # noqa: F401
from tests._symlink_guard import crear_junction, junction_guard


def _crear_ready(rig):  # noqa: ANN001, ANN202, F811
    source, root = rig
    resultado = _crear(source, root)
    assert resultado.state is GenerationVerificationState.VALID
    return source, root, resultado.candidate_id


def _probe_que_falla(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
    raise OSError("simulado: volumen desconectado")


# ── P3-K · el namespace de payload se admite ANTES de reservar el id ───────


@junction_guard
def test_p3k_la_reserva_no_muta_fuera_del_root(rig, tmp_path) -> None:  # noqa: F811
    """Si `root/candidates` es un junction, reservar no puede crear afuera.

    El namespace de metadata (`state/candidates/`) ya se admitía antes del
    `mkdir`; el de payload (`candidates/`) no, así que el `mkdir` seguía al padre
    redirigido y creaba el directorio del Candidate FUERA del root. El
    `exigir_contencion_fisica` posterior lo rechazaba, pero sólo después de haber
    mutado el árbol externo.
    """
    source, root = rig
    externo = tmp_path / "externo"
    externo.mkdir()
    shutil.rmtree(candidates_dir(root))
    if (motivo := crear_junction(candidates_dir(root), externo)) is not None:
        pytest.skip(f"no se pudo crear junction: {motivo}")

    resultado = _crear(source, root, cid="cand_" + "a" * 32)

    assert list(externo.iterdir()) == [], "se mutó el destino externo antes de detectar el enlace"
    assert resultado.state is not GenerationVerificationState.VALID


# ── P3-L · una reserva sin metadata no puede ser invisible ─────────────────


def test_p3l_reserva_sin_metadata_aparece_como_unknown(rig) -> None:  # noqa: F811
    """Un `candidates/<id>/` sin JSON debe ser diagnosticable por el inventario.

    La reserva ocurre ANTES de persistir la metadata, y P3 no borra nada por
    diseño: un crash en esa ventana deja un directorio reservado sin registro que
    bloquea el id para siempre y que un scan metadata-only no puede diagnosticar.
    """
    _source, root = rig
    huerfano = "cand_" + "b" * 32
    (candidates_dir(root) / huerfano).mkdir(parents=True)

    inventario = descubrir_candidates(root)
    estados = {r.candidate_id: r.state for r in inventario.records}
    assert estados.get(huerfano) is GenerationVerificationState.UNKNOWN


# ── P3-M · un fallo de observación fresca no afirma corrupción ─────────────


def test_p3m_fallo_de_observacion_fresca_es_indeterminate(rig, monkeypatch) -> None:  # noqa: F811
    """No se puede afirmar INVALID cuando sólo no se pudo obtener evidencia fresca."""
    _source, root, cid = _crear_ready(rig)

    def observacion_imposible(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise candidates_module.CandidateVerificationError("simulado: payload ilegible transitoriamente")

    monkeypatch.setattr(candidates_module, "_observar_candidate", observacion_imposible)

    veredicto = verificar_candidate(root, cid)
    assert veredicto.state is GenerationVerificationState.INDETERMINATE


# ── P3-N · un OSError crudo del probe de enlaces no puede escapar ──────────


def test_p3n_oserror_del_probe_no_escapa_en_la_copia(rig, monkeypatch) -> None:  # noqa: F811
    source, root, _cid = _crear_ready(rig)
    destino = candidates_dir(root) / ("cand_" + "c" * 32) / "payload"

    monkeypatch.setattr(copying_module, "link_kind_and_identity_or_raise", _probe_que_falla)

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(source.root, destino, (), (), contenedor=root)


def test_p3n_oserror_del_probe_no_deja_building_huerfano(rig, monkeypatch) -> None:  # noqa: F811
    """El fallo del probe debe volver como resultado tipado, no como excepción cruda."""
    source, root = rig

    monkeypatch.setattr(copying_module, "link_kind_and_identity_or_raise", _probe_que_falla)

    resultado = _crear(source, root)  # no debe propagar OSError crudo
    assert resultado.state is not GenerationVerificationState.VALID


def test_p3n_oserror_del_probe_de_membership_es_tipado(rig, monkeypatch) -> None:  # noqa: F811
    source, _root = rig

    monkeypatch.setattr(membership_module, "link_kind_and_identity_or_raise", _probe_que_falla)

    with pytest.raises(DirectoryMembershipError):
        capturar_membership_directorios(source.root)


# ── P3-O · un relpath de archivo no puede nombrar un directorio ────────────


@pytest.mark.parametrize("entrada", ["Data/", "Data//", "Data/."])
def test_p3o_archivo_con_forma_de_directorio_es_rechazado(entrada: str) -> None:
    """`Data/` nombra un directorio: aceptarlo como archivo convierte una entrada
    de `mkdir` en una de `open`."""
    with pytest.raises(DirectoryMembershipError):
        canonicalizar_archivo(entrada)


def test_p3o_archivo_normal_sigue_siendo_valido() -> None:
    """El rechazo no puede llevarse puestos los relpaths de archivo legítimos."""
    assert canonicalizar_archivo("Data\\a.nif") == "Data/a.nif"
    assert canonicalizar_archivo("Data/Meshes/a.nif") == "Data/Meshes/a.nif"
