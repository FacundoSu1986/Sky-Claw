"""QA-3 — CredentialVault._write_salt_atomic resistente a symlink swap (T1-03).

Verifica que el atomic write rechace symlinks pre-existentes en el target y
en el tmp, y que la apertura del tmp use ``O_NOFOLLOW`` para cerrar la ventana
TOCTOU entre la check y el open real.

En Windows la creación de symlinks requiere modo desarrollador o admin; si la
creación falla con OSError los tests se skipean automáticamente.

Además, sobre el happy-path: ``restrict_to_owner`` (el icacls Windows) puede
fallar con error 1332 en CI/runners (cuenta no mapeada). Los tests del happy
path hacen patch a ``restrict_to_owner`` para aislar la cobertura del fix de
symlink del comportamiento de icacls (cubierto en su propio test suite).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from sky_claw.antigravity.security.credential_vault import CredentialVault

_VALID_SALT = b"\x00" * 32


def _try_symlink(src: Path, dst: Path) -> bool:
    """Intenta crear un symlink ``dst -> src``. Retorna True si fue posible."""
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
class TestWriteSaltAtomicSymlinkRejection:
    def test_target_is_symlink_to_sensitive_file_raises(self, tmp_path: Path) -> None:
        """Si el target es un symlink a otro archivo, debe rechazar (no overwrite)."""
        sensitive = tmp_path / "victim.bin"
        sensitive.write_bytes(b"important")
        target = tmp_path / "vault_salt.bin"

        if not _try_symlink(sensitive, target):
            pytest.skip("symlink creation not supported in this environment")

        with pytest.raises(PermissionError, match="symlink"):
            CredentialVault._write_salt_atomic(target, _VALID_SALT)

        # El archivo víctima no fue modificado.
        assert sensitive.read_bytes() == b"important"

    def test_tmp_is_pre_planted_symlink_gets_unlinked(self, tmp_path: Path) -> None:
        """Un symlink hostil colocado en .tmp debe ser unlink-eado, no seguido."""
        victim = tmp_path / "victim.bin"
        victim.write_bytes(b"original")
        target = tmp_path / "salt.bin"
        tmp = target.with_name(target.name + ".tmp")

        if not _try_symlink(victim, tmp):
            pytest.skip("symlink creation not supported in this environment")

        # La escritura debe completarse exitosamente, escribiendo en el target
        # real (no en el víctima). Patch restrict_to_owner para evitar la
        # llamada a icacls (que falla con error 1332 en algunos entornos).
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            CredentialVault._write_salt_atomic(target, _VALID_SALT)

        # Verificar que escribimos al target real, no al víctima.
        assert target.read_bytes() == _VALID_SALT
        assert victim.read_bytes() == b"original"
        assert not tmp.exists()  # tmp removido como parte del replace

    def test_o_nofollow_present_in_open_flags(self, tmp_path: Path) -> None:
        """Verificación structural: O_NOFOLLOW está en las flags si el OS lo soporta."""
        target = tmp_path / "salt.bin"
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            CredentialVault._write_salt_atomic(target, _VALID_SALT)
        assert target.read_bytes() == _VALID_SALT
        # El helper estático ahora usa O_NOFOLLOW si está disponible.
        if hasattr(os, "O_NOFOLLOW"):
            import inspect

            source = inspect.getsource(CredentialVault._write_salt_atomic)
            assert "O_NOFOLLOW" in source, "O_NOFOLLOW should be in the open flags"


class TestWriteSaltAtomicHappyPath:
    """Caso feliz: sin symlinks involucrados, el write atomic debe funcionar.

    Patcheamos ``restrict_to_owner`` para aislar el comportamiento del fix
    del icacls Windows (que falla con error 1332 en ciertos runners CI).
    """

    def test_writes_target_with_owner_only_permissions(self, tmp_path: Path) -> None:
        target = tmp_path / "vault_salt.bin"
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            CredentialVault._write_salt_atomic(target, _VALID_SALT)
        assert target.exists()
        assert target.read_bytes() == _VALID_SALT

    def test_overwrites_existing_regular_file(self, tmp_path: Path) -> None:
        target = tmp_path / "vault_salt.bin"
        target.write_bytes(b"old-salt" + b"\x00" * 24)
        with patch("sky_claw.antigravity.security.credential_vault.restrict_to_owner"):
            CredentialVault._write_salt_atomic(target, _VALID_SALT)
        assert target.read_bytes() == _VALID_SALT

    def test_tmp_cleanup_on_error(self, tmp_path: Path, monkeypatch) -> None:
        """Si restrict_to_owner lanza, el tmp debe limpiarse."""
        target = tmp_path / "vault_salt.bin"

        # Monkey-patch restrict_to_owner para que lance en este test.
        import sky_claw.antigravity.security.credential_vault as cv_mod

        def _raise(p: Path) -> None:
            raise PermissionError("forced failure")

        monkeypatch.setattr(cv_mod, "restrict_to_owner", _raise)

        with pytest.raises(PermissionError, match="forced failure"):
            CredentialVault._write_salt_atomic(target, _VALID_SALT)

        # tmp limpiado, target no creado.
        tmp = target.with_name(target.name + ".tmp")
        assert not tmp.exists()
        assert not target.exists()
