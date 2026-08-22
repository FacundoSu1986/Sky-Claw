"""Inventario fail-closed del Runtime Vault (RV-1).

El inventario es COMPLETO (no muestreado) y determinista; la identidad por
archivo es (relpath canónico, size, sha256). Mutation checks del contrato:

- el mtime NO participa de la identidad (mutarlo no cambia el inventario);
- un enlace (symlink/junction/reparse point) dentro del scope es FAIL CLOSED:
  no se sigue NI se ignora — el inventario se rehúsa;
- un árbol ausente, no-directorio o sin archivos propios NO produce un
  inventario vacío que parezca válido (UNKNOWN != VERIFIED);
- F1: la observación de cada archivo queda SELLADA alrededor de su lectura
  (PRE/READ/POST) — un archivo que crece, se sobrescribe o se reemplaza
  durante la captura falla cerrado.
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

import sky_claw.local.runtime_vault.inventory as modulo_inv
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import InventoryError, InventoryLinkError
from sky_claw.local.runtime_vault.verification import tree_digest_from_files
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
        def _lectura_falla(_ruta: pathlib.Path, _identidad: os.stat_result) -> tuple[int, str, int]:
            raise OSError("permiso denegado")

        monkeypatch.setattr(modulo_inv, "_digest_estable", _lectura_falla)

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


#: Golden del árbol del fixture ``arbol`` producido por fa920c08 (commit
#: original de RV-1). El fix F1 refuerza la ESTABILIDAD de captura, no la
#: fórmula de identidad: este valor no puede cambiar.
_GOLDEN_DIGEST_FA920C08 = "b4a5db127fba31e714bfd36ed5f19b6b26e78d508194c386caff1575cbdcf2fa"


class TestEstabilidadDeCaptura:
    """F1 — la observación de un archivo queda sellada alrededor de su lectura.

    Los tests montan hooks controlados en los puntos exactos de la ventana de
    captura (antes del open, tras el último chunk) en vez de depender de
    sleeps: la carrera es determinista porque el hook se ejecuta SIEMPRE en el
    mismo punto del recorrido, no en un momento aleatorio del scheduler.
    """

    @staticmethod
    def _montar_hook_durante(monkeypatch: pytest.MonkeyPatch, fn) -> None:
        # raising=False: en la fase roja (fa920c08) el atributo aún no existe;
        # el test falla igual por la propiedad (no hay fail-closed).
        monkeypatch.setattr(modulo_inv, "_HOOK_DURANTE_LA_LECTURA", fn, raising=False)

    @staticmethod
    def _montar_hook_antes(monkeypatch: pytest.MonkeyPatch, fn) -> None:
        monkeypatch.setattr(modulo_inv, "_HOOK_ANTES_DE_ABRIR", fn, raising=False)

    @staticmethod
    def _montar_hook_tras(monkeypatch: pytest.MonkeyPatch, fn) -> None:
        monkeypatch.setattr(modulo_inv, "_HOOK_TRAS_LA_LECTURA", fn, raising=False)

    def test_archivo_crece_despues_de_cerrar_el_descriptor_falla_por_conteo_de_bytes(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-F1-A: el crecimiento ocurre TRAS cerrar el descriptor de lectura y
        se restaura el mtime — el ÚNICO gate que puede disparar es el de bytes
        leídos vs tamaño final. La versión anterior de este test hacía append
        con el reader aún abierto, que absorbía los bytes nuevos y disparaba
        solo el gate de mtime (hallazgo del review)."""
        objetivo = arbol / "a.txt"
        estado = objetivo.stat()
        disparado = {"ya": False}

        def _crecer_tras_lectura(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                with objetivo.open("ab") as fh:
                    fh.write(b"bytes-extra-despues-de-cerrar-el-reader")
                os.utime(objetivo, ns=(estado.st_atime_ns, estado.st_mtime_ns))

        self._montar_hook_tras(monkeypatch, _crecer_tras_lectura)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    def test_archivo_sobrescrito_mismo_tamano_durante_el_hashing_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-F1-B: overwrite same-size: mismo inode y mismo tamaño — sólo el
        evidence gate de mtime lo delata. El utime final fuerza la semántica
        del overwrite para eliminar la colisión de granularidad de 100 ns de
        NTFS entre dos writes separados por microsegundos."""
        objetivo = arbol / "a.txt"
        original = objetivo.read_bytes()
        reemplazo = bytes(reversed(original))
        assert len(reemplazo) == len(original) > 0
        disparado = {"ya": False}

        def _sobrescribir(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                with objetivo.open("wb") as fh:
                    fh.write(reemplazo)
                estado = objetivo.stat()
                os.utime(objetivo, ns=(estado.st_atime_ns, estado.st_mtime_ns + 60_000_000_000))

        self._montar_hook_durante(monkeypatch, _sobrescribir)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    def test_archivo_reemplazado_antes_de_la_lectura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-F1-C: entre el PRE y el open, el archivo pasa a ser OTRO archivo."""
        objetivo = arbol / "a.txt"
        disparado = {"ya": False}

        def _reemplazar(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                objetivo.unlink()
                objetivo.write_bytes(b"reemplazo-total-distinto")

        self._montar_hook_antes(monkeypatch, _reemplazar)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    @symlink_guard
    def test_archivo_reemplazado_por_symlink_antes_de_la_lectura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La ventana lstat → reemplazo por symlink → open no sigue al target:
        el POST detecta el enlace y descarta la observación."""
        objetivo = arbol / "a.txt"
        destino = arbol / "sub" / "b.bin"
        disparado = {"ya": False}

        def _poner_enlace(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                objetivo.unlink()
                objetivo.symlink_to(destino)

        self._montar_hook_antes(monkeypatch, _poner_enlace)

        with pytest.raises(InventoryLinkError):
            inventory_tree(arbol)

    @junction_guard
    def test_archivo_reemplazado_por_junction_antes_de_la_lectura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El reemplazo por junction (reparse point) tampoco se sigue ni se acepta."""
        objetivo = arbol / "a.txt"
        destino_dir = arbol / "sub"
        disparado = {"ya": False}

        def _poner_junction(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                objetivo.unlink()
                motivo = crear_junction(objetivo, destino_dir)
                assert motivo is None, motivo

        self._montar_hook_antes(monkeypatch, _poner_junction)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    def test_arbol_estable_produce_el_mismo_digest_que_fa920c08(self, arbol: pathlib.Path) -> None:
        """T-F1-D: regression contract — el fix no versiona el formato de identidad."""
        esperado = tree_digest_from_files(inventory_tree(arbol))

        assert esperado.digest == _GOLDEN_DIGEST_FA920C08
        assert esperado.files == 2
        assert esperado.bytes == 14

    def test_mtime_cambia_sin_contenido_durante_la_lectura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-F1-E: una captura inestable no se acepta en silencio aunque los
        bytes no hayan cambiado: el mtime es evidence gate DURANTE la captura
        (sigue sin formar parte de la identidad persistente)."""
        objetivo = arbol / "a.txt"
        estado = objetivo.stat()
        disparado = {"ya": False}

        def _tocar_mtime(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                os.utime(objetivo, ns=(estado.st_atime_ns, estado.st_mtime_ns + 60_000_000_000))

        self._montar_hook_durante(monkeypatch, _tocar_mtime)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)


class TestSelladoDeArbol:
    """A — tree-level stability seal.

    Invariante: al regresar ``inventory_tree()``, ninguna mutación observable
    ocurrida durante la captura completa puede dejar una identidad incompleta
    como válida. El seal por archivo (F1) cubre la ventana de cada lectura;
    estos tests cubren las mutaciones que el recorrido ya pasó: membership que
    aparece, desaparece o cambia, y contenido de archivos ya hasheados.

    El orden LIFO del walk procesa ``sub/b.bin`` ANTES que ``a.txt``, así que
    el hook montado durante ``a.txt`` muta siempre algo que ya fue capturado —
    y si alguna vez el orden cambiara, el walk lo detectaría igual por
    "desapareció durante el recorrido": ambos desenlaces son fail-closed.
    """

    @staticmethod
    def _montar_hook_durante(monkeypatch: pytest.MonkeyPatch, fn) -> None:
        monkeypatch.setattr(modulo_inv, "_HOOK_DURANTE_LA_LECTURA", fn, raising=False)

    def test_archivo_agregado_durante_la_captura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El caso del review: el dir ya fue esnapshotado; la entrada nueva no
        está en el recorrido y solo la validación estructural final la detecta."""
        objetivo = arbol / "a.txt"
        disparado = {"ya": False}

        def _agregar(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                (arbol / "nuevo.txt").write_bytes(b"aparecio-tarde")

        self._montar_hook_durante(monkeypatch, _agregar)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    def test_archivo_eliminado_durante_la_captura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = arbol / "a.txt"
        eliminado = arbol / "sub" / "b.bin"
        disparado = {"ya": False}

        def _eliminar(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                eliminado.unlink()

        self._montar_hook_durante(monkeypatch, _eliminar)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    def test_archivo_renombrado_durante_la_captura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = arbol / "a.txt"
        renombrado = arbol / "sub" / "b.bin"
        disparado = {"ya": False}

        def _renombrar(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                renombrado.rename(arbol / "sub" / "b2.bin")

        self._montar_hook_durante(monkeypatch, _renombrar)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    def test_archivo_ya_hasheado_modificado_despues_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overwrite same-size de un archivo ya hasheadado, detectado por la
        pasada final estructural (mtime de la captura vs el observado)."""
        objetivo = arbol / "a.txt"
        victima = arbol / "sub" / "b.bin"
        original = victima.read_bytes()
        reemplazo = bytes(reversed(original))
        assert len(reemplazo) == len(original) > 0
        disparado = {"ya": False}

        def _sobrescribir(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                with victima.open("wb") as fh:
                    fh.write(reemplazo)
                estado = victima.stat()
                os.utime(victima, ns=(estado.st_atime_ns, estado.st_mtime_ns + 60_000_000_000))

        self._montar_hook_durante(monkeypatch, _sobrescribir)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)

    def test_directorio_agregado_durante_la_captura_falla_cerrado(
        self, arbol: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        objetivo = arbol / "a.txt"
        disparado = {"ya": False}

        def _agregar_dir(ruta: pathlib.Path) -> None:
            if ruta == objetivo and not disparado["ya"]:
                disparado["ya"] = True
                nuevo = arbol / "nuevo_dir"
                nuevo.mkdir()
                (nuevo / "c.txt").write_bytes(b"c")

        self._montar_hook_durante(monkeypatch, _agregar_dir)

        with pytest.raises(InventoryError):
            inventory_tree(arbol)
