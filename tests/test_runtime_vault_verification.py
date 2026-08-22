"""Verificación de árbol y de identidad de runtime (RV-1).

Mutation checks del contrato de verificación:

- mutar un byte, agregar, borrar o renombrar un archivo → FAILED;
- tocar SOLO el mtime → VERIFIED (el mtime no forma parte de la identidad);
- mover el árbol a otro root físico → VERIFIED (el digest es independiente
  del root físico: sólo relpaths y contenido);
- inyectar un enlace → UNKNOWN, jamás VERIFIED (fail closed);
- sin identidad esperada, o sin árbol observable → UNKNOWN (UNKNOWN != VERIFIED);
- path + size + file digest forman parte de la identidad agregada: un
  TreeDigest con las mismas dimensiones pero otra tripla NO es igual.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    FileIdentity,
    RuntimeIdentity,
    TreeDigest,
    VerificationState,
)
from sky_claw.local.runtime_vault.verification import (
    tree_digest_from_files,
    verify_runtime_identity,
    verify_tree,
)
from tests._symlink_guard import symlink_guard


@pytest.fixture
def identidad_esperada(tmp_path: pathlib.Path) -> tuple[pathlib.Path, TreeDigest]:
    """Un árbol de dos archivos y su identidad calculada con la API pública."""
    raiz = tmp_path / "arbol"
    (raiz / "sub").mkdir(parents=True)
    (raiz / "a.txt").write_bytes(b"uno")
    (raiz / "sub" / "b.bin").write_bytes(b"dos")
    esperado = tree_digest_from_files(inventory_tree(raiz))
    return raiz, esperado


class TestVerifyTree:
    def test_arbol_intacto_es_verified(self, identidad_esperada: tuple[pathlib.Path, TreeDigest]) -> None:
        raiz, esperado = identidad_esperada

        resultado = verify_tree(raiz, esperado)

        assert resultado.state is VerificationState.VERIFIED
        assert resultado.success is True
        assert resultado.message == ""
        assert resultado.expected == esperado
        assert resultado.observed == esperado

    def test_sin_identidad_esperada_es_unknown(self, identidad_esperada: tuple[pathlib.Path, TreeDigest]) -> None:
        raiz, _ = identidad_esperada

        resultado = verify_tree(raiz, None)

        assert resultado.state is VerificationState.UNKNOWN
        assert resultado.success is False
        assert resultado.message != ""

    def test_root_ausente_es_unknown_no_failed(
        self, tmp_path: pathlib.Path, identidad_esperada: tuple[pathlib.Path, TreeDigest]
    ) -> None:
        _, esperado = identidad_esperada

        resultado = verify_tree(tmp_path / "no-existe", esperado)

        assert resultado.state is VerificationState.UNKNOWN
        assert resultado.success is False
        assert resultado.observed is None

    def test_byte_mutado_es_failed(self, identidad_esperada: tuple[pathlib.Path, TreeDigest]) -> None:
        raiz, esperado = identidad_esperada
        (raiz / "a.txt").write_bytes(b"uno-mutado")

        resultado = verify_tree(raiz, esperado)

        assert resultado.state is VerificationState.FAILED
        assert resultado.success is False

    def test_archivo_agregado_es_failed(self, identidad_esperada: tuple[pathlib.Path, TreeDigest]) -> None:
        raiz, esperado = identidad_esperada
        (raiz / "extra.txt").write_bytes(b"extra")

        resultado = verify_tree(raiz, esperado)

        assert resultado.state is VerificationState.FAILED

    def test_archivo_borrado_es_failed(self, identidad_esperada: tuple[pathlib.Path, TreeDigest]) -> None:
        raiz, esperado = identidad_esperada
        (raiz / "a.txt").unlink()

        resultado = verify_tree(raiz, esperado)

        assert resultado.state is VerificationState.FAILED

    def test_archivo_renombrado_es_failed(self, identidad_esperada: tuple[pathlib.Path, TreeDigest]) -> None:
        """El path forma parte de la identidad: renombrar sin tocar bytes es FAILED."""
        raiz, esperado = identidad_esperada
        (raiz / "a.txt").rename(raiz / "a-renombrado.txt")

        resultado = verify_tree(raiz, esperado)

        assert resultado.state is VerificationState.FAILED

    def test_solo_mtime_cambiado_sigue_verified(self, identidad_esperada: tuple[pathlib.Path, TreeDigest]) -> None:
        """Mutation check: el mtime no forma parte de la identidad."""
        raiz, esperado = identidad_esperada
        archivo = raiz / "a.txt"
        estado = archivo.stat()
        os.utime(archivo, ns=(estado.st_atime_ns, estado.st_mtime_ns + 60_000_000_000))

        resultado = verify_tree(raiz, esperado)

        assert resultado.state is VerificationState.VERIFIED

    def test_arbol_movido_a_otro_root_fisico_sigue_verified(
        self, tmp_path: pathlib.Path, identidad_esperada: tuple[pathlib.Path, TreeDigest]
    ) -> None:
        """El digest del árbol es independiente del root físico."""
        raiz, esperado = identidad_esperada
        movido = tmp_path / "otro-root"
        shutil.copytree(raiz, movido)

        resultado = verify_tree(movido, esperado)

        assert resultado.state is VerificationState.VERIFIED

    def test_dimensiones_forman_parte_de_la_identidad(
        self, identidad_esperada: tuple[pathlib.Path, TreeDigest]
    ) -> None:
        """Un TreeDigest con el mismo digest pero otra dimensión NO es la misma identidad."""
        raiz, esperado = identidad_esperada
        inflado = TreeDigest(digest=esperado.digest, files=esperado.files + 1, bytes=esperado.bytes)

        resultado = verify_tree(raiz, inflado)

        assert resultado.state is VerificationState.FAILED

    @symlink_guard
    def test_enlace_inyectado_es_unknown_jamas_verified(
        self, identidad_esperada: tuple[pathlib.Path, TreeDigest]
    ) -> None:
        """Mutation check: un enlace inyectado hace el árbol no verificable."""
        raiz, esperado = identidad_esperada
        (raiz / "enlace.txt").symlink_to(raiz / "sub" / "b.bin")

        resultado = verify_tree(raiz, esperado)

        assert resultado.state is VerificationState.UNKNOWN
        assert resultado.success is False
        assert resultado.observed is None


class TestTreeDigestDesdeInventario:
    def test_independiente_del_orden_de_los_archivos(self) -> None:
        a = FileIdentity(rel_path="a.txt", size=3, digest="d1")
        b = FileIdentity(rel_path="sub/b.bin", size=3, digest="d2")

        assert tree_digest_from_files([a, b]) == tree_digest_from_files([b, a])

    def test_size_forma_parte_de_la_identidad(self) -> None:
        mismo_digest = FileIdentity(rel_path="a.txt", size=3, digest="d1")
        otro_size = FileIdentity(rel_path="a.txt", size=4, digest="d1")

        assert tree_digest_from_files([mismo_digest]) != tree_digest_from_files([otro_size])

    def test_relpath_forma_parte_de_la_identidad(self) -> None:
        un_path = FileIdentity(rel_path="a.txt", size=3, digest="d1")
        otro_path = FileIdentity(rel_path="b.txt", size=3, digest="d1")

        assert tree_digest_from_files([un_path]) != tree_digest_from_files([otro_path])

    def test_file_digest_forma_parte_de_la_identidad(self) -> None:
        un_digest = FileIdentity(rel_path="a.txt", size=3, digest="d1")
        otro_digest = FileIdentity(rel_path="a.txt", size=3, digest="d2")

        assert tree_digest_from_files([un_digest]) != tree_digest_from_files([otro_digest])


class TestVerifyRuntimeIdentity:
    """La identidad de runtime se compara como DATOS DE ENTRADA, sin escanear."""

    def test_identidades_iguales_verified(self) -> None:
        esperada = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        observada = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

        resultado = verify_runtime_identity(esperada, observada)

        assert resultado.state is VerificationState.VERIFIED
        assert resultado.success is True
        assert resultado.message == ""

    def test_version_esperada_vacia_unknown(self) -> None:
        esperada = RuntimeIdentity(game_key="skyrimse", game_version="")
        observada = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

        resultado = verify_runtime_identity(esperada, observada)

        assert resultado.state is VerificationState.UNKNOWN
        assert resultado.success is False

    def test_version_observada_vacia_unknown(self) -> None:
        esperada = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        observada = RuntimeIdentity(game_key="skyrimse", game_version="")

        resultado = verify_runtime_identity(esperada, observada)

        assert resultado.state is VerificationState.UNKNOWN
        assert resultado.success is False

    def test_version_distinta_failed(self) -> None:
        esperada = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        observada = RuntimeIdentity(game_key="skyrimse", game_version="1.5.97.0")

        resultado = verify_runtime_identity(esperada, observada)

        assert resultado.state is VerificationState.FAILED

    def test_game_key_distinto_failed(self) -> None:
        esperada = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        observada = RuntimeIdentity(game_key="skyrimle", game_version="1.6.1170.0")

        resultado = verify_runtime_identity(esperada, observada)

        assert resultado.state is VerificationState.FAILED

    def test_sin_esperada_o_sin_observada_unknown(self) -> None:
        observada = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")

        assert verify_runtime_identity(None, observada).state is VerificationState.UNKNOWN
        assert verify_runtime_identity(observada, None).state is VerificationState.UNKNOWN
        assert verify_runtime_identity(None, None).state is VerificationState.UNKNOWN
