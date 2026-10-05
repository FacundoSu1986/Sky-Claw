"""P3-I · los relpaths que alimentan la copia se validan ANTES de mutar.

`copiar_arbol_independiente` es una primitive reusable del paquete: no puede
asumir que la evidencia que recibe sea perfecta. Una entrada hostil debe fallar
sin haber creado un solo directorio ni copiado un solo byte.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente
from sky_claw.local.frozen_runtime.errors import CandidateCopyError
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    canonicalizar_archivo,
    canonicalizar_directorio,
    canonicalizar_relpath_de_scope,
)
from sky_claw.local.frozen_runtime.models import FileIdentity

MALICIOSOS = [
    "../../escape",
    "..\\..\\escape",
    "Data/../../escape",
    "/raiz/absoluta",
    "\\absoluta-windows",
    "C:/escape",
    "C:\\escape",
    "Data\\..\\..\\escape",
    "Data/x.txt:ads",
    "",
    ".",
    "..",
    "///",
]


def _identidad(rel_path: str) -> FileIdentity:
    return FileIdentity(rel_path=rel_path, size=1, digest="a" * 64)


@pytest.fixture
def origen(tmp_path: pathlib.Path) -> pathlib.Path:
    raiz = tmp_path / "origen"
    (raiz / "Data").mkdir(parents=True)
    (raiz / "Data" / "bueno.txt").write_bytes(b"ok")
    return raiz


def _armar(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    raiz = tmp_path / "contenedor"
    (raiz / "candidates" / "cand_x").mkdir(parents=True)
    destino = raiz / "candidates" / "cand_x" / "payload"
    centinela = tmp_path / "SENTINELA.txt"
    centinela.write_text("intacto", encoding="utf-8")
    return raiz, destino, centinela


@pytest.mark.parametrize("rel_path", MALICIOSOS)
def test_c28_un_archivo_traversal_se_rechaza_antes_de_escribir(origen, tmp_path, rel_path: str) -> None:
    """C28/C30/C31: traversal, absoluta, unidad y ADS se rechazan SIN mutar."""
    raiz, destino, centinela = _armar(tmp_path)
    antes = sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*"))

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(origen, destino, (_identidad(rel_path),), (), contenedor=raiz)

    assert centinela.read_text(encoding="utf-8") == "intacto"
    assert not destino.exists(), "no debe crearse el payload ante una entrada estructuralmente invalida"
    assert sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*")) == antes


@pytest.mark.parametrize("directorio", MALICIOSOS)
def test_c29_un_directorio_traversal_se_rechaza_antes_de_escribir(origen, tmp_path, directorio: str) -> None:
    """C28/C30/C31: lo mismo para directorios, con todos los archivos sanos."""
    raiz, destino, centinela = _armar(tmp_path)
    antes = sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*"))

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(
            origen,
            destino,
            (_identidad("Data/bueno.txt"),),
            ("Data", directorio),
            contenedor=raiz,
        )

    assert centinela.read_text(encoding="utf-8") == "intacto"
    assert sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*")) == antes


def test_c32_una_entrada_tardia_maliciosa_no_produce_mutacion_parcial(origen, tmp_path) -> None:
    """La validacion es de LOTO: una entrada hostil al final no deja copia a medias.

    Si la copia canonicalizara entrada por entrada mientras copia, las sanas que la
    preceden ya habrian quedado escritas en disco.
    """
    raiz, destino, _centinela = _armar(tmp_path)
    entradas = tuple(_identidad(p) for p in ("Data/a.txt", "Data/b.txt", "Data/c.txt", "../../escape"))

    with pytest.raises(CandidateCopyError):
        copiar_arbol_independiente(origen, destino, entradas, ("Data",), contenedor=raiz)

    assert not destino.exists() or not any(destino.rglob("*")), "no debe quedar contenido copiado"


def test_c33_los_relpaths_se_canonicalizan_con_la_primitive_unica() -> None:
    """Las dos superficies comparten primitive: no hay logica divergente."""
    assert canonicalizar_directorio("Data\\Meshes") == "Data/Meshes"
    assert canonicalizar_archivo("Data\\a.nif") == "Data/a.nif"
    for hueste in MALICIOSOS:
        with pytest.raises(DirectoryMembershipError):
            canonicalizar_relpath_de_scope(hueste, tipo="directorio")
        with pytest.raises(DirectoryMembershipError):
            canonicalizar_relpath_de_scope(hueste, tipo="archivo")
