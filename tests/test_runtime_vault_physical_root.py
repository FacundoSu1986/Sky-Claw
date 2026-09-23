"""Tests unitarios y causales Win32 para la validación física de la raíz (physical_root).

Contratos verificados:
- derive_physical_root: abre de forma segura (sin Path.resolve() previo), verifica ReparseTag == 0
  y deriva (canonical_root, VolumeSerialNumber, root_file_id).
- verify_physical_root: exige coincidencia exacta contra expectativa autorizada.
- Mismatch en canonical_root, VolumeSerialNumber o root_file_id produce PhysicalRootMismatchError.
- Reparse points en la raíz producen PhysicalRootReparseError antes de cualquier lectura.
- En POSIX: PrivilegedBoundaryUnsupportedError.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import sys
from typing import Any
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.physical_root import (
    PhysicalRootError,
    PhysicalRootIdentity,
    PhysicalRootIdentityUnavailableError,
    PhysicalRootMismatchError,
    PhysicalRootReparseError,
    derive_physical_root,
    verify_physical_root,
)


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas causales físicas Win32")
class TestWin32PhysicalRootCausal:
    """Verificación causal de identidad física sobre el sistema de archivos real de Windows."""

    def test_derive_physical_root_directorio_real(self, tmp_path: pathlib.Path) -> None:
        """Deriva identidad física de un directorio real sin reparse points."""
        d = tmp_path / "valid_root"
        d.mkdir()

        identity = derive_physical_root(d)
        assert isinstance(identity, PhysicalRootIdentity)
        assert identity.volume_serial_number > 0
        assert identity.root_file_id > 0
        assert os.path.exists(identity.canonical_root)
        assert identity.canonical_root.lower() == str(d).lower()

    def test_verify_physical_root_coincidencia_exacta(self, tmp_path: pathlib.Path) -> None:
        """verify_physical_root pasa cuando la identidad física coincide exactamente."""
        d = tmp_path / "verify_match"
        d.mkdir()

        derived = derive_physical_root(d)
        verified = verify_physical_root(d, derived)
        assert verified == derived

    def test_verify_physical_root_mismatch_serial(self, tmp_path: pathlib.Path) -> None:
        """verify_physical_root rechaza VolumeSerialNumber distinto."""
        d = tmp_path / "mismatch_serial"
        d.mkdir()

        derived = derive_physical_root(d)
        tampered = PhysicalRootIdentity(
            canonical_root=derived.canonical_root,
            volume_serial_number=derived.volume_serial_number + 1,
            root_file_id=derived.root_file_id,
        )
        with pytest.raises(PhysicalRootMismatchError, match="VolumeSerialNumber mismatch"):
            verify_physical_root(d, tampered)

    def test_verify_physical_root_mismatch_file_id(self, tmp_path: pathlib.Path) -> None:
        """verify_physical_root rechaza root_file_id distinto."""
        d = tmp_path / "mismatch_file_id"
        d.mkdir()

        derived = derive_physical_root(d)
        tampered = PhysicalRootIdentity(
            canonical_root=derived.canonical_root,
            volume_serial_number=derived.volume_serial_number,
            root_file_id=derived.root_file_id + 1,
        )
        with pytest.raises(PhysicalRootMismatchError, match="root_file_id mismatch"):
            verify_physical_root(d, tampered)

    def test_verify_physical_root_mismatch_path(self, tmp_path: pathlib.Path) -> None:
        """verify_physical_root rechaza canonical_root distinto."""
        d1 = tmp_path / "dir1"
        d2 = tmp_path / "dir2"
        d1.mkdir()
        d2.mkdir()

        derived1 = derive_physical_root(d1)
        with pytest.raises(PhysicalRootMismatchError, match="canonical_root mismatch"):
            verify_physical_root(d2, derived1)

    def test_reparse_point_rechazado_inmediatamente(self, tmp_path: pathlib.Path) -> None:
        """Un junction o symlink en la raíz es detectado por ReparseTag != 0 y rechazado fail-closed."""
        target = tmp_path / "target_dir"
        target.mkdir()
        link = tmp_path / "reparse_dir"

        # Intentar crear un junction o directory symlink en Windows
        import _winapi

        try:
            _winapi.CreateJunction(str(target), str(link))
        except (OSError, AttributeError):
            pytest.skip("No se pudo crear junction de prueba en este host")

        with pytest.raises(PhysicalRootReparseError, match="ReparseTag != 0"):
            derive_physical_root(link)

        with pytest.raises(PhysicalRootReparseError, match="ReparseTag != 0"):
            fake_expected = PhysicalRootIdentity(
                canonical_root=str(link),
                volume_serial_number=1,
                root_file_id=1,
            )
            verify_physical_root(link, fake_expected)

    def test_directorio_inexistente_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """Apertura de ruta inexistente produce PhysicalRootError."""
        nonexistent = tmp_path / "no_existe"
        with pytest.raises(PhysicalRootError):
            derive_physical_root(nonexistent)

    def test_archivo_regular_no_es_directorio_raiz(self, tmp_path: pathlib.Path) -> None:
        """Un archivo regular pasado como raíz es rechazado tipado."""
        f = tmp_path / "un_archivo.txt"
        f.write_text("datos", encoding="utf-8")
        with pytest.raises(PhysicalRootError, match="no es un directorio"):
            derive_physical_root(f)

    def test_file_id_info_no_disponible_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """Si GetFileInformationByHandleEx(FileIdInfo) no está soportado o falla, emite PhysicalRootIdentityUnavailableError."""
        import sky_claw.local.runtime_vault.physical_root as pr_mod

        real_fn = pr_mod._kernel32.GetFileInformationByHandleEx

        def mock_get_info(handle: Any, info_class: int, p_info: Any, size: int) -> int:
            if info_class == pr_mod._FILE_INFO_BY_HANDLE_CLASS_ID:
                return 0
            return real_fn(handle, info_class, p_info, size)

        with (
            patch.object(pr_mod._kernel32, "GetFileInformationByHandleEx", side_effect=mock_get_info),
            pytest.raises(PhysicalRootIdentityUnavailableError, match="FileIdInfo"),
        ):
            derive_physical_root(tmp_path)

    def test_file_id_o_serial_nulo_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """Si FileId o VolumeSerialNumber es 0, emite PhysicalRootIdentityUnavailableError."""
        import sky_claw.local.runtime_vault.physical_root as pr_mod

        real_fn = pr_mod._kernel32.GetFileInformationByHandleEx

        def mock_get_info(handle: Any, info_class: int, p_info: Any, size: int) -> int:
            ret = real_fn(handle, info_class, p_info, size)
            if info_class == pr_mod._FILE_INFO_BY_HANDLE_CLASS_ID and ret != 0:
                p_cast = ctypes.cast(p_info, ctypes.POINTER(pr_mod._FileIdInfo))
                ctypes.memset(ctypes.byref(p_cast.contents.FileId), 0, 16)
            return ret

        with (
            patch.object(pr_mod._kernel32, "GetFileInformationByHandleEx", side_effect=mock_get_info),
            pytest.raises(PhysicalRootIdentityUnavailableError, match="no disponible o inválida"),
        ):
            derive_physical_root(tmp_path)
