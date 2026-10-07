"""P3 — Membership de directorios: cierre de ``P3_DIRECTORY_MEMBERSHIP``.

El ``TreeDigest`` de P1/P2 sella ARCHIVOS pero NO directorios vacíos: dos
fuentes con archivos idénticos y distinta membership producen el MISMO
``TreeDigest``. Perder ``Data/EmptyFolder/`` sería invisible para la identidad
de archivos, así que P3 sella la membership aparte y la exige en las tres
evidencias (PRE == Candidate == POST).

RED-first: estos tests se escribieron y corrieron contra P0/P1/P2, antes de que
existiera ``sky_claw.local.frozen_runtime.membership``.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    DirectoryMembershipEvidence,
    canonicalizar_directorio,
    capturar_membership_directorios,
)
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.verification import tree_digest_from_files
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard


def _arbol_con_vacio(root: pathlib.Path) -> pathlib.Path:
    """Data/ + Data/Meshes/ + Data/EmptyFolder/ + un archivo."""
    (root / "Data" / "Meshes").mkdir(parents=True)
    (root / "Data" / "EmptyFolder").mkdir(parents=True)
    (root / "Data" / "Meshes" / "a.nif").write_bytes(b"nif-payload")
    return root


def _arbol_sin_vacio(root: pathlib.Path) -> pathlib.Path:
    """Idénticos archivos y directorios, SIN el directorio vacío."""
    (root / "Data" / "Meshes").mkdir(parents=True)
    (root / "Data" / "Meshes" / "a.nif").write_bytes(b"nif-payload")
    return root


# ── P3_DIRECTORY_MEMBERSHIP ───────────────────────────────────────────────


def test_m03_el_tree_digest_es_ciego_a_la_membership_de_directorios(
    tmp_path: pathlib.Path,
) -> None:
    """RED del blocker: la identidad de ARCHIVOS no distingue el directorio vacío.

    Caracterización del defecto: con las primitivas de P1/P2 la diferencia de
    membership es INVISIBLE, así que un Candidate podría perder
    ``Data/EmptyFolder/`` y aun así "coincidir".
    """
    con_vacio = _arbol_con_vacio(tmp_path / "con_vacio")
    sin_vacio = _arbol_sin_vacio(tmp_path / "sin_vacio")

    digest_con = tree_digest_from_files(inventory_tree(con_vacio))
    digest_sin = tree_digest_from_files(inventory_tree(sin_vacio))

    # El agujero, documentado: identidad de archivos IDÉNTICA...
    assert digest_con == digest_sin
    # ...mientras la membership SÍ difiere (esta aserción exige P3).
    membership_con = capturar_membership_directorios(con_vacio)
    membership_sin = capturar_membership_directorios(sin_vacio)
    assert membership_con != membership_sin


def test_m02_perder_un_directorio_vacio_cambia_el_digest_de_membership(
    tmp_path: pathlib.Path,
) -> None:
    """M02: el mismo árbol con y sin el vacío no puede compartir identidad."""
    con_vacio = _arbol_con_vacio(tmp_path / "con_vacio")
    sin_vacio = _arbol_sin_vacio(tmp_path / "sin_vacio")

    con = capturar_membership_directorios(con_vacio)
    sin = capturar_membership_directorios(sin_vacio)

    assert con.directory_count == 3
    assert sin.directory_count == 2
    assert con.digest != sin.digest
    assert "Data/EmptyFolder" in con.directories
    assert "Data/EmptyFolder" not in sin.directories
    assert con.directories == ("Data", "Data/EmptyFolder", "Data/Meshes")


def test_m01_la_membership_es_determinista_y_ordena_por_canonico(
    tmp_path: pathlib.Path,
) -> None:
    """M01: dos capturas del mismo árbol dan exactamente la misma evidencia."""
    arbol = _arbol_con_vacio(tmp_path / "arbol")

    primera = capturar_membership_directorios(arbol)
    segunda = capturar_membership_directorios(arbol)

    assert primera == segunda
    assert list(primera.directories) == sorted(primera.directories)
    assert primera.directory_count == len(primera.directories)


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("Data/Meshes", "Data/Meshes"),
        ("Data\\Meshes", "Data/Meshes"),
        ("Data//Meshes", "Data/Meshes"),
        ("Data/./Meshes", "Data/Meshes"),
    ],
)
def test_m01_canonicalizacion_deja_una_sola_forma(entrada: str, esperado: str) -> None:
    """M01: separador canónico '/', sin '.', sin separadores duplicados."""
    assert canonicalizar_directorio(entrada) == esperado


@pytest.mark.parametrize(
    "entrada",
    ["../escape", "Data/../../escape", "/Data/absoluto", "", ".", "..", "Data/.."],
)
def test_m09_canonicalizacion_rechaza_entradas_no_confiables(entrada: str) -> None:
    """M09: sin traversal, sin rutas absolutas, sin entradas degeneradas."""
    with pytest.raises(DirectoryMembershipError):
        canonicalizar_directorio(entrada)


@junction_guard
def test_m04_la_membership_falla_cerrado_ante_junction(tmp_path: pathlib.Path) -> None:
    """M04: un reparse point (junction) NO se sigue ni se ignora.

    Es el caso que ``os.path.islink()`` NO reporta como enlace, asi que un
    detector ingenuo lo dejaria pasar y la membership describiría un arbol
    que en realidad esta afuera del scope.
    """
    arbol = _arbol_con_vacio(tmp_path / "arbol")
    fuera = tmp_path / "fuera"
    fuera.mkdir()

    if (motivo := crear_junction(arbol / "Data" / "Enlace", fuera)) is not None:
        pytest.skip(f"no se pudo crear junction en este host: {motivo}")

    with pytest.raises(DirectoryMembershipError):
        capturar_membership_directorios(arbol)


@junction_guard
def test_m04_la_membership_falla_cerrado_si_el_root_es_junction(tmp_path: pathlib.Path) -> None:
    """M04: el root mismo tampoco puede ser un enlace."""
    arbol = _arbol_con_vacio(tmp_path / "arbol")
    enlace = tmp_path / "enlace_root"

    if (motivo := crear_junction(enlace, arbol)) is not None:
        pytest.skip(f"no se pudo crear junction en este host: {motivo}")

    with pytest.raises(DirectoryMembershipError):
        capturar_membership_directorios(enlace)


@symlink_guard
def test_m04_la_membership_falla_cerrado_ante_symlink(tmp_path: pathlib.Path) -> None:
    """M04: un symlink a un directorio FUERA del scope tampoco se sigue."""
    arbol = _arbol_con_vacio(tmp_path / "arbol")
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    (fuera / "secreto.txt").write_bytes(b"fuera-del-scope")

    (arbol / "Data" / "Enlace").symlink_to(fuera, target_is_directory=True)

    with pytest.raises(DirectoryMembershipError):
        capturar_membership_directorios(arbol)


def test_m04_la_evidencia_es_un_contrato_minimo_con_digest_y_conteo() -> None:
    """M04: el contrato persistido compromete el conjunto completo, no una muestra."""
    evidencia = DirectoryMembershipEvidence(
        digest="0" * 64,
        directory_count=2,
        directories=("Data", "Data/Meshes"),
    )
    assert evidencia.directory_count == len(evidencia.directories)
    assert len(evidencia.digest) == 64
