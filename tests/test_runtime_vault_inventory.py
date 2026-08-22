"""Inventario fail-closed del Runtime Vault (RV-1).

El inventario es COMPLETO (no muestreado) y determinista; la identidad por
archivo es (relpath canónico, size, sha256). Mutation checks del contrato:

- el mtime NO participa de la identidad (mutarlo no cambia el inventario);
- un enlace (symlink/junction/reparse point) dentro del scope es FAIL CLOSED:
  no se sigue NI se ignora — el inventario se rehúsa;
- un árbol ausente, no-directorio o sin archivos propios NO produce un
  inventario vacío que parezca válido (UNKNOWN != VERIFIED).
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import InventoryError, InventoryLinkError
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard


def _sha256_de(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


@pytest.fixture
def arbol(tmp_path: pathlib.Path) -> pathlib.Path:
    raiz = tmp_path / "arbol"
    (raiz / "sub").mkdir(parents=True)
    (raiz / "a.txt").write_bytes(b"contenido-a")
    (raiz / "sub" / "b.bin").write_bytes(b"\x00\x01\x02")
    return raiz


class TestInventarioCompletoYDeterminista:
    def test_identidad_relpath_size_sha256(self, arbol: pathlib.Path) -> None:
        inventario = inventory_tree(arbol)
        por_relpath = {f.rel_path: f for f in inventario}

        assert set(por_relpath) == {"a.txt", "sub/b.bin"}
        assert por_relpath["a.txt"].size == len(b"contenido-a")
        assert por_relpath["a.txt"].digest == _sha256_de(b"contenido-a")
        assert por_relpath["sub/b.bin"].size == 3
        assert por_relpath["sub/b.bin"].digest == _sha256_de(b"\x00\x01\x02")

    def test_relpaths_canonicos_posix_y_orden_lexicografico(self, arbol: pathlib.Path) -> None:
        inventario = inventory_tree(arbol)
        relpaths = [f.rel_path for f in inventario]

        assert relpaths == sorted(relpaths)
        assert all("\\" not in r for r in relpaths)
        assert all(r == r.replace("\\", "/") for r in relpaths)

    def test_mismo_arbol_mismo_inventario_dos_veces(self, arbol: pathlib.Path) -> None:
        assert inventory_tree(arbol) == inventory_tree(arbol)

    def test_mtime_no_forma_parte_de_la_identidad(self, arbol: pathlib.Path) -> None:
        """Mutation check: tocar solo el mtime no puede cambiar la identidad."""
        antes = inventory_tree(arbol)
        archivo = arbol / "a.txt"
        estado = archivo.stat()
        os.utime(archivo, ns=(estado.st_atime_ns, estado.st_mtime_ns + 60_000_000_000))

        despues = inventory_tree(arbol)

        assert despues == antes

    def test_contenido_mutado_cambia_la_identidad(self, arbol: pathlib.Path) -> None:
        """Mutation check inverso: un byte distinto SÍ debe cambiar la identidad."""
        antes = inventory_tree(arbol)
        (arbol / "a.txt").write_bytes(b"contenido-a-mutado")

        despues = inventory_tree(arbol)

        assert despues != antes


class TestFailClosed:
    def test_root_ausente_es_error_no_inventario_vacio(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(InventoryError):
            inventory_tree(tmp_path / "no-existe")

    def test_root_archivo_es_error(self, tmp_path: pathlib.Path) -> None:
        archivo = tmp_path / "solo.txt"
        archivo.write_text("x", encoding="utf-8")

        with pytest.raises(InventoryError):
            inventory_tree(archivo)

    def test_arbol_sin_archivos_propios_es_error(self, tmp_path: pathlib.Path) -> None:
        vacio = tmp_path / "vacio"
        (vacio / "sub").mkdir(parents=True)

        with pytest.raises(InventoryError):
            inventory_tree(vacio)

    def test_entrada_ilegible_es_error(self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sky_claw.local.runtime_vault.inventory as modulo

        def _lectura_falla(_ruta: pathlib.Path) -> tuple[int, str]:
            raise OSError("permiso denegado")

        monkeypatch.setattr(modulo, "_digest_de_archivo", _lectura_falla)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    @symlink_guard
    def test_symlink_a_archivo_dentro_del_scope_falla_cerrado(self, arbol: pathlib.Path) -> None:
        (arbol / "enlace.txt").symlink_to(arbol / "sub" / "b.bin")

        with pytest.raises(InventoryLinkError):
            inventory_tree(arbol)

    @symlink_guard
    def test_symlink_a_directorio_como_raiz_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        destino = tmp_path / "destino"
        destino.mkdir()
        (destino / "a.txt").write_bytes(b"x")
        enlace = tmp_path / "enlace"
        enlace.symlink_to(destino, target_is_directory=True)

        with pytest.raises(InventoryLinkError):
            inventory_tree(enlace)

    @junction_guard
    def test_junction_dentro_del_scope_falla_cerrado(self, arbol: pathlib.Path) -> None:
        """El modo de falla más grave: un junction NO es un symlink para os.path.islink."""
        motivo = crear_junction(arbol / "otros", arbol / "sub")
        assert motivo is None, motivo

        with pytest.raises(InventoryLinkError):
            inventory_tree(arbol)
