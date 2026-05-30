"""QA-4 — restrict_to_owner rechaza symlinks (T1-04).

Verifica que ``restrict_to_owner`` lance ``PermissionError`` cuando se le pasa
un symbolic link, en lugar de seguir el link y chmod-ear el target.

En Windows la creación de symlinks requiere modo desarrollador o admin; si la
creación falla los tests se skipean automáticamente.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sky_claw.antigravity.security.file_permissions import restrict_to_owner


def _try_symlink(src: Path, dst: Path) -> bool:
    try:
        os.symlink(src, dst)
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("SKY_CLAW_ALLOW_WINDOWS_SYMLINK_TESTS"),
    reason="Symlink creation on Windows requires elevated privileges; "
    "set SKY_CLAW_ALLOW_WINDOWS_SYMLINK_TESTS=1 with developer mode to run.",
)
class TestRestrictToOwnerSymlinkRejection:
    def test_symlink_to_file_raises_permission_error(self, tmp_path: Path) -> None:
        """Un symlink debe ser rechazado, no seguido."""
        target = tmp_path / "real_file.bin"
        target.write_bytes(b"target-data")
        link = tmp_path / "link.bin"

        if not _try_symlink(target, link):
            pytest.skip("symlink creation not supported")

        with pytest.raises(PermissionError, match="symlink"):
            restrict_to_owner(link)

        # El target real no debe haber tenido permisos cambiados.
        # (En POSIX podríamos verificar mode bits; en Windows no aplica el chmod.
        # Lo importante: la PermissionError se lanzó ANTES de cualquier chmod/icacls.)
        assert target.exists()

    def test_symlink_to_directory_raises_permission_error(self, tmp_path: Path) -> None:
        target_dir = tmp_path / "real_dir"
        target_dir.mkdir()
        link = tmp_path / "link_dir"

        if not _try_symlink(target_dir, link):
            pytest.skip("symlink creation not supported")

        with pytest.raises(PermissionError, match="symlink"):
            restrict_to_owner(link)

    def test_dangling_symlink_raises_permission_error(self, tmp_path: Path) -> None:
        """Un symlink colgante (sin target) tambien debe ser rechazado.

        PR #141 review fix: el comportamiento original retornaba silenciosamente
        porque ``path.exists()`` era False para un symlink colgante (lo cual
        contradice el docstring "if path is a symlink, refuse"). Ahora
        ``is_symlink()`` se chequea ANTES de ``exists()`` para que CUALQUIER
        symlink (incluso colgante) sea rechazado consistentemente — un
        atacante que crea un dangling symlink antes de un retry/sync legitimo
        ya no puede aprovechar el silent-return.
        """
        link = tmp_path / "dangling.bin"
        # Apuntar a un archivo que no existe.
        if not _try_symlink(tmp_path / "nonexistent.bin", link):
            pytest.skip("symlink creation not supported")

        with pytest.raises(PermissionError, match="symlink"):
            restrict_to_owner(link)


class TestRestrictToOwnerHappyPath:
    """El happy-path sin symlinks debe seguir funcionando."""

    def test_nonexistent_path_returns_silently(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "ghost.bin"
        restrict_to_owner(nonexistent)  # no debe lanzar
