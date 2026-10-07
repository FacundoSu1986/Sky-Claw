"""P3 Cierre Final de Hardening — Verificación sistemática de las 5 familias.

Cierra y ancla:
- P3-AB: Revalidación del source root ante archivos en la raíz del Managed Source.
- P3-AC: Admisibilidad completa de rutas de Windows (caracteres inválidos y dispositivos reservados).
- P3-AD: Contención física del padre del destino existente fuera del contenedor.
- P3-AE: Coherencia integral del lote canónico (duplicados, colisiones y ancestros).
- P3-AF: Traducción fail-closed de excepciones de storage en creación de directorios.

Más las matrices completas de seguridad de SOURCE, DESTINATION, PATH y STORAGE.
"""

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from sky_claw.local.frozen_runtime import candidates, storage
from sky_claw.local.frozen_runtime.candidates import (
    CandidateResult,
    _exigir_candidates_state_dir,
)
from sky_claw.local.frozen_runtime.copying import (
    _exigir_ancestros_del_source,
    copiar_arbol_independiente,
    copiar_archivo,
)
from sky_claw.local.frozen_runtime.errors import (
    CandidateCopyError,
    FrozenRuntimeStorageError,
)
from sky_claw.local.frozen_runtime.membership import (
    DirectoryMembershipError,
    canonicalizar_relpath_de_scope,
)
from sky_claw.local.frozen_runtime.models import ManagedSource
from sky_claw.local.frozen_runtime.storage_models import GenerationVerificationState
from sky_claw.local.runtime_vault.models import FileIdentity
from tests._p3_rig import crear as _crear
from tests._p3_rig import parche_identidad, rig  # noqa: F401
from tests._symlink_guard import crear_junction, junction_guard

# ── Helpers de armado ─────────────────────────────────────────────────────────


def _file_id(rel_path: str, content: bytes = b"test") -> FileIdentity:
    return FileIdentity(rel_path=rel_path, size=len(content), digest="d" * 64)


# ═════════════════════════════════════════════════════════════════════════════
# P3-AB & SOURCE MATRIX · Revalidación y contención física de la fuente
# ═════════════════════════════════════════════════════════════════════════════


class TestSourceMatrixP3AB:
    """Matriz de seguridad de la fuente: root, ancestros, hoja e identidad."""

    @junction_guard
    def test_p3ab_source_root_reemplazado_por_junction_rechazado_para_archivo_raiz(
        self, tmp_path: pathlib.Path
    ) -> None:
        """P3-AB: si el source root es reemplazado por un junction, el archivo en la raíz debe rechazarse.

        Pre-fix: `_exigir_ancestros_del_source` terminaba por igualdad en
        `permitir_raiz=True` sin inspeccionar `raiz_origen`, y `copiar_archivo`
        copiaba bytes externos.
        Post-fix: `CandidateCopyError` fail-closed sin copiar bytes externos.
        """
        real_externo = tmp_path / "real_externo"
        real_externo.mkdir()
        (real_externo / "SkyrimSE.exe").write_bytes(b"BYTES_EXTERNOS_HOSTILES")

        junction_source = tmp_path / "junction_source"
        if (motivo := crear_junction(junction_source, real_externo)) is not None:
            pytest.skip(f"no se pudo crear junction: {motivo}")

        archivo_raiz = junction_source / "SkyrimSE.exe"

        with pytest.raises(CandidateCopyError, match="enlace|redireccion|ancestro|origen"):
            _exigir_ancestros_del_source(junction_source, archivo_raiz)

    @junction_guard
    def test_source_nested_ancestor_reemplazado_por_junction_rechazado(self, tmp_path: pathlib.Path) -> None:
        """Un ancestro anidado de la fuente redirigido por junction debe rechazarse."""
        source = tmp_path / "source"
        source.mkdir()
        externo = tmp_path / "externo"
        externo.mkdir()
        (externo / "archivo.bin").write_bytes(b"EXT")

        data_junc = source / "Data"
        if (motivo := crear_junction(data_junc, externo)) is not None:
            pytest.skip(f"no se pudo crear junction: {motivo}")

        archivo_anidado = data_junc / "archivo.bin"
        with pytest.raises(CandidateCopyError, match="ancestro|redireccion"):
            _exigir_ancestros_del_source(source, archivo_anidado)

    def test_source_archivo_normal_raiz_y_anidado_copian_correctamente(self, tmp_path: pathlib.Path) -> None:
        """Archivos normales en raíz y anidados se copian sin falsos positivos."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "SkyrimSE.exe").write_bytes(b"EXE_BYTES")
        (source / "Data").mkdir()
        (source / "Data" / "mesh.nif").write_bytes(b"MESH_BYTES")

        contenedor = tmp_path / "frozen"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        files = (
            _file_id("SkyrimSE.exe", b"EXE_BYTES"),
            _file_id("Data/mesh.nif", b"MESH_BYTES"),
        )
        directories = ("Data",)

        copiados = copiar_arbol_independiente(source, destino, files, directories, contenedor=contenedor)
        assert copiados == 2
        assert (destino / "SkyrimSE.exe").read_bytes() == b"EXE_BYTES"
        assert (destino / "Data" / "mesh.nif").read_bytes() == b"MESH_BYTES"

    def test_source_identidad_cambia_entre_inspeccion_y_apertura_rechazado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3-R: cambio de identidad del origen entre lstat y open se rechaza."""
        source = tmp_path / "source"
        source.mkdir()
        origen_f = source / "test.bin"
        origen_f.write_bytes(b"ORIGINAL")

        dest = tmp_path / "dest.bin"

        from sky_claw.local.frozen_runtime import copying as copying_mod

        monkeypatch.setattr(copying_mod, "same_file_identity", lambda _st, _fstat: False)

        with pytest.raises(CandidateCopyError, match="identidad.*cambio"):
            copiar_archivo(origen_f, dest)


# ═════════════════════════════════════════════════════════════════════════════
# P3-AC & PATH MATRIX · Caracteres y nombres reservados de Windows
# ═════════════════════════════════════════════════════════════════════════════


class TestPathMatrixP3AC:
    """Matriz de validación de paths Windows: caracteres inválidos y dispositivos DOS."""

    @pytest.mark.parametrize(
        "invalido",
        [
            "Data/a?b.bin",
            "Data/a*b.bin",
            'Data/a"b.bin',
            "Data/a<b.bin",
            "Data/a>b.bin",
            "Data/a|b.bin",
            "Data/?inicio.bin",
            "Data/final*.bin",
        ],
    )
    def test_p3ac_caracteres_invalidos_windows_rechazados_en_membership(self, invalido: str) -> None:
        """Caracteres Windows prohibidos (< > " | ? *) deben rechazarse en membership."""
        with pytest.raises(DirectoryMembershipError, match='Windows|< > " \\| \\? \\*|caracteres no permitidos'):
            canonicalizar_relpath_de_scope(invalido, tipo="archivo")

    @pytest.mark.parametrize(
        "dispositivo",
        [
            "CON",
            "con",
            "Con",
            "PRN",
            "prn",
            "AUX",
            "aux",
            "NUL",
            "nul",
            "CLOCK$",
            "clock$",
            "COM1",
            "com1",
            "COM9",
            "com9",
            "LPT1",
            "lpt1",
            "LPT9",
            "lpt9",
            "CON.txt",
            "con.txt",
            "AUX.nif",
            "aux.nif",
            "COM1.dds",
            "com1.dds",
            "LPT9.bin",
            "lpt9.bin",
            "CLOCK$.dat",
            "Data/CON",
            "Data/CON.txt",
            "Data/aux.nif",
            "Data/COM1.dds",
            "Data/LPT9.bin",
            "Data/Meshes/con.txt",
        ],
    )
    def test_p3ac_dispositivos_reservados_windows_rechazados_en_membership(self, dispositivo: str) -> None:
        """Nombres de dispositivo reservados Windows y sus stems deben rechazarse case-insensitive."""
        with pytest.raises(DirectoryMembershipError, match="dispositivo reservado"):
            canonicalizar_relpath_de_scope(dispositivo, tipo="archivo")

    @pytest.mark.parametrize(
        "valido",
        [
            "Data/Console",
            "Data/console.txt",
            "Data/Auxiliary",
            "Data/auxiliary.nif",
            "Data/COM10",
            "Data/com10.dds",
            "Data/LPT10",
            "Data/lpt10.bin",
            "Data/My File.nif",
            "Data/foo.bar",
            "Data/clock.dat",
            "Data/myCON.txt",
        ],
    )
    def test_p3ac_controles_validos_no_son_rechazados(self, valido: str) -> None:
        """Stems legítimos no deben ser falsos positivos."""
        res = canonicalizar_relpath_de_scope(valido, tipo="archivo")
        assert res == valido.replace("\\", "/")

    def test_p3ac_invalido_windows_en_copia_falla_antes_de_mutar(self, tmp_path: pathlib.Path) -> None:
        """Un path con caracter o dispositivo Windows en el lote de copia falla antes de crear nada."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.bin").write_bytes(b"DATA")

        contenedor = tmp_path / "frozen"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        files = (_file_id("Data/a?b.bin"),)
        with pytest.raises(CandidateCopyError, match="rechazado antes de copiar"):
            copiar_arbol_independiente(source, destino, files, (), contenedor=contenedor)

        assert not destino.exists(), "no debe crearse el payload si un relpath es inválido"


# ═════════════════════════════════════════════════════════════════════════════
# P3-AD & DESTINATION MATRIX · Contención física del padre del destino
# ═════════════════════════════════════════════════════════════════════════════


class TestDestinationMatrixP3AD:
    """Matriz de seguridad del destino: contención del padre y prevención de escapes."""

    def test_p3ad_padre_existente_fuera_del_contenedor_rechazado_antes_de_mkdir(self, tmp_path: pathlib.Path) -> None:
        """P3-AD: si el padre del destino ya existe fuera del contenedor, no debe haber mutación.

        Pre-fix: `if not padre.exists():` saltaba el guard de contención y creaba
        `raiz_destino` fuera del contenedor antes de fallar.
        Post-fix: `CandidateCopyError` y `raiz_destino` NO existe.
        """
        contenedor = tmp_path / "contenedor"
        contenedor.mkdir()

        source = tmp_path / "source"
        source.mkdir()
        (source / "a.bin").write_bytes(b"DATA")

        external_parent = tmp_path / "external_parent"
        external_parent.mkdir()

        destino = external_parent / "payload"
        assert not destino.exists()

        files = (_file_id("a.bin"),)

        with pytest.raises(CandidateCopyError, match="padre.*no cuelga fisicamente|fuera de la ra"):
            copiar_arbol_independiente(source, destino, files, (), contenedor=contenedor)

        assert not destino.exists(), "VULNERABILIDAD P3-AD: el payload fue creado fuera del contenedor"

    @junction_guard
    def test_padre_existente_junction_hacia_afuera_rechazado(self, tmp_path: pathlib.Path) -> None:
        """Si el padre del destino es un junction hacia afuera, debe rechazarse antes de mutar."""
        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates").mkdir(parents=True)

        source = tmp_path / "source"
        source.mkdir()
        (source / "a.bin").write_bytes(b"DATA")

        externo = tmp_path / "externo"
        externo.mkdir()

        junc_padre = contenedor / "candidates" / "cand_junc"
        if (motivo := crear_junction(junc_padre, externo)) is not None:
            pytest.skip(f"no se pudo crear junction: {motivo}")

        destino = junc_padre / "payload"
        files = (_file_id("a.bin"),)

        with pytest.raises(CandidateCopyError, match="enlace|redireccion|no cuelga"):
            copiar_arbol_independiente(source, destino, files, (), contenedor=contenedor)

        assert not (externo / "payload").exists(), "no debe mutarse el árbol externo"

    def test_padre_legitimo_ausente_dentro_del_contenedor_se_crea_correctamente(self, tmp_path: pathlib.Path) -> None:
        """Un padre que no existe todavía dentro del contenedor se crea limpiamente."""
        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates").mkdir(parents=True)

        source = tmp_path / "source"
        source.mkdir()
        (source / "a.bin").write_bytes(b"DATA")

        # cand_new no existe
        destino = contenedor / "candidates" / "cand_new" / "payload"
        files = (_file_id("a.bin"),)

        copiados = copiar_arbol_independiente(source, destino, files, (), contenedor=contenedor)
        assert copiados == 1
        assert (destino / "a.bin").read_bytes() == b"DATA"


# ═════════════════════════════════════════════════════════════════════════════
# P3-AE & BATCH MATRIX · Coherencia integral del lote canónico
# ═════════════════════════════════════════════════════════════════════════════


class TestBatchMatrixP3AE:
    """Matriz de coherencia del lote: duplicados, colisiones cruzadas y ancestros."""

    def test_p3ae_duplicado_canonico_archivos_rechazado_antes_de_mutar(self, tmp_path: pathlib.Path) -> None:
        """P3-AE: dos archivos que canonicalizan a la misma ruta fallan antes de crear el payload.

        Pre-fix: el primer archivo se copiaba y el segundo fallaba en `open('xb')`,
        dejando payload parcial.
        Post-fix: `CandidateCopyError` y el payload no se crea.
        """
        source = tmp_path / "source"
        source.mkdir()
        (source / "Data").mkdir()
        (source / "Data" / "a.bin").write_bytes(b"DATA")

        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        file_1 = _file_id("Data/a.bin")
        file_2 = _file_id("Data/./a.bin")

        with pytest.raises(CandidateCopyError, match="duplicados|coherencia|colision"):
            copiar_arbol_independiente(
                source,
                destino,
                files=(file_1, file_2),
                directories=("Data",),
                contenedor=contenedor,
            )

        assert not destino.exists(), "VULNERABILIDAD P3-AE: se creó payload parcial ante duplicado canónico de archivos"

    def test_p3ae_duplicado_canonico_directorios_rechazado_antes_de_mutar(self, tmp_path: pathlib.Path) -> None:
        """P3-AE: dos directorios que canonicalizan a la misma ruta fallan antes de crear el payload."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "Data").mkdir()
        (source / "Data" / "a.bin").write_bytes(b"DATA")

        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        with pytest.raises(CandidateCopyError, match="duplicados|coherencia|colision"):
            copiar_arbol_independiente(
                source,
                destino,
                files=(_file_id("Data/a.bin"),),
                directories=("Data/Meshes", "Data/./Meshes"),
                contenedor=contenedor,
            )

        assert not destino.exists(), "el payload no debe crearse si hay directorios duplicados"

    def test_p3ae_colision_archivo_y_directorio_misma_ruta_rechazada_antes_de_mutar(
        self, tmp_path: pathlib.Path
    ) -> None:
        """P3-AE: un archivo y un directorio con la misma ruta canónica fallan antes de mutar."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "Data").mkdir()
        (source / "Data" / "Foo").write_bytes(b"DATA")

        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        with pytest.raises(CandidateCopyError, match="colision|coherencia"):
            copiar_arbol_independiente(
                source,
                destino,
                files=(_file_id("Data/Foo"),),
                directories=("Data", "Data/Foo"),
                contenedor=contenedor,
            )

        assert not destino.exists(), "el payload no debe crearse ante colisión archivo-directorio"

    def test_p3ae_archivo_como_ancestro_de_otro_archivo_rechazado_antes_de_mutar(self, tmp_path: pathlib.Path) -> None:
        """P3-AE: un archivo no puede ser ancestro de otro archivo en el lote."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "Data").mkdir()
        (source / "Data" / "Foo").write_bytes(b"DATA")

        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        with pytest.raises(CandidateCopyError, match="ancestro|coherencia|colision"):
            copiar_arbol_independiente(
                source,
                destino,
                files=(_file_id("Data/Foo"), _file_id("Data/Foo/bar.bin")),
                directories=("Data",),
                contenedor=contenedor,
            )

        assert not destino.exists(), "el payload no debe crearse si un archivo es ancestro de otro archivo"

    def test_p3ae_archivo_como_ancestro_de_directorio_rechazado_antes_de_mutar(self, tmp_path: pathlib.Path) -> None:
        """P3-AE: un archivo no puede ser ancestro de un directorio en el lote."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "Data").mkdir()
        (source / "Data" / "Foo").write_bytes(b"DATA")

        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        with pytest.raises(CandidateCopyError, match="ancestro|coherencia|colision"):
            copiar_arbol_independiente(
                source,
                destino,
                files=(_file_id("Data/Foo"),),
                directories=("Data", "Data/Foo/Bar"),
                contenedor=contenedor,
            )

        assert not destino.exists(), "el payload no debe crearse si un archivo es ancestro de un directorio"

    def test_p3ae_anidamiento_legitimo_de_directorios_y_archivos_pasa(self, tmp_path: pathlib.Path) -> None:
        """Directorios anidados legítimos y archivos hermanos deben pasar sin objeción."""
        source = tmp_path / "source"
        source.mkdir()
        (source / "Data" / "Meshes" / "Armor").mkdir(parents=True)
        (source / "Data" / "a.bin").write_bytes(b"A")
        (source / "Data" / "b.bin").write_bytes(b"B")
        (source / "Data" / "Meshes" / "Armor" / "helm.nif").write_bytes(b"HELM")

        contenedor = tmp_path / "contenedor"
        (contenedor / "candidates" / "cand_1").mkdir(parents=True)
        destino = contenedor / "candidates" / "cand_1" / "payload"

        files = (
            _file_id("Data/a.bin", b"A"),
            _file_id("Data/b.bin", b"B"),
            _file_id("Data/Meshes/Armor/helm.nif", b"HELM"),
        )
        directories = ("Data", "Data/Meshes", "Data/Meshes/Armor")

        copiados = copiar_arbol_independiente(source, destino, files, directories, contenedor=contenedor)
        assert copiados == 3
        assert (destino / "Data" / "a.bin").read_bytes() == b"A"
        assert (destino / "Data" / "b.bin").read_bytes() == b"B"
        assert (destino / "Data" / "Meshes" / "Armor" / "helm.nif").read_bytes() == b"HELM"


# ═════════════════════════════════════════════════════════════════════════════
# P3-AF & STORAGE MATRIX · Traducción de excepciones y contratos fail-closed
# ═════════════════════════════════════════════════════════════════════════════


class TestStorageMatrixP3AF:
    """Matriz de excepciones de storage: traducción de OSError a tipos canónicos."""

    def test_p3af_exigir_candidates_state_dir_traduce_oserror_en_mkdir(self, tmp_path: pathlib.Path) -> None:
        """P3-AF: fallo de mkdir en _exigir_candidates_state_dir debe traducirse a FrozenRuntimeStorageError.

        Pre-fix: `directorio.mkdir(parents=True, exist_ok=True)` lanzaba raw `OSError`.
        Post-fix: `FrozenRuntimeStorageError` fail-closed.
        """
        storage.initialize_frozen_runtime_storage(tmp_path)
        state_cand = candidates.candidates_state_dir(tmp_path)
        if state_cand.exists():
            state_cand.rmdir()

        orig_mkdir = pathlib.Path.mkdir

        def fake_mkdir(self: pathlib.Path, *args: object, **kwargs: object) -> None:
            if self == state_cand:
                raise OSError("Disk full / Permission denied")
            orig_mkdir(self, *args, **kwargs)

        with (
            patch.object(pathlib.Path, "mkdir", new=fake_mkdir),
            pytest.raises(FrozenRuntimeStorageError, match="no se pudo crear el directorio de metadata"),
        ):
            _exigir_candidates_state_dir(tmp_path)

    def test_p3af_crear_candidate_devuelve_indeterminate_ante_fallo_de_state_dir(
        self,
        rig: tuple[ManagedSource, pathlib.Path],  # noqa: F811
        parche_identidad: object,  # noqa: F811
    ) -> None:
        """P3-AF: la operación pública crear_candidate no deja escapar OSError crudo y produce INDETERMINATE."""
        source, root = rig
        state_cand = candidates.candidates_state_dir(root)
        if state_cand.exists():
            state_cand.rmdir()

        orig_mkdir = pathlib.Path.mkdir

        def fake_mkdir(self: pathlib.Path, *args: object, **kwargs: object) -> None:
            if self == state_cand:
                raise OSError("I/O error / disk full")
            orig_mkdir(self, *args, **kwargs)

        with patch.object(pathlib.Path, "mkdir", new=fake_mkdir):
            resultado = _crear(source, root)

        assert isinstance(resultado, CandidateResult)
        assert resultado.state is GenerationVerificationState.INDETERMINATE
        assert "no se pudo reservar el candidate_id" in resultado.message
