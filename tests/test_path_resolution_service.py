"""Tests para PathResolutionService — resolución stateless de rutas MO2/Skyrim.

Verifica EAFP anti-TOCTOU, validación con PathValidator (CRIT-003),
y la interfaz Protocol PathResolver.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sky_claw.core.path_resolver import PathResolutionService, PathResolver
from sky_claw.security.path_validator import PathValidator

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
def sandbox_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Directorio raíz del sandbox para PathValidator."""
    return tmp_path.resolve()


@pytest.fixture
def path_validator(sandbox_root: pathlib.Path) -> PathValidator:
    """PathValidator configurado con el sandbox como root."""
    return PathValidator(roots=[sandbox_root])


@pytest.fixture
def path_resolver(path_validator: PathValidator) -> PathResolutionService:
    """PathResolutionService con PathValidator inyectado."""
    return PathResolutionService(
        path_validator=path_validator,
        profile_name="TestProfile",
    )


class TestPathResolverProtocol:
    """Verifica que PathResolutionService satisface el Protocol PathResolver."""

    def test_satisfies_protocol(self, path_resolver: PathResolutionService) -> None:
        """PathResolutionService es una implementación válida de PathResolver."""
        assert isinstance(path_resolver, PathResolver)


class TestValidateEnvPath:
    """Tests para validate_env_path."""

    def test_valid_path_within_sandbox(
        self,
        path_resolver: PathResolutionService,
        sandbox_root: pathlib.Path,
    ) -> None:
        """Un path dentro del sandbox se valida correctamente."""
        valid_dir = sandbox_root / "MO2"
        valid_dir.mkdir()
        result = path_resolver.validate_env_path(str(valid_dir), "TEST_VAR")
        assert result is not None
        assert sandbox_root in result.parents or result == sandbox_root

    def test_empty_string_returns_none(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """String vacío retorna None sin lanzar excepción."""
        result = path_resolver.validate_env_path("", "TEST_VAR")
        assert result is None

    def test_traversal_path_returns_none(
        self,
        path_resolver: PathResolutionService,
        sandbox_root: pathlib.Path,
    ) -> None:
        """Path con '..' retorna None (Path Traversal bloqueado)."""
        traversal_path = str(sandbox_root / ".." / ".." / "etc" / "passwd")
        result = path_resolver.validate_env_path(traversal_path, "TEST_VAR")
        assert result is None

    def test_path_outside_sandbox_returns_none(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Path fuera del sandbox retorna None."""
        result = path_resolver.validate_env_path("/etc/passwd", "TEST_VAR")
        assert result is None


class TestDetectMo2Path:
    """Tests para detect_mo2_path con EAFP anti-TOCTOU."""

    def test_detects_valid_mo2_in_candidate_paths(
        self,
        path_resolver: PathResolutionService,
        sandbox_root: pathlib.Path,
    ) -> None:
        """Detecta MO2 cuando ModOrganizer.exe existe en ruta candidata."""
        # Crear estructura MO2 dentro del sandbox
        mo2_dir = sandbox_root / "Modding" / "MO2"
        mo2_dir.mkdir(parents=True)
        (mo2_dir / "ModOrganizer.exe").write_bytes(b"fake exe")

        # Patchear las rutas candidatas para apuntar al sandbox
        with patch(
            "sky_claw.core.path_resolver._CANDIDATE_MO2_PATHS",
            (str(mo2_dir),),
        ):
            result = path_resolver.detect_mo2_path()
            assert result is not None
            assert result.name == "MO2"

    def test_returns_none_when_no_mo2_found(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Retorna None cuando ninguna ruta candidata contiene MO2."""
        with (
            patch(
                "sky_claw.core.path_resolver._CANDIDATE_MO2_PATHS",
                (r"Z:\nonexistent\path",),
            ),
            patch(
                "sky_claw.core.path_resolver._CANDIDATE_PF_PATHS",
                (r"Z:\nonexistent\pf",),
            ),
            patch.dict(os.environ, {}, clear=False),
        ):
            # Asegurar que LOCALAPPDATA no existe o apunta a nowhere
            env = os.environ.copy()
            env.pop("LOCALAPPDATA", None)
            with patch.dict(os.environ, env, clear=True):
                result = path_resolver.detect_mo2_path()
                assert result is None


class TestResolveModlistPath:
    """Tests para resolve_modlist_path."""

    def test_resolves_from_env_var(
        self,
        path_resolver: PathResolutionService,
        sandbox_root: pathlib.Path,
    ) -> None:
        """Resuelve modlist.txt desde MO2_PATH env var."""
        mo2_dir = sandbox_root / "MO2_Env"
        mo2_dir.mkdir()
        profiles_dir = mo2_dir / "profiles" / "TestProfile"
        profiles_dir.mkdir(parents=True)

        with patch.dict(os.environ, {"MO2_PATH": str(mo2_dir)}):
            result = path_resolver.resolve_modlist_path("TestProfile")
            assert result.name == "modlist.txt"
            assert "TestProfile" in str(result)

    def test_raises_runtime_error_when_all_fail(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Lanza RuntimeError si ninguna ruta puede resolverse."""
        with patch.dict(os.environ, {}, clear=True):
            # Sin MO2_PATH, sin auto-detect, sin fallback válido
            with patch.object(path_resolver, "detect_mo2_path", return_value=None):
                with pytest.raises(RuntimeError, match="No se pudo resolver"):
                    path_resolver.resolve_modlist_path("MissingProfile")


class TestGetMo2ModsPath:
    """Tests para get_mo2_mods_path."""

    def test_resolves_from_mo2_mods_path_env(
        self,
        path_resolver: PathResolutionService,
        sandbox_root: pathlib.Path,
    ) -> None:
        """Resuelve desde MO2_MODS_PATH env var."""
        mods_dir = sandbox_root / "custom_mods"
        mods_dir.mkdir()

        with patch.dict(os.environ, {"MO2_MODS_PATH": str(mods_dir)}):
            result = path_resolver.get_mo2_mods_path()
            assert result.name == "custom_mods"

    def test_raises_runtime_error_when_all_fail(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Lanza RuntimeError si no puede detectar MO2."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(path_resolver, "detect_mo2_path", return_value=None):
                with pytest.raises(RuntimeError, match="No se pudo detectar"):
                    path_resolver.get_mo2_mods_path()


class TestGetActiveProfile:
    """Tests para get_active_profile."""

    def test_returns_env_var_profile(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Retorna perfil desde MO2_PROFILE env var."""
        with patch.dict(os.environ, {"MO2_PROFILE": "CustomProfile"}):
            assert path_resolver.get_active_profile() == "CustomProfile"

    def test_returns_constructor_profile_when_no_env(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Retorna perfil del constructor si no hay env var."""
        with patch.dict(os.environ, {}, clear=True):
            assert path_resolver.get_active_profile() == "TestProfile"

    def test_returns_default_when_nothing_set(self, sandbox_root: pathlib.Path) -> None:
        """Retorna 'Default' cuando no hay perfil configurado."""
        validator = PathValidator(roots=[sandbox_root])
        resolver = PathResolutionService(
            path_validator=validator,
            profile_name="",
        )
        with patch.dict(os.environ, {}, clear=True):
            assert resolver.get_active_profile() == "Default"
