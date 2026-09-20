"""Tests para la primitive de evidencia nativa de solo lectura (GP2-S1).

Cubre:
- DTO NativeNodeEvidence y jerarquía de excepciones (RuntimeVaultError).
- Transformación determinista file_id_128_to_int (ABI Sky-Claw frozen vectors).
- AST guard anti-mutación: node_evidence.py no contiene APIs mutadoras.
- Tests Windows reales W1-W15 sobre filesystem NTFS real:
  W1: Identidad física real (VolumeSerialNumber, FileId).
  W2: Security Descriptor real (raw bytes, sha256, SIDs, control flags).
  W3: NumberOfLinks == 1 en archivos regulares recién creados.
  W4: Hardlink interno -> DuplicateFileIdError (Fase 2).
  W5: Hardlink externo -> DuplicateFileIdError (Fase 3, ADR 0010).
  W6: Symlink -> InventoryLinkError.
  W7: Junction -> InventoryLinkError.
  W8: Handle de directorio con FILE_FLAG_BACKUP_SEMANTICS.
  W9: Directorio vacío -> 1 nodo con relative_path ".".
  W10: Ordenamiento bottom-up con "." último.
  W11: No perturbar handles preexistentes share-compatibles.
  W12: Binding a HANDLE / anti-TOCTOU (evidencia ligada al handle, no al pathname).
  W13: Cleanup estricto de CloseHandle y LocalFree (regla de memoria).
  W14: Gate de expected_kind contra drift de tipo entre enumeración y apertura.
  W15: Root reparse point rechazado sin resolver.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import os
import pathlib
import sys
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.golden_protection_plan import (
    DuplicateFileIdError,
    GoldenProtectionNodeKind,
    NodeSecurityBackup,
)
from sky_claw.local.runtime_vault.models import InventoryLinkError, RuntimeVaultError
from sky_claw.local.runtime_vault.node_evidence import (
    NativeEvidenceError,
    NativeEvidenceUnsupportedError,
    NativeNodeEvidence,
    _bottom_up_evidence_sort_key,
    _probe_open_handle,
    file_id_128_to_int,
    probe_node_evidence,
)
from tests._symlink_guard import crear_junction, symlink_guard

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


# ============================================================================
# 1. Tests Puros / Cross-Platform
# ============================================================================


class TestNativeNodeEvidencePure:
    """Validación del modelo y excepciones sin requerir Windows."""

    def test_jerarquia_de_excepciones(self) -> None:
        """Verifica que las excepciones deriven de RuntimeVaultError y de NativeEvidenceError."""
        assert issubclass(NativeEvidenceError, RuntimeVaultError)
        assert issubclass(NativeEvidenceUnsupportedError, NativeEvidenceError)
        assert issubclass(InventoryLinkError, RuntimeVaultError)
        assert issubclass(DuplicateFileIdError, RuntimeVaultError)

    def test_file_id_128_to_int_vectores_congelados(self) -> None:
        """Congela la conversión ABI de FILE_ID_128 a uint128 en formato little-endian."""
        # Vector 1: todo ceros
        assert file_id_128_to_int(b"\x00" * 16) == 0

        # Vector 2: bit 0 encendido (byte 0 = 1) -> 1
        assert file_id_128_to_int(b"\x01" + b"\x00" * 15) == 1

        # Vector 3: byte 1 = 1 -> 256
        assert file_id_128_to_int(b"\x00\x01" + b"\x00" * 14) == 256

        # Vector 4: byte 15 = 1 -> 1 << 120
        assert file_id_128_to_int(b"\x00" * 15 + b"\x01") == 1 << 120

        # Vector 5: todo 0xFF -> (1 << 128) - 1
        assert file_id_128_to_int(b"\xff" * 16) == (1 << 128) - 1

        # Vector 6: secuencia conocida 1..16
        seq = bytes(range(1, 17))
        esperado = int.from_bytes(seq, byteorder="little", signed=False)
        assert file_id_128_to_int(seq) == esperado

        # Vector 7: longitud inválida rechazada
        with pytest.raises(ValueError, match="16 bytes"):
            file_id_128_to_int(b"\x00" * 15)
        with pytest.raises(ValueError, match="16 bytes"):
            file_id_128_to_int(b"\x00" * 17)

    def test_dto_native_node_evidence_inmutabilidad(self) -> None:
        """Verifica que NativeNodeEvidence sea inmutable (frozen, slots)."""
        raw_sd = b"dummy_sd_bytes_for_testing"
        backup = NodeSecurityBackup(
            relative_path="data/test.esm",
            node_kind=GoldenProtectionNodeKind.FILE,
            volume_serial_number=12345,
            file_id=67890,
            pre_sd_bytes_b64=base64.b64encode(raw_sd).decode("ascii"),
            pre_sd_length=len(raw_sd),
            pre_sd_sha256=hashlib.sha256(raw_sd).hexdigest(),
            owner_sid="S-1-5-21-1234-5678-9012-1000",
            group_sid="S-1-5-32-544",
            dacl_control_flags=0x1004,
            pre_dacl_protected_flag=True,
            sddl_diagnostic="",
        )
        evidence = NativeNodeEvidence(
            backup=backup,
            number_of_links=1,
            reparse_tag=0,
            file_attributes=0x20,
            delete_pending=False,
        )
        assert evidence.backup == backup
        assert evidence.number_of_links == 1
        assert evidence.reparse_tag == 0
        assert evidence.file_attributes == 0x20
        assert evidence.delete_pending is False

        with pytest.raises((AttributeError, TypeError)):
            evidence.number_of_links = 2  # type: ignore[misc]

    def test_sort_key_bottom_up_ordenamiento(self) -> None:
        """Verifica que el sort key coloque las hojas más profundas primero y '.' estrictamente al final."""
        raw_sd = b"dummy_sd_bytes"

        def _make_ev(rel_path: str, kind: GoldenProtectionNodeKind) -> NativeNodeEvidence:
            b = NodeSecurityBackup(
                relative_path=rel_path,
                node_kind=kind,
                volume_serial_number=1,
                file_id=hash(rel_path) & 0xFFFFFFFF,
                pre_sd_bytes_b64=base64.b64encode(raw_sd).decode("ascii"),
                pre_sd_length=len(raw_sd),
                pre_sd_sha256=hashlib.sha256(raw_sd).hexdigest(),
                owner_sid="S-1-5-21-1234-5678-9012-1000",
                group_sid="S-1-5-32-544",
                dacl_control_flags=0,
                pre_dacl_protected_flag=False,
            )
            return NativeNodeEvidence(
                backup=b,
                number_of_links=1,
                reparse_tag=0,
                file_attributes=0x10 if kind == GoldenProtectionNodeKind.DIR else 0x20,
                delete_pending=False,
            )

        desordenados = [
            _make_ev(".", GoldenProtectionNodeKind.DIR),
            _make_ev("a", GoldenProtectionNodeKind.DIR),
            _make_ev("a/b/c/leaf.txt", GoldenProtectionNodeKind.FILE),
            _make_ev("a/b", GoldenProtectionNodeKind.DIR),
            _make_ev("a/file1.txt", GoldenProtectionNodeKind.FILE),
            _make_ev("a/b/file2.txt", GoldenProtectionNodeKind.FILE),
        ]

        ordenados = sorted(desordenados, key=_bottom_up_evidence_sort_key)
        paths_ordenados = [e.backup.relative_path for e in ordenados]

        assert paths_ordenados == [
            "a/b/c/leaf.txt",  # profundidad 3
            "a/b/file2.txt",  # profundidad 2
            "a/b",  # profundidad 2
            "a/file1.txt",  # profundidad 1
            "a",  # profundidad 1
            ".",  # profundidad 0 (último)
        ]
        assert paths_ordenados[-1] == "."

    def test_plataforma_no_windows_produce_unsupported_error(self) -> None:
        """Si la plataforma simulada no es win32, probe_node_evidence debe fallar con NativeEvidenceUnsupportedError."""
        with (
            patch("sys.platform", "linux"),
            pytest.raises(NativeEvidenceUnsupportedError, match="Windows"),
        ):
            probe_node_evidence(pathlib.Path("."))


# ============================================================================
# 2. Test Anti-Mutación (AST Guard)
# ============================================================================


class TestAntiMutationASTGuard:
    """Verifica mediante inspección AST que node_evidence.py no contenga primitivas de mutación."""

    def test_node_evidence_no_contiene_primitivas_mutadoras(self) -> None:
        """Analiza el AST de node_evidence.py para certificar que es estrictamente read-only."""
        modulo_path = pathlib.Path(__file__).parent.parent / "sky_claw" / "local" / "runtime_vault" / "node_evidence.py"
        assert modulo_path.is_file(), f"El archivo {modulo_path} debe existir"

        tree = ast.parse(modulo_path.read_text(encoding="utf-8"), filename=str(modulo_path))

        prohibidas = {
            "SetSecurityInfo",
            "SetNamedSecurityInfoW",
            "SetFileSecurity",
            "SetFileSecurityW",
            "AdjustTokenPrivileges",
            "ShellExecuteExW",
            "ShellExecuteW",
            "CreateHardLinkW",
            "CreateSymbolicLinkW",
            "DeleteFileW",
            "MoveFileW",
            "SetFileInformationByHandle",
            "chmod",
            "chown",
            "icacls",
            "takeown",
            "subprocess",
            "popen",
            "system",
            "ConvertSecurityDescriptorToStringSecurityDescriptorW",  # Tech Lead point 5: sin SDDL en S1
        }

        nombres_encontrados: set[str] = set()

        for nodo in ast.walk(tree):
            if isinstance(nodo, ast.Name) and nodo.id in prohibidas:
                nombres_encontrados.add(nodo.id)
            elif isinstance(nodo, ast.Attribute) and nodo.attr in prohibidas:
                nombres_encontrados.add(nodo.attr)
            elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                for p in prohibidas:
                    if p == nodo.value:
                        nombres_encontrados.add(nodo.value)
            elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
                for alias in getattr(nodo, "names", []):
                    if alias.name in prohibidas:
                        nombres_encontrados.add(alias.name)
                if isinstance(nodo, ast.ImportFrom) and nodo.module in prohibidas:
                    nombres_encontrados.add(nodo.module)

        assert not nombres_encontrados, f"Se encontraron primitivas o referencias prohibidas: {nombres_encontrados}"


# ============================================================================
# 3. Tests Windows Reales (W1 - W15)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Tests Win32 nativos específicos de Windows")
class TestNativeNodeEvidenceWindowsReal:
    """Suite de tests autoritativos sobre Windows real con filesystem NTFS."""

    @pytest.fixture
    def tree_descartable(self) -> pathlib.Path:
        """Crea un directorio temporal dentro del volumen actual para garantizar NTFS."""
        with tempfile.TemporaryDirectory(dir=".") as td:
            base = pathlib.Path(os.path.abspath(td))
            yield base

    def test_w1_identidad_real(self, tree_descartable: pathlib.Path) -> None:
        """W1 — VolumeSerialNumber consistente dentro del volumen y FileId != 0 único por archivo."""
        f1 = tree_descartable / "archivo1.dat"
        f1.write_text("datos1", encoding="utf-8")
        f2 = tree_descartable / "archivo2.dat"
        f2.write_text("datos2", encoding="utf-8")
        subdir = tree_descartable / "sub"
        subdir.mkdir()
        f3 = subdir / "archivo3.dat"
        f3.write_text("datos3", encoding="utf-8")

        evidencias = probe_node_evidence(tree_descartable)
        assert len(evidencias) == 5  # root, f1, f2, sub, f3

        vol_serials = {e.backup.volume_serial_number for e in evidencias}
        assert len(vol_serials) == 1, "Todos los nodos deben compartir exactamente el mismo VolumeSerialNumber"
        vol = vol_serials.pop()
        assert vol > 0, "VolumeSerialNumber debe ser un entero positivo mayor a 0"

        file_ids = [e.backup.file_id for e in evidencias]
        assert all(fid > 0 for fid in file_ids), "Todos los FileId deben ser mayores a cero"
        assert len(set(file_ids)) == len(file_ids), "Cada nodo debe tener un FileId único en ausencia de hardlinks"

    def test_w2_security_descriptor_real(self, tree_descartable: pathlib.Path) -> None:
        """W2 — Security Descriptor real: base64 decode, length, sha256, owner SID, group SID y coherencia."""
        f = tree_descartable / "seguridad.dat"
        f.write_text("contenido para SD", encoding="utf-8")

        ev1 = probe_node_evidence(tree_descartable)
        ev2 = probe_node_evidence(tree_descartable)

        nodo1 = next(e for e in ev1 if e.backup.relative_path == "seguridad.dat")
        nodo2 = next(e for e in ev2 if e.backup.relative_path == "seguridad.dat")

        b1 = nodo1.backup
        b2 = nodo2.backup

        # Validación interna de integridad del NodeSecurityBackup
        sd_bytes = base64.b64decode(b1.pre_sd_bytes_b64)
        assert len(sd_bytes) == b1.pre_sd_length
        assert hashlib.sha256(sd_bytes).hexdigest() == b1.pre_sd_sha256

        # SIDs canónicos válidos
        assert b1.owner_sid.startswith("S-1-")
        assert b1.group_sid.startswith("S-1-")

        # Control flags válidos
        assert 0 <= b1.dacl_control_flags <= 0xFFFF
        assert b1.pre_dacl_protected_flag == bool(b1.dacl_control_flags & 0x1000)

        # Coherencia entre dos capturas sucesivas sin mutación
        assert b1.volume_serial_number == b2.volume_serial_number
        assert b1.file_id == b2.file_id
        assert b1.owner_sid == b2.owner_sid
        assert b1.group_sid == b2.group_sid
        assert b1.pre_sd_sha256 == b2.pre_sd_sha256
        assert b1.pre_sd_bytes_b64 == b2.pre_sd_bytes_b64

    def test_w3_number_of_links_normal(self, tree_descartable: pathlib.Path) -> None:
        """W3 — Archivo recién creado tiene NumberOfLinks == 1. Directorios tienen links >= 1."""
        archivo = tree_descartable / "nuevo.txt"
        archivo.write_text("links test", encoding="utf-8")
        sub = tree_descartable / "subdir"
        sub.mkdir()

        evidencias = probe_node_evidence(tree_descartable)
        ev_file = next(e for e in evidencias if e.backup.relative_path == "nuevo.txt")
        ev_sub = next(e for e in evidencias if e.backup.relative_path == "subdir")
        ev_root = next(e for e in evidencias if e.backup.relative_path == ".")

        assert ev_file.number_of_links == 1
        assert ev_sub.number_of_links >= 1
        assert ev_root.number_of_links >= 1

    def test_w4_hardlink_interno(self, tree_descartable: pathlib.Path) -> None:
        """W4 — Hardlink interno: dos rutas dentro del Golden comparten FileId -> DuplicateFileIdError."""
        a = tree_descartable / "A.dat"
        a.write_text("hardlink interno", encoding="utf-8")
        b = tree_descartable / "B.dat"
        os.link(a, b)

        # La Fase 2 debe detectar los FileIds duplicados en el NodeSet y lanzar DuplicateFileIdError
        with pytest.raises(DuplicateFileIdError, match="comparten.*FileId"):
            probe_node_evidence(tree_descartable)

    def test_w5_hardlink_externo(self, tree_descartable: pathlib.Path) -> None:
        """W5 — Hardlink externo: archivo con NumberOfLinks != 1 produce DuplicateFileIdError (ADR 0010)."""
        with tempfile.TemporaryDirectory(dir=".") as td_outside:
            outside_dir = pathlib.Path(os.path.abspath(td_outside))
            internal_file = tree_descartable / "internal.dat"
            internal_file.write_text("hardlink externo", encoding="utf-8")
            outside_file = outside_dir / "outside.dat"

            os.link(internal_file, outside_file)

            # En el árbol tree_descartable el FileId es único, pero NumberOfLinks == 2
            # La Fase 3 debe detectar NumberOfLinks != 1 y lanzar DuplicateFileIdError según ADR 0010
            with pytest.raises(DuplicateFileIdError, match="NumberOfLinks=2.*hardlink externo"):
                probe_node_evidence(tree_descartable)

    def test_w6_symlink(self, tree_descartable: pathlib.Path) -> None:
        """W6 — Symlink real dentro del árbol es detectado y produce InventoryLinkError."""
        if not symlink_guard:
            pytest.skip("Crear symlinks requiere privilegios elevados o Developer Mode en Windows")

        target = tree_descartable / "target.txt"
        target.write_text("target", encoding="utf-8")
        link = tree_descartable / "link.txt"
        try:
            link.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"No se pudo crear symlink: {exc}")

        with pytest.raises(InventoryLinkError, match="[Ee]nlace|[Rr]eparse point"):
            probe_node_evidence(tree_descartable)

    def test_w7_junction(self, tree_descartable: pathlib.Path) -> None:
        """W7 — Junction real (mklink /J) dentro del árbol produce InventoryLinkError."""
        target_dir = tree_descartable / "target_dir"
        target_dir.mkdir()
        (target_dir / "inside.txt").write_text("hello", encoding="utf-8")
        junc_dir = tree_descartable / "junc_link"

        err = crear_junction(junc_dir, target_dir)
        assert err is None, f"Fallo al crear junction con mklink /J: {err}"

        with pytest.raises(InventoryLinkError, match="[Ee]nlace|[Rr]eparse point"):
            probe_node_evidence(tree_descartable)

    def test_w8_directory_handle(self, tree_descartable: pathlib.Path) -> None:
        """W8 — Directorios son observados correctamente con FILE_FLAG_BACKUP_SEMANTICS."""
        sub = tree_descartable / "un_directorio"
        sub.mkdir()
        sub_sub = sub / "otro_directorio"
        sub_sub.mkdir()

        evidencias = probe_node_evidence(tree_descartable)
        relpaths = {e.backup.relative_path for e in evidencias}
        assert "." in relpaths
        assert "un_directorio" in relpaths
        assert "un_directorio/otro_directorio" in relpaths

        for e in evidencias:
            if e.backup.relative_path in (".", "un_directorio", "un_directorio/otro_directorio"):
                assert e.backup.node_kind is GoldenProtectionNodeKind.DIR

    def test_w9_empty_tree(self, tree_descartable: pathlib.Path) -> None:
        """W9 — Directorio vacío produce exactamente 1 nodo con relative_path='.'."""
        evidencias = probe_node_evidence(tree_descartable)
        assert len(evidencias) == 1
        assert evidencias[0].backup.relative_path == "."
        assert evidencias[0].backup.node_kind is GoldenProtectionNodeKind.DIR

    def test_w10_orden_bottom_up(self, tree_descartable: pathlib.Path) -> None:
        """W10 — El resultado está estrictamente ordenado bottom-up y '.' queda último."""
        d1 = tree_descartable / "dir1"
        d1.mkdir()
        d2 = d1 / "dir2"
        d2.mkdir()
        (d2 / "file_deep.txt").write_text("deep", encoding="utf-8")
        (d1 / "file_mid.txt").write_text("mid", encoding="utf-8")
        (tree_descartable / "file_root.txt").write_text("root_lvl", encoding="utf-8")

        evidencias = probe_node_evidence(tree_descartable)
        paths = [e.backup.relative_path for e in evidencias]

        # Comprobar que '.' es el último nodo absoluto
        assert paths[-1] == "."

        # Comprobar que file_deep.txt (profundidad 3) precede a dir1/dir2 (profundidad 2)
        assert paths.index("dir1/dir2/file_deep.txt") < paths.index("dir1/dir2")
        # Comprobar que dir1/dir2 precede a dir1
        assert paths.index("dir1/dir2") < paths.index("dir1")
        # Comprobar que file_root.txt precede a "."
        assert paths.index("file_root.txt") < paths.index(".")

    def test_w11_no_perturbar_handle_existente(self, tree_descartable: pathlib.Path) -> None:
        """W11 — Abrir handle con share modes compatibles y verificar que probe no lo perturba."""
        archivo = tree_descartable / "abierto.dat"
        archivo.write_text("datos iniciales", encoding="utf-8")

        import ctypes
        from ctypes import wintypes

        from sky_claw.local.runtime_vault import node_evidence

        kernel32 = node_evidence._kernel32
        kernel32.SetFilePointer.argtypes = [
            wintypes.HANDLE,
            wintypes.LONG,
            ctypes.POINTER(wintypes.LONG),
            wintypes.DWORD,
        ]
        kernel32.SetFilePointer.restype = wintypes.DWORD
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL

        h = kernel32.CreateFileW(
            str(archivo),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            1 | 2 | 4,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        assert h != -1 and h != 0, f"No se pudo abrir handle inicial: {ctypes.get_last_error()}"

        try:
            # Ejecutar el probe; no debe fallar por sharing violation
            evidencias = probe_node_evidence(tree_descartable)
            assert any(e.backup.relative_path == "abierto.dat" for e in evidencias)

            # Verificar que el handle original continúa siendo utilizable para lectura
            buf = ctypes.create_string_buffer(100)
            bytes_read = wintypes.DWORD()
            # Mover puntero al inicio
            kernel32.SetFilePointer(h, 0, None, 0)
            res = kernel32.ReadFile(h, buf, 100, ctypes.byref(bytes_read), None)
            assert res != 0, f"ReadFile falló tras el probe: {ctypes.get_last_error()}"
            assert bytes_read.value > 0
        finally:
            kernel32.CloseHandle(h)

    def test_w12_binding_por_handle_anti_toctou(self, tree_descartable: pathlib.Path) -> None:
        """W12 — Demuestra que la evidencia del handle pertenece al objeto abierto y no al pathname posterior."""
        from sky_claw.local.runtime_vault import node_evidence

        kernel32 = node_evidence._kernel32

        archivo_a = tree_descartable / "archivo_a.txt"
        archivo_a.write_text("primer objeto original", encoding="utf-8")

        # 1. Abrir handle sobre A
        h_a = kernel32.CreateFileW(
            str(archivo_a),
            0x80 | 0x00020000,  # FILE_READ_ATTRIBUTES | READ_CONTROL
            1 | 2 | 4,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        assert h_a != -1 and h_a != 0

        try:
            # 2. Renombrar archivo_a a archivo_a_temp y crear un nuevo archivo_a con diferente contenido
            archivo_temp = tree_descartable / "archivo_a_temp.txt"
            os.replace(archivo_a, archivo_temp)
            archivo_a.write_text("segundo objeto reemplazado", encoding="utf-8")

            # 3. Invocar _probe_open_handle sobre el handle original
            ev = _probe_open_handle(h_a, "archivo_a.txt", expected_kind=GoldenProtectionNodeKind.FILE)

            # 4. Obtener la identidad del nuevo archivo en el path mediante un probe separado
            h_nuevo = kernel32.CreateFileW(
                str(archivo_a),
                0x80 | 0x00020000,
                1 | 2 | 4,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            assert h_nuevo != -1 and h_nuevo != 0
            try:
                ev_nuevo = _probe_open_handle(h_nuevo, "archivo_a.txt", expected_kind=GoldenProtectionNodeKind.FILE)
            finally:
                kernel32.CloseHandle(h_nuevo)

            # Demostrar causalmente que el handle h_a mantuvo el FileId del objeto original
            assert ev.backup.file_id != ev_nuevo.backup.file_id, (
                "El handle retenido debe identificar el objeto original, no el reemplazado en el pathname"
            )
        finally:
            kernel32.CloseHandle(h_a)

    def test_w13_cleanup_recursos_win32(self, tree_descartable: pathlib.Path) -> None:
        """W13 — Verifica que CloseHandle y LocalFree sean invocados exhaustivamente tanto en éxito como en fallo."""
        (tree_descartable / "archivo.txt").write_text("cleanup test", encoding="utf-8")

        import ctypes
        from ctypes import wintypes

        from sky_claw.local.runtime_vault import node_evidence

        kernel32 = node_evidence._kernel32

        handles_abiertos: list[int] = []
        handles_cerrados: list[int] = []
        ptrs_liberados: list[int] = []

        orig_create_file = kernel32.CreateFileW
        orig_close_handle = kernel32.CloseHandle
        orig_local_free = kernel32.LocalFree

        def mock_create_file(*args: Any) -> int:
            h = orig_create_file(*args)
            if h != -1 and h != 0:
                handles_abiertos.append(h)
            return h

        def mock_close_handle(h: int) -> int:
            handles_cerrados.append(h)
            return orig_close_handle(h)

        def mock_local_free(ptr: Any) -> Any:
            val = ctypes.cast(ptr, wintypes.LPVOID).value
            if val:
                ptrs_liberados.append(val)
            return orig_local_free(ptr)

        with (
            patch.object(kernel32, "CreateFileW", side_effect=mock_create_file),
            patch.object(kernel32, "CloseHandle", side_effect=mock_close_handle),
            patch.object(kernel32, "LocalFree", side_effect=mock_local_free),
        ):
            # Ruta de éxito
            res = probe_node_evidence(tree_descartable)
            assert len(res) == 2

            # Todos los handles abiertos deben haberse cerrado
            assert len(handles_abiertos) > 0
            assert set(handles_abiertos).issubset(set(handles_cerrados))
            assert len(ptrs_liberados) > 0, "LocalFree debe haberse llamado para los Security Descriptors y SIDs"

        # Ruta de fallo: inyectar un error en el segundo nodo para verificar cleanup en excepción
        handles_abiertos.clear()
        handles_cerrados.clear()
        ptrs_liberados.clear()

        (tree_descartable / "segundo.txt").write_text("fallará", encoding="utf-8")

        with (
            patch.object(kernel32, "CreateFileW", side_effect=mock_create_file),
            patch.object(kernel32, "CloseHandle", side_effect=mock_close_handle),
            patch.object(kernel32, "LocalFree", side_effect=mock_local_free),
            patch("sky_claw.local.runtime_vault.node_evidence._probe_open_handle", side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                probe_node_evidence(tree_descartable)

            # Aun con excepción, los handles abiertos deben haberse cerrado
            assert set(handles_abiertos).issubset(set(handles_cerrados))

    def test_w14_drift_tipo_con_expected_kind(self, tree_descartable: pathlib.Path) -> None:
        """W14 — expected_kind actúa como gate real: si un archivo es reemplazado por directorio, falla cerrado."""
        from sky_claw.local.runtime_vault import node_evidence

        kernel32 = node_evidence._kernel32

        d = tree_descartable / "directorio_real"
        d.mkdir()

        h = kernel32.CreateFileW(
            str(d),
            0x80 | 0x00020000,
            1 | 2 | 4,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        assert h != -1 and h != 0
        try:
            # Esperamos FILE pero es DIR -> FAIL CLOSED
            with pytest.raises(NativeEvidenceError, match="[Dd]rift de tipo"):
                _probe_open_handle(h, "directorio_real", expected_kind=GoldenProtectionNodeKind.FILE)
        finally:
            kernel32.CloseHandle(h)

    def test_w15_root_reparse_point_rechazado_sin_resolve(self, tree_descartable: pathlib.Path) -> None:
        """W15 — Root como reparse point es rechazado inmediatamente sin resolver a su destino."""
        target_dir = tree_descartable / "target_root"
        target_dir.mkdir()
        (target_dir / "child.txt").write_text("inside", encoding="utf-8")
        junc_root = tree_descartable / "junc_root"

        err = crear_junction(junc_root, target_dir)
        assert err is None, f"Fallo al crear junction: {err}"

        # Probar el junction como root: debe fallar con InventoryLinkError sin resolver
        with pytest.raises(InventoryLinkError, match="[Ee]nlace|[Rr]eparse point"):
            probe_node_evidence(junc_root)

    def test_w16_ancestor_directory_replacement_race(self, tree_descartable: pathlib.Path) -> None:
        """W16 — Demuestra que la contención de directorios (sin FILE_SHARE_DELETE) bloquea
        la sustitución de un ancestro por un junction durante el recorrido (anti-TOCTOU causal).
        """
        from sky_claw.local.runtime_vault import node_evidence

        subdir = tree_descartable / "sub"
        subdir.mkdir()
        leaf = subdir / "leaf.txt"
        leaf.write_text("datos seguros", encoding="utf-8")

        with tempfile.TemporaryDirectory(dir=tree_descartable.parent) as td_outside:
            outside_dir = pathlib.Path(td_outside)
            (outside_dir / "external.txt").write_text("datos fuera del golden", encoding="utf-8")

            # 1. Probar directamente la primitiva: mientras el containment handle está abierto
            # sin FILE_SHARE_DELETE, intentar renombrar/reemplazar el directorio debe fallar con PermissionError.
            h_guard = node_evidence._open_containment_handle(subdir)
            assert h_guard != -1 and h_guard != 0
            try:
                sub_temp = tree_descartable / "sub_temp"
                with pytest.raises(PermissionError):
                    os.replace(subdir, sub_temp)
            finally:
                node_evidence._kernel32.CloseHandle(h_guard)

            # 2. Carrera simulada durante el traversal real:
            # Interceptar os.scandir justo cuando evalúa subdir para intentar reemplazarlo por un junction
            # hacia outside_dir. La contención activa debe impedirlo.
            replacement_attempted = False
            replacement_blocked = False

            orig_scandir = os.scandir

            def guarded_scandir(path: Any) -> Any:
                nonlocal replacement_attempted, replacement_blocked
                p = pathlib.Path(path)
                if p == subdir:
                    replacement_attempted = True
                    # Intentar renombrar subdir para sustituirlo
                    try:
                        os.replace(subdir, tree_descartable / "sub_renamed")
                    except PermissionError:
                        replacement_blocked = True
                return orig_scandir(path)

            with patch("os.scandir", side_effect=guarded_scandir):
                evidencias = probe_node_evidence(tree_descartable)

            assert replacement_attempted, "El gancho debió ejecutarse durante el escaneo de subdir"
            assert replacement_blocked, "El reemplazo de subdir debió ser bloqueado por el containment handle"

            # 3. S1 nunca observa nodos del target externo
            rel_paths = {e.backup.relative_path for e in evidencias}
            assert rel_paths == {".", "sub", "sub/leaf.txt"}
            assert "external.txt" not in rel_paths

    def test_root_inexistente_o_no_directorio_falla_cerrado(self, tree_descartable: pathlib.Path) -> None:
        """Verifica que root inexistente o archivo regular como root falle con NativeEvidenceError."""
        inexistente = tree_descartable / "no_existe"
        with pytest.raises(NativeEvidenceError, match="no existe"):
            probe_node_evidence(inexistente)

        archivo = tree_descartable / "archivo.txt"
        archivo.write_text("no es dir", encoding="utf-8")
        with pytest.raises(NativeEvidenceError, match="no es un directorio"):
            probe_node_evidence(archivo)

    def test_compatibilidad_con_prepare_golden_protection_plan(self, tree_descartable: pathlib.Path) -> None:
        """Demuestra que la evidencia producida por probe_node_evidence alimenta prepare_golden_protection_plan."""
        from sky_claw.local.runtime_vault.golden_protection_plan import (
            GoldenProtectionSealChecks,
            prepare_golden_protection_plan,
        )
        from sky_claw.local.runtime_vault.models import (
            GoldenMasterDescriptor,
            GoldenMasterVerificationResult,
            RuntimeIdentity,
            RuntimeVerificationResult,
            TreeDigest,
            TreeVerificationResult,
            VerificationState,
        )
        from sky_claw.local.runtime_vault.protection import (
            GoldenProtectionEvidence,
            GoldenProtectionRight,
            NodeProtectionObservation,
            classify_protection,
        )

        f1 = tree_descartable / "test.esm"
        f1.write_text("mod data", encoding="utf-8")

        evidencias = probe_node_evidence(tree_descartable)
        assert len(evidencias) == 2

        # Convertir a tupla de NodeSecurityBackup para GP2
        nodes_backup = tuple(e.backup for e in evidencias)
        root_backup = evidencias[-1].backup
        assert root_backup.relative_path == "."

        # Mock de verificación RV-2
        tree = TreeDigest(digest="b" * 64, files=1, bytes=len("mod data"))
        runtime = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        golden_verif = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=TreeVerificationResult(
                state=VerificationState.VERIFIED,
                expected=tree,
                observed=tree,
            ),
            runtime_result=RuntimeVerificationResult(
                state=VerificationState.VERIFIED,
                expected=runtime,
                observed=runtime,
            ),
            descriptor=GoldenMasterDescriptor(
                location=tree_descartable,
                runtime_identity=runtime,
                tree_digest=tree,
            ),
        )

        # Mock de clasificación GP1 congruente con los nodos observados (estado inicial UNPROTECTED)
        read_only = frozenset({GoldenProtectionRight.READ_DATA})
        write_rights = frozenset({GoldenProtectionRight.READ_DATA, GoldenProtectionRight.WRITE_DATA})
        observed_nodes = (
            NodeProtectionObservation(
                relative_path=".",
                node_kind="dir",
                owner_sid=root_backup.owner_sid,
                granted_rights=read_only,
                owner_rights_ace_present=True,
                dacl_inheritance_protected=True,
            ),
            NodeProtectionObservation(
                relative_path="test.esm",
                node_kind="file",
                owner_sid=nodes_backup[0].owner_sid,
                granted_rights=write_rights,
                owner_rights_ace_present=True,
                dacl_inheritance_protected=True,
            ),
        )
        gp1_evidence = GoldenProtectionEvidence(
            platform="windows",
            drive_type=3,
            filesystem="NTFS",
            filesystem_persistent_acls=True,
            current_user_sid="S-1-5-21-9999",
            current_group_sids=frozenset(),
            current_token_elevated=False,
            mutation_privileges_present=frozenset(),
            diagnostic_privileges_present=frozenset(),
            nodes=observed_nodes,
            parent_observation=NodeProtectionObservation(
                relative_path="..",
                node_kind="dir",
                owner_sid="S-1-5-21-9999",
                granted_rights=read_only,
                owner_rights_ace_present=True,
                dacl_inheritance_protected=True,
            ),
            pre_post_structural_match=True,
            observation_error=None,
        )
        protection_result = classify_protection(gp1_evidence)

        seal_checks = GoldenProtectionSealChecks(
            reparse_points_absent=True,
            external_hardlinks_absent=True,
            post_inventory_structural_match=True,
        )

        # Invocar prepare_golden_protection_plan con la evidencia nativa real
        plan = prepare_golden_protection_plan(
            golden_verification=golden_verif,
            protection_result=protection_result,
            operation_id="123e4567-e89b-12d3-a456-426614174000",
            canonical_root=tree_descartable,
            volume_serial_number=root_backup.volume_serial_number,
            root_file_id=root_backup.file_id,
            nodes=nodes_backup,
            seal_checks=seal_checks,
        )

        assert plan.node_count == 2
        assert plan.volume_serial_number == root_backup.volume_serial_number
        assert plan.root_file_id == root_backup.file_id
