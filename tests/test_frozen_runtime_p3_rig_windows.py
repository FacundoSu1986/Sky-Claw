"""Rig sintetico de Windows para las propiedades de filesystem de P3.

Ejercita sobre NTFS real lo que los tests unitarios no pueden fingir:
hardlinks, junctions y el contrato cross-volume. NO toca el Skyrim del usuario
(§49): todo corre contra directorios temporales.

Cross-volume se declara honestamente: si el host no expone un segundo volumen
real, el test lo dice (`CROSS_VOLUME_REAL_RIG=NOT_AVAILABLE`) en vez de fingir un
PASS.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile

import pytest

from sky_claw.app.security.links import link_kind_and_identity_or_raise
from sky_claw.local.frozen_runtime.copying import copiar_arbol_independiente, copiar_archivo, payload_dir
from sky_claw.local.frozen_runtime.independence import (
    verify_generation_physical_integrity,
)
from sky_claw.local.frozen_runtime.membership import capturar_membership_directorios
from sky_claw.local.frozen_runtime.storage_models import IndependenceState
from tests._symlink_guard import crear_junction, junction_guard

RIG = "skyclaw-p3-rig"


def test_rig_junction_real_es_rechazado(tmp_path: pathlib.Path) -> None:
    """RIG/junction: un junction REAL dentro del scope no se sigue ni se ignora."""
    arbol = tmp_path / "arbol"
    (arbol / "Data").mkdir(parents=True)
    (arbol / "Data" / "x.bin").write_bytes(b"datos")
    fuera = tmp_path / "fuera"
    fuera.mkdir()

    motivo = crear_junction(arbol / "Data" / "Enlace", fuera)
    if motivo is not None:
        pytest.skip(f"junction no disponible: {motivo}")

    tipo, _st = link_kind_and_identity_or_raise(arbol / "Data" / "Enlace")
    assert tipo == "junction"

    from sky_claw.local.frozen_runtime.membership import DirectoryMembershipError

    with pytest.raises(DirectoryMembershipError):
        capturar_membership_directorios(arbol)


def test_rig_hardlink_real_rompe_la_independencia(tmp_path: pathlib.Path) -> None:
    """RIG/hardlink: bytes identicos pero objeto compartido ⇒ INDEPENDENT != Yes."""
    origen = tmp_path / "origen"
    copia = tmp_path / "copia"
    origen.mkdir()
    copia.mkdir()
    fuente = origen / "SkyrimSE.exe"
    fuente.write_bytes(b"contenido-identico")

    shutil.copyfile(fuente, copia / "SkyrimSE.exe")
    # Copia real: objeto independiente ⇒ nlink 1.
    assert verify_generation_physical_integrity(copia).state is IndependenceState.INDEPENDENT

    # Se reemplaza por un hardlink al original: mismo digest, misma membership.
    (copia / "SkyrimSE.exe").unlink()
    os.link(fuente, copia / "SkyrimSE.exe")
    assert (copia / "SkyrimSE.exe").read_bytes() == fuente.read_bytes()

    veredicto = verify_generation_physical_integrity(copia)
    assert veredicto.state is IndependenceState.VIOLATED
    assert "multi-link" in veredicto.message


def test_rig_preserva_directorios_vacios(tmp_path: pathlib.Path) -> None:
    """RIG/empty-dir: un directorio vacío sobrevive a la copia real."""
    origen = tmp_path / "origen"
    (origen / "Data" / "Vacio").mkdir(parents=True)
    (origen / "Data" / "con-archivo.bin").write_bytes(b"x")
    destino = payload_dir(tmp_path / "candidato")

    from sky_claw.local.runtime_vault.inventory import inventory_tree

    membership = capturar_membership_directorios(origen)
    copiar_arbol_independiente(origen, destino, inventory_tree(origen), membership.directories, contenedor=tmp_path)

    assert (destino / "Data" / "Vacio").is_dir()
    assert list((destino / "Data" / "Vacio").iterdir()) == []
    assert capturar_membership_directorios(destino) == membership


def test_rig_cross_volume_real_si_existe(tmp_path: pathlib.Path) -> None:
    """RIG/cross-volume: contract exercised on a REAL second volume if present.

    La Managed Source puede vivir en otra unidad; la copia usa ``open()`` +
    ``copyfileobj`` justamente por eso. Si el host no expone un segundo volumen,
    se declara NO DISPONIBLE en vez de reportar un verde falso.
    """
    origen = tmp_path / "origen.bin"
    origen.write_bytes(b"contenido" * 100)

    volumenes: dict[str, str] = {}
    for raiz in ("C:\\", "D:\\", "E:\\", "G:\\"):
        if os.path.isdir(raiz):
            volumenes[raiz] = os.stat(raiz).st_dev

    otros = [raiz for raiz, dev in volumenes.items() if dev != os.stat(str(origen)).st_dev]
    if not otros:
        pytest.skip("CROSS_VOLUME_REAL_RIG=NOT_AVAILABLE: el host expone un solo volumen")

    destino_dir = pathlib.Path(tempfile.mkdtemp(dir=otros[0], prefix=f"{RIG}-"))
    try:
        destino = destino_dir / "copiado.bin"
        copiar_archivo(origen, destino)
        assert destino.read_bytes() == origen.read_bytes()
        assert os.stat(destino).st_dev != os.stat(origen).st_dev
    finally:
        shutil.rmtree(destino_dir, ignore_errors=True)


@junction_guard
def test_rig_metadata_atmica_sobrevive_a_un_fallo_de_serializacion(tmp_path: pathlib.Path) -> None:
    """RIG/metadata: un temporal fallido NO trunca la metadata anterior."""
    import json

    from sky_claw.local.frozen_runtime.state import write_json_atomic

    ruta = tmp_path / "meta.json"
    write_json_atomic(ruta, {"schema_version": 1, "estado": "building"})

    class _NoSerializable:
        pass

    with pytest.raises(TypeError):
        write_json_atomic(ruta, {"schema_version": 1, "estado": _NoSerializable()})  # type: ignore[dict-item]

    # La metadata anterior sigue intacta y legible.
    assert json.loads(ruta.read_text(encoding="utf-8"))["estado"] == "building"
    assert list(tmp_path.glob(".sky_claw_*")) == []
